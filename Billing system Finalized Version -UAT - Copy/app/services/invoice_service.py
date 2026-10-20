from datetime import datetime

from app.extensions import db
from app.models import ActivityLog, ServiceItem
from app.utils.pdf_generator import (
    generate_company_invoice_pdf,
    generate_invoice_from_service_item,
)


def generate_service_invoice_number(service_item_id):
    return f"INV-{service_item_id}-{datetime.now().strftime('%Y%m%d%H%M%S')}"


def create_service_item_invoice(service_item, user_id):
    if service_item.invoice_generated:
        raise ValueError('Invoice already generated for this service')
    invoice_number = generate_service_invoice_number(service_item.id)
    service_item.invoice_generated = True
    service_item.invoice_number = invoice_number
    service_item.invoice_generated_by = user_id
    service_item.invoice_generated_at = datetime.utcnow()

    entry = service_item.time_entry
    all_items = ServiceItem.query.filter_by(time_entry_id=entry.id).all()
    if all(item.invoice_generated for item in all_items):
        entry.status = 'invoiced'
        entry.invoice_generated = True

    db.session.add(ActivityLog(
        time_entry_id=entry.id,
        user_id=user_id,
        action='invoice_generated',
        details={'service_item_id': service_item.id, 'invoice_number': invoice_number},
    ))
    return invoice_number


def render_service_item_invoice_pdf(service_item):
    if not service_item.invoice_generated or not service_item.invoice_number:
        raise ValueError('Generate the invoice record before generating its PDF')
    return generate_invoice_from_service_item(
        service_item,
        service_item.time_entry,
        service_item.invoice_number,
    )


def get_or_create_monthly_invoice(year, month, user_id):
    from app.utils.invoice_helper import get_or_create_monthly_invoice as get_or_create
    return get_or_create(year, month, user_id)


def create_company_invoice(services, company, user_id):
    invoice_number = f"INV-{datetime.now().strftime('%Y%m%d%H%M%S')}"
    pdf_bytes = generate_company_invoice_pdf(services, company, invoice_number)
    generated_at = datetime.utcnow()
    for service in services:
        service.invoice_generated = True
        service.invoice_number = invoice_number
        service.invoice_pdf = pdf_bytes
        service.invoice_pdf_filename = f'{invoice_number}.pdf'
        service.invoice_generated_by = user_id
        service.invoice_generated_at = generated_at
    return invoice_number, pdf_bytes


def store_monthly_invoice_pdf(invoice, pdf_bytes, total_amount, filename):
    invoice.invoice_pdf = pdf_bytes
    invoice.pdf_filename = filename
    invoice.total_amount = total_amount
    invoice.updated_at = datetime.utcnow()


def mark_monthly_services_invoiced(services, invoice_number):
    for service in services:
        service.invoice_generated = True
        service.invoice_number = invoice_number


def get_service_invoice(service_item_id):
    return ServiceItem.query.get_or_404(service_item_id)
