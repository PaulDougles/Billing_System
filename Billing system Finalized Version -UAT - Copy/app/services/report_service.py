from sqlalchemy import extract, func
from sqlalchemy.orm import joinedload

from app.extensions import db
from app.models import Company, ResourceType, ServiceItem, TimeEntry, User
from app.services.resource_service import calculate_resource_rates


def get_company_invoice_summary(company_id, year, month):
    """Aggregate approved service counts and totals for a company and period."""
    count, total = db.session.query(
        func.count(ServiceItem.id),
        func.coalesce(func.sum(ServiceItem.total_amount), 0),
    ).join(TimeEntry, ServiceItem.time_entry_id == TimeEntry.id).filter(
        ServiceItem.company_id == company_id,
        ServiceItem.finance_approval_status == 'approved',
        func.extract('year', TimeEntry.service_date) == year,
        func.extract('month', TimeEntry.service_date) == month,
    ).one()
    return {'service_count': count, 'total_amount': float(total)}


def get_active_companies():
    return Company.query.filter_by(is_active=True).order_by(Company.name).all()


def get_company_dashboard_data(*, company_ids=None, month=None, year=None):
    service_items_query = ServiceItem.query.options(
        joinedload(ServiceItem.time_entry)
        .joinedload(TimeEntry.user)
        .joinedload(User.unit),
        joinedload(ServiceItem.company),
    ).join(TimeEntry).filter(
        ServiceItem.finance_approval_status == 'approved',
    )
    if company_ids is not None:
        service_items_query = service_items_query.filter(
            ServiceItem.company_id.in_(company_ids)
        )
    if month:
        service_items_query = service_items_query.filter(
            extract('month', TimeEntry.service_date) == month
        )
    if year:
        service_items_query = service_items_query.filter(
            extract('year', TimeEntry.service_date) == year
        )
    service_items = service_items_query.order_by(ServiceItem.created_at.desc()).all()
    resource_by_id = {
        str(resource_type.id): resource_type
        for resource_type in ResourceType.query.all()
    }

    services = []
    total_amount = 0.0
    for item in service_items:
        entry = item.time_entry
        if not entry:
            continue
        bu_user = entry.user
        rate_data = calculate_resource_rates(
            item.resources,
            resource_by_id.values(),
        )
        processed_resources = []
        for resource, detail in zip(item.resources or [], rate_data['details']):
            resource_type = detail['resource_type']
            resource_type_id = str(resource.get('type', ''))
            processed_resources.append({
                'name': resource_type.name if resource_type else f"Unknown (ID {resource_type_id})",
                'quantity': resource.get('quantity', 0),
                'uom': resource_type.unit_of_measure or 'units' if resource_type else 'units',
                'chargeback_rate': detail['chargeback_rate'],
                'showback_rate': detail['showback_rate'],
                'flat_fee': resource.get('flat_fee', 0),
            })

        amount = float(item.total_amount or 0)
        services.append({
            'id': item.id,
            'company_name': item.company.name if item.company else 'Unknown',
            'submission_date': (
                entry.submission_date.strftime('%Y-%m-%d %H:%M')
                if entry.submission_date
                else entry.created_at.strftime('%Y-%m-%d %H:%M')
            ),
            'description': item.service_description,
            'total_amount': amount,
            'resources': processed_resources,
            'invoice_generated': item.invoice_generated,
            'invoice_number': item.invoice_number,
            'bu_user': {
                'full_name': bu_user.full_name if bu_user else 'Unknown',
                'employee_id': bu_user.employee_id if bu_user else 'N/A',
                'team_name': bu_user.unit.name if bu_user and bu_user.unit else 'N/A',
            },
        })
        total_amount += amount

    return {
        'services': services,
        'total_services': len(services),
        'total_amount': total_amount,
        'all_invoiced': bool(services) and all(
            service['invoice_generated'] for service in services
        ),
    }
