from flask import Blueprint, render_template, redirect, url_for, flash, request, jsonify
from flask_login import login_required, current_user
from app.models import TimeEntry, ServiceItem, Company, User, Unit, ResourceType
from app.extensions import db
from app.utils.auth_decorators import role_required
from app.services import company_service, report_service, resource_service
from datetime import datetime
import traceback
from sqlalchemy import extract 
import calendar 
payment_bp = Blueprint('payment', __name__)


# ============ DASHBOARD - SUPER ADMIN ONLY ============

@payment_bp.route('/dashboard')
@login_required
@role_required('super_admin')
def dashboard():
    """Payment Team Dashboard - Super Admin Only"""
    
    return render_legacy_dashboard()


@payment_bp.route('/invoices')
@login_required
@role_required('super_admin')
def invoices():
    """View generated invoices - Super Admin Only"""
    
    return render_legacy_invoices()


@payment_bp.route('/report-generator')
@login_required
@role_required('super_admin')
def report_generator():
    """Generate reports - Super Admin Only"""
    
    return render_legacy_report_generator()


# ============ ANALYTICS DASHBOARD - SUPER ADMIN ONLY ============

@payment_bp.route('/analytics')
@login_required
@role_required('super_admin')
def analytics_dashboard():
    """Analytics Dashboard - Super Admin Only"""
    
    companies = Company.query.filter_by(is_active=True).order_by(Company.name).all()
    return render_template('payment/analytics_dashboard.html', companies=companies)


@payment_bp.route('/analytics/user-search')
@login_required
@role_required('super_admin')
def analytics_user_search():
    """User Search page - Super Admin Only"""
    
    return render_template('payment/analytics_user_search.html')


@payment_bp.route('/analytics/unit-analysis')
@login_required
@role_required('super_admin')
def analytics_unit_analysis():
    """Unit Analysis page - Super Admin Only"""
    
    units = Unit.query.filter_by(is_active=True).order_by(Unit.name).all()
    return render_template('payment/analytics_unit_analysis.html', units=units)


# ============ PAYMENT COMPANY USER (Non-LOLC Companies) ============

@payment_bp.route('/company-dashboard')
@login_required
@role_required('payment_company_user')
def company_dashboard():
    """Dashboard for payment_company_user"""
    if not current_user.company_id:
        flash('No company assigned.', 'danger')
        return redirect(url_for('dashboard.index'))
    
    company = Company.query.get_or_404(current_user.company_id)
    company_ids = (
        None
        if company_service.is_parent_company_user(current_user)
        else [current_user.company_id]
    )
    return render_company_dashboard(
        company,
        company_ids=company_ids,
    )


@payment_bp.route('/company-reports')
@login_required
@role_required('payment_company_user')
def company_reports():
    """Report generator for payment_company_user"""
    if not current_user.company_id:
        flash('No company assigned.', 'danger')
        return redirect(url_for('dashboard.index'))
    
    company = Company.query.get_or_404(current_user.company_id)
    return render_template(
        'payment/company_reports.html',
        company=company,
        is_parent_payment_user=company_service.is_parent_company_user(current_user),
    )


@payment_bp.route('/company-invoice/<int:company_id>')
@login_required
@role_required('payment_company_user')
def company_invoice(company_id):
    """Company Payment User - View Invoice for their company"""
    if not company_service.can_access_company(current_user, company_id):
        flash('You can only view invoices for your own company.', 'danger')
        return redirect(url_for('payment.company_dashboard'))
    
    company = Company.query.get_or_404(company_id)
    
    month = request.args.get('month', datetime.now().month, type=int)
    year = request.args.get('year', datetime.now().year, type=int)
    
    # Get all resource types for lookup
    all_resource_types = resource_service.list_resource_types()
    resource_by_id = {str(rt.id): rt for rt in all_resource_types}
    
    # Get all service items for this company
    from sqlalchemy import extract
    services = db.session.query(
        ServiceItem.id.label('service_id'),
        ServiceItem.service_description.label('description'),
        ServiceItem.chargeback_total.label('loit_total_from_db'),
        ServiceItem.showback_total.label('lolc_showback_from_db'),
        ServiceItem.total_amount,
        ServiceItem.resources,
        TimeEntry.service_date,
        TimeEntry.submission_date,
        User.first_name,
        User.last_name,
        User.employee_id,
        Unit.name.label('unit_name')
    ).select_from(ServiceItem).join(
        TimeEntry, ServiceItem.time_entry_id == TimeEntry.id
    ).join(
        User, TimeEntry.user_id == User.id
    ).outerjoin(
        Unit, User.unit_id == Unit.id
    ).filter(
        ServiceItem.company_id == company_id,
        ServiceItem.finance_approval_status == 'approved',
        extract('month', TimeEntry.service_date) == month,
        extract('year', TimeEntry.service_date) == year
    ).order_by(TimeEntry.service_date.desc()).all()
    
    # Calculate totals properly from resource types
    total_loit = 0
    total_lolc_showback = 0
    grand_total = 0
    
    processed_services = []
    for s in services:
        loit_cb = 0
        loit_sb = 0
        lolc_sb = 0
        
        for resource in s.resources or []:
            resource_type_id = str(resource.get('type', ''))
            rt = resource_by_id.get(resource_type_id)
            
            if rt:
                quantity = float(resource.get('quantity') or resource.get('hours') or 0)
                loit_cb += quantity * float(rt.loit_chargeback or 0)
                loit_sb += quantity * float(rt.loit_showback or 0)
                lolc_sb += quantity * float(rt.showback_rate or 0)
        
        loit_total = loit_cb + loit_sb
        if loit_total == 0 and s.loit_total_from_db:
            loit_total = float(s.loit_total_from_db)
        
        if lolc_sb == 0 and s.lolc_showback_from_db:
            lolc_sb = float(s.lolc_showback_from_db)
        
        service_total = loit_total + lolc_sb
        
        total_loit += loit_total
        total_lolc_showback += lolc_sb
        grand_total += service_total
        
        processed_services.append({
            'service_id': s.service_id,
            'description': s.description,
            'service_date': s.service_date,
            'submission_date': s.submission_date,
            'bu_user_name': f"{s.first_name or ''} {s.last_name or ''}".strip() or 'N/A',
            'employee_id': s.employee_id or 'N/A',
            'unit_name': s.unit_name or 'N/A',
            'loit_total': loit_total,
            'lolc_showback': lolc_sb,
            'total': service_total
        })
    
    import calendar
    month_name = calendar.month_name[month]
    period = f"{month_name} {year}"
    
    return render_template('payment/company_invoice_view.html',
                         company=company,
                         services=processed_services,
                         total_loit=total_loit,
                         total_lolc_showback=total_lolc_showback,
                         grand_total=grand_total,
                         selected_month=month,
                         selected_year=year,
                         period=period)


@payment_bp.route('/company-invoice/<int:company_id>/export-pdf')
@login_required
@role_required('payment_company_user')
def export_company_invoice_pdf(company_id):
    """Export Company Invoice as PDF - Grouped by Unit with professional layout"""
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer, PageBreak, Image
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import inch
    from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
    import io
    import calendar
    import os
    from flask import send_file, current_app
    from collections import defaultdict
    
    if not company_service.can_access_company(current_user, company_id):
        flash('Access denied.', 'danger')
        return redirect(url_for('payment.company_dashboard'))
    
    company = Company.query.get_or_404(company_id)
    month = request.args.get('month', datetime.now().month, type=int)
    year = request.args.get('year', datetime.now().year, type=int)
    month_name = calendar.month_name[month]
    period = f"{month_name} {year}"
    current_date = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    
    # Get all resource types for lookup
    all_resource_types = resource_service.list_resource_types()
    resource_by_id = {str(rt.id): rt for rt in all_resource_types}
    
    # Get services
    services = db.session.query(
        ServiceItem.id,
        ServiceItem.service_description,
        ServiceItem.chargeback_total,
        ServiceItem.showback_total,
        ServiceItem.resources,
        TimeEntry.service_date,
        Unit.name.label('unit_name'),
        Unit.id.label('unit_id')
    ).select_from(ServiceItem).join(
        TimeEntry, ServiceItem.time_entry_id == TimeEntry.id
    ).outerjoin(
        Unit, TimeEntry.unit_id == Unit.id
    ).filter(
        ServiceItem.company_id == company_id,
        ServiceItem.finance_approval_status == 'approved',
        extract('month', TimeEntry.service_date) == month,
        extract('year', TimeEntry.service_date) == year
    ).order_by(Unit.name, TimeEntry.service_date).all()
    
    # Group services by unit
    units_data = defaultdict(lambda: {
        'name': '',
        'services': [],
        'total_loit': 0,
        'total_lolc_showback': 0,
        'total_grand': 0
    })
    
    for s in services:
        unit_name = s.unit_name or 'Unassigned'
        unit_id = s.unit_id or 0
        
        loit_cb, loit_sb, lolc_sb = 0, 0, 0
        resource_details = []
        
        for resource in s.resources or []:
            resource_type_id = str(resource.get('type', ''))
            rt = resource_by_id.get(resource_type_id)
            quantity = float(resource.get('quantity') or resource.get('hours') or 0)
            
            if rt:
                loit_cb_rate = float(rt.loit_chargeback or 0)
                loit_sb_rate = float(rt.loit_showback or 0)
                lolc_sb_rate = float(rt.showback_rate or 0)
                
                loit_cb += quantity * loit_cb_rate
                loit_sb += quantity * loit_sb_rate
                lolc_sb += quantity * lolc_sb_rate
                
                resource_details.append({
                    'name': rt.name,
                    'quantity': quantity,
                    'uom': rt.unit_of_measure or 'units'
                })
        
        loit_total = loit_cb + loit_sb
        if loit_total == 0 and s.chargeback_total:
            loit_total = float(s.chargeback_total)
        
        if lolc_sb == 0 and s.showback_total:
            lolc_sb = float(s.showback_total)
        
        service_total = loit_total + lolc_sb
        
        if units_data[unit_id]['name'] == '':
            units_data[unit_id]['name'] = unit_name
        
        units_data[unit_id]['services'].append({
            'service_date': s.service_date,
            'description': s.service_description,
            'resource_details': resource_details,
            'loit_total': loit_total,
            'lolc_showback': lolc_sb,
            'total': service_total
        })
        
        units_data[unit_id]['total_loit'] += loit_total
        units_data[unit_id]['total_lolc_showback'] += lolc_sb
        units_data[unit_id]['total_grand'] += service_total
    
    company_total_loit = sum(u['total_loit'] for u in units_data.values())
    company_total_lolc = sum(u['total_lolc_showback'] for u in units_data.values())
    company_total_grand = sum(u['total_grand'] for u in units_data.values())
    
    # Document Setup (Landscape A4 Width is ~11.69 inches. Margins 0.5" left/right leaves ~10.69")
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer, 
        pagesize=landscape(A4), 
        leftMargin=36, 
        rightMargin=36, 
        topMargin=36, 
        bottomMargin=36
    )
    elements = []
    styles = getSampleStyleSheet()
    
    # Executive Palette Configuration
    PRIMARY_COLOR = colors.HexColor('#0F172A')   # Slate Navy
    SECONDARY_COLOR = colors.HexColor('#475569') # Slate Muted
    BG_LIGHT = colors.HexColor('#F8FAFC')        # Off-white row accent
    BORDER_COLOR = colors.HexColor('#E2E8F0')    # Thin clean borders
    
    # Custom Styled Hierarchy
    wrap_style = ParagraphStyle('WrapStyle', parent=styles['Normal'], fontSize=9, leading=13, alignment=TA_LEFT, textColor=colors.HexColor('#334155'))
    header_style = ParagraphStyle('HeaderStyle', parent=styles['Normal'], fontSize=10, leading=14, alignment=TA_LEFT, textColor=colors.white, fontName='Helvetica-Bold')
    header_style_right = ParagraphStyle('HeaderStyleRight', parent=header_style, alignment=TA_RIGHT)
    amount_style = ParagraphStyle('AmountStyle', parent=styles['Normal'], fontSize=9, leading=13, alignment=TA_RIGHT, textColor=colors.HexColor('#0F172A'))
    total_style_right = ParagraphStyle('TotalStyleRight', parent=styles['Normal'], fontSize=10, leading=14, alignment=TA_RIGHT, fontName='Helvetica-Bold', textColor=PRIMARY_COLOR)
    total_style_left = ParagraphStyle('TotalStyleLeft', parent=styles['Normal'], fontSize=10, leading=14, alignment=TA_LEFT, fontName='Helvetica-Bold', textColor=PRIMARY_COLOR)
    
    right_info_style = ParagraphStyle('RightInfo', parent=styles['Normal'], fontSize=10, leading=15, alignment=TA_RIGHT, textColor=SECONDARY_COLOR)
    title_style = ParagraphStyle('CustomTitle', parent=styles['Title'], fontName='Helvetica-Bold', fontSize=24, alignment=TA_LEFT, textColor=PRIMARY_COLOR)
    unit_title_style = ParagraphStyle('UnitTitle', parent=styles['Normal'], fontSize=14, leading=18, alignment=TA_LEFT, textColor=PRIMARY_COLOR, fontName='Helvetica-Bold', spaceAfter=8, spaceBefore=4)

    # ============ PAGE 1: PROFESSIONAL COVER & SUMMARY ============
    
    # Top Row: Invoice Title & Company Logo Block
    logo_path = os.path.join(current_app.root_path, 'static', 'images', 'LOIT_LOGO.jpg')
    try:
        if os.path.exists(logo_path):
            logo = Image(logo_path, width=1.8*inch, height=0.7*inch)
            logo.hAlign = 'RIGHT'
        else:
            logo = Paragraph("", wrap_style)
    except:
        logo = Paragraph("", wrap_style)
        
    title_p = Paragraph("INVOICE ", title_style)
    
    # Spans entire 10.2 inch width layout cleanly
    top_meta_table = Table([[title_p, logo]], colWidths=[6.2*inch, 4.0*inch])
    top_meta_table.setStyle(TableStyle([
        ('VALIGN', (0,0), (-1,-1), 'MIDDLE'),
        ('LEFTPADDING', (0,0), (-1,-1), 0),
        ('RIGHTPADDING', (0,0), (-1,-1), 0),
    ]))
    elements.append(top_meta_table)
    elements.append(Spacer(1, 0.2*inch))
    
    # Metadata Info Block Grid
    meta_left = f"""
    <font size="11" color="{PRIMARY_COLOR.hexval()}"><b>PREPARED FOR:</b></font><br/>
    <font size="14" color="#000000"><b>{company.name}</b></font><br/>
   
    """
    meta_right = f"""
    <b>Invoice Period:</b> {period}<br/>
    <b>Date Generated:</b> {current_date}<br/>
    <b>Status:</b> Approved
    """
    
    info_table = Table([
        [Paragraph(meta_left, wrap_style), Paragraph(meta_right, right_info_style)]
    ], colWidths=[5.1*inch, 5.1*inch])
    info_table.setStyle(TableStyle([
        ('VALIGN', (0,0), (-1,-1), 'TOP'),
        ('LEFTPADDING', (0,0), (-1,-1), 0),
        ('RIGHTPADDING', (0,0), (-1,-1), 0),
    ]))
    elements.append(info_table)
    elements.append(Spacer(1, 0.5*inch))
    
    # Centralized Executive Financial Summary Cards
    summary_headers = [
        Paragraph('Financial Account Item', header_style), 
        Paragraph('Total Outstanding Balance', header_style_right)
    ]
    summary_data = [
        summary_headers,
        [Paragraph('LOIT Total Chargeback', wrap_style), Paragraph(f"{company_total_loit:,.2f}", amount_style)],
        [Paragraph(f"{company.showback_label}", wrap_style), Paragraph(f"{company_total_lolc:,.2f}", amount_style)],
        [Paragraph('GRAND TOTAL ', total_style_left), Paragraph(f"{company_total_grand:,.2f}", total_style_right)]
    ]
    
    summary_table = Table(summary_data, colWidths=[6.2*inch, 4.0*inch])
    summary_table.setStyle(TableStyle([
        ('BACKGROUND', (0,0), (-1,0), PRIMARY_COLOR),
        ('LINEBELOW', (0,0), (-1,0), 2, PRIMARY_COLOR),
        ('VALIGN', (0,0), (-1,-1), 'MIDDLE'),
        ('ROWBACKGROUNDS', (0,1), (-1,-2), [colors.white, BG_LIGHT]),
        ('LINEBELOW', (0,-1), (-1,-1), 2, PRIMARY_COLOR),
        ('BACKGROUND', (0,-1), (-1,-1), BG_LIGHT),
        ('TOPPADDING', (0,0), (-1,-1), 12),
        ('BOTTOMPADDING', (0,0), (-1,-1), 12),
        ('LEFTPADDING', (0,0), (-1,-1), 14),
        ('RIGHTPADDING', (0,0), (-1,-1), 14),
        ('LINEBELOW', (0,1), (-1,-3), 0.5, BORDER_COLOR),
    ]))
    
    elements.append(summary_table)
    elements.append(PageBreak())
    
    # ============ BREAKDOWN PAGES BY UNIT ============
    # Full 10.2 inch column layout adaptation 
    col_widths = [1.2*inch, 4.5*inch, 1.5*inch, 1.5*inch, 1.5*inch]
    unit_count = 0
    
    for unit_id, unit_data in units_data.items():
        if not unit_data['services']:
            continue
        
        if unit_count > 0:
            elements.append(PageBreak())
        
        elements.append(Paragraph(f"{unit_data['name']}".upper(), unit_title_style))
        
        # Wrapped string variables cleanly in ReportLab paragraph blocks 
        table_headers = [
            Paragraph('Service Date', header_style),
            Paragraph('Description / Allocation Resources', header_style),
            Paragraph('LOIT Total', header_style_right),
            Paragraph(f"{company.showback_label}", header_style_right),
            Paragraph('Total', header_style_right)
        ]
        table_data = [table_headers]
        
        for service in unit_data['services']:
            date_str = service['service_date'].strftime('%Y-%m-%d') if service['service_date'] else 'N/A'
            desc_text = service['description'] or 'N/A'
            
            resources_text = ''
            if service['resource_details']:
                resource_lines = []
                for r in service['resource_details']:
                    resource_lines.append(f"&bull; {r['name']}: {r['quantity']} {r['uom']}")
                resources_text = '<br/>'.join(resource_lines)
            
            full_description = f"<b>{desc_text}</b>"
            if resources_text:
                full_description += f"<br/><font color='#64748B' size='8'><i>Resources:</i><br/>{resources_text}</font>"
            
            table_data.append([
                Paragraph(date_str, wrap_style),
                Paragraph(full_description, wrap_style),
                Paragraph(f"{service['loit_total']:,.2f}", amount_style),
                Paragraph(f"{service['lolc_showback']:,.2f}", amount_style),
                Paragraph(f"{service['total']:,.2f}", amount_style)
            ])
        
        # Unit Summary Subtotal Row
        table_data.append([
            Paragraph("UNIT TOTAL", total_style_left),
            '',
            Paragraph(f"{unit_data['total_loit']:,.2f}", total_style_right),
            Paragraph(f"{unit_data['total_lolc_showback']:,.2f}", total_style_right),
            Paragraph(f"{unit_data['total_grand']:,.2f}", total_style_right)
        ])
        
        table = Table(table_data, colWidths=col_widths, repeatRows=1)
        table.setStyle(TableStyle([
            ('BACKGROUND', (0,0), (-1,0), PRIMARY_COLOR),
            ('VALIGN', (0,0), (-1,-1), 'TOP'),
            ('VALIGN', (0,-1), (-1,-1), 'MIDDLE'),
            ('ROWBACKGROUNDS', (0,1), (-1,-2), [colors.white, BG_LIGHT]),
            ('BACKGROUND', (0,-1), (-1,-1), BG_LIGHT),
            ('SPAN', (0,-1), (1,-1)),
            ('LINEBELOW', (0,-1), (-1,-1), 1.5, PRIMARY_COLOR),
            ('TOPPADDING', (0,0), (-1,-1), 8),
            ('BOTTOMPADDING', (0,0), (-1,-1), 8),
            ('LEFTPADDING', (0,0), (-1,-1), 8),
            ('RIGHTPADDING', (0,0), (-1,-1), 8),
            ('LINEBELOW', (0,1), (-1,-2), 0.5, BORDER_COLOR),
        ]))
        elements.append(table)
        unit_count += 1
    
    # ============ FINAL CONSOLIDATED SUMMARY PAGE ============
    if len(units_data) > 1:
        elements.append(PageBreak())
        elements.append(Paragraph("CONSOLIDATED UNIT SUMMARY", unit_title_style))
        elements.append(Spacer(1, 0.1*inch))
        
        summary_col_widths = [3.2*inch, 1.5*inch, 1.8*inch, 1.8*inch, 1.9*inch]
        summary_headers = [
            Paragraph('Business Operational Unit', header_style), 
            Paragraph('Services Count', header_style_right), 
            Paragraph('LOIT Total', header_style_right), 
            Paragraph(f"{company.showback_label}", header_style_right), 
            Paragraph('Grand Total', header_style_right)
        ]
        summary_data = [summary_headers]
        
        for unit_id, unit_data in units_data.items():
            summary_data.append([
                Paragraph(unit_data['name'], wrap_style),
                Paragraph(str(len(unit_data['services'])), amount_style),
                Paragraph(f"{unit_data['total_loit']:,.2f}", amount_style),
                Paragraph(f"{unit_data['total_lolc_showback']:,.2f}", amount_style),
                Paragraph(f"{unit_data['total_grand']:,.2f}", amount_style)
            ])
        
        summary_data.append([
            Paragraph("TOTAL CONSOLIDATED DUE", total_style_left),
            Paragraph(f"{sum(len(u['services']) for u in units_data.values())}", total_style_right),
            Paragraph(f"{company_total_loit:,.2f}", total_style_right),
            Paragraph(f"{company_total_lolc:,.2f}", total_style_right),
            Paragraph(f"{company_total_grand:,.2f}", total_style_right)
        ])
        
        summary_table = Table(summary_data, colWidths=summary_col_widths, repeatRows=1)
        summary_table.setStyle(TableStyle([
            ('BACKGROUND', (0,0), (-1,0), PRIMARY_COLOR),
            ('VALIGN', (0,0), (-1,-1), 'MIDDLE'),
            ('ROWBACKGROUNDS', (0,1), (-1,-2), [colors.white, BG_LIGHT]),
            ('BACKGROUND', (0,-1), (-1,-1), BG_LIGHT),
            ('LINEBELOW', (0,-1), (-1,-1), 2, PRIMARY_COLOR),
            ('TOPPADDING', (0,0), (-1,-1), 10),
            ('BOTTOMPADDING', (0,0), (-1,-1), 10),
            ('LEFTPADDING', (0,0), (-1,-1), 10),
            ('RIGHTPADDING', (0,0), (-1,-1), 10),
            ('LINEBELOW', (0,1), (-1,-2), 0.5, BORDER_COLOR),
        ]))
        elements.append(summary_table)
    
    # Persistent Corporate Disclaimer Footer
    elements.append(Spacer(1, 0.4*inch))
    footer_style = ParagraphStyle('FooterStyle', parent=styles['Normal'], fontSize=8, alignment=TA_CENTER, textColor=colors.HexColor('#94A3B8'))
    elements.append(Paragraph("This document serves as an authorized, system-generated financial statement. For adjustments or transaction queries, please contact corporate finance.", footer_style))
    
    doc.build(elements)
    buffer.seek(0)
    
    return send_file(
        buffer,
        mimetype='application/pdf',
        as_attachment=True,
        download_name=f"Invoice_{company.name}_{year}_{month}.pdf"
    )


# ============ RENDER FUNCTIONS ============

def render_legacy_dashboard():
    """Original dashboard for Super Admin - shows ALL companies"""
    service_items = ServiceItem.query.filter(
        ServiceItem.finance_approval_status == 'approved'
    ).order_by(ServiceItem.created_at.desc()).all()
    
    all_resource_types = resource_service.list_resource_types()
    resource_by_id = {str(rt.id): rt for rt in all_resource_types}
    
    companies_dict = {}
    total_services = 0
    
    for item in service_items:
        entry = TimeEntry.query.get(item.time_entry_id)
        if not entry:
            continue
        
        bu_user = User.query.get(entry.user_id)
        company = Company.query.get(item.company_id)
        
        if not company:
            continue
        
        team_name = 'N/A'
        if bu_user and bu_user.unit_id:
            unit = Unit.query.get(bu_user.unit_id)
            if unit:
                team_name = unit.name
        
        processed_resources = []
        for resource in item.resources or []:
            resource_type_id = str(resource.get('type', ''))
            rt = resource_by_id.get(resource_type_id)
            if rt:
                resource_name = rt.name
                uom = rt.unit_of_measure or 'units'
                chargeback_rate = float(rt.chargeback_rate) if rt.chargeback_rate else 0
                showback_rate = float(rt.showback_rate) if rt.showback_rate else 0
            else:
                resource_name = f"Unknown (ID {resource_type_id})"
                uom = 'units'
                chargeback_rate = 0
                showback_rate = 0
            quantity = resource.get('quantity', 0)
            processed_resources.append({
                'name': resource_name,
                'quantity': quantity,
                'uom': uom,
                'chargeback_rate': chargeback_rate,
                'showback_rate': showback_rate,
                'flat_fee': resource.get('flat_fee', 0)
            })
        
        if company.id not in companies_dict:
            companies_dict[company.id] = {
                'id': company.id,
                'name': company.name,
                'services': [],
                'total_amount': 0,
                'invoiced': False,
                'invoice_number': None
            }
        
        service_data = {
            'id': item.id,
            'submission_date': entry.submission_date.strftime('%Y-%m-%d %H:%M') if entry.submission_date else entry.created_at.strftime('%Y-%m-%d %H:%M'),
            'description': item.service_description,
            'total_amount': float(item.total_amount),
            'resources': processed_resources,
            'invoice_generated': item.invoice_generated,
            'invoice_number': item.invoice_number,
            'bu_user': {
                'full_name': bu_user.full_name if bu_user else 'Unknown',
                'employee_id': bu_user.employee_id if bu_user else 'N/A',
                'team_name': team_name
            }
        }
        
        companies_dict[company.id]['services'].append(service_data)
        companies_dict[company.id]['total_amount'] += float(item.total_amount)
        
        if item.invoice_generated:
            companies_dict[company.id]['invoiced'] = True
            companies_dict[company.id]['invoice_number'] = item.invoice_number
        
        total_services += 1
    
    for company in companies_dict.values():
        all_invoiced = all(s.get('invoice_generated', False) for s in company['services'])
        if all_invoiced:
            company['invoiced'] = True
        else:
            company['invoiced'] = False
    
    companies_list = list(companies_dict.values())
    total_value = sum(c['total_amount'] for c in companies_list)
    invoiced_companies = len([c for c in companies_list if c['invoiced']])
    
    all_companies = Company.query.filter_by(is_active=True).all()
    companies_data = [{'id': c.id, 'name': c.name, 'code': c.code} for c in all_companies]
    
    # ========== FIX: Only Super Admin ==========
    payment_team_name = "Super Admin Dashboard"
    
    return render_template('payment/dashboard.html',
                         companies=companies_list,
                         total_services=total_services,
                         total_value=total_value,
                         invoiced_companies=invoiced_companies,
                         all_companies=companies_data,
                         payment_team_name=payment_team_name)


def render_legacy_invoices():
    """Original invoices page for Super Admin"""
    service_items = ServiceItem.query.filter(
        ServiceItem.invoice_generated == True
    ).order_by(ServiceItem.invoice_generated_at.desc()).all()
    
    all_resource_types = resource_service.list_resource_types()
    resource_by_id = {str(rt.id): rt for rt in all_resource_types}
    
    companies_dict = {}
    total_services = 0
    grand_total = 0
    
    for item in service_items:
        company = Company.query.get(item.company_id)
        if not company:
            continue
        
        if company.id not in companies_dict:
            companies_dict[company.id] = {
                'company_id': company.id,
                'company_name': company.name,
                'invoice_number': item.invoice_number,
                'invoice_date': item.invoice_generated_at.strftime('%Y-%m-%d') if item.invoice_generated_at else '',
                'services': [],
                'services_count': 0,
                'total_amount': 0
            }
        
        entry = TimeEntry.query.get(item.time_entry_id)
        bu_user = User.query.get(entry.user_id) if entry else None
        
        team_name = 'N/A'
        if bu_user and bu_user.unit_id:
            unit = Unit.query.get(bu_user.unit_id)
            if unit:
                team_name = unit.name
        
        processed_resources = []
        for resource in item.resources or []:
            resource_type_id = str(resource.get('type', ''))
            rt = resource_by_id.get(resource_type_id)
            if rt:
                resource_name = rt.name
                uom = rt.unit_of_measure or 'units'
                chargeback_rate = float(rt.chargeback_rate) if rt.chargeback_rate else 0
            else:
                resource_name = f"Unknown (ID {resource_type_id})"
                uom = 'units'
                chargeback_rate = 0
            
            quantity = resource.get('quantity', 0)
            processed_resources.append({
                'name': resource_name,
                'quantity': quantity,
                'uom': uom,
                'chargeback_rate': chargeback_rate,
                'flat_fee': resource.get('flat_fee', 0)
            })
        
        companies_dict[company.id]['services'].append({
            'submission_date': entry.submission_date.strftime('%Y-%m-%d %H:%M') if entry and entry.submission_date else entry.created_at.strftime('%Y-%m-%d %H:%M'),
            'bu_user': {
                'full_name': bu_user.full_name if bu_user else 'Unknown',
                'employee_id': bu_user.employee_id if bu_user else 'N/A',
                'team_name': team_name
            },
            'description': item.service_description,
            'resources': processed_resources,
            'amount': float(item.total_amount)
        })
        companies_dict[company.id]['services_count'] += 1
        companies_dict[company.id]['total_amount'] += float(item.total_amount)
        total_services += 1
        grand_total += float(item.total_amount)
    
    invoices_list = list(companies_dict.values())
    
    companies = Company.query.filter_by(is_active=True).all()
    companies_data = []
    for company in companies:
        companies_data.append({
            'id': company.id,
            'name': company.name,
            'code': company.code
        })
    
    return render_template('payment/invoices.html', 
                         invoices=invoices_list,
                         companies=companies_data,
                         total_services=total_services,
                         grand_total=grand_total)


def render_legacy_report_generator():
    """Original report generator for Super Admin"""
    companies = report_service.get_active_companies()
    companies_data = []
    for company in companies:
        companies_data.append({
            'id': company.id,
            'name': company.name,
            'code': company.code
        })
    
    return render_template('payment/report_generator.html', companies=companies_data)


def render_company_dashboard(company, *, company_ids):
    """Company-specific dashboard for payment_company_user"""
    month = request.args.get('month', type=int)
    year = request.args.get('year', type=int)
    dashboard_data = report_service.get_company_dashboard_data(
        company_ids=company_ids,
        month=month,
        year=year,
    )
    return render_template(
        'payment/company_dashboard.html',
        company=company,
        selected_month=month,
        selected_year=year,
        **dashboard_data,
    )