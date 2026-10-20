from flask import render_template, url_for
from flask_mail import Message
from app.extensions import mail
from app.models import Company, Unit, User, ResourceType
from datetime import datetime
from app.models import Company, ServiceItem
import traceback


def send_otp_email(user, otp, company_id=None, unit_id=None): # Removed is_password_reset parameter
    """Send an OTP email without exposing the code in application logs."""
    try:
        from flask import current_app
        if current_app.config.get('MAIL_SUPPRESS_SEND', False):
            current_app.logger.info("OTP email sending is suppressed by configuration.")
            return False
        
        company_name = None
        unit_name = None
        
        if company_id:
            company = Company.query.get(company_id)
            company_name = company.name if company else None
        
        if unit_id:
            unit = Unit.query.get(unit_id)
            unit_name = unit.name if unit else None
        
        login_url = url_for('auth.login', _external=True)
        
        html_content = render_template(
            'emails/otp_email.html',
            user=user,
            otp=otp,
            company_name=company_name,
            unit_name=unit_name,
            login_url=login_url
        )
        
        msg = Message(
            subject='Your OTP for Invoice System',
            recipients=[user.email]
        )
        msg.html = html_content
        
        mail.send(msg)
        current_app.logger.info("OTP email sent.")
        return True
    except Exception:
        from flask import current_app
        current_app.logger.exception("Failed to send OTP email.")
        return False


def send_submission_email(supervisor, entry, service_item):
    """Send email to Supervisor when service is submitted"""
    try:
        bu_user = User.query.get(entry.user_id)
        company = Company.query.get(service_item.company_id)
        
        # Get unit name
        unit_name = 'N/A'
        if bu_user and bu_user.unit_id:
            unit = Unit.query.get(bu_user.unit_id)
            if unit:
                unit_name = unit.name
        
        # Prepare resources HTML with professional styling
        resources_html = ''
        all_resource_types = ResourceType.query.all()
        resource_by_code = {rt.code: rt for rt in all_resource_types}

        for r in service_item.resources or []:
            resource_code = str(r.get('type', ''))
            rt = resource_by_code.get(resource_code)

            resource_name = rt.name if rt else resource_code.replace('_', ' ').title()
            uom = rt.unit_of_measure or 'units' if rt else 'units'
            quantity = r.get('quantity', r.get('hours', 0))
            rate = float(rt.chargeback_rate) if rt and rt.chargeback_rate else float(r.get('rate', 0))

            if quantity > 0 and rate > 0:
                resources_html += f"""
                <div style="background: #F1F5F9; padding: 10px 12px; margin: 8px 0; border-radius: 8px; display: flex; justify-content: space-between; align-items: center;">
                    <div>
                        <strong style="color: #1E293B;">{resource_name}</strong>
                        <span style="color: #64748B; font-size: 12px; margin-left: 8px;">({quantity} {uom})</span>
                    </div>
                    <div>
                        <span style="background: #E2E8F0; padding: 4px 12px; border-radius: 20px; font-size: 12px; font-weight: 600;">
                            Rs {(quantity * rate):,.2f}
                        </span>
                    </div>
                </div>
                """
            elif r.get('flat_fee', 0) > 0:
                resources_html += f"""
                <div style="background: #F1F5F9; padding: 10px 12px; margin: 8px 0; border-radius: 8px; display: flex; justify-content: space-between; align-items: center;">
                    <div>
                        <strong style="color: #1E293B;">{resource_name}</strong>
                        <span style="color: #64748B; font-size: 12px; margin-left: 8px;">(Flat Fee)</span>
                    </div>
                    <div>
                        <span style="background: #E2E8F0; padding: 4px 12px; border-radius: 20px; font-size: 12px; font-weight: 600;">
                            Rs {float(r['flat_fee']):,.2f}
                        </span>
                    </div>
                </div>
                """
        
        review_url = url_for('supervisor.dashboard', _external=True)
        
        html_content = render_template(
            'emails/supervisor_notification.html',
            supervisor=supervisor,
            bu_user=bu_user,
            unit_name=unit_name,
            service=service_item,
            company=company,
            entry=entry,
            resources_html=resources_html,
            review_url=review_url
        )
        
        msg = Message(
            subject=f'New Service Entry Pending Your Approval - {unit_name}',
            recipients=[supervisor.email]
        )
        msg.html = html_content
        mail.send(msg)
        print(f"Submission email sent to supervisor: {supervisor.email}")
        return True
    except Exception as e:
        print(f"Error sending submission email: {e}")
        traceback.print_exc()
        return False


def send_approval_email(user, service_item, entry, status, comment=None, approver_role=None):
    """Send approval/rejection email with professional template"""
    try:
        # Safely get company name
        company_name = 'N/A'
        if service_item is not None:
            company = Company.query.get(service_item.company_id)
            company_name = company.name if company else 'N/A'
        else:
            first_item = ServiceItem.query.filter_by(time_entry_id=entry.id).first()
            if first_item and first_item.company_id:
                company = Company.query.get(first_item.company_id)
                company_name = company.name if company else 'N/A'
            else:
                company_name = 'Multiple companies'

        # Build service data for template
        if service_item is not None:
            service_data = {
                'id': service_item.id,
                'company_name': company_name,
                'description': service_item.service_description,
                'total_amount': float(service_item.total_amount),
            }
        else:
            service_data = {
                'id': entry.id,
                'company_name': company_name,
                'description': entry.description or 'See dashboard for details',
                'total_amount': float(entry.get_total_amount()),
            }

        dashboard_url = url_for('bu_team.dashboard', _external=True)

        html_content = render_template(
            'emails/approval_email.html',
            user=user,
            service=service_data,
            status=status,
            comment=comment,
            approver_role=approver_role or 'Approver',
            dashboard_url=dashboard_url
        )

        subject = f'Service {status.title()} - Invoice System'
        msg = Message(subject=subject, recipients=[user.email])
        msg.html = html_content
        mail.send(msg)
        print(f"Approval email sent to {user.email}")
        return True
    except Exception as e:
        print(f"Error sending approval email: {e}")
        traceback.print_exc()
        return False


def send_finance_notification(finance_controller, entry, service_item):
    """Send notification to Finance Controller when Head of IT approves"""
    try:
        if service_item is None:
            service_item = ServiceItem.query.filter_by(time_entry_id=entry.id).first()
            
        if not service_item:
            print(f"Warning: send_finance_notification called for entry {entry.id} with no service items")
            return False
            
        company = Company.query.get(service_item.company_id)
        bu_user = User.query.get(entry.user_id)
        head_of_it = User.query.get(service_item.head_approved_by)
        
        review_url = url_for('finance_controller.review_service', service_id=service_item.id, _external=True)
        
        html_content = render_template(
            'emails/finance_notification.html',
            finance_controller=finance_controller,
            bu_user=bu_user,
            head_of_it=head_of_it,
            service=service_item,
            company=company,
            review_url=review_url
        )
        
        msg = Message(
            subject=f'Service Ready for Finance Review - {company.name if company else "N/A"}',
            recipients=[finance_controller.email]
        )
        msg.html = html_content
        mail.send(msg)
        print(f"Finance notification sent to {finance_controller.email}")
        return True
    except Exception as e:
        print(f"Error sending finance notification: {e}")
        traceback.print_exc()
        return False


def send_payment_notification(payment_user, entry, service_item):
    """Send notification to Payment Team when Finance approves"""
    try:
        company = Company.query.get(service_item.company_id)
        bu_user = User.query.get(entry.user_id)
        
        dashboard_url = url_for('payment.dashboard', _external=True)
        
        html_content = render_template(
            'emails/payment_notification.html',
            payment_user=payment_user,
            bu_user=bu_user,
            service=service_item,
            company=company,
            dashboard_url=dashboard_url
        )
        
        msg = Message(
            subject=f'Service Ready for Invoicing - {company.name if company else "N/A"}',
            recipients=[payment_user.email]
        )
        msg.html = html_content
        mail.send(msg)
        print(f"Payment notification sent to {payment_user.email}")
        return True
    except Exception as e:
        print(f"Error sending payment notification: {e}")
        traceback.print_exc()
        return False


def send_ready_for_invoice_email(payment_user, entry, service_item, dashboard_url):
    """Send email to Payment Team when service is ready for invoicing"""
    try:
        company = Company.query.get(service_item.company_id)
        bu_user = User.query.get(entry.user_id)
        finance_controller = User.query.get(service_item.finance_approved_by)
        
        unit_name = 'N/A'
        if bu_user and bu_user.unit_id:
            unit = Unit.query.get(bu_user.unit_id)
            if unit:
                unit_name = unit.name
        
        resources_html = ''
        all_resource_types = ResourceType.query.all()
        resource_by_code = {rt.code: rt for rt in all_resource_types}

        for r in service_item.resources or []:
            resource_code = str(r.get('type', ''))
            rt = resource_by_code.get(resource_code)

            resource_name = rt.name if rt else resource_code.replace('_', ' ').title()
            uom = rt.unit_of_measure or 'units' if rt else 'units'
            quantity = r.get('quantity', r.get('hours', 0))
            rate = float(rt.chargeback_rate) if rt and rt.chargeback_rate else float(r.get('rate', 0))

            if quantity > 0 and rate > 0:
                resources_html += f"""
                <div style="background: #F1F5F9; padding: 10px 12px; margin: 8px 0; border-radius: 8px;">
                    <strong>{resource_name}</strong>: {quantity} {uom} @ Rs {rate:,.2f} = Rs {(quantity * rate):,.2f}
                </div>
                """
        
        html_content = render_template(
            'emails/ready_for_invoice_email.html',
            payment_user=payment_user,
            bu_user=bu_user,
            unit_name=unit_name,
            finance_controller=finance_controller,
            service=service_item,
            company=company,
            resources_html=resources_html,
            dashboard_url=dashboard_url
        )
        
        msg = Message(
            subject=f'Service Ready for Invoicing - {company.name if company else "N/A"}',
            recipients=[payment_user.email]
        )
        msg.html = html_content
        mail.send(msg)
        print(f"Ready for invoice email sent to {payment_user.email}")
        return True
    except Exception as e:
        print(f"Error sending ready for invoice email: {e}")
        traceback.print_exc()
        return False


def send_invoice_ready_email(user, service_item, invoice_number, company_name, total_amount):
    """Send invoice ready email with HTML template"""
    try:
        invoice_url = url_for('bu_team.dashboard', _external=True)
        download_url = url_for(
            'api.download_service_invoice_pdf',
            service_item_id=service_item.id,
            _external=True,
        )
        
        html_content = render_template(
            'emails/invoice_ready_email.html',
            user=user,
            invoice_number=invoice_number,
            company_name=company_name,
            total_amount=total_amount,
            generated_date=service_item.invoice_generated_at.strftime('%Y-%m-%d %H:%M'),
            invoice_url=invoice_url,
            download_url=download_url
        )
        
        msg = Message(
            subject=f'Invoice {invoice_number} Ready - Invoice System',
            recipients=[user.email]
        )
        msg.html = html_content
        mail.send(msg)
        print(f"Invoice ready email sent to {user.email}")
        return True
    except Exception as e:
        print(f"Error sending invoice email: {e}")
        traceback.print_exc()
        return False


def send_supervisor_rejection_email(supervisor, entry, service_item, comment):
    """Send email to supervisor when Head of IT rejects a service"""
    try:
        bu_user = User.query.get(entry.user_id)
        company_name = 'N/A'
        if service_item and service_item.company_id:
            company = Company.query.get(service_item.company_id)
            if company:
                company_name = company.name
        
        dashboard_url = url_for('supervisor.dashboard', _external=True)
        
        html_content = render_template(
            'emails/supervisor_rejection_email.html',
            supervisor=supervisor,
            bu_user=bu_user,
            entry=entry,
            service_item=service_item,
            company_name=company_name,
            comment=comment,
            dashboard_url=dashboard_url
        )
        
        msg = Message(
            subject=f'Service Rejected by Head of IT - Action Required',
            recipients=[supervisor.email]
        )
        msg.html = html_content
        mail.send(msg)
        print(f"Supervisor rejection email sent to {supervisor.email}")
        return True
    except Exception as e:
        print(f"Error sending supervisor rejection email: {e}")
        traceback.print_exc()
        return False


def send_head_of_it_notification(head_of_it, batch, cycle):
    """Send email to Head of IT when a new batch is auto-submitted"""
    try:
        company = Company.query.get(batch.company_id)

        # Safely build URL for background tasks (handle missing SERVER_NAME or context)
        dashboard_url = "#"
        try:
            dashboard_url = url_for('head_of_it.dashboard', _external=True)
        except Exception as e:
            print(f"Warning: url_for external failed: {e}")
            dashboard_url = "/head-of-it/dashboard"
        
        html_content = render_template(
            'emails/head_of_it_notification.html',
            head_of_it=head_of_it,
            company=company,
            batch=batch,
            cycle_start=cycle['cycle_start'],
            cycle_end=cycle['cycle_end'],
            dashboard_url=dashboard_url
        )
        
        msg = Message(
            subject=f'New Batch Ready for Review - {company.name}',
            recipients=[head_of_it.email]
        )
        msg.html = html_content
        mail.send(msg)
        return True
    except Exception as e:
        print(f"Error sending Head of IT notification: {e}")
        return False


def send_welcome_email(user):
    try:
        login_url = url_for('auth.login', _external=True)
        html = render_template('emails/welcome_email.html', user=user, login_url=login_url)
        msg = Message(subject='Welcome to Invoice System', recipients=[user.email])
        msg.html = html
        mail.send(msg)
        return True
    except Exception as e:
        print(f"Error sending welcome email: {e}")
        return False