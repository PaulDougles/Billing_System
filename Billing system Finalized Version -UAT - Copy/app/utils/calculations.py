from datetime import datetime

def generate_invoice_number():
    """Generate a unique invoice number"""
    from app.models import Invoice
    from app.extensions import db
    
    year = datetime.now().strftime('%Y')
    month = datetime.now().strftime('%m')
    
    # Get last invoice number for this month
    last_invoice = Invoice.query.filter(
        Invoice.invoice_number.like(f'INV-{year}{month}%')
    ).order_by(Invoice.invoice_number.desc()).first()
    
    if last_invoice:
        last_num = int(last_invoice.invoice_number.split('-')[-1])
        new_num = last_num + 1
    else:
        new_num = 1
    
    return f"INV-{year}{month}-{new_num:04d}"


