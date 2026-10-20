from flask import Blueprint, render_template, redirect, url_for, flash, request, jsonify
from flask_login import login_required, current_user

from app.models import (
    TimeEntry, ServiceItem, Company, User, ActivityLog, Unit, ResourceType,
    MonthlyBatch, MonthlyBatchEntry, SystemSetting
)
from app.extensions import db
from datetime import datetime
from calendar import month_name
import io
from reportlab.lib import colors
from sqlalchemy import extract
import traceback
from flask import Blueprint, render_template, redirect, url_for, flash, request, send_file
from flask_login import login_required, current_user
from app.models import Company, ServiceItem, TimeEntry, User, Unit, ResourceType
from app.extensions import db
from datetime import datetime
from sqlalchemy import extract
import calendar
import io
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer, PageBreak
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch
from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT


head_of_it_bp = Blueprint('head_of_it', __name__)


def get_batched_entry_ids(company_id, year, month):
    """Get all time_entry_ids that are part of a batch for the given company and period"""
    batch = MonthlyBatch.query.filter_by(
        company_id=company_id,
        year=year,
        month=month,
        status='sent_to_head'
    ).first()
    
    if not batch:
        return []
    
    batch_entries = MonthlyBatchEntry.query.filter_by(batch_id=batch.id).all()
    return [be.time_entry_id for be in batch_entries]


@head_of_it_bp.route('/dashboard')
@login_required
def dashboard():
    """Head of IT Dashboard - Show ONLY batched services (auto-submitted)."""
    if current_user.role != 'head_of_it':
        flash('Access denied.', 'danger')
        return redirect(url_for('dashboard.index'))

    # Get month and year filters from query params, default to current date
    from datetime import date
    current_date = date.today()
    month = request.args.get('month', current_date.month, type=int)
    year = request.args.get('year', current_date.year, type=int)
    
    # Validate month and year
    if not (1 <= month <= 12):
        month = current_date.month
    if year < 2020 or year > 2100:
        year = current_date.year

    if not current_user.company_id:
        return render_template('head_of_it/dashboard.html',
                             pending_units=[], approved_units=[], rejected_units=[],
                             pending_total=0, approved_total=0, rejected_total=0,
                             selected_month=month, selected_year=year,
                             current_date=current_date,
                             has_batch=False)

    company = current_user.company
    company_name = company.name
    
    # Check if there's a batch for this period
    batch_entry_ids = get_batched_entry_ids(company.id, year, month)
    
    if not batch_entry_ids:
        # No batch found - show message
        return render_template('head_of_it/dashboard.html',
                             company_name=company_name,
                             pending_units=[], approved_units=[], rejected_units=[],
                             pending_total=0, approved_total=0, rejected_total=0,
                             selected_month=month, selected_year=year,
                             current_date=datetime.now(),
                             has_batch=False,
                             no_batch_message=f"No services have been auto-submitted for {calendar.month_name[month]} {year}. Auto-submit runs on the 26th of each cycle.")

    def get_unit_data(status):
        """Get unit data ONLY for batched services"""
        # Get services from batched entries only
        services = ServiceItem.query.join(TimeEntry).filter(
            ServiceItem.company_id == company.id,
            ServiceItem.head_approval_status == status,
            TimeEntry.id.in_(batch_entry_ids),  # ONLY batched entries
            extract('month', TimeEntry.service_date) == month,
            extract('year', TimeEntry.service_date) == year
        ).all()

        unit_map = {}
        for item in services:
            entry = TimeEntry.query.get(item.time_entry_id)
            if not entry:
                continue
            user = User.query.get(entry.user_id)
            if not user or not user.unit_id:
                continue
            unit = Unit.query.get(user.unit_id)
            if not unit:
                continue

            if unit.id not in unit_map:
                unit_map[unit.id] = {
                    'id': unit.id,
                    'name': unit.name,
                    'service_count': 0,
                    'total_amount': 0.0
                }
            unit_map[unit.id]['service_count'] += 1
            unit_map[unit.id]['total_amount'] += float(item.total_amount)
        return list(unit_map.values())

    pending_units = get_unit_data('pending')
    approved_units = get_unit_data('approved')
    rejected_units = get_unit_data('rejected')

    pending_total = sum(u['total_amount'] for u in pending_units)
    approved_total = sum(u['total_amount'] for u in approved_units)
    rejected_total = sum(u['total_amount'] for u in rejected_units)

    return render_template('head_of_it/dashboard.html',
                         company_name=company_name,
                         pending_units=pending_units,
                         approved_units=approved_units,
                         rejected_units=rejected_units,
                         pending_total=pending_total,
                         approved_total=approved_total,
                         rejected_total=rejected_total,
                         selected_month=month,
                         selected_year=year,
                         current_date=datetime.now(),
                         has_batch=True)


@head_of_it_bp.route('/unit/<int:unit_id>/services')
@login_required
def unit_services(unit_id):
    """Show all services for a specific unit, grouped by BU user (ONLY batched services)."""
    if current_user.role != 'head_of_it':
        flash('Access denied.', 'danger')
        return redirect(url_for('dashboard.index'))

    status = request.args.get('status', 'pending')
    unit = Unit.query.get_or_404(unit_id)
    
    # Get current month/year
    from datetime import date
    current_date = date.today()
    month = request.args.get('month', current_date.month, type=int)
    year = request.args.get('year', current_date.year, type=int)
    
    # Get batched entry IDs for this period
    batch_entry_ids = get_batched_entry_ids(current_user.company_id, year, month)
    
    if not batch_entry_ids:
        return render_template('head_of_it/unit_services.html',
                             unit=unit,
                             users=[],
                             status=status,
                             no_batch_message=f"No batched services found for {calendar.month_name[month]} {year}")

    # Get ONLY batched services
    services = ServiceItem.query\
        .join(TimeEntry, ServiceItem.time_entry_id == TimeEntry.id)\
        .join(User, TimeEntry.user_id == User.id)\
        .filter(
            User.unit_id == unit_id,
            ServiceItem.company_id == current_user.company_id,
            ServiceItem.head_approval_status == status,
            TimeEntry.id.in_(batch_entry_ids),  # ONLY batched entries
            extract('month', TimeEntry.service_date) == month,
            extract('year', TimeEntry.service_date) == year
        ).all()

    # Pre-fetch all resource types for lookup
    all_resource_types = ResourceType.query.all()
    resource_by_id = {str(rt.id): rt for rt in all_resource_types}
    
    # Group by BU user
    users_dict = {}
    for item in services:
        entry = TimeEntry.query.get(item.time_entry_id)
        if not entry:
            continue
        bu_user = User.query.get(entry.user_id)
        if not bu_user:
            continue
        user_key = bu_user.id
        
        # ========== CALCULATE LOIT Chargeback and LOIT Showback from Resource Types ==========
        loit_chargeback_total = 0
        loit_showback_total = 0
        lolc_showback_total = 0
        
        # Process each resource in the service item
        for resource in item.resources or []:
            resource_type_id = str(resource.get('type', ''))
            rt = resource_by_id.get(resource_type_id)
            
            if rt:
                quantity = float(resource.get('quantity') or resource.get('hours') or 0)
                # Get the individual values from ResourceType table
                loit_chargeback_rate = float(rt.loit_chargeback or 0)
                loit_showback_rate = float(rt.loit_showback or 0)
                lolc_showback_rate = float(rt.showback_rate or 0)
                
                loit_chargeback_total += quantity * loit_chargeback_rate
                loit_showback_total += quantity * loit_showback_rate
                lolc_showback_total += quantity * lolc_showback_rate
        
        if user_key not in users_dict:
            users_dict[user_key] = {
                'user_id': bu_user.id,
                'user_name': bu_user.full_name,
                'employee_id': bu_user.employee_id,
                'services': [],
                'total_amount': 0,
                'total_loit_chargeback': 0,
                'total_loit_showback': 0
            }
        
        # Get company name
        company_name = 'N/A'
        if item.company_id:
            company = Company.query.get(item.company_id)
            if company:
                company_name = company.name
        
        users_dict[user_key]['services'].append({
            'id': item.id,
            'description': item.service_description,
            'total_amount': float(item.total_amount),
            'submission_date': entry.submission_date.strftime('%Y-%m-%d %H:%M') if entry.submission_date else entry.created_at.strftime('%Y-%m-%d %H:%M'),
            'resources': item.resources,
            'company_id': item.company_id,
            'company_name': company_name,
            # NEW FIELDS - Individual values from ResourceType
            'loit_chargeback': loit_chargeback_total,
            'loit_showback': loit_showback_total,
            'lolc_showback': lolc_showback_total
        })
        
        users_dict[user_key]['total_amount'] += float(item.total_amount)
        users_dict[user_key]['total_loit_chargeback'] += loit_chargeback_total
        users_dict[user_key]['total_loit_showback'] += loit_showback_total

    users_list = list(users_dict.values())

    return render_template('head_of_it/unit_services.html',
                         unit=unit,
                         users=users_list,
                         status=status,
                         month=month,
                         year=year)


@head_of_it_bp.route('/review-service/<int:service_id>')
@login_required
def review_service(service_id):
    """Review a specific service item"""
    if current_user.role != 'head_of_it':
        flash('Access denied. Head of IT users only.', 'danger')
        return redirect(url_for('dashboard.index'))
    
    service_item = ServiceItem.query.get_or_404(service_id)
    entry = TimeEntry.query.get(service_item.time_entry_id)
    
    # Verify this service is from a batched entry
    from datetime import date
    current_date = date.today()
    year = current_date.year
    month = current_date.month
    
    batch_entry_ids = get_batched_entry_ids(current_user.company_id, year, month)
    
    if entry.id not in batch_entry_ids:
        flash('This service is not available for review. Only batched services can be reviewed.', 'warning')
        return redirect(url_for('head_of_it.dashboard'))
    
    if service_item.company_id != current_user.company_id:
        flash('You are not authorized to review this service.', 'danger')
        return redirect(url_for('head_of_it.dashboard'))
    
    bu_user = User.query.get(entry.user_id)
    
    unit_name = 'N/A'
    if bu_user and bu_user.unit_id:
        unit = Unit.query.get(bu_user.unit_id)
        if unit:
            unit_name = unit.name
    
    # Get resource types
    all_resource_types = ResourceType.query.all()
    resource_by_code = {rt.code: rt for rt in all_resource_types}
    
    processed_resources = []
    for resource in service_item.resources or []:
        resource_code = str(resource.get('type', ''))
        rt = resource_by_code.get(resource_code)
        
        if rt:
            resource_name = rt.name
            uom = rt.unit_of_measure or 'units'
            chargeback_rate = float(rt.chargeback_rate) if rt.chargeback_rate else 0
            showback_rate = float(rt.showback_rate) if rt.showback_rate else 0
        else:
            resource_name = resource_code.replace('_', ' ').title()
            uom = 'units'
            chargeback_rate = resource.get('rate', 0)
            showback_rate = resource.get('rate', 0)
        
        processed_resources.append({
            'name': resource_name,
            'hours': resource.get('hours', 0),
            'uom': uom,
            'rate': resource.get('rate', 0),
            'chargeback_rate': chargeback_rate,
            'showback_rate': showback_rate,
            'flat_fee': resource.get('flat_fee', 0)
        })
    
    service_data = {
        'id': service_item.id,
        'company_id': service_item.company_id,
        'entry_id': entry.id,
        'submission_date': entry.submission_date.strftime('%Y-%m-%d %H:%M:%S') if entry.submission_date else entry.created_at.strftime('%Y-%m-%d %H:%M:%S'),
        'description': service_item.service_description,
        'resources': processed_resources,
        'flat_fee': float(getattr(service_item, 'flat_fee', 0) or 0),
        'total_amount': float(service_item.total_amount),
        'head_approval_status': service_item.head_approval_status,
        'head_approval_comment': service_item.head_approval_comment,
        'finance_approval_comment': service_item.finance_approval_comment,
        'bu_user': {
            'full_name': bu_user.full_name if bu_user else 'Unknown',
            'employee_id': bu_user.employee_id if bu_user else 'N/A',
            'unit_name': unit_name
        }
    }
    
    return render_template('head_of_it/review_service.html', service=service_data)


@head_of_it_bp.route('/view-service/<int:service_id>')
@login_required
def view_service(service_id):
    """View a specific service item (read-only)"""
    if current_user.role != 'head_of_it':
        flash('Access denied. Head of IT users only.', 'danger')
        return redirect(url_for('dashboard.index'))
    
    service_item = ServiceItem.query.get_or_404(service_id)
    entry = TimeEntry.query.get(service_item.time_entry_id)
    
    if service_item.company_id != current_user.company_id:
        flash('You are not authorized to view this service.', 'danger')
        return redirect(url_for('head_of_it.dashboard'))
    
    bu_user = User.query.get(entry.user_id)
    
    unit_name = 'N/A'
    if bu_user and bu_user.unit_id:
        unit = Unit.query.get(bu_user.unit_id)
        if unit:
            unit_name = unit.name
    
    all_resource_types = ResourceType.query.all()
    resource_by_code = {rt.code: rt for rt in all_resource_types}
    
    processed_resources = []
    for resource in service_item.resources or []:
        resource_code = str(resource.get('type', ''))
        rt = resource_by_code.get(resource_code)
        
        if rt:
            resource_name = rt.name
            uom = rt.unit_of_measure or 'units'
            chargeback_rate = float(rt.chargeback_rate) if rt.chargeback_rate else 0
            showback_rate = float(rt.showback_rate) if rt.showback_rate else 0
        else:
            resource_name = resource_code.replace('_', ' ').title()
            uom = 'units'
            chargeback_rate = resource.get('rate', 0)
            showback_rate = resource.get('rate', 0)
        
        processed_resources.append({
            'name': resource_name,
            'hours': resource.get('hours', 0),
            'uom': uom,
            'rate': resource.get('rate', 0),
            'chargeback_rate': chargeback_rate,
            'showback_rate': showback_rate,
            'flat_fee': resource.get('flat_fee', 0)
        })
    
    service_data = {
        'id': service_item.id,
        'entry_id': entry.id,
        'submission_date': entry.submission_date.strftime('%Y-%m-%d %H:%M:%S') if entry.submission_date else entry.created_at.strftime('%Y-%m-%d %H:%M:%S'),
        'description': service_item.service_description,
        'resources': processed_resources,
        'flat_fee': float(getattr(service_item, 'flat_fee', 0) or 0),
        'total_amount': float(service_item.total_amount),
        'head_approval_status': service_item.head_approval_status,
        'head_approval_comment': service_item.head_approval_comment,
        'finance_approval_comment': service_item.finance_approval_comment,
        'head_approved_at': service_item.head_approved_at.strftime('%Y-%m-%d %H:%M:%S') if service_item.head_approved_at else None,
        'bu_user': {
            'full_name': bu_user.full_name if bu_user else 'Unknown',
            'employee_id': bu_user.employee_id if bu_user else 'N/A',
            'unit_name': unit_name
        }
    }
    
    return render_template('head_of_it/view_service.html', service=service_data)


@head_of_it_bp.route('/resource-group/<int:resource_type_id>')
@login_required
def view_resource_group(resource_type_id):
    """Show all service items for a specific resource type (group) with status filtering (ONLY batched)."""
    if current_user.role != 'head_of_it':
        flash('Access denied.', 'danger')
        return redirect(url_for('dashboard.index'))
    
    rt = ResourceType.query.get_or_404(resource_type_id)
    status = request.args.get('status', 'pending')
    
    # Get current month/year
    from datetime import date
    current_date = date.today()
    month = request.args.get('month', current_date.month, type=int)
    year = request.args.get('year', current_date.year, type=int)
    
    # Get batched entry IDs
    batch_entry_ids = get_batched_entry_ids(current_user.company_id, year, month)
    
    if not batch_entry_ids:
        return render_template('head_of_it/resource_group.html',
                             resource_name=rt.name,
                             services=[],
                             company_name=current_user.company.name if current_user.company else 'N/A',
                             status=status,
                             no_batch_message=f"No batched services found for {calendar.month_name[month]} {year}")
    
    # Get services based on status - ONLY from batched entries
    query = ServiceItem.query.filter(
        ServiceItem.company_id == current_user.company_id,
        ServiceItem.supervisor_approval_status == 'approved',
        ServiceItem.time_entry_id.in_(batch_entry_ids)  # ONLY batched entries
    )
    
    if status == 'pending':
        query = query.filter(ServiceItem.head_approval_status == 'pending')
    elif status == 'approved':
        query = query.filter(ServiceItem.head_approval_status == 'approved')
    elif status == 'rejected':
        query = query.filter(ServiceItem.head_approval_status == 'rejected')
    
    services = query.all()
    
    # Build service list
    service_list = []
    for item in services:
        for res in item.resources or []:
            if str(res.get('type')) == str(resource_type_id):
                entry = TimeEntry.query.get(item.time_entry_id)
                bu_user = User.query.get(entry.user_id) if entry else None
                quantity = res.get('quantity', 0)
                uom = rt.unit_of_measure or ''
                service_list.append({
                    'id': item.id,
                    'company_id': item.company_id,
                    'submission_date': entry.submission_date.strftime('%Y-%m-%d %H:%M:%S') if entry else '',
                    'bu_user': bu_user.full_name if bu_user else 'Unknown',
                    'employee_id': bu_user.employee_id if bu_user else 'N/A',
                    'unit_name': bu_user.unit.name if bu_user and bu_user.unit else 'N/A',
                    'description': item.service_description,
                    'quantity': quantity,
                    'uom': uom,
                    'amount': float(item.total_amount),
                    'head_approval_status': item.head_approval_status
                })
                break
    
    return render_template('head_of_it/resource_group.html',
                         resource_name=rt.name,
                         services=service_list,
                         company_name=current_user.company.name if current_user.company else 'N/A',
                         status=status)




@head_of_it_bp.route('/loit-report')
@login_required
def loit_report():
    """LOIT Report Page - Only for Head of IT users"""
    print("=" * 60)
    print("LOIT REPORT ROUTE CALLED")
    print(f"User: {current_user.email if current_user else 'None'}")
    print(f"Role: {current_user.role if current_user else 'None'}")
    print(f"Request path: {request.path}")
    print(f"Request full path: {request.full_path}")
    print("=" * 60)
    
    if current_user.role != 'head_of_it':
        flash('Access denied. Head of IT only.', 'danger')
        return redirect(url_for('dashboard.index'))
    
    if not current_user.company_id:
        flash('No company assigned.', 'danger')
        return redirect(url_for('head_of_it.dashboard'))

    company = Company.query.get(current_user.company_id)
    
    today = datetime.now().date()
    month = request.args.get('month', today.month, type=int)
    year = request.args.get('year', today.year, type=int)
    
    print(f"Month: {month}, Year: {year}")
    
    if not (1 <= month <= 12):
        month = today.month
    if year < 2020 or year > 2100:
        year = today.year
    
    services = db.session.query(
        ServiceItem.id.label('service_id'),
        ServiceItem.service_description.label('description'),
        ServiceItem.chargeback_total.label('loit_cb'),
        ServiceItem.showback_total.label('loit_sb'),
        ServiceItem.total_amount,
        TimeEntry.service_date,
        TimeEntry.submission_date,
        User.first_name,
        User.last_name,
        User.employee_id,
        Unit.name.label('unit_name')
    ).select_from(ServiceItem).join(
        TimeEntry, ServiceItem.time_entry_id == TimeEntry.id
    ).join(
        User, TimeEntry.user_id == User.id
    ).outerjoin(
        Unit, User.unit_id == Unit.id
    ).filter(
        ServiceItem.company_id == current_user.company_id,
        extract('month', TimeEntry.service_date) == month,
        extract('year', TimeEntry.service_date) == year
    ).order_by(TimeEntry.service_date.desc()).all()
    
    print(f"Found {len(services)} services")
    
    processed_services = []
    for s in services:
        processed_services.append({
            'service_id': s.service_id,
            'description': s.description,
            'loit_cb': float(s.loit_cb or 0),
            'loit_sb': float(s.loit_sb or 0),
            'total_amount': float(s.total_amount or 0),
            'service_date': s.service_date,
            'submission_date': s.submission_date,
            'bu_user_name': f"{s.first_name or ''} {s.last_name or ''}".strip() or 'N/A',
            'employee_id': s.employee_id or 'N/A',
            'unit_name': s.unit_name or 'N/A'
        })
    
    total_cb = sum(s['loit_cb'] for s in processed_services)
    total_sb = sum(s['loit_sb'] for s in processed_services)
    month_name = calendar.month_name[month]
    
    print(f"Rendering template: head_of_it/loit_report.html")
    print(f"Template data: company={company.name}, services_count={len(processed_services)}, total_cb={total_cb}")
    
    return render_template('head_of_it/loit_report.html',
                         company=company,
                         services=processed_services,
                         total_cb=total_cb,
                         total_sb=total_sb,
                         total_loit=total_cb + total_sb,
                         selected_month=month,
                         selected_year=year,
                         month_name=month_name)





@head_of_it_bp.route('/export-loit-report-pdf')
@login_required
def export_loit_report_pdf():  # Change this function name
    """Export LOIT Report as PDF - Only assigned company"""
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer, PageBreak
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import inch
    from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
    from reportlab.platypus import Image
    import io
    import os
    from flask import send_file, current_app
    

    if current_user.role != 'head_of_it':
        flash('Access denied.', 'danger')
        return redirect(url_for('dashboard.index'))
    
    if not current_user.company_id:
        flash('No company assigned.', 'danger')
        return redirect(url_for('head_of_it.dashboard'))
    
    company = Company.query.get(current_user.company_id)
    if not company:
        flash('Company not found.', 'danger')
        return redirect(url_for('head_of_it.dashboard'))
    
    month = request.args.get('month', datetime.now().month, type=int)
    year = request.args.get('year', datetime.now().year, type=int)
    month_name = calendar.month_name[month]
    
    # Get all resource types for lookup
    all_resource_types = ResourceType.query.all()
    resource_by_id = {str(rt.id): rt for rt in all_resource_types}
    
    # Get services with calculated LOIT values
    services = db.session.query(
        ServiceItem.id,
        ServiceItem.service_description,
        ServiceItem.chargeback_total,
        ServiceItem.showback_total,
        ServiceItem.total_amount,
        ServiceItem.resources,
        TimeEntry.service_date,
        TimeEntry.submission_date,
        User.first_name,
        User.last_name,
        User.employee_id,
        Unit.name.label('unit_name'),
        Unit.id.label('unit_id')
    ).select_from(ServiceItem).join(
        TimeEntry, ServiceItem.time_entry_id == TimeEntry.id
    ).join(
        User, TimeEntry.user_id == User.id
    ).outerjoin(
        Unit, User.unit_id == Unit.id
    ).filter(
        ServiceItem.company_id == company.id,
        extract('month', TimeEntry.service_date) == month,
        extract('year', TimeEntry.service_date) == year
    ).order_by(Unit.name, TimeEntry.service_date).all()
    
    # Group services by unit
    units_data = {}
    for s in services:
        unit_name = s.unit_name or 'Unassigned'
        unit_id = s.unit_id or 0
        
        if unit_id not in units_data:
            units_data[unit_id] = {
                'name': unit_name,
                'services': [],
                'total_loit_cb': 0,
                'total_loit_sb': 0,
                'total_loit': 0,
                'total_lolc_sb': 0,
                'total_grand': 0
            }
        
        # Calculate LOIT values from Resource Types
        loit_cb = 0
        loit_sb = 0
        lolc_sb = 0
        resource_details = []
        
        for resource in s.resources or []:
            resource_type_id = str(resource.get('type', ''))
            rt = resource_by_id.get(resource_type_id)
            quantity = float(resource.get('quantity') or resource.get('hours') or 0)
            
            # Get resource description from the resource object
            resource_description = resource.get('description', '').strip()
            
            if rt:
                loit_cb_rate = float(rt.loit_chargeback or 0)
                loit_sb_rate = float(rt.loit_showback or 0)
                lolc_sb_rate = float(rt.showback_rate or 0)
                
                loit_cb += quantity * loit_cb_rate
                loit_sb += quantity * loit_sb_rate
                lolc_sb += quantity * lolc_sb_rate
                
                resource_details.append({
                    'name': rt.name,
                    'quantity': quantity,
                    'uom': rt.unit_of_measure or 'units',
                    'loit_cb_rate': loit_cb_rate,
                    'loit_sb_rate': loit_sb_rate,
                    'lolc_sb_rate': lolc_sb_rate,
                    'loit_cb_amount': quantity * loit_cb_rate,
                    'loit_sb_amount': quantity * loit_sb_rate,
                    'lolc_sb_amount': quantity * lolc_sb_rate,
                    'description': resource_description or s.service_description or ''
                })
            else:
                resource_details.append({
                    'name': resource_type_id.replace('_', ' ').title(),
                    'quantity': quantity,
                    'uom': 'units',
                    'loit_cb_rate': 0,
                    'loit_sb_rate': 0,
                    'lolc_sb_rate': 0,
                    'loit_cb_amount': 0,
                    'loit_sb_amount': 0,
                    'lolc_sb_amount': 0,
                    'description': resource_description or s.service_description or ''
                })
        
        # Use calculated values or fallback to stored
        loit_cb = loit_cb if loit_cb > 0 else float(s.chargeback_total or 0)
        loit_sb = loit_sb if loit_sb > 0 else float(s.showback_total or 0)
        
        service_total = loit_cb + loit_sb
        service_grand = service_total + lolc_sb
        
        units_data[unit_id]['total_loit_cb'] += loit_cb
        units_data[unit_id]['total_loit_sb'] += loit_sb
        units_data[unit_id]['total_loit'] += service_total
        units_data[unit_id]['total_lolc_sb'] += lolc_sb
        units_data[unit_id]['total_grand'] += service_grand
        
        units_data[unit_id]['services'].append({
            'id': s.id,
            'description': s.service_description,
            'service_date': s.service_date,
            'submission_date': s.submission_date,
            'bu_user_name': f"{s.first_name or ''} {s.last_name or ''}".strip() or 'N/A',
            'employee_id': s.employee_id or 'N/A',
            'unit_name': unit_name,
            'loit_cb': loit_cb,
            'loit_sb': loit_sb,
            'lolc_sb': lolc_sb,
            'service_total': service_total,
            'grand_total': service_grand,
            'resources': resource_details
        })
    
    # Calculate company totals
    company_total_loit_cb = sum(u['total_loit_cb'] for u in units_data.values())
    company_total_loit_sb = sum(u['total_loit_sb'] for u in units_data.values())
    company_total_loit = sum(u['total_loit'] for u in units_data.values())
    company_total_lolc_sb = sum(u['total_lolc_sb'] for u in units_data.values())
    company_grand_total = sum(u['total_grand'] for u in units_data.values())
    
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=landscape(A4), leftMargin=30, rightMargin=30, topMargin=40, bottomMargin=40)
    elements = []
    styles = getSampleStyleSheet()
    
    # Custom styles
    # Date style
    date_style = ParagraphStyle('DateStyle', parent=styles['Normal'], fontSize=7.5, leading=9, alignment=TA_LEFT, wordWrap='CJK')
    
    # Amount styles
    amount_style = ParagraphStyle('AmountStyle', parent=styles['Normal'], fontSize=7.5, leading=9, alignment=TA_RIGHT, wordWrap='CJK')
    
    # Resource name style
    name_style = ParagraphStyle('NameStyle', parent=styles['Normal'], fontSize=7.5, leading=9, alignment=TA_LEFT, wordWrap='CJK')
    
    # Description style
    desc_style = ParagraphStyle('DescStyle', parent=styles['Normal'], fontSize=7.5, leading=9, alignment=TA_LEFT, wordWrap='CJK', textColor=colors.HexColor('#1E293B'))
    
    header_style = ParagraphStyle('HeaderStyle', parent=styles['Normal'], fontSize=9, leading=12, alignment=TA_CENTER, textColor=colors.black, fontName='Helvetica-Bold')
    title_style = ParagraphStyle('CustomTitle', parent=styles['Title'], fontSize=16, alignment=TA_CENTER, spaceAfter=20, textColor=colors.HexColor('#1E3A5F'))
    info_style = ParagraphStyle('InfoStyle', parent=styles['Normal'], fontSize=10, leading=14)
    unit_title_style = ParagraphStyle('UnitTitle', parent=styles['Normal'], fontSize=12, leading=16, alignment=TA_LEFT, textColor=colors.HexColor('#1E3A5F'), fontName='Helvetica-Bold', spaceAfter=10, spaceBefore=15)
    
    # Title
    elements.append(Paragraph("<b>INVOICE</b>", title_style))
    elements.append(Spacer(1, 0.2*inch))
    
    # Company Info
    elements.append(Paragraph(f"<b>Company:</b> {company.name}", info_style))
    elements.append(Paragraph(f"<b>Period:</b> {month_name} {year}", info_style))
    elements.append(Paragraph(f"<b>Generated By:</b> {current_user.full_name} (Record Assistant)", info_style))
    elements.append(Paragraph(f"<b>Generated Date:</b> {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}", info_style))
    elements.append(Spacer(1, 0.3*inch))
    
    # Column widths - optimized for landscape A4 (approx 10.5 inches usable width)
    # Date: 0.7, BU User: 1.0, Resource: 1.2, Description: 3.0, Qty: 0.6
    # LOIT CB: 0.8, LOIT SB: 0.8, LOIT Total: 0.9, LOLC SB: 0.8, Grand Total: 0.9
    service_col_widths = [0.7*inch, 1.0*inch, 1.2*inch, 3.0*inch, 0.6*inch, 0.8*inch, 0.8*inch, 0.9*inch, 0.8*inch, 0.9*inch]
    
    # ============ UNIT-WISE TABLES ============
    for unit_id, unit_data in sorted(units_data.items(), key=lambda x: x[1]['name']):
        if not unit_data['services']:
            continue
        
        # Unit header
        elements.append(Paragraph(f"<b>{unit_data['name']}</b>", unit_title_style))
        elements.append(Spacer(1, 0.05*inch))
        
        # Build table data
        table_data = [
            ['Date', 'BU User', 'Resource', 'Description', 'Qty', 
             'LOIT CB', 'LOIT SB', 'LOIT Total', 
             'LOLC SB', 'Grand Total']
        ]
        
        for s in unit_data['services']:
            date_str = s['service_date'].strftime('%Y-%m-%d') if s['service_date'] else 'N/A'
            bu_user_name = s['bu_user_name']
            
            if s.get('resources') and len(s['resources']) > 0:
                for resource in s['resources']:
                    resource_desc = resource.get('description', '').strip()
                    if not resource_desc:
                        resource_desc = s['description'] if s['description'] else 'N/A'
                    
                    # Calculate totals for this resource
                    loit_cb_amt = resource['loit_cb_amount']
                    loit_sb_amt = resource['loit_sb_amount']
                    lolc_sb_amt = resource['lolc_sb_amount']
                    loit_total = loit_cb_amt + loit_sb_amt
                    grand_total = loit_total + lolc_sb_amt
                    
                    table_data.append([
                        Paragraph(date_str, date_style),  # Date - NO WRAP
                        Paragraph(bu_user_name, name_style),  # BU User - can wrap
                        Paragraph(resource['name'], name_style),  # Resource - can wrap
                        Paragraph(resource_desc, desc_style),  # Description - can wrap
                        Paragraph(f"{resource['quantity']:.1f}", amount_style),  # Qty - NO WRAP
                        Paragraph(f"{loit_cb_amt:,.2f}", amount_style),  # LOIT CB - NO WRAP
                        Paragraph(f"{loit_sb_amt:,.2f}", amount_style),  # LOIT SB - NO WRAP
                        Paragraph(f"{loit_total:,.2f}", amount_style),  # LOIT Total - NO WRAP
                        Paragraph(f"{lolc_sb_amt:,.2f}", amount_style),  # LOLC SB - NO WRAP
                        Paragraph(f"{grand_total:,.2f}", amount_style)  # Grand Total - NO WRAP
                    ])
            else:
                table_data.append([
                    Paragraph(date_str, date_style),
                    Paragraph(bu_user_name, name_style),
                    Paragraph('N/A', name_style),
                    Paragraph(s['description'][:100] if s['description'] else 'N/A', desc_style),
                    Paragraph('', amount_style),
                    Paragraph(f"{s['loit_cb']:,.2f}", amount_style),
                    Paragraph(f"{s['loit_sb']:,.2f}", amount_style),
                    Paragraph(f"{s['service_total']:,.2f}", amount_style),
                    Paragraph(f"{s['lolc_sb']:,.2f}", amount_style),
                    Paragraph(f"{s['grand_total']:,.2f}", amount_style)
                ])
        
        # Unit totals row
        table_data.append([
            Paragraph("<b>UNIT</b>", header_style),
            '', '', '', '',
            Paragraph(f"<b>{unit_data['total_loit_cb']:,.2f}</b>", amount_style),
            Paragraph(f"<b>{unit_data['total_loit_sb']:,.2f}</b>", amount_style),
            Paragraph(f"<b>{unit_data['total_loit']:,.2f}</b>", amount_style),
            Paragraph(f"<b>{unit_data['total_lolc_sb']:,.2f}</b>", amount_style),
            Paragraph(f"<b>{unit_data['total_grand']:,.2f}</b>", amount_style)
        ])
        
        # Create table with splitByRow=1 to allow splitting across pages
        table = Table(table_data, colWidths=service_col_widths, repeatRows=1, splitByRow=1)
        
        # Table Style
        table.setStyle(TableStyle([
            # Header row
            ('BACKGROUND', (0,0), (-1,0), colors.HexColor('#1E3A5F')),
            ('TEXTCOLOR', (0,0), (-1,0), colors.white),
            ('FONTNAME', (0,0), (-1,0), 'Helvetica-Bold'),
            ('FONTSIZE', (0,0), (-1,0), 9), # Slightly larger header font
            ('ALIGN', (0,0), (-1,0), 'CENTER'), 
            ('VALIGN', (0,0), (-1,0), 'MIDDLE'),
            
            # Body rows
            ('VALIGN', (0,1), (-1,-2), 'TOP'),
            
            # Alignment - amount columns right-aligned, others left
            ('ALIGN', (5,1), (9,-1), 'RIGHT'),  # All amount columns right-aligned
            ('ALIGN', (0,1), (4,-1), 'LEFT'),   # Left-aligned for text columns
            
            # Alternate row colors
            ('ROWBACKGROUNDS', (0,1), (-1,-2), [colors.white, colors.HexColor('#F8FAFC')]),
            
            # Unit total row
            ('BACKGROUND', (0,-1), (-1,-1), colors.HexColor('#EBF5FB')),
            ('FONTNAME', (0,-1), (-1,-1), 'Helvetica-Bold'),
            ('FONTSIZE', (0,-1), (-1,-1), 8.5), # Slightly larger total font
            ('ALIGN', (5,-1), (9,-1), 'RIGHT'),
            ('VALIGN', (0,-1), (-1,-1), 'MIDDLE'),
            
            # Borders
            ('GRID', (0,0), (-1,-1), 0.5, colors.HexColor('#CBD5E1')),
            
            # Padding
            ('TOPPADDING', (0,0), (-1,-1), 5),
            ('BOTTOMPADDING', (0,0), (-1,-1), 5),
            ('LEFTPADDING', (0,0), (-1,-1), 5),
            ('RIGHTPADDING', (0,0), (-1,-1), 5),
            
            # Set minimum row height for readability
            ('MINSIZE', (0,0), (-1,-1), 12),  # Minimum row height
        ]))
        
        elements.append(table)
        elements.append(Spacer(1, 0.15*inch))
    
    # ============ UNIT SUMMARY TABLE ============
    elements.append(PageBreak())
    elements.append(Paragraph("<b>UNIT SUMMARY</b>", title_style))
    elements.append(Spacer(1, 0.2*inch))
    
    summary_data = [
        ['Unit Name', 'LOIT CB (Rs)', 'LOIT SB (Rs)', 'LOIT Total (Rs)', 'LOLC SB (Rs)', 'Grand Total (Rs)']
    ]
    
    for unit_id, unit_data in sorted(units_data.items(), key=lambda x: x[1]['name']):
        if unit_data['services']:
            summary_data.append([
                Paragraph(unit_data['name'], name_style),
                Paragraph(f"{unit_data['total_loit_cb']:,.2f}", amount_style),
                Paragraph(f"{unit_data['total_loit_sb']:,.2f}", amount_style),
                Paragraph(f"{unit_data['total_loit']:,.2f}", amount_style),
                Paragraph(f"{unit_data['total_lolc_sb']:,.2f}", amount_style),
                Paragraph(f"{unit_data['total_grand']:,.2f}", amount_style)
            ])
    
    summary_data.append([
        Paragraph("<b>COMPANY TOTAL</b>", header_style),
        Paragraph(f"<b>{company_total_loit_cb:,.2f}</b>", amount_style),
        Paragraph(f"<b>{company_total_loit_sb:,.2f}</b>", amount_style),
        Paragraph(f"<b>{company_total_loit:,.2f}</b>", amount_style),
        Paragraph(f"<b>{company_total_lolc_sb:,.2f}</b>", amount_style),
        Paragraph(f"<b>{company_grand_total:,.2f}</b>", amount_style)
    ])
    
    summary_col_widths = [1.8*inch, 1.1*inch, 1.1*inch, 1.2*inch, 1.1*inch, 1.3*inch]
    
    summary_table = Table(summary_data, colWidths=summary_col_widths, repeatRows=1)
    summary_table.setStyle(TableStyle([
        ('BACKGROUND', (0,0), (-1,0), colors.HexColor('#1E3A5F')),
        ('TEXTCOLOR', (0,0), (-1,0), colors.white),
        ('FONTNAME', (0,0), (-1,0), 'Helvetica-Bold'),
        ('FONTSIZE', (0,0), (-1,0), 10),
        ('ALIGN', (0,0), (-1,0), 'CENTER'),
        ('VALIGN', (0,0), (-1,0), 'MIDDLE'),
        
        ('FONTSIZE', (0,1), (-1,-2), 9),
        ('VALIGN', (0,1), (-1,-2), 'MIDDLE'),
        
        ('ALIGN', (1,1), (5,-1), 'RIGHT'),
        ('ALIGN', (0,1), (0,-1), 'LEFT'),
        
        ('ROWBACKGROUNDS', (0,1), (-1,-2), [colors.white, colors.HexColor('#F8FAFC')]),
        
        ('BACKGROUND', (0,-1), (-1,-1), colors.HexColor('#DBEAFE')),
        ('FONTNAME', (0,-1), (-1,-1), 'Helvetica-Bold'),
        ('FONTSIZE', (0,-1), (-1,-1), 10),
        ('ALIGN', (1,-1), (5,-1), 'RIGHT'),
        
        ('GRID', (0,0), (-1,-1), 0.5, colors.HexColor('#CBD5E1')),
        
        ('TOPPADDING', (0,0), (-1,-1), 8),
        ('BOTTOMPADDING', (0,0), (-1,-1), 8),
        ('LEFTPADDING', (0,0), (-1,-1), 8),
        ('RIGHTPADDING', (0,0), (-1,-1), 8),
    ]))
    elements.append(summary_table)
    
    # Footer
    elements.append(Spacer(1, 0.5*inch))
    footer_style = ParagraphStyle('FooterStyle', parent=styles['Normal'], fontSize=8, alignment=TA_CENTER, textColor=colors.HexColor('#94A3B8'))
    elements.append(Paragraph("This is a system-generated report. For questions, contact the finance team.", footer_style))
    
    doc.build(elements)
    buffer.seek(0)
    
    return send_file(
        buffer,
        mimetype='application/pdf',
        as_attachment=True,
        download_name=f"LOIT_Report_{company.name}_{year}_{month}.pdf"
    
    )