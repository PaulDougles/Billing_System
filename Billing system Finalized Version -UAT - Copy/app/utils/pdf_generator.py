from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer, PageBreak
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch
from reportlab.lib.enums import TA_CENTER, TA_RIGHT
from datetime import datetime, timedelta
import os
import io
from flask import current_app

# Get the project root directory
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '../..'))
INVOICES_DIR = os.path.join(PROJECT_ROOT, 'invoices')


def generate_invoice_from_entry(entry, invoice_number=None):
    """Generate invoice PDF from TimeEntry object (legacy) - updated for new JSON structure"""
    from app.models import ResourceType
    
    if invoice_number is None:
        invoice_number = f"INV-{entry.id}-{datetime.now().strftime('%Y%m%d%H%M%S')}"
    
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=A4)
    elements = []
    
    styles = getSampleStyleSheet()
    
    title = Paragraph(f"INVOICE<br/>{invoice_number}", styles['Title'])
    elements.append(title)
    elements.append(Spacer(1, 0.2*inch))
    
    info = Paragraph(f"""
    Date: {datetime.now().strftime('%Y-%m-%d')}<br/>
    Project: {entry.project_name}<br/>
    Amount: Rs {entry.total_amount}
    """, styles['Normal'])
    elements.append(info)
    
    if entry.resources:
        elements.append(Spacer(1, 0.2*inch))
        elements.append(Paragraph("<b>Resource Details:</b>", styles['Normal']))
        
        # Fetch all resource types for lookup
        resource_types = {str(rt.id): rt for rt in ResourceType.query.all()}
        
        table_data = [['Resource Type', 'Quantity', 'Rate (Rs)', 'Amount (Rs)']]
        for resource in entry.resources:
            resource_type_id = str(resource.get('type', ''))
            rt = resource_types.get(resource_type_id)
            if rt:
                resource_name = rt.name
                unit = rt.unit_of_measure or 'units'
                chargeback_rate = float(rt.chargeback_rate) if rt.chargeback_rate else 0
            else:
                resource_name = resource.get('type', 'Unknown')
                unit = 'units'
                chargeback_rate = 0
            
            quantity = resource.get('quantity', 0)
            amount = quantity * chargeback_rate
            table_data.append([f"{resource_name} ({unit})", str(quantity), f"Rs {chargeback_rate:.2f}", f"Rs {amount:.2f}"])
        
        table = Table(table_data)
        table.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, 0), colors.grey),
            ('TEXTCOLOR', (0, 0), (-1, 0), colors.whitesmoke),
            ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
            ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
            ('FONTSIZE', (0, 0), (-1, 0), 10),
            ('BOTTOMPADDING', (0, 0), (-1, 0), 12),
            ('BACKGROUND', (0, 1), (-1, -1), colors.beige),
            ('GRID', (0, 0), (-1, -1), 1, colors.black),
        ]))
        elements.append(table)
    
    doc.build(elements)
    buffer.seek(0)
    
    return buffer.getvalue(), invoice_number


def generate_invoice_from_service_item(service_item, entry, invoice_number):
    """Generate a simple invoice PDF for a service item - updated for new JSON structure"""
    from app.models import Company, User, ResourceType
    
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=A4)
    elements = []
    
    styles = getSampleStyleSheet()
    
    title = Paragraph("INVOICE", styles['Title'])
    elements.append(title)
    elements.append(Spacer(1, 0.2*inch))
    
    company = Company.query.get(service_item.company_id)
    company_name = company.name if company else 'N/A'
    
    bu_user = User.query.get(entry.user_id)
    bu_name = bu_user.full_name if bu_user else 'Unknown'
    
    details = f"""
    <b>Invoice Number:</b> {invoice_number}<br/>
    <b>Date:</b> {datetime.now().strftime('%Y-%m-%d')}<br/>
    <b>Due Date:</b> {(datetime.now() + timedelta(days=30)).strftime('%Y-%m-%d')}<br/>
    <b>Company:</b> {company_name}<br/>
    <b>BU User:</b> {bu_name}<br/>
    <b>Description:</b> {service_item.service_description}<br/>
    <b>Total Amount:</b> Rs {service_item.total_amount}
    """
    
    para = Paragraph(details, styles['Normal'])
    elements.append(para)
    
    if service_item.resources:
        elements.append(Spacer(1, 0.2*inch))
        elements.append(Paragraph("<b>Resource Details:</b>", styles['Normal']))
        
        resource_types = {str(rt.id): rt for rt in ResourceType.query.all()}
        
        table_data = [['Resource Type', 'Quantity', 'Rate (Rs)', 'Amount (Rs)']]
        for resource in service_item.resources:
            resource_type_id = str(resource.get('type', ''))
            rt = resource_types.get(resource_type_id)
            if rt:
                resource_name = rt.name
                unit = rt.unit_of_measure or 'units'
                chargeback_rate = float(rt.chargeback_rate) if rt.chargeback_rate else 0
            else:
                resource_name = resource.get('type', 'Unknown')
                unit = 'units'
                chargeback_rate = 0
            
            quantity = resource.get('quantity', 0)
            amount = quantity * chargeback_rate
            table_data.append([f"{resource_name} ({unit})", str(quantity), f"Rs {chargeback_rate:.2f}", f"Rs {amount:.2f}"])
        
        table = Table(table_data)
        table.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, 0), colors.grey),
            ('TEXTCOLOR', (0, 0), (-1, 0), colors.whitesmoke),
            ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
            ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
            ('FONTSIZE', (0, 0), (-1, 0), 10),
            ('BOTTOMPADDING', (0, 0), (-1, 0), 12),
            ('BACKGROUND', (0, 1), (-1, -1), colors.beige),
            ('GRID', (0, 0), (-1, -1), 1, colors.black),
        ]))
        elements.append(table)
    
    elements.append(Spacer(1, 0.5*inch))
    footer = Paragraph("Thank you for your business!<br/>Payment due within 30 days.", styles['Normal'])
    elements.append(footer)
    
    doc.build(elements)
    buffer.seek(0)
    
    return buffer.getvalue()


def generate_company_invoice_pdf(services, company, invoice_number):
    """Generate a combined invoice PDF for all services of a company - One page per company
       Updated for new JSON structure (type + quantity) and live resource type lookup."""
    from app.models import User, TimeEntry, Unit, ResourceType
    
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=A4)
    elements = []
    
    styles = getSampleStyleSheet()
    
    # Title
    title = Paragraph("INVOICE", styles['Title'])
    elements.append(title)
    elements.append(Spacer(1, 0.2*inch))
    
    # Calculate total amount
    total_amount = sum(float(s.total_amount) for s in services)
    
    # Invoice details
    details = f"""
    <b>Invoice Number:</b> {invoice_number}<br/>
    <b>Date:</b> {datetime.now().strftime('%Y-%m-%d')}<br/>
    <b>Due Date:</b> {(datetime.now() + timedelta(days=30)).strftime('%Y-%m-%d')}<br/>
    <b>Company:</b> {company.name}<br/>
    <br/>
    <b>Total Amount:</b> Rs {total_amount:.2f}
    """
    
    para = Paragraph(details, styles['Normal'])
    elements.append(para)
    elements.append(Spacer(1, 0.2*inch))
    
    # Services table
    elements.append(Paragraph("<b>Service Details by BU Team:</b>", styles['Normal']))
    
    # Table headers
    table_data = [['Date', 'BU User', 'Unit', 'Service Description', 'Amount (Rs)']]
    
    # Pre-fetch all resource types for lookup
    all_resource_types = {str(rt.id): rt for rt in ResourceType.query.all()}
    
    for service in services:
        entry = TimeEntry.query.get(service.time_entry_id)
        bu_user = User.query.get(entry.user_id) if entry else None
        
        # Get unit name
        unit_name = 'N/A'
        if bu_user and bu_user.unit_id:
            unit = Unit.query.get(bu_user.unit_id)
            if unit:
                unit_name = unit.name
        
        # Format resources for display using the new structure
        resource_text = ''
        if service.resources:
            resource_parts = []
            for r in service.resources:
                resource_type_id = str(r.get('type', ''))
                rt = all_resource_types.get(resource_type_id)
                if rt:
                    r_name = rt.name
                    uom = rt.unit_of_measure or 'units'
                    rate = rt.chargeback_rate
                else:
                    r_name = 'Unknown'
                    uom = 'units'
                    rate = 0
                qty = r.get('quantity', 0)
                resource_parts.append(f"{r_name}: {qty} {uom} @ Rs {rate}")
            resource_text = '<br/>'.join(resource_parts)
        elif service.flat_fee and service.flat_fee > 0:
            resource_text = f"Flat Fee: Rs {service.flat_fee}"
        
        description_text = f"{service.service_description}<br/><font size=8 color='gray'>{resource_text}</font>"
        
        submission_date = entry.submission_date.strftime('%Y-%m-%d %H:%M') if entry and entry.submission_date else entry.created_at.strftime('%Y-%m-%d %H:%M')
        
        table_data.append([
            submission_date,
            f"{bu_user.full_name}\n({bu_user.employee_id})" if bu_user else 'Unknown',
            unit_name,
            Paragraph(description_text, styles['Normal']),
            f"Rs {service.total_amount:.2f}"
        ])
    
    # Add total row
    table_data.append(['', '', '', '<b>Total</b>', f"<b>Rs {total_amount:.2f}</b>"])
    
    # Create table with adjusted column widths
    table = Table(table_data, colWidths=[1.2*inch, 1.5*inch, 1*inch, 2.5*inch, 1.2*inch])
    table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.grey),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.whitesmoke),
        ('ALIGN', (0, 0), (-1, 0), 'CENTER'),
        ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
        ('FONTSIZE', (0, 0), (-1, 0), 10),
        ('BOTTOMPADDING', (0, 0), (-1, 0), 12),
        ('BACKGROUND', (0, 1), (-1, -2), colors.beige),
        ('BACKGROUND', (0, -1), (-1, -1), colors.lightgrey),
        ('GRID', (0, 0), (-1, -1), 1, colors.black),
        ('FONTNAME', (0, -1), (-1, -1), 'Helvetica-Bold'),
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
    ]))
    elements.append(table)
    
    # Footer
    elements.append(Spacer(1, 0.5*inch))
    footer = Paragraph("Thank you for your business!<br/>Payment due within 30 days.", styles['Normal'])
    elements.append(footer)
    
    doc.build(elements)
    buffer.seek(0)
    
    return buffer.getvalue()


def generate_master_invoice_pdf(companies, invoice_number, invoice_date, period):
    """Generate a highly polished corporate master invoice PDF for all companies."""
    from app.models import ResourceType
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer, PageBreak, Image
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import inch
    from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
    import io
    import os
    from flask import current_app

    buffer = io.BytesIO()
    
    # Document Setup - Landscape A4 margins optimized to prevent accidental page trailing
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
    
    # Professional Executive Slate Palette 
    PRIMARY_COLOR = colors.HexColor('#0F172A')   # Sleek Slate Navy
    SECONDARY_COLOR = colors.HexColor('#475569') # Professional Slate Grey
    BG_LIGHT = colors.HexColor('#F8FAFC')        # Clean row highlight
    BORDER_COLOR = colors.HexColor('#E2E8F0')    # Subtle grid lines
    
    # Typography & Styles Hierarchy
    wrap_style = ParagraphStyle('WrapStyle', parent=styles['Normal'], fontSize=9, leading=14, alignment=TA_LEFT, textColor=colors.HexColor('#334155'))
    header_style = ParagraphStyle('HeaderStyle', parent=styles['Normal'], fontSize=10, leading=14, alignment=TA_LEFT, textColor=colors.white, fontName='Helvetica-Bold')
    header_style_right = ParagraphStyle('HeaderStyleRight', parent=header_style, alignment=TA_RIGHT)
    header_style_center = ParagraphStyle('HeaderStyleCenter', parent=header_style, alignment=TA_CENTER)
    
    amount_style = ParagraphStyle('AmountStyle', parent=styles['Normal'], fontSize=9, leading=14, alignment=TA_RIGHT, textColor=PRIMARY_COLOR)
    center_style = ParagraphStyle('CenterStyle', parent=styles['Normal'], fontSize=9, leading=14, alignment=TA_CENTER, textColor=colors.HexColor('#334155'))
    
    total_style_right = ParagraphStyle('TotalStyleRight', parent=styles['Normal'], fontSize=10, leading=14, alignment=TA_RIGHT, fontName='Helvetica-Bold', textColor=PRIMARY_COLOR)
    total_style_left = ParagraphStyle('TotalStyleLeft', parent=styles['Normal'], fontSize=10, leading=14, alignment=TA_LEFT, fontName='Helvetica-Bold', textColor=PRIMARY_COLOR)
    
    
    right_info_style = ParagraphStyle('RightInfo', parent=styles['Normal'], fontSize=10, leading=15, alignment=TA_RIGHT, textColor=SECONDARY_COLOR)
    title_style = ParagraphStyle('CustomTitle', parent=styles['Title'], fontName='Helvetica-Bold', fontSize=24, alignment=TA_LEFT, textColor=PRIMARY_COLOR)
    section_title_style = ParagraphStyle('SectionTitle', parent=styles['Normal'], fontSize=14, leading=18, alignment=TA_LEFT, textColor=PRIMARY_COLOR, fontName='Helvetica-Bold', spaceAfter=10, spaceBefore=4)
    footer_notice_style = ParagraphStyle('FooterNotice', parent=styles['Normal'], fontSize=8, alignment=TA_CENTER, textColor=colors.HexColor('#94A3B8'))

    # Pre-fetch resource types if needed for structural model tracking
    all_resource_types = {str(rt.id): rt for rt in ResourceType.query.all()}

    # ============ PAGE 1: COVER HEADER & CORPORATE TOTALS ============
    
    # Header Banner Row
    title_p = Paragraph("MASTER INVOICE", title_style)
    
    # Corporate Logo Loading Safe-Check
    logo_path = os.path.join(current_app.root_path, 'static', 'images', 'lolc-technologies.jpg')
    try:
        if os.path.exists(logo_path):
            logo = Image(logo_path, width=2.5*inch, height=1.6*inch)
            logo.hAlign = 'RIGHT'
        else:
            logo = Paragraph("", wrap_style)
    except:
        logo = Paragraph("", wrap_style)
    
    top_meta_table = Table([[title_p, logo]], colWidths=[6.2*inch, 4.4*inch])
    top_meta_table.setStyle(TableStyle([
        ('VALIGN', (0,0), (-1,-1), 'MIDDLE'),
        ('LEFTPADDING', (0,0), (-1,-1), 0),
        ('RIGHTPADDING', (0,0), (-1,-1), 0),
    ]))
    elements.append(top_meta_table)
    elements.append(Spacer(1, 0.2*inch))
    
    # Statement Identifiers
    meta_left = f"""
    <b>Invoice Number:</b> {invoice_number}<br/>
    <b>Date Issued:</b> {invoice_date}<br/>
    <b>Billing Period:</b> {period}
    """
    meta_right = """
    <font size="11" color="#0F172A"><b>ISSUED BY:</b></font><br/>
    <b>LOLC Technologies Limited</b><br/>
    137 Rajagiriya Rd,<br/>
    Sri Jayawardenepura Kotte 10100
    """
    
    info_table = Table([
        [Paragraph(meta_left, wrap_style), Paragraph(meta_right, right_info_style)]
    ], colWidths=[5.3*inch, 5.3*inch])
    info_table.setStyle(TableStyle([
        ('VALIGN', (0,0), (-1,-1), 'TOP'),
        ('LEFTPADDING', (0,0), (-1,-1), 0),
        ('RIGHTPADDING', (0,0), (-1,-1), 0),
    ]))
    elements.append(info_table)
    elements.append(Spacer(1, 0.5*inch))
    
    # Financial Consolidated Totals Overview Box
    grand_total = sum(c.get('total_amount', 0) for c in companies)
    
    bottom_headers = [Paragraph("Consolidated Account Metric", header_style), Paragraph("Aggregate Summary Value", header_style_right)]
    bottom_data = [
        bottom_headers,
        [Paragraph("TOTAL REGISTERED SUBSIDIARY COMPANIES", wrap_style), Paragraph(str(len(companies)), amount_style)],
        [Paragraph("GRAND CONSOLIDATED TOTAL DUE", total_style_left), Paragraph(f"Rs {grand_total:,.2f}", total_style_right)]
    ]
    
    bottom_table = Table(bottom_data, colWidths=[6.6*inch, 4.0*inch])
    bottom_table.setStyle(TableStyle([
        ('BACKGROUND', (0,0), (-1,0), PRIMARY_COLOR),
        ('VALIGN', (0,0), (-1,-1), 'MIDDLE'),
        ('ROWBACKGROUNDS', (0,1), (-1,-2), [colors.white]),
        ('BACKGROUND', (0,-1), (-1,-1), BG_LIGHT),
        ('LINEBELOW', (0,-1), (-1,-1), 2, PRIMARY_COLOR),
        ('TOPPADDING', (0,0), (-1,-1), 12),
        ('BOTTOMPADDING', (0,0), (-1,-1), 12),
        ('LEFTPADDING', (0,0), (-1,-1), 14),
        ('RIGHTPADDING', (0,0), (-1,-1), 14),
        ('LINEBELOW', (0,1), (-1,-2), 0.5, BORDER_COLOR),
    ]))
    elements.append(bottom_table)
    
    elements.append(Spacer(1, 0.4*inch))
    elements.append(Paragraph("This is an official system-generated invoice compilation requiring no manual signatures.<br/>LOLC Technologies Corporate Finance", footer_notice_style))
    
    # ============ BREAKDOWN PAGES BY INDIVIDUAL COMPANY ============
    col_widths = [1.2*inch, 1.7*inch, 1.4*inch, 4.3*inch, 1.5*inch]
    
    for idx, company in enumerate(companies):
        elements.append(PageBreak())
        
        elements.append(Paragraph(f"{company['name']}".upper(), section_title_style))
        
        meta_summary_text = f"Total Logged Services: <b>{len(company['services'])}</b> &nbsp;|&nbsp; Balance Due: <b>Rs {company['total_amount']:,.2f}</b>"
        elements.append(Paragraph(meta_summary_text, wrap_style))
        elements.append(Spacer(1, 0.15*inch))
        
        table_headers = [
            Paragraph('Service Date', header_style_center),
            Paragraph('BU Requesting User', header_style),
            Paragraph('Operational Unit', header_style),
            Paragraph('Allocation Description &amp; Resource Details', header_style),
            Paragraph('Amount Due', header_style_right)
        ]
        table_data = [table_headers]
        
        for service in company['services']:
            service_date = service.get('submission_date', '')
            if ' ' in service_date:
                service_date = service_date.split(' ')[0]
                
            bu_user_name = service.get('bu_user', 'Unknown')
            team_name = service.get('team_name', 'N/A')
            description = service.get('description', '')
            amount = service.get('amount', 0)
            
            # Formulate structural Resource block formatting matching requested style
            resources_text = ''
            if service.get('resources'):
                resource_parts = []
                for r in service['resources']:
                    resource_parts.append(f"&bull; {r['name']}: {r['quantity']} {r['uom']}")
                resources_text = '<br/>Resources:<br/>' + '<br/>'.join(resource_parts)
            
            description_text = f"<b>{description}</b>"
            if service.get('resources'):
                description_text += f"<font size='8.5' color='#475569'>{resources_text}</font>"
            
            table_data.append([
                Paragraph(service_date, center_style),
                Paragraph(bu_user_name, wrap_style),
                Paragraph(team_name, wrap_style),
                Paragraph(description_text, wrap_style),
                Paragraph(f"Rs {amount:,.2f}", amount_style)
            ])
        
        # Subsidiary Section Summary Footer Line
        table_data.append([
            Paragraph("SUBSIDIARY BALANCE DUE", total_style_left),
            '', '', '',
            Paragraph(f"Rs {company['total_amount']:,.2f}", total_style_right)
        ])
        
        table = Table(table_data, colWidths=col_widths, repeatRows=1)
        table.setStyle(TableStyle([
            ('BACKGROUND', (0,0), (-1,0), PRIMARY_COLOR),
            ('VALIGN', (0,0), (-1,-1), 'TOP'),
            ('VALIGN', (0,-1), (-1,-1), 'MIDDLE'),
            ('ROWBACKGROUNDS', (0,1), (-1,-2), [colors.white, BG_LIGHT]),
            ('BACKGROUND', (0,-1), (-1,-1), BG_LIGHT),
            ('SPAN', (0,-1), (3,-1)),
            ('LINEBELOW', (0,-1), (-1,-1), 1.5, PRIMARY_COLOR),
            ('TOPPADDING', (0,0), (-1,-1), 8),
            ('BOTTOMPADDING', (0,0), (-1,-1), 8),
            ('LEFTPADDING', (0,0), (-1,-1), 8),
            ('RIGHTPADDING', (0,0), (-1,-1), 8),
            ('LINEBELOW', (0,1), (-1,-2), 0.5, BORDER_COLOR),
        ]))
        elements.append(table)
        
    # ============ FINAL CONSOLIDATED SUMMARY MATRIX PAGE ============
    if len(companies) > 1:
        elements.append(PageBreak())
        elements.append(Paragraph("CONSOLIDATED SUBSIDIARY ACCOUNTS MATRIX", section_title_style))
        elements.append(Spacer(1, 0.1*inch))
        
        summary_col_widths = [4.5*inch, 2.1*inch, 3.5*inch]
        summary_headers = [
            Paragraph('Subsidiary Entity Name', header_style), 
            Paragraph('Logged Services', header_style_center), 
            Paragraph('Total Balance Due', header_style_right)
        ]
        summary_data = [summary_headers]
        
        for c in companies:
            summary_data.append([
                Paragraph(c['name'], wrap_style),
                Paragraph(str(len(c['services'])), center_style),
                Paragraph(f"Rs {c['total_amount']:,.2f}", amount_style)
            ])
            
        summary_data.append([
            Paragraph('TOTAL NET CONSOLIDATED LIABILITY', total_style_left),
            Paragraph(str(sum(len(c['services']) for c in companies)), total_style_left),
            Paragraph(f"Rs {grand_total:,.2f}", total_style_right)
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
            ('LEFTPADDING', (0,0), (-1,-1), 12),
            ('RIGHTPADDING', (0,0), (-1,-1), 12),
            ('LINEBELOW', (0,1), (-1,-2), 0.5, BORDER_COLOR),
        ]))
        elements.append(summary_table)
        
        elements.append(Spacer(1, 0.4*inch))
        elements.append(Paragraph("End of compiled statement list. This data maps directly to audited system metrics.", footer_notice_style))
        
    doc.build(elements)
    buffer.seek(0)
    return buffer.getvalue()

