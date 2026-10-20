# app/routes/supervisor.py
from flask import Blueprint, render_template, redirect, url_for, flash, request, jsonify
from flask_login import login_required, current_user
from app.models import TimeEntry, ServiceItem, User, Unit, UnitMember, Company, ResourceType, SystemSetting
from app.extensions import db
from datetime import datetime, timedelta
from calendar import month_name
from sqlalchemy import extract
from app.utils.cycle_helper import CycleHelper
from app.utils.week_helper import get_setting
import traceback

supervisor_bp = Blueprint('supervisor', __name__, url_prefix='/supervisor')


@supervisor_bp.route('/dashboard')
@login_required
def dashboard():
    """Supervisor Dashboard - Group pending services by company (using supervisor_approval_status)"""
    if current_user.role != 'supervisor':
        flash('Access denied. Supervisor access only.', 'danger')
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
    
    # Get units where this user is supervisor
    units = Unit.query.filter_by(supervisor_id=current_user.id).all()
    unit_ids = [u.id for u in units]
    
    if not unit_ids:
        return render_template('supervisor/dashboard.html',
                             pending_companies=[],
                             approved_companies=[],
                             rejected_companies=[],
                             total_pending_services=0,
                             total_pending_amount=0,
                             total_approved_services=0,
                             total_approved_amount=0,
                             total_rejected_services=0,
                             total_rejected_amount=0,
                             units=[],
                             selected_month=month,
                             selected_year=year,
                             current_date=current_date)
    
    def get_companies_by_status(status):
        services = ServiceItem.query.join(TimeEntry).filter(
            TimeEntry.unit_id.in_(unit_ids),
            ServiceItem.supervisor_approval_status == status,
            extract('month', TimeEntry.service_date) == month,
            extract('year', TimeEntry.service_date) == year
        ).all()
        companies_dict = {}
        for svc in services:
            company = Company.query.get(svc.company_id)
            if not company:
                continue
            cid = company.id
            if cid not in companies_dict:
                companies_dict[cid] = {
                    'id': cid,
                    'name': company.name,
                    'service_count': 0,
                    'total_amount': 0.0
                }
            companies_dict[cid]['service_count'] += 1
            companies_dict[cid]['total_amount'] += float(svc.total_amount)
        return list(companies_dict.values())
    
    pending_companies = get_companies_by_status('pending')
    approved_companies = get_companies_by_status('approved')
    rejected_companies = get_companies_by_status('rejected')
    
    total_pending_services = sum(c['service_count'] for c in pending_companies)
    total_pending_amount = sum(c['total_amount'] for c in pending_companies)
    total_approved_services = sum(c['service_count'] for c in approved_companies)
    total_approved_amount = sum(c['total_amount'] for c in approved_companies)
    total_rejected_services = sum(c['service_count'] for c in rejected_companies)
    total_rejected_amount = sum(c['total_amount'] for c in rejected_companies)
    
    return render_template('supervisor/dashboard.html',
                         pending_companies=pending_companies,
                         approved_companies=approved_companies,
                         rejected_companies=rejected_companies,
                         total_pending_services=total_pending_services,
                         total_pending_amount=total_pending_amount,
                         total_approved_services=total_approved_services,
                         total_approved_amount=total_approved_amount,
                         total_rejected_services=total_rejected_services,
                         total_rejected_amount=total_rejected_amount,
                         units=units,
                         selected_month=month,
                         selected_year=year,
                         current_date=datetime.now())


@supervisor_bp.route('/company/<int:company_id>')
@login_required
def company_services(company_id):
    """Show service items for a specific company filtered by supervisor_approval_status"""
    if current_user.role != 'supervisor':
        flash('Access denied.', 'danger')
        return redirect(url_for('dashboard.index'))
    
    status = request.args.get('status', 'pending')
    if status not in ['pending', 'approved', 'rejected']:
        status = 'pending'
    
    from datetime import date
    current_date = date.today()
    month = request.args.get('month', current_date.month, type=int)
    year = request.args.get('year', current_date.year, type=int)
    if not (1 <= month <= 12):
        month = current_date.month
    if year < 2020 or year > 2100:
        year = current_date.year
    
    units = Unit.query.filter_by(supervisor_id=current_user.id).all()
    unit_ids = [u.id for u in units]
    if not unit_ids:
        flash('No units assigned.', 'danger')
        return redirect(url_for('supervisor.dashboard'))
    
    services = ServiceItem.query.join(TimeEntry).filter(
        TimeEntry.unit_id.in_(unit_ids),
        ServiceItem.supervisor_approval_status == status,
        ServiceItem.company_id == company_id,
        extract('month', TimeEntry.service_date) == month,
        extract('year', TimeEntry.service_date) == year
    ).all()
    
    services_data = []
    total_amount = 0
    for svc in services:
        entry = TimeEntry.query.get(svc.time_entry_id)
        if not entry:
            continue
        bu_user = User.query.get(entry.user_id)
        services_data.append({
            'service_id': svc.id,
            'entry_id': entry.id,
            'description': svc.service_description,
            'amount': float(svc.total_amount),
            'bu_user_name': bu_user.full_name if bu_user else 'Unknown',
            'bu_user_employee_id': bu_user.employee_id if bu_user else 'N/A',
            'unit_name': bu_user.unit.name if bu_user and bu_user.unit else 'N/A',
            'service_date': entry.service_date.strftime('%Y-%m-%d') if entry.service_date else 'N/A',
            'week_number': entry.week_number,
            'submission_date': entry.submission_date.strftime('%Y-%m-%d %H:%M:%S') if entry.submission_date else entry.created_at.strftime('%Y-%m-%d %H:%M:%S')
        })
        total_amount += float(svc.total_amount)
    
    company = Company.query.get(company_id)
    return render_template('supervisor/company_services.html',
                         company=company,
                         services=services_data,
                         total_amount=total_amount,
                         services_count=len(services_data),
                         selected_month=month,
                         selected_year=year,
                         current_status=status)


@supervisor_bp.route('/entry/<int:entry_id>')
@login_required
def view_entry(entry_id):
    """View a specific time entry details"""
    if current_user.role != 'supervisor':
        flash('Access denied. Supervisor access only.', 'danger')
        return redirect(url_for('dashboard.index'))
    
    entry = TimeEntry.query.get_or_404(entry_id)
    
    # Check if current user is supervisor of this entry's unit
    user = User.query.get(entry.user_id)
    if not user or not user.unit_id:
        flash('User not in any unit', 'danger')
        return redirect(url_for('supervisor.dashboard'))
    
    unit = Unit.query.get(user.unit_id)
    if unit.supervisor_id != current_user.id:
        flash('You are not authorized to view this entry', 'danger')
        return redirect(url_for('supervisor.dashboard'))
    
    bu_user = User.query.get(entry.user_id)
    services = ServiceItem.query.filter_by(time_entry_id=entry.id).all()
    
    # Get resource types for display
    resource_types = {rt.code: rt for rt in ResourceType.query.all()}
    
    services_data = []
    for service in services:
        resources = []
        for r in service.resources or []:
            rt = resource_types.get(r.get('type'))
            resources.append({
                'name': rt.name if rt else r.get('type'),
                'hours': r.get('hours', 0),
                'rate': r.get('rate', 0),
                'amount': (r.get('hours', 0) * r.get('rate', 0)) if r.get('hours') else r.get('flat_fee', 0)
            })
        
        company_name = 'N/A'
        if service.company_id:
            company = Company.query.get(service.company_id)
            if company:
                company_name = company.name
        
        services_data.append({
            'id': service.id,
            'company_name': company_name,
            'description': service.service_description,
            'total_amount': float(service.total_amount),
            'resources': resources
        })
    
    # Get week info from cycle
    week_info = CycleHelper.get_week_for_date(entry.service_date if entry.service_date else datetime.now().date())
    
    entry_data = {
        'id': entry.id,
        'user_name': bu_user.full_name if bu_user else 'Unknown',
        'employee_id': bu_user.employee_id if bu_user else 'N/A',
        'unit_name': unit.name if unit else 'N/A',
        'submission_date': entry.submission_date.strftime('%Y-%m-%d %H:%M') if entry.submission_date else entry.created_at.strftime('%Y-%m-%d %H:%M'),
        'service_date': entry.service_date.strftime('%Y-%m-%d') if entry.service_date else 'N/A',
        'week_number': week_info['week_number'],
        'week_start': week_info['week_start'].strftime('%Y-%m-%d') if week_info['week_start'] else 'N/A',
        'week_end': week_info['week_end'].strftime('%Y-%m-%d') if week_info['week_end'] else 'N/A',
        'total_amount': float(entry.get_total_amount()),
        'services': services_data,
        'status': entry.supervisor_approval_status,
        'supervisor_comment': entry.supervisor_comment
    }
    
    return render_template('supervisor/view_entry.html', entry=entry_data)


@supervisor_bp.route('/debug')
@login_required
def debug():
    """Debug route to check data - Only for troubleshooting"""
    if current_user.role != 'supervisor' and current_user.role != 'admin':
        return jsonify({'error': 'Unauthorized'}), 403
    
    result = {
        'user': {
            'id': current_user.id,
            'email': current_user.email,
            'role': current_user.role
        },
        'units_as_supervisor': [],
        'unit_members': [],
        'pending_entries': [],
        'cycle_info': CycleHelper.format_cycle_for_display()
    }
    
    # Get units where user is supervisor
    units = Unit.query.filter_by(supervisor_id=current_user.id).all()
    for unit in units:
        result['units_as_supervisor'].append({
            'id': unit.id,
            'name': unit.name,
            'supervisor_id': unit.supervisor_id
        })
    
    unit_ids = [u.id for u in units]
    
    # Get team members
    members = UnitMember.query.filter(UnitMember.unit_id.in_(unit_ids), UnitMember.is_active == True).all()
    for m in members:
        user = User.query.get(m.user_id)
        result['unit_members'].append({
            'user_id': m.user_id,
            'email': user.email if user else 'Unknown',
            'full_name': user.full_name if user else 'Unknown',
            'unit_id': m.unit_id
        })
    
    user_ids = [m.user_id for m in members]
    
    # Get pending entries
    entries = TimeEntry.query.filter(
        TimeEntry.user_id.in_(user_ids),
        TimeEntry.supervisor_approval_status == 'pending'
    ).all()
    
    for entry in entries:
        user = User.query.get(entry.user_id)
        result['pending_entries'].append({
            'id': entry.id,
            'user_id': entry.user_id,
            'user_email': user.email if user else 'Unknown',
            'supervisor_approval_status': entry.supervisor_approval_status,
            'status': entry.status,
            'total_amount': float(entry.get_total_amount()),
            'service_date': entry.service_date.strftime('%Y-%m-%d') if entry.service_date else 'N/A'
        })
    
    return jsonify(result)
