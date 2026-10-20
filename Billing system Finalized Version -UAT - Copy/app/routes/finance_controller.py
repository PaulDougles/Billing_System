from flask import Blueprint, render_template, redirect, url_for, flash, request, jsonify
from flask_login import login_required, current_user
from app.models import TimeEntry, ServiceItem, Company, User, ActivityLog, Unit, ResourceType
from app.extensions import db
from datetime import datetime
from sqlalchemy import func, extract
import traceback

finance_controller_bp = Blueprint('finance_controller', __name__)

@finance_controller_bp.route('/dashboard')
@login_required
def dashboard():
    """Finance Controller Dashboard - Show companies grouped by status (Pending, Approved, Rejected)."""
    if current_user.role != 'finance_controller':
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

    # Helper function to fetch companies by finance approval status
    def get_companies_by_status(status):
        # For 'rejected' we ignore head_approval_status (keep all finance rejected)
        if status == 'rejected':
            services_query = db.session.query(
                ServiceItem.company_id,
                func.count(ServiceItem.id).label('service_count'),
                func.sum(ServiceItem.total_amount).label('total_amount')
            ).join(TimeEntry).filter(
                ServiceItem.finance_approval_status == status,
                extract('month', TimeEntry.service_date) == month,
                extract('year', TimeEntry.service_date) == year
            ).group_by(ServiceItem.company_id).subquery()
        else:
            # For 'pending' and 'approved' we require head_approval = 'approved'
            services_query = db.session.query(
                ServiceItem.company_id,
                func.count(ServiceItem.id).label('service_count'),
                func.sum(ServiceItem.total_amount).label('total_amount')
            ).join(TimeEntry).filter(
                ServiceItem.head_approval_status == 'approved',
                ServiceItem.finance_approval_status == status,
                extract('month', TimeEntry.service_date) == month,
                extract('year', TimeEntry.service_date) == year
            ).group_by(ServiceItem.company_id).subquery()

        companies_data = db.session.query(
            Company.id,
            Company.name,
            services_query.c.service_count,
            services_query.c.total_amount
        ).join(services_query, services_query.c.company_id == Company.id)\
         .order_by(Company.name).all()

        companies = []
        for c in companies_data:
            companies.append({
                'id': c.id,
                'name': c.name,
                'service_count': c.service_count,
                'total_amount': float(c.total_amount or 0)
            })
        return companies

    pending_companies = get_companies_by_status('pending')
    approved_companies = get_companies_by_status('approved')
    rejected_companies  = get_companies_by_status('rejected')

    # Calculate totals
    pending_total_services = sum(c['service_count'] for c in pending_companies)
    pending_total_amount  = sum(c['total_amount'] for c in pending_companies)
    approved_total_services = sum(c['service_count'] for c in approved_companies)
    approved_total_amount  = sum(c['total_amount'] for c in approved_companies)
    rejected_total_services = sum(c['service_count'] for c in rejected_companies)
    rejected_total_amount  = sum(c['total_amount'] for c in rejected_companies)

    return render_template(
        'finance_controller/dashboard.html',
        pending_companies=pending_companies,
        approved_companies=approved_companies,
        rejected_companies=rejected_companies,
        pending_total_services=pending_total_services,
        pending_total_amount=pending_total_amount,
        approved_total_services=approved_total_services,
        approved_total_amount=approved_total_amount,
        rejected_total_services=rejected_total_services,
        rejected_total_amount=rejected_total_amount,
        selected_month=month,
        selected_year=year,
        current_date=datetime.now()
    )


@finance_controller_bp.route('/company/<int:company_id>/units')
@login_required
def unit_service(company_id):
    status = request.args.get('status', 'pending')
    
    if current_user.role != 'finance_controller':
        flash('Access denied.', 'danger')
        return redirect(url_for('dashboard.index'))

    from datetime import date
    current_date = date.today()
    month = request.args.get('month', current_date.month, type=int)
    year = request.args.get('year', current_date.year, type=int)
    if not (1 <= month <= 12):
        month = current_date.month
    if year < 2020 or year > 2100:
        year = current_date.year

    company = Company.query.get_or_404(company_id)

    # Get all service items for this company with the given finance_approval_status
    if status == 'rejected':
        # For rejected, ignore head_approval_status
        pending_items = ServiceItem.query.join(TimeEntry, ServiceItem.time_entry_id == TimeEntry.id).filter(
            ServiceItem.company_id == company_id,
            ServiceItem.finance_approval_status == status,
            extract('month', TimeEntry.service_date) == month,
            extract('year', TimeEntry.service_date) == year
        ).all()
    else:
        # For pending/approved, require head_approval_status == 'approved'
        pending_items = ServiceItem.query.join(TimeEntry, ServiceItem.time_entry_id == TimeEntry.id).filter(
            ServiceItem.company_id == company_id,
            ServiceItem.head_approval_status == 'approved',
            ServiceItem.finance_approval_status == status,
            extract('month', TimeEntry.service_date) == month,
            extract('year', TimeEntry.service_date) == year
        ).all()

    # Collect unit ids from the time entries' users
    unit_map = {}
    for item in pending_items:
        entry = TimeEntry.query.get(item.time_entry_id)
        if entry:
            user = User.query.get(entry.user_id)
            if user and user.unit_id:
                uid = user.unit_id
                if uid not in unit_map:
                    unit = Unit.query.get(uid)
                    if unit:
                        unit_map[uid] = {
                            'id': unit.id,
                            'name': unit.name,
                            'service_count': 0,
                            'total_amount': 0.0
                        }
                unit_map[uid]['service_count'] += 1
                unit_map[uid]['total_amount'] += float(item.total_amount)

    units = list(unit_map.values())

    return render_template('finance_controller/unit_service.html',
                         company=company,
                         units=units,
                         status=status,
                         selected_month=month,
                         selected_year=year)


@finance_controller_bp.route('/unit/<int:unit_id>/services')
@login_required
def unit_details(unit_id):
    status = request.args.get('status', 'pending')
    
    if current_user.role != 'finance_controller':
        flash('Access denied.', 'danger')
        return redirect(url_for('dashboard.index'))

    from datetime import date
    current_date = date.today()
    month = request.args.get('month', current_date.month, type=int)
    year = request.args.get('year', current_date.year, type=int)
    if not (1 <= month <= 12):
        month = current_date.month
    if year < 2020 or year > 2100:
        year = current_date.year

    unit = Unit.query.get_or_404(unit_id)

    # Build query based on status
    query = db.session.query(ServiceItem).\
        join(TimeEntry, ServiceItem.time_entry_id == TimeEntry.id).\
        join(User, TimeEntry.user_id == User.id).\
        filter(
            User.unit_id == unit_id,
            extract('month', TimeEntry.service_date) == month,
            extract('year', TimeEntry.service_date) == year
        )

    if status == 'rejected':
        query = query.filter(ServiceItem.finance_approval_status == 'rejected')
    else:
        query = query.filter(
            ServiceItem.head_approval_status == 'approved',
            ServiceItem.finance_approval_status == status
        )

    services = query.all()

    # Group by BU user (same as before)
    users_dict = {}
    for item in services:
        entry = TimeEntry.query.get(item.time_entry_id)
        if not entry:
            continue
        bu_user = User.query.get(entry.user_id)
        if not bu_user:
            continue
        user_key = bu_user.id
        if user_key not in users_dict:
            users_dict[user_key] = {
                'user_id': bu_user.id,
                'user_name': bu_user.full_name,
                'employee_id': bu_user.employee_id,
                'services': [],
                'total_amount': 0
            }
        users_dict[user_key]['services'].append({
            'id': item.id,
            'description': item.service_description,
            'total_amount': float(item.total_amount),
            'submission_date': entry.submission_date.strftime('%Y-%m-%d %H:%M') if entry.submission_date else entry.created_at.strftime('%Y-%m-%d %H:%M'),
            'resources': item.resources,
            'company_id': item.company_id
        })
        users_dict[user_key]['total_amount'] += float(item.total_amount)

    users_list = list(users_dict.values())

    return render_template('finance_controller/unit_details.html',
                         unit=unit,
                         users=users_list,
                         status=status,
                         selected_month=month,
                         selected_year=year)

@finance_controller_bp.route('/unit/<int:unit_id>')
@login_required
def unit_services(unit_id):
    """Show services for a specific unit, grouped by BU user."""
    if current_user.role != 'finance_controller':
        flash('Access denied.', 'danger')
        return redirect(url_for('dashboard.index'))

    unit = Unit.query.get_or_404(unit_id)

    # Get all services pending finance approval for this unit
    services = ServiceItem.query.join(TimeEntry).join(User).filter(
        User.unit_id == unit_id,
        ServiceItem.head_approval_status == 'approved',
        ServiceItem.finance_approval_status == 'pending'
    ).all()

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
        if user_key not in users_dict:
            users_dict[user_key] = {
                'user_id': bu_user.id,
                'user_name': bu_user.full_name,
                'employee_id': bu_user.employee_id,
                'services': [],
                'total_amount': 0
            }
        users_dict[user_key]['services'].append({
            'id': item.id,
            'description': item.service_description,
            'total_amount': float(item.total_amount),
            'submission_date': entry.submission_date.strftime('%Y-%m-%d %H:%M') if entry.submission_date else entry.created_at.strftime('%Y-%m-%d %H:%M'),
            'resources': item.resources
        })
        users_dict[user_key]['total_amount'] += float(item.total_amount)

    users_list = list(users_dict.values())

    return render_template('finance_controller/unit_services.html',
                         unit=unit,
                         users=users_list)


@finance_controller_bp.route('/company/<int:company_id>')
@login_required
def company_services(company_id):
    """Show services for a specific company, grouped by Unit."""
    if current_user.role != 'finance_controller':
        flash('Access denied.', 'danger')
        return redirect(url_for('dashboard.index'))

    from datetime import date
    current_date = date.today()
    month = request.args.get('month', current_date.month, type=int)
    year = request.args.get('year', current_date.year, type=int)
    if not (1 <= month <= 12):
        month = current_date.month
    if year < 2020 or year > 2100:
        year = current_date.year

    company = Company.query.get_or_404(company_id)

    # Get all services pending finance approval for this company
    services = ServiceItem.query.join(TimeEntry, ServiceItem.time_entry_id == TimeEntry.id).filter(
        ServiceItem.company_id == company_id,
        ServiceItem.head_approval_status == 'approved',
        ServiceItem.finance_approval_status == 'pending',
        extract('month', TimeEntry.service_date) == month,
        extract('year', TimeEntry.service_date) == year
    ).all()

    # Group by Unit (via the BU user's unit)
    units_dict = {}
    for item in services:
        entry = TimeEntry.query.get(item.time_entry_id)
        if not entry:
            continue
        bu_user = User.query.get(entry.user_id)
        if not bu_user:
            continue
        unit = Unit.query.get(bu_user.unit_id)
        unit_name = unit.name if unit else 'Unassigned'
        unit_id = unit.id if unit else 0

        if unit_id not in units_dict:
            units_dict[unit_id] = {
                'unit_id': unit_id,
                'unit_name': unit_name,
                'services': [],
                'total_quantity': 0,
                'total_amount': 0
            }

        # For each service, we need resources list (for display, but not for batch approval)
        # We'll store the service id, description, amount, etc.
        units_dict[unit_id]['services'].append({
            'id': item.id,
            'description': item.service_description,
            'total_amount': float(item.total_amount),
            'submission_date': entry.submission_date.strftime('%Y-%m-%d %H:%M') if entry.submission_date else entry.created_at.strftime('%Y-%m-%d %H:%M'),
            'bu_user': bu_user.full_name,
            'employee_id': bu_user.employee_id,
            'resources': item.resources  # for display in modal
        })
        units_dict[unit_id]['total_amount'] += float(item.total_amount)
        # total quantity (if needed) – could sum resource quantities
        for res in item.resources or []:
            units_dict[unit_id]['total_quantity'] += res.get('quantity', 0)

    units_list = list(units_dict.values())

    return render_template('finance_controller/company_services.html',
                         company=company,
                         units=units_list,
                         selected_month=month,
                         selected_year=year)

@finance_controller_bp.route('/resource-group/<int:resource_type_id>')
@login_required
def view_resource_group(resource_type_id):
    """Show all service items for a specific resource type (Finance Controller view)"""
    if current_user.role != 'finance_controller':
        flash('Access denied.', 'danger')
        return redirect(url_for('dashboard.index'))

    rt = ResourceType.query.get_or_404(resource_type_id)

    # Use the SAME query as the dashboard grouping
    all_services = ServiceItem.query.filter(
        ServiceItem.head_approval_status == 'approved'
    ).all()

    services = []
    for item in all_services:
        if not item.resources:
            continue
        # Check if this service contains the requested resource type
        matching_resource = None
        for res in item.resources:
            if str(res.get('type')) == str(resource_type_id):
                matching_resource = res
                break
        if not matching_resource:
            continue

        entry = TimeEntry.query.get(item.time_entry_id)
        if not entry:
            continue
        bu_user = User.query.get(entry.user_id)
        quantity = matching_resource.get('quantity', 0)
        uom = rt.unit_of_measure or ''

        services.append({
            'id': item.id,
            'company_id': item.company_id,
            'submission_date': entry.submission_date.strftime('%Y-%m-%d %H:%M:%S') if entry.submission_date else entry.created_at.strftime('%Y-%m-%d %H:%M:%S'),
            'bu_user': bu_user.full_name if bu_user else 'Unknown',
            'employee_id': bu_user.employee_id if bu_user else 'N/A',
            'unit_name': bu_user.unit.name if bu_user and bu_user.unit else 'N/A',
            'description': item.service_description,
            'quantity': quantity,
            'uom': uom,
            'amount': float(item.total_amount),
            'finance_approval_status': item.finance_approval_status   # show the full service amount
        })

    return render_template('finance_controller/resource_group.html',
                         resource_name=rt.name,
                         services=services,
                         company_name='All Companies')

@finance_controller_bp.route('/review-service/<int:service_id>')
@login_required
def review_service(service_id):
    """Review a specific service item for finance approval"""
    if current_user.role != 'finance_controller':
        flash('Access denied. Finance Controller only.', 'danger')
        return redirect(url_for('dashboard.index'))
    
    service_item = ServiceItem.query.get_or_404(service_id)
    entry = TimeEntry.query.get(service_item.time_entry_id)
    
    # Get BU user
    bu_user = User.query.get(entry.user_id) if entry else None
    
    # Get unit name for BU user
    unit_name = 'N/A'
    if bu_user and bu_user.unit_id:
        unit = Unit.query.get(bu_user.unit_id)
        if unit:
            unit_name = unit.name
    
    # Get Head of IT who approved this service
    head_of_it = None
    if service_item.head_approved_by:
        head_of_it = User.query.get(service_item.head_approved_by)
    
    # Get company name
    company = Company.query.get(service_item.company_id)
    company_name = company.name if company else 'N/A'
    
    # Get resource types for lookup
    all_resource_types = ResourceType.query.all()
    resource_by_id = {str(rt.id): rt for rt in all_resource_types}
    
    # Process resources to add display names, quantities, and amounts
    processed_resources = []
    for resource in service_item.resources or []:
        resource_type_id = str(resource.get('type', ''))
        rt = resource_by_id.get(resource_type_id)
        
        if rt:
            resource_name = rt.name
            uom = rt.unit_of_measure or 'units'
            chargeback_rate = float(rt.chargeback_rate) if rt.chargeback_rate else 0
            showback_rate = float(rt.showback_rate) if rt.showback_rate else 0
        else:
            resource_name = f'Unknown ({resource_type_id})'
            uom = 'units'
            chargeback_rate = resource.get('chargeback_rate', 0)
            showback_rate = resource.get('showback_rate', 0)
        
        quantity = resource.get('quantity', resource.get('hours', 0))
        
        processed_resources.append({
            'name': resource_name,
            'quantity': quantity,
            'uom': uom,
            'chargeback_rate': chargeback_rate,
            'showback_rate': showback_rate,
            'chargeback_amount': quantity * chargeback_rate,
            'showback_amount': quantity * showback_rate,
            'flat_fee': resource.get('flat_fee', 0),
            'annual_cost': resource.get('annual_cost', 0)
        })
    
    service_data = {
        'id': service_item.id,
        'entry_id': entry.id if entry else None,
        'submission_date': entry.submission_date.strftime('%Y-%m-%d %H:%M') if entry and entry.submission_date else (entry.created_at.strftime('%Y-%m-%d %H:%M') if entry else 'N/A'),
        'description': service_item.service_description,
        'total_amount': float(service_item.total_amount),
        'chargeback_total': float(service_item.chargeback_total) if service_item.chargeback_total else 0,
        'showback_total': float(service_item.showback_total) if service_item.showback_total else 0,
        'resources': processed_resources,
        'flat_fee': float(getattr(service_item, 'flat_fee', 0) or 0),
        'company_name': company_name,
        'bu_user': {
            'full_name': bu_user.full_name if bu_user else 'Unknown',
            'employee_id': bu_user.employee_id if bu_user else 'N/A',
            'unit_name': unit_name
        },
        'head_of_it': {
            'full_name': head_of_it.full_name if head_of_it else 'N/A'
        },
        'head_approval_comment': service_item.head_approval_comment,
        'finance_approval_comment': service_item.finance_approval_comment,
        'finance_approval_status': service_item.finance_approval_status
    }
    
    return render_template('finance_controller/review_service.html', service=service_data)