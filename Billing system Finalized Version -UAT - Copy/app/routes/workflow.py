from flask import Blueprint, render_template, redirect, url_for, flash, request
from flask_login import login_required, current_user
from app.models import TimeEntry, Invoice, InvoiceLineItem
from app.extensions import db
from datetime import datetime, timedelta
from app.utils.calculations import generate_invoice_number

workflow_bp = Blueprint('workflow', __name__, url_prefix='/workflow')

@workflow_bp.route('/create-invoice-from-time-entries')
@login_required
def create_invoice_from_time_entries():
    """Step 1: Select time entries to invoice"""
    # Get unbilled time entries
    unbilled_entries = TimeEntry.query.filter(
        TimeEntry.user_id == current_user.id,
        TimeEntry.invoice_id == None,
        TimeEntry.is_billable == True
    ).all()
    
    # Group by project
    projects_with_entries = {}
    for entry in unbilled_entries:
        if entry.project_id not in projects_with_entries:
            projects_with_entries[entry.project_id] = {
                'project': entry.project,
                'entries': []
            }
        projects_with_entries[entry.project_id]['entries'].append(entry)
    
    return render_template('workflow/select_time_entries.html', 
                         projects_with_entries=projects_with_entries)


@workflow_bp.route('/generate-invoice', methods=['POST'])
@login_required
def generate_invoice():
    """Step 2: Generate invoice from selected time entries"""
    entry_ids = request.form.getlist('entry_ids')
    
    if not entry_ids:
        flash('Please select at least one time entry', 'danger')
        return redirect(url_for('workflow.create_invoice_from_time_entries'))
    
    entries = TimeEntry.query.filter(TimeEntry.id.in_(entry_ids)).all()
    
    if not entries:
        flash('No valid entries selected', 'danger')
        return redirect(url_for('workflow.create_invoice_from_time_entries'))
    
    # Get project and client (assuming all entries are from same project)
    project = entries[0].project
    client = project.client
    
    # Calculate totals
    subtotal = sum(float(e.amount) for e in entries)
    tax_amount = subtotal * 0.10  # 10% tax
    total = subtotal + tax_amount
    
    # Create invoice
    invoice = Invoice(
        invoice_number=generate_invoice_number(),
        client_id=client.id,
        project_id=project.id,
        user_id=current_user.id,
        invoice_date=datetime.now().date(),
        due_date=datetime.now().date() + timedelta(days=30),
        subtotal=subtotal,
        tax_rate=10,
        tax_amount=tax_amount,
        total_amount=total,
        status='draft'
    )
    db.session.add(invoice)
    db.session.flush()
    
    # Create line items for each time entry
    for entry in entries:
        line_item = InvoiceLineItem(
            invoice_id=invoice.id,
            description=f"{entry.description} - {entry.date.strftime('%Y-%m-%d')}",
            quantity=float(entry.hours),
            unit_price=float(project.hourly_rate),
            total=float(entry.amount),
            time_entry_id=entry.id
        )
        db.session.add(line_item)
        
        # Link time entry to invoice
        entry.invoice_id = invoice.id
    
    db.session.commit()
    
    flash(f'Invoice {invoice.invoice_number} created successfully from {len(entries)} time entries!', 'success')
    return redirect(url_for('invoices.view', invoice_id=invoice.id))