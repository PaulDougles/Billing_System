from flask import Blueprint, render_template, request, redirect, url_for, flash, jsonify
from flask_login import login_required, current_user
from app.models import Project, TimeEntry, Invoice, InvoiceLineItem
from app.extensions import db
from datetime import datetime
from app.utils.calculations import generate_invoice_number
import json

time_based_bp = Blueprint('time_based', __name__, url_prefix='/time-based')

@time_based_bp.route('/entry')
@login_required
def entry():
    """Page for entering new time entries"""
    projects = Project.query.filter_by(status='active', billing_type='time_based').all()
    return render_template('time_based/entry.html', projects=projects)


@time_based_bp.route('/create', methods=['POST'])
@login_required
def create():
    """Create a new time entry"""
    project_id = request.form.get('project_id')
    date = datetime.strptime(request.form.get('date'), '%Y-%m-%d').date()
    hours = float(request.form.get('hours'))
    description = request.form.get('description')
    is_billable = 'is_billable' in request.form
    
    if not project_id:
        flash('Please select a project', 'danger')
        return redirect(url_for('time_based.entry'))
    
    if hours <= 0:
        flash('Hours must be greater than 0', 'danger')
        return redirect(url_for('time_based.entry'))
    
    project = Project.query.get_or_404(project_id)
    
    entry = TimeEntry(
        user_id=current_user.id,
        project_id=project_id,
        date=date,
        hours=hours,
        description=description,
        is_billable=is_billable
    )
    
    db.session.add(entry)
    db.session.commit()
    
    flash(f'Time entry logged successfully! {hours} hours at Rs {project.hourly_rate}/hr', 'success')
    return redirect(url_for('time_based.list'))


@time_based_bp.route('/list')
@login_required
def list():
    """Page for viewing all time entries"""
    entries = TimeEntry.query\
        .filter_by(user_id=current_user.id)\
        .order_by(TimeEntry.date.desc())\
        .all()
    
    # Calculate totals
    total_hours = sum(float(e.hours) for e in entries)
    total_amount = sum(float(e.hours) * float(e.project.hourly_rate) for e in entries)
    
    return render_template('time_based/list.html', 
                         entries=entries,
                         total_hours=total_hours,
                         total_amount=total_amount)


@time_based_bp.route('/<int:entry_id>/delete', methods=['POST'])
@login_required
def delete(entry_id):
    """Delete a time entry"""
    entry = TimeEntry.query.get_or_404(entry_id)
    
    if entry.user_id != current_user.id:
        flash('You can only delete your own entries', 'danger')
        return redirect(url_for('time_based.list'))
    
    if entry.invoice_id:
        flash('Cannot delete entry that is already invoiced', 'danger')
        return redirect(url_for('time_based.list'))
    
    db.session.delete(entry)
    db.session.commit()
    
    flash('Time entry deleted successfully', 'success')
    return redirect(url_for('time_based.list'))


@time_based_bp.route('/<int:entry_id>/generate-invoice', methods=['POST'])
@login_required
def generate_invoice(entry_id):
    """Generate invoice from a single time entry"""
    entry = TimeEntry.query.get_or_404(entry_id)
    
    if entry.user_id != current_user.id:
        return jsonify({'success': False, 'error': 'Unauthorized'}), 403
    
    if entry.invoice_id:
        return jsonify({'success': False, 'error': 'Entry already invoiced'}), 400
    
    project = entry.project
    client = project.client
    
    # Calculate totals
    subtotal = float(entry.hours) * float(project.hourly_rate)
    tax_amount = subtotal * 0.10
    total = subtotal + tax_amount
    
    # Create invoice
    invoice = Invoice(
        invoice_number=generate_invoice_number(),
        client_id=client.id,
        project_id=project.id,
        user_id=current_user.id,
        invoice_date=datetime.now().date(),
        due_date=datetime.now().date(),
        subtotal=subtotal,
        tax_rate=10,
        tax_amount=tax_amount,
        total_amount=total,
        status='draft'
    )
    db.session.add(invoice)
    db.session.flush()
    
    # Create line item
    line_item = InvoiceLineItem(
        invoice_id=invoice.id,
        description=f"{entry.description} - {entry.date.strftime('%Y-%m-%d')}",
        quantity=float(entry.hours),
        unit_price=float(project.hourly_rate),
        total=subtotal,
        time_entry_id=entry.id
    )
    db.session.add(line_item)
    
    # Link time entry to invoice
    entry.invoice_id = invoice.id
    
    db.session.commit()
    
    return jsonify({
        'success': True,
        'invoice_id': invoice.id,
        'invoice_number': invoice.invoice_number
    })


@time_based_bp.route('/api/entries/unbilled')
@login_required
def get_unbilled_entries():
    """Get unbilled time entries for current user"""
    entries = TimeEntry.query.filter(
        TimeEntry.user_id == current_user.id,
        TimeEntry.invoice_id == None,
        TimeEntry.is_billable == True
    ).all()
    
    result = [{
        'id': e.id,
        'date': e.date.strftime('%Y-%m-%d'),
        'project_name': e.project.name,
        'project_id': e.project_id,
        'hours': float(e.hours),
        'rate': float(e.project.hourly_rate),
        'amount': float(e.hours) * float(e.project.hourly_rate),
        'description': e.description
    } for e in entries]
    
    return jsonify(result)