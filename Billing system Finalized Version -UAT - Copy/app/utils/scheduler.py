from datetime import datetime, timedelta
from apscheduler.schedulers.background import BackgroundScheduler
from app import create_app
from app.extensions import db
from app.models import TimeEntry, ServiceItem, MonthlyBatch, MonthlyBatchEntry, Company, User, SystemSetting
from app.utils.cycle_helper import CycleHelper, get_sri_lanka_today, get_sri_lanka_now
from app.utils.email_helper import send_head_of_it_notification   # <-- ADD THIS IMPORT
import traceback

scheduler = BackgroundScheduler()
_app_instance = None


def auto_submit_to_head_of_it():
    """
    Auto-submit approved weekly entries to Head of IT on cycle end date (26th)
    Based on 4-week cycle: 27th of previous month to 26th of current month
    """
    global _app_instance
    app = _app_instance or create_app()
    
    # Background tasks need SERVER_NAME for url_for(_external=True) to work
    # We use SERVER_NAME from configuration or a safe fallback.
    server_name = app.config.get('SERVER_NAME') or 'localhost:5000'
    
    with app.test_request_context(base_url=f"http://{server_name}"):
        try:
            # Use Sri Lanka time for both the date and time checks.
            today = get_sri_lanka_today()
            now = get_sri_lanka_now()

            print(f"\n{'='*60}")
            print(f"[{now.strftime('%Y-%m-%d %H:%M:%S')}] Running auto-submit to Head of IT...")
            print(f"{'='*60}")
            
            # Get auto-submit time from settings
            auto_submit_time_setting = SystemSetting.query.filter_by(setting_key='auto_submit_time').first()
            auto_submit_time = auto_submit_time_setting.setting_value if auto_submit_time_setting else "10:00"
            
            print(f"  Auto-submit time setting: {auto_submit_time}")
            print(f"  Current date (Sri Lanka): {today.strftime('%Y-%m-%d')}")
            
            # Get auto-submit date from cycle logic (26th of current cycle)
            auto_submit_date = CycleHelper.get_auto_submit_date()
            print(f"  Cycle auto-submit date: {auto_submit_date.strftime('%Y-%m-%d')}")
            
            # Check if today is the auto-submit date
            if today != auto_submit_date:
                print(f"  ❌ Not auto-submit date. Today={today}, Expected={auto_submit_date}")
                return
            
            # Check if it's the right time (within 5 minutes of scheduled time)
            scheduled_hour, scheduled_minute = map(int, auto_submit_time.split(':'))
            current_total_minutes = now.hour * 60 + now.minute
            scheduled_total_minutes = scheduled_hour * 60 + scheduled_minute
            
            # Check if current time is at or within 5 minutes AFTER the scheduled time
            if not (0 <= current_total_minutes - scheduled_total_minutes <= 5):
                print(f"  ❌ Not scheduled time (current={now.strftime('%H:%M')}, expected={auto_submit_time})")
                return
            
            print(f"  ✅ Time condition matched! Running auto-submit...")
            
            # Get concluding cycle information (the cycle that ends on/before auto_submit_day)
            concluding_cycle_date = today - timedelta(days=1)
            cycle = CycleHelper.get_current_cycle(concluding_cycle_date)
            cycle_start = cycle['cycle_start']
            cycle_end = cycle['cycle_end']
            
            print(f"\n  📅 Concluding Cycle: {cycle_start.strftime('%Y-%m-%d')} to {cycle_end.strftime('%Y-%m-%d')}")
            
            # FIX: Get approved service items first, then filter by service_date
            approved_service_items = ServiceItem.query.filter(
                ServiceItem.supervisor_approval_status == 'approved'
            ).all()
            
            # Get unique time entry IDs from approved service items
            entry_ids = set(si.time_entry_id for si in approved_service_items)
            
            # Get time entries within the cycle
            entries = TimeEntry.query.filter(
                TimeEntry.id.in_(entry_ids),
                TimeEntry.service_date >= cycle_start,
                TimeEntry.service_date <= cycle_end,
                TimeEntry.status != 'sent_to_head'  # Prevent processing already sent entries
            ).all()
            
            print(f"  Total approved entries in this cycle: {len(entries)}")
            
            # Get all active companies
            companies = Company.query.filter_by(is_active=True).all()
            print(f"\n  Found {len(companies)} active companies")
            
            for company in companies:
                print(f"\n  {'='*40}")
                print(f"  Processing company: {company.name} (ID: {company.id})")
                print(f"  {'='*40}")
                
                # Find relevant entries for this company
                relevant_entries = []
                for entry in entries:
                    services = ServiceItem.query.filter_by(
                        time_entry_id=entry.id,
                        company_id=company.id
                    ).all()
                    if services:
                        relevant_entries.append({
                            'entry': entry,
                            'services': services
                        })
                        print(f"      Entry {entry.id}: {len(services)} services, date={entry.service_date}")
                
                print(f"    Relevant entries for {company.name}: {len(relevant_entries)}")
                
                if not relevant_entries:
                    continue
                
                # Delete any existing batch for this company/cycle to avoid duplicates
                existing_batch = MonthlyBatch.query.filter_by(
                    company_id=company.id,
                    year=cycle_end.year,
                    month=cycle_end.month
                ).first()
                
                if existing_batch:
                    # Check if already processed to avoid multiple emails/batches during the matching window
                    if existing_batch.status and existing_batch.status.strip().lower() == 'sent_to_head':
                        # If batch exists and is already sent, add any NEWLY approved entries to it
                        new_items_count = 0
                        for item in relevant_entries:
                            entry = item['entry']
                            week_info = CycleHelper.get_week_for_date(entry.service_date)
                            
                            batch_entry = MonthlyBatchEntry(
                                batch_id=existing_batch.id,
                                time_entry_id=entry.id,
                                week_number=week_info['week_number'],
                                status='pending'
                            )
                            db.session.add(batch_entry)
                            entry.status = 'sent_to_head'
                            new_items_count += 1
                        
                        if new_items_count > 0:
                            db.session.commit()
                            print(f"    ✅ Added {new_items_count} new entries to existing batch {existing_batch.id} for {company.name}")
                            
                            # Re-notify Head of IT about the updated batch
                            head_of_it = User.query.filter_by(role='head_of_it', company_id=company.id).first()
                            if head_of_it:
                                if send_head_of_it_notification(head_of_it, existing_batch, cycle):
                                    print(f"    ✅ Update email notification sent to Head of IT: {head_of_it.email}")
                        else:
                            print(f"    ⚠️ Batch already auto-submitted for {company.name}. No new entries found.")
                        continue
                    
                    # Clean up failed or pending attempts before recreating
                    MonthlyBatchEntry.query.filter_by(batch_id=existing_batch.id).delete()
                    db.session.delete(existing_batch)
                    db.session.commit()
                    print(f"    Deleted existing batch (ID: {existing_batch.id})")
                
                # Create new batch
                batch = MonthlyBatch(
                    company_id=company.id,
                    year=cycle_end.year,
                    month=cycle_end.month,
                    status='sent_to_head',
                    sent_to_head_at=now.replace(tzinfo=None)
                )
                db.session.add(batch)
                db.session.flush()
                print(f"    ✅ Created new batch ID: {batch.id}")
                
                # Add entries to batch with correct week numbers
                for item in relevant_entries:
                    entry = item['entry']
                    week_info = CycleHelper.get_week_for_date(entry.service_date)
                    week_num = week_info['week_number']
                    
                    batch_entry = MonthlyBatchEntry(
                        batch_id=batch.id,
                        time_entry_id=entry.id,
                        week_number=week_num,
                        status='pending'
                    )
                    db.session.add(batch_entry)
                    print(f"      Added entry {entry.id} (Week {week_num}) to batch")
                    
                    # Update entry status
                    entry.status = 'sent_to_head'
                    print(f"      Updated entry {entry.id} status to 'sent_to_head'")
                
                db.session.commit()
                print(f"    ✅ Auto-submit completed for {company.name}")
                
                # Send notification to Head of IT
                head_of_it = User.query.filter_by(
                    role='head_of_it',
                    company_id=company.id
                ).first()
                
                if head_of_it:
                    email_success = send_head_of_it_notification(head_of_it, batch, cycle)
                    if email_success:
                        print(f"    ✅ Email notification sent to Head of IT: {head_of_it.email}")
                    else:
                        print(f"    ❌ Failed to send email to Head of IT: {head_of_it.email}")
            
            # Verify batch creation
            print("\n" + "=" * 60)
            print("VERIFICATION AFTER RUN:")
            batches = MonthlyBatch.query.filter_by(
                year=cycle_end.year,
                month=cycle_end.month
            ).all()
            print(f"Total batches for {cycle_end.strftime('%B %Y')}: {len(batches)}")
            for b in batches:
                company = Company.query.get(b.company_id)
                entries_count = MonthlyBatchEntry.query.filter_by(batch_id=b.id).count()
                print(f"  Batch {b.id}: {company.name if company else 'N/A'} - {entries_count} entries, Status={b.status}")
            print("=" * 60)
            
        except Exception as e:
            print(f"  ❌ Error in auto-submit: {str(e)}")
            traceback.print_exc()
            db.session.rollback()


def start_scheduler(flask_app=None):
    """Start the background scheduler"""
    global _app_instance
    if flask_app:
        _app_instance = flask_app

    if scheduler.running:
        print("ℹ️ Scheduler is already running")
        return True

    try:
        # Remove any existing jobs
        scheduler.remove_all_jobs()
        
        # Check conditions every minute
        scheduler.add_job(
            func=auto_submit_to_head_of_it,
            trigger='interval',
            minutes=1,
            id='auto_submit_check',
            replace_existing=True
        )
        scheduler.start()
        print("✅ Scheduler started - Checking every minute for auto-submit condition")
        print("   (Auto-submit will trigger on the 26th of each cycle at configured time)")
        return True
    except Exception as e:
        print(f"⚠️ Could not start scheduler: {e}")
        return False


def create_batches_for_current_month():
    """Create batches for the current month regardless of cycle date."""
    global _app_instance
    from app import create_app
    from app.extensions import db
    from app.models import Company, TimeEntry, ServiceItem, MonthlyBatch, MonthlyBatchEntry, User
    from datetime import datetime, date
    from calendar import monthrange
    import traceback

    app = _app_instance or create_app()
    with app.app_context():
        try:
            today = get_sri_lanka_today()
            year = today.year
            month = today.month
            print(f"Force creating batches for {year}-{month:02d}...")
            companies = Company.query.filter_by(is_active=True).all()
            for company in companies:
                # Find service items that are approved by supervisor and belong to this company
                # and have a service date within the current month
                services = ServiceItem.query.join(TimeEntry).filter(
                    ServiceItem.company_id == company.id,
                    ServiceItem.supervisor_approval_status == 'approved',
                    TimeEntry.service_date >= date(year, month, 1),
                    TimeEntry.service_date <= date(year, month, monthrange(year, month)[1])
                ).all()
                if not services:
                    continue
                # Get unique time entries from these services
                entry_ids = set(s.time_entry_id for s in services)
                entries = [TimeEntry.query.get(eid) for eid in entry_ids]
                # Delete existing batch for this company/month
                existing = MonthlyBatch.query.filter_by(company_id=company.id, year=year, month=month).first()
                if existing:
                    MonthlyBatchEntry.query.filter_by(batch_id=existing.id).delete()
                    db.session.delete(existing)
                    db.session.commit()
                batch = MonthlyBatch(
                    company_id=company.id,
                    year=year,
                    month=month,
                    status='sent_to_head',
                    sent_to_head_at=datetime.utcnow()
                )
                db.session.add(batch)
                db.session.flush()
                for entry in entries:
                    week_num = ((entry.service_date.day - 1) // 7) + 1
                    be = MonthlyBatchEntry(
                        batch_id=batch.id,
                        time_entry_id=entry.id,
                        week_number=week_num,
                        status='pending'
                    )
                    db.session.add(be)
                    entry.status = 'sent_to_head'
                db.session.commit()
                print(f"Batch created for {company.name} ({len(entries)} entries)")
                head = User.query.filter_by(role='head_of_it', company_id=company.id).first()
                if head:
                    from app.utils.email_helper import send_head_of_it_notification
                    send_head_of_it_notification(head, batch, None)
            print("Force batch creation completed.")
        except Exception as e:
            print(f"Error: {e}")
            traceback.print_exc()
            db.session.rollback()