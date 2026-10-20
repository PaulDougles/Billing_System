from flask import Blueprint, render_template, request, send_file
from flask_login import login_required
from app.models import Invoice, Client, TimeEntry
from app.extensions import db
from datetime import datetime, timedelta
from sqlalchemy import func, extract
import io
import csv

reports_bp = Blueprint('reports', __name__, url_prefix='/reports')

@reports_bp.route('/')
@login_required
def index():
    return render_template('reports/index.html')


@reports_bp.route('/financial')
@login_required
def financial():
    start_date = request.args.get('start_date')
    end_date = request.args.get('end_date')
    
    if start_date:
        start_date = datetime.strptime(start_date, '%Y-%m-%d').date()
    else:
        start_date = datetime.now().date().replace(day=1)
    
    if end_date:
        end_date = datetime.strptime(end_date, '%Y-%m-%d').date()
    else:
        end_date = datetime.now().date()
    
    # Revenue by month
    revenue_by_month = db.session.query(
        extract('year', Invoice.payment_date).label('year'),
        extract('month', Invoice.payment_date).label('month'),
        func.sum(Invoice.total_amount).label('total')
    ).filter(
        Invoice.status == 'paid',
        Invoice.payment_date >= start_date,
        Invoice.payment_date <= end_date
    ).group_by('year', 'month').order_by('year', 'month').all()
    
    # Revenue by client
    revenue_by_client = db.session.query(
        Client.name,
        func.sum(Invoice.total_amount).label('total')
    ).join(Invoice).filter(
        Invoice.status == 'paid',
        Invoice.payment_date >= start_date,
        Invoice.payment_date <= end_date
    ).group_by(Client.id).order_by(func.sum(Invoice.total_amount).desc()).all()
    
    # Summary
    total_invoiced = db.session.query(func.sum(Invoice.total_amount)).filter(
        Invoice.invoice_date >= start_date,
        Invoice.invoice_date <= end_date
    ).scalar() or 0
    
    total_paid = db.session.query(func.sum(Invoice.total_amount)).filter(
        Invoice.status == 'paid',
        Invoice.payment_date >= start_date,
        Invoice.payment_date <= end_date
    ).scalar() or 0
    
    collection_rate = (total_paid / total_invoiced * 100) if total_invoiced > 0 else 0
    
    return render_template('reports/financial.html',
                         start_date=start_date,
                         end_date=end_date,
                         revenue_by_month=revenue_by_month,
                         revenue_by_client=revenue_by_client,
                         total_invoiced=float(total_invoiced),
                         total_paid=float(total_paid),
                         collection_rate=collection_rate)


@reports_bp.route('/export-csv')
@login_required
def export_csv():
    report_type = request.args.get('type', 'invoices')
    start_date = request.args.get('start_date')
    end_date = request.args.get('end_date')
    
    if start_date:
        start_date = datetime.strptime(start_date, '%Y-%m-%d').date()
    else:
        start_date = datetime.now().date().replace(day=1)
    
    if end_date:
        end_date = datetime.strptime(end_date, '%Y-%m-%d').date()
    else:
        end_date = datetime.now().date()
    
    output = io.StringIO()
    writer = csv.writer(output)
    
    if report_type == 'invoices':
        writer.writerow(['Invoice #', 'Client', 'Date', 'Due Date', 'Amount', 'Status', 'Payment Date'])
        invoices = Invoice.query.filter(
            Invoice.invoice_date >= start_date,
            Invoice.invoice_date <= end_date
        ).all()
        for inv in invoices:
            writer.writerow([
                inv.invoice_number,
                inv.client.name,
                inv.invoice_date,
                inv.due_date,
                inv.total_amount,
                inv.status,
                inv.payment_date.strftime('%Y-%m-%d') if inv.payment_date else ''
            ])
    elif report_type == 'time_entries':
        writer.writerow(['Date', 'Project', 'User', 'Hours', 'Description', 'Billable'])
        entries = TimeEntry.query.filter(
            TimeEntry.date >= start_date,
            TimeEntry.date <= end_date
        ).all()
        for entry in entries:
            writer.writerow([
                entry.date,
                entry.project.name if entry.project else '',
                entry.user.full_name,
                entry.hours,
                entry.description,
                'Yes' if entry.is_billable else 'No'
            ])
    
    output.seek(0)
    return send_file(
        io.BytesIO(output.getvalue().encode('utf-8')),
        mimetype='text/csv',
        as_attachment=True,
        download_name=f'{report_type}_report_{start_date}_to_{end_date}.csv'
    )