from flask import Blueprint, render_template, redirect, url_for, flash, request, jsonify
from flask_login import login_required, current_user
from app.models import TimeEntry, ServiceItem, Company, User, ActivityLog, Unit, ResourceType, UnitMember
from app.extensions import db
from datetime import datetime
import traceback
import io
import pandas as pd
from flask import send_file, make_response
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from app.utils.cycle_helper import CycleHelper

bu_team_bp = Blueprint('bu_team', __name__)


@bu_team_bp.route('/dashboard')
@login_required
def dashboard():
    """BU Team Dashboard - Show service items for user's unit only"""
    if current_user.role != 'bu_user':
        flash('Access denied. BU Team users only.', 'danger')
        return redirect(url_for('dashboard.index'))
    
    # Determine user's unit
    unit_id = current_user.unit_id
    if not unit_id:
        membership = UnitMember.query.filter_by(user_id=current_user.id).first()
        if membership:
            unit_id = membership.unit_id
    
    if not unit_id:
        flash('You are not assigned to any unit. Please contact administrator.', 'danger')
        return redirect(url_for('dashboard.index'))
    
    unit = Unit.query.get(unit_id)
    unit_name = unit.name if unit else 'N/A'
    
    # Get only entries belonging to this unit
    entries = TimeEntry.query.filter_by(user_id=current_user.id, unit_id=unit_id).order_by(TimeEntry.id.desc()).all()
    
    service_items_list = []
    
    for entry in entries:
        items = ServiceItem.query.filter_by(time_entry_id=entry.id).all()
        for item in items:
            # Determine status based on service item's own approval statuses
            if item.supervisor_approval_status == 'pending':
                item_status = 'pending_supervisor'
                status_display = 'Pending Supervisor Approval'
                status_badge = 'warning'
                can_edit = False
            elif item.supervisor_approval_status == 'rejected':
                item_status = 'supervisor_rejected'
                status_display = 'Rejected by Supervisor'
                status_badge = 'danger'
                can_edit = True
            elif item.supervisor_approval_status == 'approved':
                # Check head of IT approval
                if item.head_approval_status == 'pending':
                    item_status = 'with_head_of_it'
                    status_display = 'With Head of IT'
                    status_badge = 'info'
                    can_edit = False
                elif item.head_approval_status == 'approved':
                    # Check finance controller approval
                    if item.finance_approval_status == 'pending':
                        item_status = 'with_finance'
                        status_display = 'With Finance Controller'
                        status_badge = 'info'
                        can_edit = False
                    elif item.finance_approval_status == 'approved':
                        if item.invoice_generated:
                            item_status = 'invoiced'
                            status_display = 'Invoiced'
                            status_badge = 'success'
                            can_edit = False
                        else:
                            item_status = 'finance_approved'
                            status_display = 'Finance Approved'
                            status_badge = 'primary'
                            can_edit = False
                    elif item.finance_approval_status == 'rejected':
                        item_status = 'finance_rejected'
                        status_display = 'Rejected by Finance'
                        status_badge = 'danger'
                        can_edit = False
                elif item.head_approval_status == 'rejected':
                    item_status = 'head_rejected'
                    status_display = 'Rejected by Head of IT'
                    status_badge = 'danger'
                    can_edit = False
            else:
                # Draft or unknown
                item_status = 'draft'
                status_display = 'Draft'
                status_badge = 'secondary'
                can_edit = True
            
            company_name = 'N/A'
            if item.company_id:
                company = Company.query.get(item.company_id)
                if company:
                    company_name = company.name
            
            # Get rejection comment from the appropriate level
            rejection_comment = None
            if item.supervisor_approval_status == 'rejected':
                rejection_comment = item.supervisor_comment
            elif item.head_approval_status == 'rejected':
                rejection_comment = item.head_approval_comment
            elif item.finance_approval_status == 'rejected':
                rejection_comment = item.finance_approval_comment
            
            submission_date = entry.submission_date.strftime('%Y-%m-%d %H:%M') if entry.submission_date else entry.created_at.strftime('%Y-%m-%d %H:%M')
            
            service_items_list.append({
                'service_item_id': item.id,
                'entry_id': entry.id,
                'company_name': company_name,
                'company_id': item.company_id,
                'description': item.service_description,
                'total_amount': float(item.total_amount),
                'status': item_status,
                'status_display': status_display,
                'status_badge': status_badge,
                'can_edit': can_edit,
                'submission_date': submission_date,
                'rejection_comment': rejection_comment,
                'head_approval_status': item.head_approval_status,
                'head_approval_comment': item.head_approval_comment,
                'finance_approval_status': item.finance_approval_status,
                'finance_approval_comment': item.finance_approval_comment,
                'resources': item.resources if item.resources else [],
                'bu_user_name': current_user.full_name,
                'bu_user_id': current_user.employee_id,
                'team_name': unit_name,
                'service_date': entry.service_date,
                'supervisor_status': item.supervisor_approval_status,
                'unit_name': unit_name
            })
    
    service_items_list.sort(key=lambda x: x['submission_date'], reverse=True)
    
    draft_count = len([s for s in service_items_list if s['status'] == 'draft'])
    processing_count = len([s for s in service_items_list if s['status'] in ('pending_supervisor', 'with_head_of_it', 'with_finance')])
    approved_count = len([s for s in service_items_list if s['status'] in ('finance_approved', 'head_approved')])
    rejected_count = len([s for s in service_items_list if s['status'] in ('supervisor_rejected', 'head_rejected', 'finance_rejected')])
    invoiced_count = len([s for s in service_items_list if s['status'] == 'invoiced'])
    
    return render_template('bu_team/dashboard.html',
                         service_items=service_items_list,
                         draft_count=draft_count,
                         processing_count=processing_count,
                         approved_count=approved_count,
                         rejected_count=rejected_count,
                         invoiced_count=invoiced_count,
                         unit_name=unit_name)

@bu_team_bp.route('/new-entry')
@login_required
def new_entry():
    """Create new time entry"""
    if current_user.role != 'bu_user':
        flash('Access denied. BU Team users only.', 'danger')
        return redirect(url_for('dashboard.index'))
    
    # Check if user has a unit
    if not current_user.unit_id:
        flash('You are not assigned to any unit. Please contact administrator.', 'danger')
        return redirect(url_for('bu_team.dashboard'))
    
    # Get companies accessible to this user
    companies = current_user.accessible_companies
    companies_data = [{'id': c.id, 'name': c.name} for c in companies]
    
    if not companies_data:
        flash('No companies assigned to your unit. Please contact administrator.', 'danger')
        return redirect(url_for('bu_team.dashboard'))
    
    # Get auto-submit day from settings
    from app.models import SystemSetting
    auto_submit_setting = SystemSetting.query.filter_by(setting_key='auto_submit_day').first()
    auto_submit_day = int(auto_submit_setting.setting_value) if auto_submit_setting else 26
    
    # Get current week info
    from app.utils.week_helper import get_current_week_info
    week_info = get_current_week_info()
    
    # Get resource types
    from app.models import ResourceType
    resource_types = ResourceType.query.filter_by(is_active=True, unit_id=current_user.unit_id).order_by(ResourceType.display_order).all()
    
    resource_types_data = []
    for rt in resource_types:
        resource_types_data.append({
            'id': rt.id,
            'name': rt.name,
            'code': rt.code,
            'chargeback_rate': float(rt.chargeback_rate),
            'showback_rate': float(rt.showback_rate),
            'showback_applied': rt.showback_applied,
            'unit_of_measure': rt.unit_of_measure or 'Units',
            'allow_fractional': True if rt.unit_of_measure and rt.unit_of_measure.lower() == 'hour' else False,
            'document_required': rt.document_required
        })
    
    return render_template('bu_team/new_entry.html', 
                         companies=companies_data,
                         resource_types=resource_types_data,
                         week_info=week_info,
                         auto_submit_day=auto_submit_day)


@bu_team_bp.route('/edit-service-item/<int:service_item_id>')
@login_required
def edit_service_item(service_item_id):
    """Edit a specific service item (only if it is rejected by supervisor)"""
    if current_user.role != 'bu_user':
        flash('Access denied. BU Team users only.', 'danger')
        return redirect(url_for('dashboard.index'))
    
    # Get current week info for date picker restrictions
    from app.utils.week_helper import get_current_week_info
    week_info = get_current_week_info()
    
    service_item = ServiceItem.query.get_or_404(service_item_id)
    entry = TimeEntry.query.get(service_item.time_entry_id)
    
    # Check ownership
    if entry.user_id != current_user.id:
        flash('You can only edit your own entries.', 'danger')
        return redirect(url_for('bu_team.dashboard'))
    
    # Allow editing only if this specific service item is rejected
    if service_item.supervisor_approval_status != 'rejected':
        flash(f'Only rejected service items can be edited. Current status: {service_item.supervisor_approval_status}', 'warning')
        return redirect(url_for('bu_team.dashboard'))
    
    # Get company name
    company_name = 'N/A'
    if service_item.company_id:
        company = Company.query.get(service_item.company_id)
        if company:
            company_name = company.name
    
    # Rejection comment (from service item)
    rejection_comment = service_item.supervisor_comment
    
    # Get all resource types for lookup
    all_resource_types = ResourceType.query.all()
    resource_by_id = {str(rt.id): rt for rt in all_resource_types}
    
    # Process resources – convert stored JSON to display format
    processed_resources = []
    for resource in service_item.resources or []:
        resource_type_id = str(resource.get('type', ''))
        rt = resource_by_id.get(resource_type_id)
        
        if rt:
            resource_name = rt.name
            uom = rt.unit_of_measure or 'units'
            chargeback_rate = float(rt.chargeback_rate) if rt.chargeback_rate else 0
            showback_rate = float(rt.showback_rate) if rt.showback_rate else 0
        else:
            resource_name = f'Unknown (ID {resource_type_id})'
            uom = 'units'
            chargeback_rate = 0
            showback_rate = 0
        
        quantity = resource.get('quantity', 0)
        resource_desc = resource.get('description', '').strip()
        if not resource_desc and service_item.service_description:
            resource_desc = service_item.service_description
        
        processed_resources.append({
            'type': resource_type_id,
            'name': resource_name,
            'quantity': quantity,
            'description': resource_desc,
            'uom': uom,
            'chargeback_rate': chargeback_rate,
            'showback_rate': showback_rate
           
            
        })
    
    service_data = {
        'id': service_item.id,
        'entry_id': entry.id,
        'company_id': service_item.company_id,
        'company_name': company_name,
        'description': service_item.service_description,
        'resources': processed_resources,
        'total_amount': float(service_item.total_amount) if service_item.total_amount else 0,
        'chargeback_total': float(service_item.chargeback_total) if service_item.chargeback_total else 0,
        'showback_total': float(service_item.showback_total) if service_item.showback_total else 0,
        'rejection_comment': rejection_comment,
        'service_date': entry.service_date.strftime('%Y-%m-%d') if entry.service_date else None
    }
    
    # Prepare companies list for dropdown
    companies = Company.query.filter_by(is_active=True).all()
    companies_data = [{'id': c.id, 'name': c.name, 'code': c.code} for c in companies]
    
    # Get resource types for pricing reference (for the user's unit)
    unit_id = current_user.unit_id
    if not unit_id:
        membership = UnitMember.query.filter_by(user_id=current_user.id).first()
        if membership:
            unit_id = membership.unit_id
    
    resource_types = ResourceType.query.filter_by(is_active=True, unit_id=unit_id).order_by(ResourceType.display_order).all()
    resource_types_data = []
    for rt in resource_types:
        resource_types_data.append({
        'id': rt.id,
        'name': rt.name,
        'code': rt.code,
        'chargeback_rate': float(rt.chargeback_rate),
        'showback_rate': float(rt.showback_rate),
        'showback_applied': rt.showback_applied,
        'unit_of_measure': rt.unit_of_measure,
        'allow_fractional': True if rt.unit_of_measure and rt.unit_of_measure.lower() == 'hour' else False,
        'document_required': rt.document_required
    })
    
    return render_template('bu_team/edit_service_item.html', 
                         service_data=service_data,
                         companies=companies_data,
                         resource_types=resource_types_data,
                         week_info=week_info)





@bu_team_bp.route('/download-template')
@login_required
def download_template():
    """Download Excel template for bulk upload with dropdowns"""
    if current_user.role != 'bu_user':
        flash('Access denied. BU Team users only.', 'danger')
        return redirect(url_for('dashboard.index'))
    
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    from openpyxl.worksheet.datavalidation import DataValidation
    
    wb = Workbook()
    ws = wb.active
    ws.title = "Service Entry Template"
    
    ws_lists = wb.create_sheet("Lists") # Create Lists sheet for dropdowns
    
    # Get companies
    companies = Company.query.filter_by(is_active=True).all()
    company_names = [c.name for c in companies]
    
    # Get resource types
    resource_types = ResourceType.query.filter_by(is_active=True).all()
    resource_names = [rt.name for rt in resource_types]
    resource_codes = [rt.code.upper() for rt in resource_types]
    
    all_resources = list(set(resource_names + resource_codes)) # Removed short names as they are not in the model
    
    headers = [
        'Company Name', 'Service Description', 'Resource Type', 'Quantity', 'Service Date'
    ]
    
    header_fill = PatternFill(start_color="366092", end_color="366092", fill_type="solid")
    header_font = Font(color="FFFFFF", bold=True)
    header_alignment = Alignment(horizontal="center", vertical="center")
    thin_border = Border(
        left=Side(style='thin'), right=Side(style='thin'),
        top=Side(style='thin'), bottom=Side(style='thin')
    )
    
    for col, header in enumerate(headers, 1):
        cell = ws.cell(row=1, column=col, value=header)
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = header_alignment
        cell.border = thin_border
    
    # Add Data Validation dropdowns
    if company_names:
        company_range = f"Lists!$A$2:$A${len(company_names)+1}"
        ws_lists['A1'] = "COMPANY LIST"
        ws_lists['A1'].font = Font(bold=True)
        for idx, name in enumerate(company_names, start=2):
            ws_lists.cell(row=idx, column=1, value=name)
        dv_company = DataValidation(type="list", formula1=company_range, allow_blank=False)
        ws.add_data_validation(dv_company)
        for row in range(2, 102):
            dv_company.add(f"A{row}")
    
    if all_resources:
        resource_range = f"Lists!$B$2:$B${len(all_resources)+1}" # Changed column to B
        ws_lists['B1'] = "RESOURCE LIST" # Changed column to B
        ws_lists['B1'].font = Font(bold=True)
        for idx, name in enumerate(all_resources, start=2):
            ws_lists.cell(row=idx, column=2, value=name) # Changed column to B
        dv_resource = DataValidation(type="list", formula1=resource_range, allow_blank=False)
        ws.add_data_validation(dv_resource)
        for row in range(2, 102):
            dv_resource.add(f"C{row}") # Changed column to C
    
    # Removed billing_type and charge_type dropdowns
    
    # Add example data row
    # Example data adjusted to new headers:
    # 'Company Name', 'Service Description', 'Resource Type', 'Quantity', 'Service Date'
    example_data = [
        company_names[0] if company_names else 'LOLC Technologies',
        'Sample Service - Mobile App Development',
        'BA',
        '40', # Quantity
        '2024-01-15'
    ]
    
    for col, value in enumerate(example_data, 1):
        cell = ws.cell(row=2, column=col, value=value)
        cell.border = thin_border
        cell.font = Font(color="808080", italic=True)
    
    # Add instruction sheet
    ws_instructions = wb.create_sheet("Instructions", 0)
    
    instructions = [
        ["=" * 80],
        ["BULK UPLOAD INSTRUCTIONS"],
        ["=" * 80],
        [""],
        ["1. HOW TO USE THIS TEMPLATE:"],
        ["   - The dropdown arrows (▼) appear when you click on cells in columns A, C, D, E"],
        ["   - Select values from the dropdown lists to ensure data consistency (for Company Name and Resource Type)"],
        ["   - Do not modify the 'Lists' sheet - it contains the dropdown options"],
        [""],
        ["2. COLUMN DESCRIPTIONS:"],
        ["   Column A - Company Name: Select from dropdown (Required)"],
        ["   Column B - Service Description: Enter detailed description (Required)"],
        ["   Column C - Resource Type: Select from dropdown (Required)"],
        ["   Column D - Quantity: Enter number of units/hours (Required)"],
        ["   Column E - Service Date: Enter date in YYYY-MM-DD format (Optional, defaults to today)"],
        [""],
        ["5. DATE FORMAT: YYYY-MM-DD (e.g., 2024-01-15)"],
        [""],
        ["6. AFTER FILLING THE TEMPLATE: Save and upload on the 'New Time Entry' page"],
        [""],
        ["=" * 80],
        ["For support, contact your system administrator"],
        ["=" * 80],
    ]
    
    for row_idx, row_data in enumerate(instructions, 1):
        for col_idx, value in enumerate(row_data, 1):
            cell = ws_instructions.cell(row=row_idx, column=col_idx, value=value)
            if "BULK UPLOAD INSTRUCTIONS" in str(value):
                cell.font = Font(bold=True, size=14)
            elif value and value.startswith("   "):
                cell.font = Font(size=10)
            cell.alignment = Alignment(wrap_text=True, vertical='top')
    
    ws_instructions.column_dimensions['A'].width = 100
    ws_instructions.row_dimensions[1].height = 20
    
    # Auto-size columns
    for col in ws.columns:
        max_length = 0
        col_letter = col[0].column_letter
        for cell in col:
            try:
                if cell.value and len(str(cell.value)) > max_length:
                    max_length = len(str(cell.value))
            except:
                pass
        adjusted_width = min(max_length + 2, 30)
        ws.column_dimensions[col_letter].width = adjusted_width
    
    ws_lists.sheet_state = 'hidden'
    
    output = io.BytesIO()
    wb.save(output)
    output.seek(0)
    
    return send_file(
        output,
        mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        as_attachment=True,
        download_name='service_entry_template.xlsx'
    )