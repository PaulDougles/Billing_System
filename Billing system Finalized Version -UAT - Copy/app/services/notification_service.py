import traceback

from flask import current_app, url_for

from app.extensions import db
from app.models import Company, Notification


def send_notification(user, entry, service_item=None, type=None, url=None):
    if not user:
        return

    try:
        dashboard_url = url
        if type == 'ready_for_invoice':
            endpoint = (
                'payment.company_dashboard'
                if user.role == 'payment_company_user'
                else 'payment.dashboard'
            )
            dashboard_url = url_for(endpoint, _external=True)

        if type == 'supervisor_pending':
            from app.utils.email_helper import send_submission_email
            send_submission_email(user, entry, service_item)
        elif type in ('supervisor_approved', 'supervisor_rejected'):
            from app.utils.email_helper import send_approval_email
            status = 'approved' if type == 'supervisor_approved' else 'rejected'
            comment = service_item.supervisor_comment if service_item else None
            send_approval_email(
                user, service_item, entry, status,
                comment=comment, approver_role='Supervisor',
            )
        elif type == 'pending_approval':
            from app.utils.email_helper import send_submission_email
            send_submission_email(user, entry, service_item)
        elif type in ('head_approved', 'head_rejected'):
            from app.utils.email_helper import send_approval_email
            status = 'approved' if type == 'head_approved' else 'rejected'
            comment = (
                service_item.head_approval_comment
                if service_item
                else entry.head_approval_comment
            )
            send_approval_email(
                user, service_item, entry, status,
                comment=comment, approver_role='Head of IT',
            )
        elif type == 'pending_finance':
            from app.utils.email_helper import send_finance_notification
            send_finance_notification(user, entry, service_item)
        elif type in ('finance_approved', 'finance_rejected'):
            from app.utils.email_helper import send_approval_email
            status = 'approved' if type == 'finance_approved' else 'rejected'
            comment = (
                service_item.finance_approval_comment
                if service_item
                else entry.finance_approval_comment
            )
            send_approval_email(
                user, service_item, entry, status,
                comment=comment, approver_role='Finance Controller',
            )
        elif type == 'ready_for_invoice':
            from app.utils.email_helper import send_ready_for_invoice_email
            send_ready_for_invoice_email(user, entry, service_item, dashboard_url)
        elif type == 'invoiced' and service_item:
            from app.utils.email_helper import send_invoice_ready_email
            company = Company.query.get(service_item.company_id)
            email_sent = send_invoice_ready_email(
                user,
                service_item,
                service_item.invoice_number,
                company.name if company else 'N/A',
                service_item.total_amount,
            )
            if not email_sent:
                current_app.logger.error(
                    'Invoice email delivery failed for user %s and invoice %s',
                    user.email,
                    service_item.invoice_number,
                )

        notification = Notification(
            user_id=user.id,
            time_entry_id=entry.id,
            notification_type=type,
            subject=f'Service Update - {type.replace("_", " ").title()}',
            message=f'Your service has been {type.replace("_", " ")}',
        )
        db.session.add(notification)
        db.session.commit()
        current_app.logger.info(
            'Notification sent to %s for type: %s', user.email, type
        )
    except Exception:
        current_app.logger.exception(
            'Failed to send notification to %s for type %s',
            user.email,
            type,
        )
        traceback.print_exc()
