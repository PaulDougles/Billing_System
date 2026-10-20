from datetime import datetime

from app.extensions import db
from app.models import ActivityLog, Company, ServiceItem
from app.services.company_service import get_payment_team_for_company


def assign_payment_team(service_item):
    company = Company.query.get(service_item.company_id)
    service_item.assigned_payment_team = get_payment_team_for_company(company)
    return service_item.assigned_payment_team


def approve_service_item(service_item, *, user_id, comment='', remote_addr=None):
    if service_item.head_approval_status != 'approved':
        raise ValueError('Service not approved by Head of IT')
    if service_item.finance_approval_status != 'pending':
        raise ValueError('Service already processed')

    service_item.finance_approval_status = 'approved'
    service_item.finance_approval_comment = comment
    service_item.finance_approved_by = user_id
    service_item.finance_approved_at = datetime.utcnow()
    assigned_team = assign_payment_team(service_item)
    entry = service_item.time_entry
    db.session.add(ActivityLog(
        time_entry_id=entry.id,
        user_id=user_id,
        action='finance_approved',
        details={
            'service_item_id': service_item.id,
            'comment': comment,
            'assigned_payment_team': assigned_team,
            'ip': remote_addr,
        },
    ))
    return assigned_team


def get_service_item(service_item_id):
    return ServiceItem.query.get_or_404(service_item_id)
