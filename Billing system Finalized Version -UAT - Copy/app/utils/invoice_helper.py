# app/utils/invoice_helper.py
from datetime import date
from calendar import monthrange
from app.models import MonthlyMasterInvoice
from app.extensions import db

def get_or_create_monthly_invoice(year, month, user_id):
    """Return existing MonthlyMasterInvoice or create a new one with a persistent number."""
    existing = MonthlyMasterInvoice.query.filter_by(year=year, month=month).first()
    if existing:
        return existing, False

    # Generate a persistent invoice number (e.g., M-2026-04-001)
    last = MonthlyMasterInvoice.query.order_by(MonthlyMasterInvoice.id.desc()).first()
    if last and last.invoice_number.startswith('M-'):
        try:
            seq = int(last.invoice_number.split('-')[-1]) + 1
        except:
            seq = 1
    else:
        seq = 1
    inv_number = f"M-{year}-{month:02d}-{seq:03d}"

    new_inv = MonthlyMasterInvoice(
        year=year,
        month=month,
        invoice_number=inv_number,
        generated_by=user_id,
        period_start=date(year, month, 1),
        period_end=date(year, month, monthrange(year, month)[1])
    )
    db.session.add(new_inv)
    db.session.commit()
    return new_inv, True