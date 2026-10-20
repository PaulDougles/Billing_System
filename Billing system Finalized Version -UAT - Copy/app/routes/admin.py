from flask import Blueprint, render_template, redirect, url_for, flash, request, jsonify
from flask_login import login_required, current_user

from app.models import (
    User, Company, Unit, TimeEntry, ServiceItem, ResourceType, 
    UnitMember, SystemSetting, ActivityLog, Notification, SystemAudit, TaxFeeConfig
)
from app.extensions import db, mail
from flask_mail import Message
from datetime import datetime
from functools import wraps
from app.utils.system_audit import SystemAuditLogger
from app.services import company_service, user_service
from sqlalchemy import inspect
from sqlalchemy.orm import joinedload
import re

admin_bp = Blueprint('admin', __name__)

# app/routes/admin.py - Update the admin_required decorator

from functools import wraps
from flask import session, flash, redirect, url_for
from flask_login import current_user

import pandas as pd
from werkzeug.utils import secure_filename
import io
from flask import send_file, make_response

ALLOWED_EXTENSIONS = {'xlsx', 'xls', 'csv'}

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

def admin_required(f):
    """Decorator to check if user has admin access (including switched mode)"""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not current_user.is_authenticated:
            flash('Please log in to access this page.', 'danger')
            return redirect(url_for('auth.login'))
        
        # Check if user is admin by role
        if current_user.role == 'admin':
            return f(*args, **kwargs)
        
        # Check if finance controller switched to admin mode
        active_role = session.get('active_role', current_user.role)
        if (current_user.role == 'finance_controller' and 
            active_role == 'admin' and 
            hasattr(current_user, 'has_admin_access') and 
            current_user.has_admin_access):
            return f(*args, **kwargs)
        
        flash('You need admin privileges to access this page.', 'danger')
        return redirect(url_for('dashboard.index'))
    return decorated_function

def generate_resource_code(name):
    """Generate a code from the name"""
    import re
    code = name.lower()
    code = re.sub(r'[^a-z0-9\s]', '', code)  # Remove special characters
    code = re.sub(r'\s+', '_', code)          # Replace spaces with underscores
    code = code[:20]                          # Limit to 20 characters
    
    # Ensure uniqueness
    original_code = code
    counter = 1
    from app.models import ResourceType
    while ResourceType.query.filter_by(code=code).first():
        code = f"{original_code}_{counter}"
        counter += 1
    
    return code


@admin_bp.route('/users')
@login_required
@admin_required
def list_users():
    """List all users"""
    users = User.query.order_by(User.created_at.desc()).all()
    return render_template('admin/users.html', users=users)


@admin_bp.route('/companies')
@login_required
@admin_required
def manage_companies():
    """Manage companies"""
    companies = company_service.list_companies()
    return render_template('admin/companies.html', companies=companies)


@admin_bp.route('/companies/create', methods=['POST'])
@login_required
@admin_required
def create_company():
    """Create new company"""
    name = request.form.get('name')
    code = request.form.get('code')
    address = request.form.get('address')
    phone = request.form.get('phone')
    email = request.form.get('email')
    
    company_type = request.form.get('company_type', 'external')
    try:
        company = company_service.create_company(
            name=name,
            code=code,
            address=address,
            phone=phone,
            email=email,
            company_type=company_type,
            is_parent_company=company_type == 'parent',
        )
    except ValueError as error:
        flash(str(error), 'danger')
        return redirect(url_for('admin.manage_companies'))
    
    SystemAuditLogger.log_company_management(
        admin=current_user,
        action='company_created',
        company=company,
        details={
            'code': code, 
            'email': email, 
            'address': address
        },
        status='success'
    )
    
    flash('Company created successfully', 'success')
    return redirect(url_for('admin.manage_companies'))


@admin_bp.route('/companies/<int:company_id>/edit', methods=['POST'])
@login_required
@admin_required
def edit_company(company_id):
    """Edit company"""
    company = Company.query.get_or_404(company_id)
    
    company_type = request.form.get('company_type', company.company_type or 'external')
    try:
        company_service.update_company(
            company,
            name=request.form.get('name'),
            code=request.form.get('code'),
            address=request.form.get('address'),
            phone=request.form.get('phone'),
            email=request.form.get('email'),
            company_type=company_type,
            is_parent_company=company_type == 'parent',
        )
    except ValueError as error:
        flash(str(error), 'danger')
        return redirect(url_for('admin.manage_companies'))
    
    flash('Company updated successfully', 'success')
    return redirect(url_for('admin.manage_companies'))

# app/routes/admin.py - Add these routes

@admin_bp.route('/bulk-create-companies', methods=['GET', 'POST'])
@login_required
@admin_required
def bulk_create_companies():
    """Bulk create companies from Excel/CSV file"""
    
    if request.method == 'POST':
        print("=" * 50)
        print("DEBUG: POST request received")
        print(f"All files in request: {list(request.files.keys())}")
        
        # Check if file was uploaded
        file = request.files.get('company_file')
        
        if not file or file.filename == '':
            print("No file selected")
            flash('No file selected. Please choose a file to upload.', 'danger')
            return redirect(url_for('admin.bulk_create_companies'))
        
        if not allowed_file(file.filename):
            print(f"Invalid file type: {file.filename}")
            flash('Invalid file type. Please upload .xlsx, .xls, or .csv file', 'danger')
            return redirect(url_for('admin.bulk_create_companies'))
        
        # Read the file
        try:
            if file.filename.endswith('.csv'):
                df = pd.read_csv(file)
            else:
                df = pd.read_excel(file)
            
            # Normalize column names: strip whitespace, remove trailing asterisks, and lowercase
            df.columns = [str(col).strip().rstrip('*').lower() for col in df.columns]
            
            print(f"Successfully read file with {len(df)} rows")
            print(f"Columns found: {list(df.columns)}")
            print(f"First row data: {df.iloc[0].to_dict()}")
        except Exception as e:
            print(f"Error reading file: {str(e)}")
            flash(f'Error reading file: {str(e)}', 'danger')
            return redirect(url_for('admin.bulk_create_companies'))
        
        # Validate required columns
        required_columns = ['name']
        missing_columns = [col for col in required_columns if col not in df.columns]
        
        if missing_columns:
            print(f"Missing columns: {missing_columns}")
            flash(f'Missing required columns: {", ".join(missing_columns)}', 'danger')
            return redirect(url_for('admin.bulk_create_companies'))
        
        created_companies = []
        errors = []
        
        for index, row in df.iterrows():
            row_num = index + 2
            
            try:
                name = str(row.get('name', '')).strip()
                
                # Skip empty rows
                if not name or name == 'nan' or name == 'None':
                    print(f"Row {row_num}: Skipping empty row")
                    continue
                
                print(f"\n--- Processing row {row_num}: {name} ---")
                
                # Get code
                code = None
                if pd.notna(row.get('code')):
                    code = str(row.get('code', '')).strip()
                    if code == 'nan':
                        code = None
                
                # Get email
                email = None
                if pd.notna(row.get('email')):
                    email = str(row.get('email', '')).strip()
                    if email == 'nan':
                        email = None
                
                # Get phone
                phone = None
                if pd.notna(row.get('phone')):
                    phone = str(row.get('phone', '')).strip()
                    if phone == 'nan':
                        phone = None
                
                # Get address
                address = None
                if pd.notna(row.get('address')):
                    address = str(row.get('address', '')).strip()
                    if address == 'nan':
                        address = None

                # Support the labels shown in the Excel template while storing
                # the existing canonical values used throughout the app.
                company_type_value = row.get('company_type')
                if pd.isna(company_type_value) or not str(company_type_value).strip():
                    company_type = 'external'
                else:
                    company_type_value = str(company_type_value).strip().lower()
                    if company_type_value == 'parent':
                        company_type = 'parent'
                    elif company_type_value in ['other', 'external']:
                        company_type = 'external'
                    else:
                        errors.append(
                            f"Row {row_num}: Company type must be 'Parent' or 'Other'"
                        )
                        continue
                
                # Check is_active
                is_active = True
                if pd.notna(row.get('is_active')):
                    active_val = str(row.get('is_active')).strip().lower()
                    is_active = active_val in ['yes', 'true', '1', 'y', 'active']
                
                print(f"  Name: {name}")
                print(f"  Code: {code}")
                print(f"  Email: {email}")
                print(f"  Phone: {phone}")
                print(f"  Company Type: {'Parent' if company_type == 'parent' else 'Other'}")
                print(f"  Is Active: {is_active}")
                
                # Check if company already exists
                existing = Company.query.filter_by(name=name).first()
                if existing:
                    errors.append(f"Row {row_num}: Company '{name}' already exists")
                    print(f"  ERROR: Company already exists")
                    continue
                
                # Auto-generate code if not provided
                if not code:
                    code = name.lower().replace(' ', '_').replace('-', '_').replace('.', '')[:20]
                    # Remove special characters
                    import re
                    code = re.sub(r'[^a-z0-9_]', '', code)
                    # Ensure uniqueness
                    original_code = code
                    counter = 1
                    while Company.query.filter_by(code=code).first():
                        code = f"{original_code}_{counter}"
                        counter += 1
                    print(f"  Auto-generated code: {code}")
                
                # Create company
                company = Company(
                    name=name,
                    code=code,
                    email=email,
                    phone=phone,
                    address=address,
                    is_active=is_active,
                    company_type=company_type,
                    is_parent_company=company_type == 'parent',
                    has_full_access=company_type == 'parent'
                )
                
                db.session.add(company)
                db.session.flush()
                
                created_companies.append({
                    'name': name,
                    'code': code,
                    'email': email or 'N/A',
                    'company_type': 'Parent' if company_type == 'parent' else 'Other'
                })
                
                print("  Created company successfully")
                
            except Exception as e:
                error_msg = f"Row {row_num}: {str(e)}"
                errors.append(error_msg)
                print(f"  ERROR: {str(e)}")
                db.session.rollback()
        
        # Commit all changes
        try:
            db.session.commit()
            print(f"\nCommitted {len(created_companies)} companies to database")
        except Exception as e:
            db.session.rollback()
            print(f"Database commit error: {str(e)}")
            flash(f'Database error: {str(e)}', 'danger')
            return redirect(url_for('admin.bulk_create_companies'))
        
        # Show result
        print(f"\nRESULTS: {len(created_companies)} created, {len(errors)} errors")
        
        if created_companies:
            flash(f'Successfully created {len(created_companies)} companies!', 'success')
            if errors:
                flash(f'Completed with {len(errors)} errors. Check details below.', 'warning')
        else:
            flash('No companies were created. Please check the errors.', 'danger')
        
        # Store results in session
        session['bulk_company_errors'] = errors
        session['bulk_company_created'] = created_companies
        session['bulk_rows'] = len(df)
        session['bulk_columns'] = list(df.columns)
        
        return redirect(url_for('admin.bulk_create_companies'))
    
    # GET request - show the form
    errors = session.pop('bulk_company_errors', [])
    created_companies = session.pop('bulk_company_created', [])
    rows = session.pop('bulk_rows', 0)
    columns = session.pop('bulk_columns', [])
    
    return render_template('admin/bulk_create_companies.html',
                         errors=errors,
                         created_companies=created_companies,
                         rows=rows,
                         columns=columns,
                         table=None,
                         data=[])

@admin_bp.route('/download-company-template')
@login_required
@admin_required
def download_company_template():
    """Download Excel template for bulk company upload"""
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    from openpyxl.worksheet.datavalidation import DataValidation
    import io
    
    wb = Workbook()
    ws = wb.active
    ws.title = "Company Template"
    
    # Headers
    headers = ['name*', 'code', 'email', 'phone', 'address', 'company_type', 'is_active']
    
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
    
    # Add drop-down validation for company type and active status.
    ws_lists = wb.create_sheet("Lists")
    yes_no = ['Yes', 'No']
    for idx, yn in enumerate(yes_no, start=1):
        ws_lists.cell(row=idx, column=1, value=yn)
    yn_range = f"Lists!$A$1:$A${len(yes_no)}"
    
    dv_company_type = DataValidation(
        type="list",
        formula1='"Parent,Other"',
        allow_blank=True
    )
    ws.add_data_validation(dv_company_type)
    dv_company_type.add("F2:F101")

    dv_active = DataValidation(type="list", formula1=yn_range, allow_blank=True)
    ws.add_data_validation(dv_active)
    for row in range(2, 102):
        dv_active.add(f"G{row}")
    
    # Example row
    example_row = 2
    ws.cell(row=example_row, column=1, value="LOLC Technologies")
    ws.cell(row=example_row, column=2, value="LOLC-TECH")
    ws.cell(row=example_row, column=3, value="info@lolctech.com")
    ws.cell(row=example_row, column=4, value="0112345678")
    ws.cell(row=example_row, column=5, value="137 Rajagiriya Rd, Sri Jayawardenepura Kotte")
    ws.cell(row=example_row, column=6, value="Parent")
    ws.cell(row=example_row, column=7, value="Yes")
    
    for col in range(1, len(headers) + 1):
        cell = ws.cell(row=example_row, column=col)
        cell.border = thin_border
        cell.font = Font(color="808080", italic=True)
    
    ws_lists.sheet_state = 'hidden'
    
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
    
    output = io.BytesIO()
    wb.save(output)
    output.seek(0)
    
    return send_file(
        output,
        mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        as_attachment=True,
        download_name='company_bulk_upload_template.xlsx'
    )

@admin_bp.route('/companies/<int:company_id>/toggle', methods=['POST'])
@login_required
@admin_required
def toggle_company(company_id):
    """Activate/deactivate company"""
    company = Company.query.get_or_404(company_id)
    old_status, new_status = company_service.toggle_company_status(company)
    
    # Log status change
    SystemAuditLogger.log_company_management(
        admin=current_user,
        action='company_status_changed',
        company=company,
        details={'old_status': old_status, 'new_status': new_status},
        status='success'
    )
    
    status = 'activated' if company.is_active else 'deactivated'
    flash(f'Company {status} successfully', 'success')
    return redirect(url_for('admin.manage_companies'))


# In admin.py, update the create_user function

# app/routes/admin.py - Update the create_user function

@admin_bp.route('/users/create', methods=['GET', 'POST'])
@login_required
@admin_required
def create_user():
    """Create user with role-specific fields"""
    if request.method == 'POST':
        email = request.form.get('email')
        employee_id = request.form.get('employee_id')
        nic = request.form.get('nic')
        first_name = request.form.get('first_name')
        last_name = request.form.get('last_name')
        role = request.form.get('role')
        company_id = request.form.get('company_id')
        unit_id = request.form.get('unit_id')
        
        # NEW: Get admin access flag for finance controller
        has_admin_access = request.form.get('has_admin_access') == 'true'
        
        # Validation
        if not email or not employee_id or not nic or not first_name or not last_name or not role:
            flash('All fields are required', 'danger')
            return redirect(url_for('admin.create_user'))
        
        # ========== VALID ROLES (including super_admin) ==========
        valid_roles = ['super_admin', 'admin', 'bu_user', 'supervisor', 'head_of_it', 
                       'finance_controller', 'payment_company_user', 'record_assistant']
        
        if role not in valid_roles:
            flash('Invalid role selected', 'danger')
            return redirect(url_for('admin.create_user'))
        
        # Role-specific validation
        if role == 'bu_user' and not unit_id:
            flash('BU Team users must be assigned to a unit', 'danger')
            return redirect(url_for('admin.create_user'))

        if role == 'record_assistant' and not company_id:
            flash('Record Assistant must be assigned to a company', 'danger')
            return redirect(url_for('admin.create_user'))

        if role in ['supervisor', 'head_of_it', 'payment_company_user', 'record_assistant'] and not company_id:
            flash(f'{role.replace("_", " ").title()} must be assigned to a company', 'danger')
            return redirect(url_for('admin.create_user'))
        
        # Get LOLC Technologies company ID for BU users
        parent_company = company_service.get_parent_company()
        
        if role == 'bu_user' and parent_company:
            company_id = parent_company.id
        
        try:
            user = user_service.create_user(
                email=email,
                employee_id=employee_id,
                nic=nic,
                first_name=first_name,
                last_name=last_name,
                role=role,
                company_id=company_id if company_id else None,
                unit_id=unit_id if unit_id else None,
                has_admin_access=has_admin_access,
            )
        except ValueError as error:
            flash(str(error), 'danger')
            return redirect(url_for('admin.create_user'))
        
        # Log user creation
        SystemAuditLogger.log_user_management(
            admin=current_user,
            action='user_created',
            target_user=user,
            details={
                'email': email,
                'employee_id': employee_id,
                'role': role,
                'has_admin_access': has_admin_access,
                'company_id': company_id,
                'unit_id': unit_id
            },
            status='success'
        )
        
        # Send welcome email
        from app.utils.email_helper import send_welcome_email
        if send_welcome_email(user):
            flash(f'User created successfully. Welcome email sent to {email}', 'success')
        else:
            flash(f'User created but failed to send welcome email', 'warning')
        
        return redirect(url_for('admin.list_users'))
    
    companies = Company.query.filter_by(is_active=True).all()
    units = Unit.query.all()
    return render_template('admin/create_user.html', 
                         companies=companies, 
                         units=units)





@admin_bp.route('/users/<int:user_id>/edit', methods=['GET', 'POST'])
@login_required
@admin_required
def edit_user(user_id):
    """Edit user details"""
    user = User.query.get_or_404(user_id)
    
    if request.method == 'POST':
        old_email = user.email
        old_role = user.role
        
        user.first_name = request.form.get('first_name')
        user.last_name = request.form.get('last_name')
        user.email = request.form.get('email')
        user.employee_id = request.form.get('employee_id')
        user.nic = request.form.get('nic')
        user.role = request.form.get('role')
        user.company_id = request.form.get('company_id') or None
        user.unit_id = request.form.get('team_id') or None
        
        # ========== VALID ROLES (including super_admin) ==========
        valid_roles = ['super_admin', 'admin', 'bu_user', 'supervisor', 'head_of_it', 
                       'finance_controller', 'payment_company_user', 'record_assistant']
        
        if user.role not in valid_roles:
            flash('Invalid role selected', 'danger')
            return redirect(url_for('admin.edit_user', user_id=user_id))
        
        # NEW: Handle admin access for finance controller
        if user.role == 'finance_controller':
            user.has_admin_access = request.form.get('has_admin_access') == 'true'
        else:
            user.has_admin_access = False
        
        db.session.commit()
        
        # Log user update
        SystemAuditLogger.log_user_management(
            admin=current_user,
            action='user_updated',
            target_user=user,
            details={
                'old_email': old_email,
                'new_email': user.email,
                'old_role': old_role,
                'new_role': user.role,
                'has_admin_access': user.has_admin_access,
                'changes': {
                    'first_name': user.first_name,
                    'last_name': user.last_name,
                    'employee_id': user.employee_id
                }
            },
            status='success'
        )
        
        flash('User updated successfully', 'success')
        return redirect(url_for('admin.list_users'))
    
    # Get company and team names for display - query separately
    companies = Company.query.filter_by(is_active=True).all()
    teams = Unit.query.all()
    
    # Get current company and team names for the template
    current_company = None
    if user.company_id:
        current_company = Company.query.get(user.company_id)
    
    current_unit = None
    if user.unit_id:
        current_unit = Unit.query.get(user.unit_id)
    
    return render_template('admin/edit_user.html', 
                         user=user, 
                         companies=companies, 
                         units=teams,
                         current_company=current_company,
                         current_unit=current_unit)

@admin_bp.route('/bulk-create-users', methods=['GET', 'POST'])
@login_required
@admin_required
def bulk_create_users():
    """Bulk create users from Excel/CSV file"""
    
    if request.method == 'POST':
        # Check if file was uploaded
        if 'user_file' not in request.files:
            flash('No file uploaded', 'danger')
            return redirect(url_for('admin.bulk_create_users'))
        
        file = request.files['user_file']
        if file.filename == '':
            flash('No file selected', 'danger')
            return redirect(url_for('admin.bulk_create_users'))
        
        if not allowed_file(file.filename):
            flash('Invalid file type. Please upload .xlsx, .xls, or .csv file', 'danger')
            return redirect(url_for('admin.bulk_create_users'))
        
        # Read the file
        try:
            if file.filename.endswith('.csv'):
                df = pd.read_csv(file)
            else:
                df = pd.read_excel(file)
            
            # Normalize column names: strip whitespace, remove trailing asterisks, and lowercase
            df.columns = [str(col).strip().rstrip('*').lower() for col in df.columns]
            
        except Exception as e:
            flash(f'Error reading file: {str(e)}', 'danger')
            return redirect(url_for('admin.bulk_create_users'))
        
        # Validate required columns
        required_columns = ['first_name', 'last_name', 'email', 'employee_id', 'nic', 'role']
        missing_columns = [col for col in required_columns if col not in df.columns]
        
        if missing_columns:
            flash(f'Missing required columns: {", ".join(missing_columns)}', 'danger')
            return redirect(url_for('admin.bulk_create_users'))
        
        # Get existing companies and units for lookup
        companies = {c.name: c.id for c in Company.query.all()}
        units = {u.name: u.id for u in Unit.query.all()}
        
        # Get LOLC Technologies company for BU users
        parent_company = company_service.get_parent_company()
        
        created_users = []
        users_to_email = []
        errors = []
        
        for index, row in df.iterrows():
            row_num = index + 2  # Excel row number (1-indexed + header)
            
            try:
                # Get values
                first_name = str(row.get('first_name', '')).strip()
                last_name = str(row.get('last_name', '')).strip()
                email = str(row.get('email', '')).strip()
                employee_id = str(row.get('employee_id', '')).strip()
                nic = str(row.get('nic', '')).strip().upper()
                role = str(row.get('role', '')).strip().lower()
                company_name = str(row.get('company_name', '')).strip() if pd.notna(row.get('company_name')) else None
                unit_name = str(row.get('unit_name', '')).strip() if pd.notna(row.get('unit_name')) else None
                
                # NEW: Admin access for finance controller
                has_admin_access = str(row.get('has_admin_access', '')).strip().lower() in ['yes', 'true', '1', 'y']
                
                # Validate required fields
                if not all([first_name, last_name, email, employee_id, nic, role]):
                    errors.append(f"Row {row_num}: Missing required fields (first_name, last_name, email, employee_id, nic, role)")
                    continue
                
                # Validate role
                valid_roles = ['admin', 'bu_user', 'supervisor', 'head_of_it', 'finance_controller', 'payment_company_user','record_assistant']
                if role not in valid_roles:
                    errors.append(f"Row {row_num}: Invalid role '{role}'. Valid roles: {', '.join(valid_roles)}")
                    continue
                
                validation_error = user_service.validate_user_fields(
                    email, employee_id, nic, first_name, last_name, role
                )
                if validation_error:
                    errors.append(f"Row {row_num}: {validation_error}")
                    continue
                
                # Get company ID for roles that require company
                company_id = None
                if role in ['supervisor', 'head_of_it', 'payment_company_user', 'payment_lolc_technologies', 'payment_lolc_finance','record_assistant']:
                    if not company_name:
                        errors.append(f"Row {row_num}: Company name required for role '{role}'")
                        continue
                    if company_name not in companies:
                        errors.append(f"Row {row_num}: Company '{company_name}' not found")
                        continue
                    company_id = companies[company_name]
                
                # Get unit ID for BU user
                unit_id = None
                if role == 'bu_user':
                    if not unit_name:
                        errors.append(f"Row {row_num}: Unit name required for BU user")
                        continue
                    if unit_name not in units:
                        errors.append(f"Row {row_num}: Unit '{unit_name}' not found")
                        continue
                    unit_id = units[unit_name]
                    
                    # Set company to LOLC Technologies for BU users
                    if parent_company:
                        company_id = parent_company.id
                
                user = user_service.add_user_to_session(
                    first_name=first_name,
                    last_name=last_name,
                    email=email,
                    employee_id=employee_id,
                    nic=nic,
                    role=role,
                    company_id=company_id,
                    unit_id=unit_id,
                    has_admin_access=has_admin_access,
                )
                
                created_user = {
                    'email': email,
                    'name': f"{first_name} {last_name}",
                    'role': role
                }
                created_users.append(created_user)
                users_to_email.append((user, created_user))
                
            except Exception as e:
                errors.append(f"Row {row_num}: {str(e)}")
                db.session.rollback()
        
        # Commit all changes
        try:
            db.session.commit()
        except Exception as e:
            db.session.rollback()
            flash(f'Database error: {str(e)}', 'danger')
            return redirect(url_for('admin.bulk_create_users'))

        # Send welcome emails only after the created users are committed.
        persisted_users = [
            (user, details)
            for user, details in users_to_email
            if inspect(user).persistent
        ]
        created_users = [details for _, details in persisted_users]
        from app.utils.email_helper import send_welcome_email
        failed_welcome_emails = 0
        for user, details in persisted_users:
            if send_welcome_email(user):
                details['email_status'] = 'Sent'
            else:
                details['email_status'] = 'Failed'
                failed_welcome_emails += 1
        
        # Show result
        if created_users:
            flash(f'Successfully created {len(created_users)} users!', 'success')
            if errors:
                flash(f'Completed with {len(errors)} errors. Check details below.', 'warning')
            if failed_welcome_emails:
                flash(
                    f'{failed_welcome_emails} welcome email(s) could not be sent. '
                    'Check SMTP configuration and the email status below.',
                    'warning'
                )
            elif created_users:
                flash('Welcome emails sent to all created users.', 'success')
        else:
            flash('No users were created. Please check the errors.', 'danger')
        
        # Store errors in session to display
        session['bulk_create_errors'] = errors
        session['bulk_create_created'] = created_users
        
        return redirect(url_for('admin.bulk_create_users'))
    
    # GET request - show the form
    errors = session.pop('bulk_create_errors', [])
    created_users = session.pop('bulk_create_created', [])
    
    # Get template for download
    companies = Company.query.all()
    units = Unit.query.all()
    
    return render_template('admin/bulk_create_users.html',
                         errors=errors,
                         created_users=created_users,
                         companies=companies,
                         units=units)


@admin_bp.route('/download-user-template')
@login_required
@admin_required
def download_user_template():
    """Download Excel template for bulk user upload"""
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    from openpyxl.worksheet.datavalidation import DataValidation
    import io
    
    wb = Workbook()
    ws = wb.active
    ws.title = "User Template"
    
    ws_lists = wb.create_sheet("Lists")
    
    # Get data for dropdowns
    companies = Company.query.filter_by(is_active=True).all()
    company_names = [c.name for c in companies]
    
    units = Unit.query.filter_by(is_active=True).all()
    unit_names = [u.name for u in units]
    
    roles = ['admin','super_admin','bu_user', 'supervisor', 'head_of_it', 'record_assistant', 'finance_controller', 'payment_company_user']
    yes_no = ['Yes', 'No']
    
    # Populate Lists sheet
    for idx, name in enumerate(company_names, start=1):
        ws_lists.cell(row=idx, column=1, value=name)
    company_range = f"Lists!$A$1:$A${len(company_names)}" if company_names else None
    
    for idx, name in enumerate(unit_names, start=1):
        ws_lists.cell(row=idx, column=2, value=name)
    unit_range = f"Lists!$B$1:$B${len(unit_names)}" if unit_names else None
    
    for idx, role in enumerate(roles, start=1):
        ws_lists.cell(row=idx, column=3, value=role)
    role_range = f"Lists!$C$1:$C${len(roles)}"
    
    for idx, yn in enumerate(yes_no, start=1):
        ws_lists.cell(row=idx, column=4, value=yn)
    yn_range = f"Lists!$D$1:$D${len(yes_no)}"
    
    # Headers
    headers = [
        'first_name*', 'last_name*', 'email*', 'employee_id*', 'nic*', 'role*',
        'company_name', 'unit_name', 'has_admin_access'
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
    
    # Add data validation
    if company_range:
        dv_company = DataValidation(type="list", formula1=company_range, allow_blank=True)
        ws.add_data_validation(dv_company)
        for row in range(2, 102):
            dv_company.add(f"G{row}")
    
    if unit_range:
        dv_unit = DataValidation(type="list", formula1=unit_range, allow_blank=True)
        ws.add_data_validation(dv_unit)
        for row in range(2, 102):
            dv_unit.add(f"H{row}")
    
    dv_role = DataValidation(type="list", formula1=role_range, allow_blank=False)
    ws.add_data_validation(dv_role)
    for row in range(2, 102):
        dv_role.add(f"F{row}")
    
    dv_admin = DataValidation(type="list", formula1=yn_range, allow_blank=True)
    ws.add_data_validation(dv_admin)
    for row in range(2, 102):
        dv_admin.add(f"I{row}")
    
    # Example row
    example_row = 2
    ws.cell(row=example_row, column=1, value="John")
    ws.cell(row=example_row, column=2, value="Doe")
    ws.cell(row=example_row, column=3, value="john.doe@example.com")
    ws.cell(row=example_row, column=4, value="EMP001")
    ws.cell(row=example_row, column=5, value="123456789V")
    ws.cell(row=example_row, column=6, value="bu_user")
    if company_names:
        ws.cell(row=example_row, column=7, value=company_names[0])
    if unit_names:
        ws.cell(row=example_row, column=8, value=unit_names[0])
    ws.cell(row=example_row, column=9, value="No")
    
    for col in range(1, len(headers) + 1):
        cell = ws.cell(row=example_row, column=col)
        cell.border = thin_border
        cell.font = Font(color="808080", italic=True)
    
    ws_lists.sheet_state = 'hidden'
    
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
    
    output = io.BytesIO()
    wb.save(output)
    output.seek(0)
    
    return send_file(
        output,
        mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        as_attachment=True,
        download_name='user_bulk_upload_template.xlsx'
    )

@admin_bp.route('/users/<int:user_id>/toggle-status')
@login_required
@admin_required
def toggle_user_status(user_id):
    """Activate or deactivate a user"""
    user = User.query.get_or_404(user_id)
    
    if user.id == current_user.id:
        flash('You cannot deactivate your own account.', 'danger')
        return redirect(url_for('admin.list_users'))
    
    old_status = user.is_active
    user.is_active = not user.is_active
    db.session.commit()
    
    # Log status change
    SystemAuditLogger.log_user_management(
        admin=current_user,
        action='user_status_changed',
        target_user=user,
        details={'old_status': old_status, 'new_status': user.is_active},
        status='success'
    )
    
    status = 'activated' if user.is_active else 'deactivated'
    flash(f'User {user.email} has been {status}.', 'success')
    return redirect(url_for('admin.list_users'))





@admin_bp.route('/users/<int:user_id>/delete', methods=['POST'])
@login_required
@admin_required
def delete_user(user_id):
    """Delete a user"""
    user = User.query.get_or_404(user_id)
    
    if user.id == current_user.id:
        flash('You cannot delete your own account.', 'danger')
        return redirect(url_for('admin.list_users'))
    
    # Check if user has time entries - use query instead of relationship
    time_entries = TimeEntry.query.filter_by(user_id=user.id).all()
    
    if time_entries:
        flash('Cannot delete user with existing time entries. Deactivate the user instead.', 'danger')
        return redirect(url_for('admin.list_users'))
    
    # Also check if user has service items directly
    service_items = ServiceItem.query.filter_by(time_entry_id=user.id).all()
    if service_items:
        flash('Cannot delete user with existing service entries. Deactivate the user instead.', 'danger')
        return redirect(url_for('admin.list_users'))
    
    # Log before deletion
    SystemAuditLogger.log_user_management(
        admin=current_user,
        action='user_deleted',
        target_user=user,
        details={
            'email': user.email,
            'employee_id': user.employee_id,
            'role': user.role
        },
        status='success'
    )
    
    db.session.delete(user)
    db.session.commit()
    
    flash(f'User {user.email} has been deleted.', 'success')
    return redirect(url_for('admin.list_users'))


@admin_bp.route('/system-audit')
@login_required
@admin_required
def system_audit():
    """View all system audit logs"""
    from app.models import SystemAudit
    from datetime import timedelta
    
    page = request.args.get('page', 1, type=int)
    per_page = 50
    
    query = SystemAudit.query
    
    # Apply filters
    action = request.args.get('action')
    if action:
        query = query.filter(SystemAudit.action_type == action)
    
    user_email = request.args.get('user')
    if user_email:
        query = query.filter(SystemAudit.user_email.contains(user_email))
    
    date_from = request.args.get('date_from')
    if date_from:
        from datetime import datetime
        query = query.filter(SystemAudit.created_at >= datetime.strptime(date_from, '%Y-%m-%d'))
    
    date_to = request.args.get('date_to')
    if date_to:
        from datetime import datetime
        query = query.filter(SystemAudit.created_at <= datetime.strptime(date_to, '%Y-%m-%d') + timedelta(days=1))
    
    audits = query.order_by(SystemAudit.created_at.desc()).paginate(
        page=page, per_page=per_page, error_out=False
    )
    
    return render_template('admin/system_audit.html', audits=audits)


@admin_bp.route('/system-audit/export')
@login_required
@admin_required
def export_system_audit():
    """Export system audit logs to CSV"""
    from app.models import SystemAudit
    import csv
    import io
    from flask import Response
    
    # Get all audits
    audits = SystemAudit.query.order_by(SystemAudit.created_at.desc()).all()
    
    output = io.StringIO()
    writer = csv.writer(output)
    
    # Write headers
    writer.writerow(['Timestamp', 'User Email', 'User Role', 'Action Type', 'Action Category', 
                     'Target Type', 'Target Name', 'Target ID', 'Status', 'Error Message', 'Details', 'IP Address'])
    
    # Write data
    for audit in audits:
        writer.writerow([
            audit.created_at.strftime('%Y-%m-%d %H:%M:%S'),
            audit.user_email,
            audit.user_role,
            audit.action_type,
            audit.action_category,
            audit.target_type or '',
            audit.target_name or '',
            audit.target_id or '',
            audit.status,
            audit.error_message or '',
            str(audit.details) if audit.details else '',
            audit.ip_address or ''
        ])
    
    output.seek(0)
    return Response(
        output.getvalue(),
        mimetype='text/csv',
        headers={'Content-Disposition': 'attachment; filename=system_audit_logs.csv'}
    )


# ============ RESOURCE TYPE MANAGEMENT ============

@admin_bp.route('/resource-types')
@login_required
@admin_required
def manage_resource_types():
    """Manage resource types"""
    page = request.args.get('page', 1, type=int)
    per_page = 10
    
    pagination = ResourceType.query.options(joinedload(ResourceType.unit)).order_by(ResourceType.display_order).paginate(
        page=page, per_page=per_page, error_out=False
    )
    
    resource_types = pagination.items
    units = Unit.query.all()
    
    return render_template('admin/resource_types.html', 
                         resource_types=resource_types,
                         pagination=pagination,
                         units=units)


@admin_bp.route('/resource-types/create', methods=['GET', 'POST'])
@login_required
@admin_required
def create_resource_type():
    if request.method == 'POST':
        name = request.form.get('name')
        category = request.form.get('category', '')
        description = request.form.get('description', '')
        unit_id = request.form.get('unit_id')
        unit_of_measure = request.form.get('unit_of_measure', '')
        display_order = int(request.form.get('display_order', 0))
        is_active = request.form.get('is_active') == 'on'

        document_required = request.form.get('document_required') == 'on'
        
        if not unit_id:
            flash('Please select a unit for this resource type', 'danger')
            return redirect(url_for('admin.create_resource_type'))
        
        unit = Unit.query.get(unit_id)
        if not unit:
            flash('Invalid unit selected', 'danger')
            return redirect(url_for('admin.create_resource_type'))
        
        # Check for unique name within unit
        existing_name = ResourceType.query.filter_by(name=name, unit_id=unit_id).first()
        if existing_name:
            flash(f'A resource type with the name "{name}" already exists in the unit "{unit.name}".', 'danger')
            return redirect(url_for('admin.create_resource_type'))
            
        # Generate code
        code = generate_resource_code(name)
        if ResourceType.query.filter_by(code=code).first():
            flash('Resource type code already exists', 'danger')
            return redirect(url_for('admin.create_resource_type'))
        
        # Create resource type object
        rt = ResourceType(
            name=name,
            code=code,
            description=description,
            category=category,
            unit_id=unit_id,
            unit_of_measure=unit_of_measure,
            display_order=display_order,
            is_active=is_active,
            document_required=document_required
        )
        
        # --- Tax logic ---
        tax_applied = request.form.get('tax_applied') == 'on'
        rt.tax_applied = tax_applied
        
        if tax_applied:
            tax_config = TaxFeeConfig.get_config()
            handling_fee = float(tax_config.handling_fee_percent)
            finance_fee = float(tax_config.finance_fee_percent)
            base_cost = float(request.form.get('base_cost', 0))
            tax_company_name = request.form.get('tax_company')
            
            if not tax_company_name:
                flash('Please select a company when Tax Applied is enabled', 'danger')
                return redirect(url_for('admin.create_resource_type'))
            
            rt.tax_company_name = tax_company_name
            rt.base_cost = base_cost
            
            if tax_company_name == 'LOLC Holdings':
                showback = base_cost + (base_cost * finance_fee / 100)
                chargeback = (showback * handling_fee / 100)
                rt.showback_applied = True
            else:  # LOLC Technologies
                chargeback = base_cost + (base_cost * handling_fee / 100)
                showback = 0
                rt.showback_applied = False
            
            # Set rates
            rt.chargeback_rate = chargeback  # LOIT Total Chargeback
            rt.showback_rate = showback      # LOLC Showback
            # Reset manual fields
            rt.loit_chargeback = 0
            rt.loit_showback = 0
            
        else:
            # MANUAL MODE - Get values from form
            loit_chargeback = float(request.form.get('loit_chargeback', 0))
            loit_showback = float(request.form.get('loit_showback', 0))
            loit_total = loit_chargeback + loit_showback
            
            rt.loit_chargeback = loit_chargeback
            rt.loit_showback = loit_showback
            rt.chargeback_rate = loit_total  # LOIT Total Chargeback
            rt.showback_rate = float(request.form.get('showback_rate', 0))  # LOLC Showback
            rt.showback_applied = request.form.get('showback_separate') == 'on'
            rt.base_cost = 0
            rt.tax_company_name = None
        
        db.session.add(rt)
        db.session.commit()
        
        flash(f'Resource type "{name}" created successfully for unit {unit.name}', 'success')
        return redirect(url_for('admin.manage_resource_types'))
    
    # GET request
    units = Unit.query.filter_by(is_active=True).all()
    tax_config = TaxFeeConfig.get_config()
    return render_template('admin/create_resource_type.html',
                         units=units,
                         tax_config=tax_config)

@admin_bp.route('/resource-types/<int:resource_id>/edit', methods=['GET', 'POST'])
@login_required
@admin_required
def edit_resource_type(resource_id):
    rt = ResourceType.query.get_or_404(resource_id)
    
    if request.method == 'POST':
        rt.name = request.form.get('name')
        rt.category = request.form.get('category', '')
        rt.description = request.form.get('description', '')
        rt.unit_id = request.form.get('unit_id')
        rt.unit_of_measure = request.form.get('unit_of_measure', '')
        rt.display_order = int(request.form.get('display_order', 0))
        rt.is_active = request.form.get('is_active') == 'on'
        rt.document_required = request.form.get('document_required') == 'on'
        
        unit = Unit.query.get(rt.unit_id)
        if not unit:
            flash('Invalid unit selected', 'danger')
            return redirect(url_for('admin.edit_resource_type', resource_id=resource_id))
        
        # Check for unique name within unit (excluding itself)
        existing_name = ResourceType.query.filter(
            ResourceType.name == rt.name, 
            ResourceType.unit_id == rt.unit_id, 
            ResourceType.id != resource_id
        ).first()
        
        if existing_name:
            flash(f'A resource type with the name "{rt.name}" already exists in the selected unit.', 'danger')
            return redirect(url_for('admin.edit_resource_type', resource_id=resource_id))
            
        # Update code if name changed
        new_code = generate_resource_code(rt.name)
        if new_code != rt.code:
            if ResourceType.query.filter_by(code=new_code).first():
                flash('Resource type code already exists', 'danger')
                return redirect(url_for('admin.edit_resource_type', resource_id=resource_id))
            rt.code = new_code
        
        # --- Tax logic ---
        tax_applied = request.form.get('tax_applied') == 'on'
        rt.tax_applied = tax_applied
        
        if tax_applied:
            tax_config = TaxFeeConfig.get_config()
            handling_fee = float(tax_config.handling_fee_percent)
            finance_fee = float(tax_config.finance_fee_percent)
            base_cost = float(request.form.get('base_cost', 0))
            tax_company_name = request.form.get('tax_company')
            
            if not tax_company_name:
                flash('Please select a company when Tax Applied is enabled', 'danger')
                return redirect(url_for('admin.edit_resource_type', resource_id=resource_id))
            
            rt.tax_company_name = tax_company_name
            rt.base_cost = base_cost
            
            if tax_company_name == 'LOLC Holdings':
                showback = base_cost + (base_cost * finance_fee / 100)
                chargeback = (showback * handling_fee / 100)
                rt.showback_applied = True
            else:  # LOLC Technologies
                chargeback = base_cost + (base_cost * handling_fee / 100)
                showback = 0
                rt.showback_applied = False
            
            # Set rates
            rt.chargeback_rate = chargeback  # LOIT Total Chargeback
            rt.showback_rate = showback      # LOLC Showback
            # Reset manual fields
            rt.loit_chargeback = 0
            rt.loit_showback = 0
            
        else:
            # MANUAL MODE - Get values from form
            loit_chargeback = float(request.form.get('loit_chargeback', 0))
            loit_showback = float(request.form.get('loit_showback', 0))
            loit_total = loit_chargeback + loit_showback
            
            rt.loit_chargeback = loit_chargeback
            rt.loit_showback = loit_showback
            rt.chargeback_rate = loit_total  # LOIT Total Chargeback
            rt.showback_rate = float(request.form.get('showback_rate', 0))  # LOLC Showback
            rt.showback_applied = request.form.get('showback_separate') == 'on'
            rt.base_cost = 0
            rt.tax_company_name = None
        
        db.session.commit()
        flash(f'Resource type "{rt.name}" updated successfully', 'success')
        return redirect(url_for('admin.manage_resource_types'))
    
    # GET request
    units = Unit.query.filter_by(is_active=True).all()
    tax_config = TaxFeeConfig.get_config()
    
    # Determine selected company for pre-filling radio buttons
    selected_company = None
    if rt.tax_applied and rt.tax_company_name:
        selected_company = rt.tax_company_name
    
    return render_template('admin/edit_resource_type.html',
                         resource=rt,
                         units=units,
                         tax_config=tax_config,
                         selected_company=selected_company)

@admin_bp.route('/resource-types/<int:resource_id>/toggle', methods=['POST'])
@login_required
@admin_required
def toggle_resource_type(resource_id):
    """Activate/deactivate resource type"""
    resource_type = ResourceType.query.get_or_404(resource_id)
    old_status = resource_type.is_active
    resource_type.is_active = not resource_type.is_active
    db.session.commit()
    
    # Log status change
    SystemAuditLogger.log(
        user=current_user,
        action_type='resource_type_status_changed',
        action_category='admin',
        target_type='resource_type',
        target_id=resource_type.id,
        target_name=resource_type.name,
        details={'old_status': old_status, 'new_status': resource_type.is_active},
        status='success'
    )
    
    status = 'activated' if resource_type.is_active else 'deactivated'
    flash(f'Resource type "{resource_type.name}" {status}', 'success')
    return redirect(url_for('admin.manage_resource_types'))


@admin_bp.route('/resource-types/<int:resource_id>/delete', methods=['POST'])
@login_required
@admin_required
def delete_resource_type(resource_id):
    """Delete resource type"""
    resource_type = ResourceType.query.get_or_404(resource_id)
    
    # Check if this resource type is being used in any service items
    used = False
    service_items = ServiceItem.query.all()
    for item in service_items:
        if item.resources:
            for resource in item.resources:
                if resource.get('type') == resource_type.code:
                    used = True
                    break
            if used: break
    
    if used:
        flash(f'Cannot delete resource type "{resource_type.name}" because it is used in existing services.', 'danger')
        return redirect(url_for('admin.manage_resource_types'))
    
    db.session.delete(resource_type)
    db.session.commit()
    flash(f'Resource type "{resource_type.name}" deleted successfully', 'success')
    return redirect(url_for('admin.manage_resource_types'))


@admin_bp.route('/api/resource-types', methods=['GET', 'POST'])
@login_required
def api_resource_types():
    """API endpoint to get all active resource types with full configuration"""
    if request.method == 'POST':
        if current_user.role != 'admin':
            return jsonify({'success': False, 'error': 'Admin privileges required'}), 403
            
        data = request.get_json()
        if not data or not data.get('name'):
            return jsonify({'success': False, 'error': 'Resource name is required'}), 400
            
        try:
            resource_id = data.get('id')
            if resource_id:
                rt = ResourceType.query.get_or_404(resource_id)
                is_new = False
            else:
                rt = ResourceType()
                is_new = True
                
            # Basic info
            rt.name = data.get('name')
            rt.description = data.get('description')
            rt.category = data.get('category')
           
            rt.unit_id = data.get('unit_id')
            
            # Code management - Calculate BEFORE adding to session to avoid autoflush errors
            new_code = data.get('code') or generate_resource_code(rt.name)
            if new_code != rt.code:
                existing = ResourceType.query.filter_by(code=new_code).first()
                if existing and existing.id != rt.id:
                    return jsonify({'success': False, 'error': f'Code "{new_code}" is already in use'}), 400
                rt.code = new_code

            if is_new:
                db.session.add(rt)

            # Rates and settings
            rt.chargeback_rate = float(data.get('chargeback_rate', 0))
            
            # Sync legacy field for backward compatibility
            rt.default_hourly_rate = rt.chargeback_rate
            
            rt.showback_applied = data.get('showback_separate', False)
            if rt.showback_applied:
                rt.showback_rate = float(data.get('showback_rate', 0))
            else:
                rt.showback_rate = 0.0   

            rt.unit_of_measure = data.get('unit_of_measure', '')
           
            rt.display_order = int(data.get('display_order', 0))
            rt.is_active = data.get('is_active', True)
            

            

           

            db.session.commit()
            
            SystemAuditLogger.log(
                user=current_user,
                action_type='resource_type_saved',
                action_category='admin',
                target_type='resource_type',
                target_id=rt.id,
                target_name=rt.name,
                details={'method': 'api', 'is_new': not resource_id},
                status='success'
            )
            
            return jsonify({'success': True, 'id': rt.id})
            
        except Exception as e:
            db.session.rollback()
            return jsonify({'success': False, 'error': str(e)}), 500

    resource_types = ResourceType.query.filter_by(is_active=True).order_by(ResourceType.display_order).all()
    
    result = []
    for rt in resource_types:
        rt_data = {
            'id': rt.id,
            'code': rt.code,
            'name': rt.name,
            'description': rt.description,
            'category': rt.category,
            'unit_id': rt.unit_id,
            # These fields are not in the provided app/models.py, so they are removed to avoid AttributeError.
           
            'showback_applied': rt.showback_applied,
           
            'is_active': rt.is_active,
            'display_order': rt.display_order,
            'chargeback_rate': float(rt.chargeback_rate),
            'showback_rate': float(rt.showback_rate),
            'unit_of_measure': rt.unit_of_measure,
           
        }
        
        # Backward compatibility for legacy frontend
        rate_type = 'flat'
        if rt.unit_of_measure and rt.unit_of_measure.lower() == 'hour':
            rate_type = 'hourly'

        rt_data.update({
            'rate_type': rate_type,
            'default_hourly_rate': float(rt.chargeback_rate) if rate_type == 'hourly' else 0,
            'default_flat_fee': float(rt.chargeback_rate) if rate_type == 'flat' else 0,
            'default_annual_cost': 0
        })
        
        result.append(rt_data)
    
    return jsonify(result)


@admin_bp.route('/units')
@login_required
@admin_required
def manage_units():
    """Manage units"""
    units = Unit.query.order_by(Unit.created_at.desc()).all()
    companies = Company.query.filter_by(is_active=True).all()
    users = User.query.filter(User.role.in_(['bu_user', 'head_of_it'])).all()
    return render_template('admin/units.html', 
                         units=units, 
                         companies=companies, 
                         users=users)


@admin_bp.route('/units/create', methods=['POST'])
@login_required
@admin_required
def create_unit():
    name = request.form.get('name')
    description = request.form.get('description')
    supervisor_id = request.form.get('supervisor_id')
    
    # Handle comma-separated IDs from the hidden input fields used in unit_create.html
    company_ids_raw = ",".join(request.form.getlist('company_ids'))
    company_ids = [cid.strip() for cid in company_ids_raw.split(',') if cid.strip()]
    member_ids_raw = ",".join(request.form.getlist('member_ids'))
    member_ids = [mid.strip() for mid in member_ids_raw.split(',') if mid.strip()]
    
    # Validate supervisor uniqueness (one supervisor per unit)
    if supervisor_id:
        existing_supervisor_unit = Unit.query.filter_by(supervisor_id=supervisor_id).first()
        if existing_supervisor_unit:
            flash(f'The selected supervisor is already assigned to unit "{existing_supervisor_unit.name}".', 'danger')
            return redirect(url_for('admin.create_unit_page'))
            
    if not name:
        flash('Unit name is required', 'danger')
        return redirect(url_for('admin.create_unit_page'))
    
    if not company_ids:
        flash('Please select at least one company', 'danger')
        return redirect(url_for('admin.create_unit_page'))
    
    unit = Unit(name=name, description=description, supervisor_id=supervisor_id or None)
    db.session.add(unit)
    db.session.flush()
    
    # Add companies
    for company_id in company_ids:
        company = Company.query.get(company_id)
        if company:
            unit.companies.append(company)
    
    # Add members
    for member_id in member_ids:
        user = User.query.get(member_id)
        if user and user.role == 'bu_user':
            unit_member = UnitMember(unit_id=unit.id, user_id=member_id)
            db.session.add(unit_member)
            user.unit_id = unit.id
    
    db.session.commit()
    flash(f'Unit "{name}" created successfully', 'success')
    return redirect(url_for('admin.manage_units'))

@admin_bp.route('/units/<int:unit_id>/edit', methods=['POST'])
@login_required
@admin_required
def edit_unit(unit_id):
    """Edit unit details INCLUDING members"""
    unit = Unit.query.get_or_404(unit_id)
    
    name = request.form.get('name')
    description = request.form.get('description')
    supervisor_id = request.form.get('supervisor_id')
    
    # Handle comma-separated IDs from the hidden input fields used in unit_edit.html
    company_ids_raw = ",".join(request.form.getlist('company_ids'))
    company_ids = [cid.strip() for cid in company_ids_raw.split(',') if cid.strip()]
    selected_member_ids_raw = ",".join(request.form.getlist('member_ids'))
    selected_member_ids = [mid.strip() for mid in selected_member_ids_raw.split(',') if mid.strip()]
    
    # Validate supervisor uniqueness (one supervisor per unit)
    if supervisor_id:
        existing_supervisor_unit = Unit.query.filter(Unit.supervisor_id == supervisor_id, Unit.id != unit_id).first()
        if existing_supervisor_unit:
            flash(f'The selected supervisor is already assigned to unit "{existing_supervisor_unit.name}".', 'danger')
            return redirect(url_for('admin.manage_units'))
            
    # Check for duplicate name
    existing = Unit.query.filter(Unit.name == name, Unit.id != unit_id).first()
    if existing:
        flash(f'Unit "{name}" already exists', 'danger')
        return redirect(url_for('admin.manage_units'))
    
    # Update basic info
    unit.name = name
    unit.description = description
    unit.supervisor_id = supervisor_id if supervisor_id else None
    
    # Update companies
    unit.companies.clear()
    for company_id in company_ids:
        company = Company.query.get(company_id)
        if company:
            unit.companies.append(company)
    
    # ========== UPDATE UNIT MEMBERS (BU USERS) ==========
    # Get existing members of this unit
    existing_members = UnitMember.query.filter_by(unit_id=unit_id).all()
    existing_member_ids = {str(m.user_id) for m in existing_members}
    selected_ids = set(selected_member_ids)
    
    # Add new members
    for user_id in selected_ids - existing_member_ids:
        # Check if user is already in another unit
        existing_in_other = UnitMember.query.filter(
            UnitMember.user_id == user_id,
            UnitMember.unit_id != unit_id,
            UnitMember.is_active == True
        ).first()
        
        if existing_in_other:
            user = User.query.get(user_id)
            flash(f'User "{user.full_name if user else user_id}" is already assigned to another unit. Please remove them from that unit first.', 'warning')
            continue
        
        unit_member = UnitMember(unit_id=unit.id, user_id=user_id)
        db.session.add(unit_member)
        
        # Update user's unit_id field for quick access
        user = User.query.get(user_id)
        if user and user.role == 'bu_user':
            user.unit_id = unit.id
    
    # Remove members not selected
    for member in existing_members:
        if str(member.user_id) not in selected_ids:
            db.session.delete(member)
            # Clear user's unit_id
            user = User.query.get(member.user_id)
            if user and user.unit_id == unit.id:
                user.unit_id = None
    
    db.session.commit()
    
    flash(f'Unit "{name}" updated successfully', 'success')
    return redirect(url_for('admin.manage_units'))





@admin_bp.route('/units/<int:unit_id>/members', methods=['POST'])
@login_required
@admin_required
def update_unit_members(unit_id):
    """Update unit members"""
    unit = Unit.query.get_or_404(unit_id)
    
    # Get selected member IDs
    selected_member_ids = set(request.form.getlist('member_ids'))
    
    # Get existing members
    existing_members = UnitMember.query.filter_by(unit_id=unit_id).all()
    existing_member_ids = {str(m.user_id) for m in existing_members}
    
    # Add new members
    for user_id in selected_member_ids - existing_member_ids:
        unit_member = UnitMember(unit_id=unit.id, user_id=user_id)
        db.session.add(unit_member)
        
        # Update user's unit_id
        user = User.query.get(user_id)
        if user and user.role == 'bu_user':
            user.unit_id = unit.id
    
    # Remove members not selected
    for member in existing_members:
        if str(member.user_id) not in selected_member_ids:
            db.session.delete(member)
            # Clear user's unit_id
            user = User.query.get(member.user_id)
            if user and user.unit_id == unit.id:
                user.unit_id = None
    
    db.session.commit()
    
    flash(f'Unit members updated successfully', 'success')
    return redirect(url_for('admin.manage_units'))


@admin_bp.route('/units/<int:unit_id>/toggle', methods=['POST'])
@login_required
@admin_required
def toggle_unit(unit_id):
    """Activate/deactivate unit"""
    unit = Unit.query.get_or_404(unit_id)
    old_status = unit.is_active
    unit.is_active = not unit.is_active
    db.session.commit()
    
    status = 'activated' if unit.is_active else 'deactivated'
    flash(f'Unit "{unit.name}" {status}', 'success')
    return redirect(url_for('admin.manage_units'))


@admin_bp.route('/units/<int:unit_id>/delete', methods=['POST'])
@login_required
@admin_required
def delete_unit(unit_id):
    """Delete unit (only if no members or time entries)"""
    unit = Unit.query.get_or_404(unit_id)
    
    # Check if unit has members
    member_count = UnitMember.query.filter_by(unit_id=unit_id).count()
    if member_count > 0:
        flash(f'Cannot delete unit "{unit.name}" because it has {member_count} members. Remove members first.', 'danger')
        return redirect(url_for('admin.manage_units'))
    
    # Check if unit has time entries
    users_in_unit = [m.user_id for m in UnitMember.query.filter_by(unit_id=unit_id).all()]
    if users_in_unit:
        time_entries = TimeEntry.query.filter(TimeEntry.user_id.in_(users_in_unit)).count()
        if time_entries > 0:
            flash(f'Cannot delete unit "{unit.name}" because it has associated time entries.', 'danger')
            return redirect(url_for('admin.manage_units'))
    
    name = unit.name
    db.session.delete(unit)
    db.session.commit()
    
    flash(f'Unit "{name}" deleted successfully', 'success')
    return redirect(url_for('admin.manage_units'))


@admin_bp.route('/api/unit-members/<int:unit_id>')
@login_required
@admin_required
def api_unit_members(unit_id):
    """API endpoint to get unit details"""
    unit = Unit.query.get_or_404(unit_id)
    members = UnitMember.query.filter_by(unit_id=unit_id, is_active=True).all()
    
    result = {
        'id': unit.id,
        'name': unit.name,
        'description': unit.description,
        'supervisor_id': unit.supervisor_id,
        'company_ids': [c.id for c in unit.companies],
        'member_ids': [m.user_id for m in members]
    }
    return jsonify(result)



@admin_bp.route('/units/create-page')
@login_required
@admin_required
def create_unit_page():
    """Create unit page - shows only unassigned BU users"""
    companies = Company.query.filter_by(is_active=True).all()
    
    # Get IDs of BU users who are already assigned to ANY unit
    assigned_user_ids = db.session.query(UnitMember.user_id).filter(
        UnitMember.is_active == True
    ).distinct().subquery()
    
    # Get IDs of users who are already supervisors of any unit
    assigned_supervisor_ids = db.session.query(Unit.supervisor_id).filter(
        Unit.supervisor_id.isnot(None)
    ).distinct().subquery()
    
    # Only show BU users who are NOT assigned to any unit yet
    unassigned_bu_users = User.query.filter(
        User.role == 'bu_user',
        User.is_active == True,
        User.id.notin_(assigned_user_ids)
    ).all()
    
    # For supervisor dropdown - only show users with supervisor role who aren't already assigned
    supervisor_candidates = User.query.filter(
        User.role == 'supervisor',
        User.is_active == True,
        User.id.notin_(assigned_supervisor_ids)
    ).all()
    
    return render_template('admin/unit_create.html', 
                         companies=companies,
                         bu_users=unassigned_bu_users,
                         supervisor_candidates=supervisor_candidates)


@admin_bp.route('/units/<int:unit_id>/edit-page')
@login_required
@admin_required
def edit_unit_page(unit_id):
    """Edit unit page - shows current members + unassigned BU users only"""
    unit = Unit.query.get_or_404(unit_id)
    companies = Company.query.filter_by(is_active=True).all()
    
    # Get current member IDs of THIS unit (only where user exists)
    current_member_ids = [m.user_id for m in unit.members if m.is_active and m.user_id]
    
    # Get IDs of BU users who are assigned to OTHER units (excluding this unit)
    other_assigned_ids = db.session.query(UnitMember.user_id).filter(
        UnitMember.unit_id != unit_id,
        UnitMember.is_active == True,
        UnitMember.user_id.isnot(None)
    ).distinct().subquery()
    
    # Available BU users = current members of this unit + unassigned users (not in any other unit)
    bu_users = User.query.filter(
        User.role == 'bu_user',
        User.is_active == True,
        db.or_(
            User.id.in_(current_member_ids),      # Already in this unit
            User.id.notin_(other_assigned_ids)    # Not assigned to any other unit
        )
    ).all()
    
    # Get IDs of users who are already supervisors of OTHER units
    other_supervisor_ids = db.session.query(Unit.supervisor_id).filter(
        Unit.id != unit_id,
        Unit.supervisor_id.isnot(None)
    ).distinct().subquery()
    
    # For supervisor dropdown - show users with supervisor role who aren't already assigned elsewhere
    supervisor_candidates = User.query.filter(
        User.role == 'supervisor',
        User.is_active == True,
        User.id.notin_(other_supervisor_ids)
    ).all()
    
    # Get all units for the dropdown (if needed elsewhere)
    units = Unit.query.filter_by(is_active=True).all()
    
    # Selected data for the template (with null checks)
    selected_company_ids = [c.id for c in unit.companies]
    selected_company_names = [c.name for c in unit.companies]
    selected_member_ids = [m.user_id for m in unit.members if m.is_active and m.user_id]
    
    # FIX: Only include members where user exists and user.full_name is accessible
    selected_member_names = []
    for m in unit.members:
        if m.is_active and m.user and m.user.full_name:
            selected_member_names.append(m.user.full_name)
        elif m.is_active and m.user:
            selected_member_names.append(m.user.email or f"User {m.user_id}")
        elif m.is_active:
            # Orphaned record - we can still show the ID
            selected_member_names.append(f"Unknown User (ID: {m.user_id})")

    return render_template('admin/unit_edit.html', 
                       unit=unit,
                       units=units,
                       companies=companies,
                       supervisor_candidates=supervisor_candidates,
                       bu_users=bu_users,
                       selected_company_ids=selected_company_ids,
                       selected_company_names=selected_company_names,
                       selected_member_ids=selected_member_ids,
                       selected_member_names=selected_member_names)


@admin_bp.route('/bulk-upload-resource-types', methods=['POST'])
@login_required
@admin_required
def bulk_upload_resource_types():
    """Process uploaded Excel file for resource types using openpyxl."""
    from openpyxl import load_workbook
    import re

    if 'excel_file' not in request.files:
        return jsonify({'success': False, 'error': 'No file uploaded'}), 400

    file = request.files['excel_file']
    if file.filename == '':
        return jsonify({'success': False, 'error': 'No file selected'}), 400

    if not file.filename.endswith(('.xlsx', '.xls')):
        return jsonify({'success': False, 'error': 'Invalid file type. Please upload .xlsx or .xls'}), 400

    try:
        wb = load_workbook(file)
        ws = wb.active
    except Exception as e:
        return jsonify({'success': False, 'error': f'Error reading file: {str(e)}'}), 400

    # ========== COLUMN MAPPING ==========
    col_map = {
        'Name': 1,
        'Code': 2,
        'Description': 3,
        'Category': 4,
        'Unit': 5,
        'Unit of Measure': 6,
        'LOIT Chargeback (Rs)': 7,
        'LOIT Showback (Rs)': 8,
        'Tax Applied': 9,
        'Tax Company': 10,
        'Base Cost': 11,
        'Chargeback Rate': 12,
        'Showback Applied': 13,
        'Showback Rate': 14,
        'Display Order': 15,
        'Is Active': 16,
        'Document Required': 17
    }

    # Validate headers
    headers = []
    for col_idx in range(1, 18):  # Now 17 columns (1-17)
        cell_value = ws.cell(row=1, column=col_idx).value
        if cell_value:
            headers.append(str(cell_value).strip())
    
    # Get expected headers
    expected_headers = list(col_map.keys())
    
    # Check for missing required columns (only Name and Unit are truly required)
    required_columns = ['Name', 'Unit']
    missing_required = [h for h in required_columns if h not in headers]
    
    if missing_required:
        return jsonify({
            'success': False, 
            'error': f'Missing required columns: {", ".join(missing_required)}. Please use the template.'
        }), 400
    
    # Warn about optional missing columns but don't fail
    optional_missing = [h for h in expected_headers if h not in headers and h not in required_columns]
    if optional_missing:
        print(f"Optional columns missing: {optional_missing}")

    # Pre-fetch units mapping
    units_dict = {unit.name: unit.id for unit in Unit.query.all()}

    # Get tax config for calculations
    tax_config = TaxFeeConfig.get_config()
    handling_fee = float(tax_config.handling_fee_percent)
    finance_fee = float(tax_config.finance_fee_percent)

    created = 0
    updated = 0
    errors = []

    # Start from row 2
    for row in range(2, ws.max_row + 1):
        row_num = row
        try:
            name = ws.cell(row=row, column=col_map['Name']).value
            if not name:
                errors.append(f"Row {row_num}: Name is required")
                continue
            name = str(name).strip()

            # Code
            code = ws.cell(row=row, column=col_map['Code']).value
            if code:
                code = str(code).strip()
            else:
                code = re.sub(r'[^a-z0-9]+', '_', name.lower())[:50]
            # Ensure uniqueness
            original_code = code
            counter = 1
            while ResourceType.query.filter_by(code=code).first():
                code = f"{original_code}_{counter}"
                counter += 1

            # Unit
            unit_name = ws.cell(row=row, column=col_map['Unit']).value
            if not unit_name:
                errors.append(f"Row {row_num}: Unit is required")
                continue
            unit_name = str(unit_name).strip()
            unit_id = units_dict.get(unit_name)
            if not unit_id:
                errors.append(f"Row {row_num}: Unit '{unit_name}' not found")
                continue

            # Unit of Measure
            uom_cell = ws.cell(row=row, column=col_map['Unit of Measure']).value
            uom = str(uom_cell).strip() if uom_cell else ''

            # ========== LOIT Chargeback & Showback ==========
            loit_chargeback = ws.cell(row=row, column=col_map['LOIT Chargeback (Rs)']).value or 0
            loit_showback = ws.cell(row=row, column=col_map['LOIT Showback (Rs)']).value or 0
            loit_chargeback = float(loit_chargeback) if loit_chargeback else 0
            loit_showback = float(loit_showback) if loit_showback else 0

            # ========== Tax Logic ==========
            tax_applied_val = ws.cell(row=row, column=col_map['Tax Applied']).value
            tax_applied = str(tax_applied_val).strip().lower() == 'yes' if tax_applied_val else False
            
            tax_company = ws.cell(row=row, column=col_map['Tax Company']).value
            tax_company = str(tax_company).strip() if tax_company else None
            
            base_cost_val = ws.cell(row=row, column=col_map['Base Cost']).value
            base_cost = float(base_cost_val) if base_cost_val else 0

            if tax_applied:
                if not tax_company:
                    errors.append(f"Row {row_num}: Tax Company is required when Tax Applied is Yes")
                    continue
                
                if tax_company == 'LOLC Holdings':
                    showback_rate = base_cost + (base_cost * finance_fee / 100)
                    chargeback_rate = (showback_rate * handling_fee / 100)
                    showback_applied = True
                else:  # LOLC Technologies
                    chargeback_rate = base_cost + (base_cost * handling_fee / 100)
                    showback_rate = 0
                    showback_applied = False
                
                # Override LOIT values when tax is applied
                loit_chargeback = 0
                loit_showback = 0
            else:
                # Manual entry - use LOIT values from template
                chargeback_rate = loit_chargeback + loit_showback
                
                showback_applied_val = ws.cell(row=row, column=col_map['Showback Applied']).value
                showback_applied = str(showback_applied_val).strip().lower() == 'yes' if showback_applied_val else False
                
                showback_rate_val = ws.cell(row=row, column=col_map['Showback Rate']).value
                showback_rate = float(showback_rate_val) if showback_rate_val else 0
                
                tax_company = None
                base_cost = 0

            # ========== Display Order ==========
            display_order_val = ws.cell(row=row, column=col_map['Display Order']).value
            display_order = int(display_order_val) if display_order_val else 0

            # ========== Is Active ==========
            is_active_val = ws.cell(row=row, column=col_map['Is Active']).value
            if is_active_val:
                is_active = str(is_active_val).strip().lower() in ['yes', 'true', '1', 'y', 'active']
            else:
                is_active = True

            # ========== Document Required ==========
            doc_required_val = ws.cell(row=row, column=col_map['Document Required']).value
            if doc_required_val:
                document_required = str(doc_required_val).strip().lower() in ['yes', 'true', '1', 'y']
            else:
                document_required = False

            # Description (optional)
            description_cell = ws.cell(row=row, column=col_map['Description']).value
            description = str(description_cell).strip() if description_cell else ''

            # Category (optional)
            category_cell = ws.cell(row=row, column=col_map['Category']).value
            category = str(category_cell).strip() if category_cell else ''

            # Check if already exists
            existing = ResourceType.query.filter_by(name=name).first()
            if existing:
                existing.code = code
                existing.description = description
                existing.category = category
                existing.unit_id = unit_id
                existing.unit_of_measure = uom
                
                existing.loit_chargeback = loit_chargeback
                existing.loit_showback = loit_showback
                existing.tax_applied = tax_applied
                existing.tax_company_name = tax_company
                existing.base_cost = base_cost
                existing.chargeback_rate = chargeback_rate
                existing.showback_applied = showback_applied
                existing.showback_rate = showback_rate
                existing.display_order = display_order
                existing.is_active = is_active
                existing.document_required = document_required  # <-- ADD THIS
                updated += 1
            else:
                rt = ResourceType(
                    name=name,
                    code=code,
                    description=description,
                    category=category,
                    unit_id=unit_id,
                    unit_of_measure=uom,
                    loit_chargeback=loit_chargeback,
                    loit_showback=loit_showback,
                    tax_applied=tax_applied,
                    tax_company_name=tax_company,
                    base_cost=base_cost,
                    chargeback_rate=chargeback_rate,
                    showback_applied=showback_applied,
                    showback_rate=showback_rate,
                    display_order=display_order,
                    is_active=is_active,
                    document_required=document_required  # <-- ADD THIS
                )
                db.session.add(rt)
                created += 1

        except Exception as e:
            errors.append(f"Row {row_num}: {str(e)}")
            continue

    try:
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'error': f'Database error: {str(e)}'}), 500

    return jsonify({
        'success': True,
        'created': created,
        'updated': updated,
        'errors': errors[:50]
    })

@admin_bp.route('/download-resource-template')
@login_required
@admin_required
def download_resource_template():
    """Download Excel template for bulk upload of resource types."""
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    from openpyxl.worksheet.datavalidation import DataValidation
    import io
    from flask import send_file

    wb = Workbook()
    ws = wb.active
    ws.title = "Resource Types Template"

    # Create a separate sheet for lists (dropdown sources)
    ws_lists = wb.create_sheet("Lists")

    # ========== Dropdown Lists ==========
    # Units
    units = Unit.query.filter_by(is_active=True).all()
    unit_names = [unit.name for unit in units]
    if unit_names:
        for idx, name in enumerate(unit_names, start=1):
            ws_lists.cell(row=idx, column=1, value=name)
        unit_range = f"Lists!$A$1:$A${len(unit_names)}"
    else:
        unit_range = None

    # Unit of Measure
    uom_options = ['Hour', 'User', 'Unit', 'Device', 'License', 'Service']
    for idx, uom in enumerate(uom_options, start=1):
        ws_lists.cell(row=idx, column=2, value=uom)
    uom_range = f"Lists!$B$1:$B${len(uom_options)}"

    # Yes/No options
    yn_options = ['Yes', 'No']
    for idx, yn in enumerate(yn_options, start=1):
        ws_lists.cell(row=idx, column=3, value=yn)
    yn_range = f"Lists!$C$1:$C${len(yn_options)}"

    # Categories
    categories = ['Infrastructure', 'Security', 'Database', 'BI', 'Network', 'Application', 'Consulting', 'Other']
    for idx, cat in enumerate(categories, start=1):
        ws_lists.cell(row=idx, column=4, value=cat)
    cat_range = f"Lists!$D$1:$D${len(categories)}"

    # Tax Companies
    tax_companies = ['LOLC Holdings', 'LOLC Technologies']
    for idx, company in enumerate(tax_companies, start=1):
        ws_lists.cell(row=idx, column=5, value=company)
    tax_company_range = f"Lists!$E$1:$E${len(tax_companies)}"

    # ========== COMPLETE HEADERS ==========
    headers = [
        'Name',
        'Code',
        'Description',
        'Category',
        'Unit',
        'Unit of Measure',
        'LOIT Chargeback (Rs)',
        'LOIT Showback (Rs)',
        'Tax Applied',
        'Tax Company',
        'Base Cost',
        'Chargeback Rate',
        'Showback Applied',
        'Showback Rate',
        'Display Order',
        'Is Active',
        'Document Required'
    ]

    header_fill = PatternFill(start_color="366092", end_color="366092", fill_type="solid")
    header_font = Font(color="FFFFFF", bold=True)
    header_alignment = Alignment(horizontal="center", vertical="center")
    thin_border = Border(
        left=Side(style='thin'),
        right=Side(style='thin'),
        top=Side(style='thin'),
        bottom=Side(style='thin')
    )

    for col, header in enumerate(headers, 1):
        cell = ws.cell(row=1, column=col, value=header)
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = header_alignment
        cell.border = thin_border

    # ========== Data Validation ==========
    # Unit (E)
    if unit_range:
        dv_unit = DataValidation(type="list", formula1=unit_range, allow_blank=False)
        ws.add_data_validation(dv_unit)
        for row in range(2, 102):
            dv_unit.add(f"E{row}")

    # Unit of Measure (F)
    dv_uom = DataValidation(type="list", formula1=uom_range, allow_blank=False)
    ws.add_data_validation(dv_uom)
    for row in range(2, 102):
        dv_uom.add(f"F{row}")

    # Tax Applied (I)
    dv_tax = DataValidation(type="list", formula1=yn_range, allow_blank=False)
    ws.add_data_validation(dv_tax)
    for row in range(2, 102):
        dv_tax.add(f"I{row}")

    # Tax Company (J)
    dv_tax_comp = DataValidation(type="list", formula1=tax_company_range, allow_blank=True)
    ws.add_data_validation(dv_tax_comp)
    for row in range(2, 102):
        dv_tax_comp.add(f"J{row}")

    # Showback Applied (M)
    dv_showback = DataValidation(type="list", formula1=yn_range, allow_blank=False)
    ws.add_data_validation(dv_showback)
    for row in range(2, 102):
        dv_showback.add(f"M{row}")

    # Is Active (P)
    dv_active = DataValidation(type="list", formula1=yn_range, allow_blank=False)
    ws.add_data_validation(dv_active)
    for row in range(2, 102):
        dv_active.add(f"P{row}")

    # Category (D)
    dv_cat = DataValidation(type="list", formula1=cat_range, allow_blank=True)
    ws.add_data_validation(dv_cat)
    for row in range(2, 102):
        dv_cat.add(f"D{row}")

    # Document Required (Q)
    dv_doc_required = DataValidation(type="list", formula1=yn_range, allow_blank=False)
    ws.add_data_validation(dv_doc_required)
    for row in range(2, 102):
        dv_doc_required.add(f"Q{row}")

    # ========== Example Row ==========
    example_row = 2
    ws.cell(row=example_row, column=1, value="Cloud Support")
    ws.cell(row=example_row, column=2, value="cloud_sup")
    ws.cell(row=example_row, column=3, value="24/7 Support for Cloud Services")
    ws.cell(row=example_row, column=4, value="Infrastructure")
    if unit_names:
        ws.cell(row=example_row, column=5, value=unit_names[0])
    ws.cell(row=example_row, column=6, value="Hour")
    ws.cell(row=example_row, column=7, value=100)          # LOIT Chargeback
    ws.cell(row=example_row, column=8, value=50)           # LOIT Showback
    ws.cell(row=example_row, column=9, value="Yes")
    ws.cell(row=example_row, column=10, value="LOLC Holdings")
    ws.cell(row=example_row, column=11, value=1000)
    ws.cell(row=example_row, column=12, value=150)         # Chargeback Rate
    ws.cell(row=example_row, column=13, value="No")
    ws.cell(row=example_row, column=14, value=0)
    ws.cell(row=example_row, column=15, value=1)
    ws.cell(row=example_row, column=16, value="Yes")
    ws.cell(row=example_row, column=17, value="Yes")       # Document Required

    # Apply borders to example row
    for col in range(1, len(headers) + 1):
        cell = ws.cell(row=example_row, column=col)
        cell.border = thin_border

    # ========== Hide Lists sheet ==========
    ws_lists.sheet_state = 'hidden'

    # Auto-size columns
    for col in ws.columns:
        max_length = 0
        col_letter = col[0].column_letter
        for cell in col:
            try:
                if cell.value:
                    max_length = max(max_length, len(str(cell.value)))
            except:
                pass
        adjusted_width = min(max_length + 2, 35)
        ws.column_dimensions[col_letter].width = adjusted_width

    output = io.BytesIO()
    wb.save(output)
    output.seek(0)

    return send_file(
        output,
        mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        as_attachment=True,
        download_name='resource_types_template.xlsx'
    )


# Add to app/routes/admin.py

from app.utils.cycle_helper import CycleHelper
from datetime import datetime


@admin_bp.route('/system-settings', methods=['GET', 'POST'])
@login_required
@admin_required
def system_settings():
    """Manage system settings - Auto-submit configuration only"""
    
    if request.method == 'POST':
        auto_submit_day = request.form.get('auto_submit_day', '')
        auto_submit_time = request.form.get('auto_submit_time', '')
        
        settings = {
            'auto_submit_day': auto_submit_day,
            'auto_submit_time': auto_submit_time
        }
        
        for key, value in settings.items():
            setting = SystemSetting.query.filter_by(setting_key=key).first()
            if setting:
                setting.setting_value = value
                setting.updated_at = datetime.utcnow()
            else:
                setting = SystemSetting(setting_key=key, setting_value=value)
                db.session.add(setting)
        
        db.session.commit()
        flash('System settings updated successfully!', 'success')
        return redirect(url_for('admin.system_settings'))
    
    def get_setting(key, default=None):
        setting = SystemSetting.query.filter_by(setting_key=key).first()
        return setting.setting_value if setting else default
    
    settings = {
        'auto_submit_day': get_setting('auto_submit_day', '26'),
        'auto_submit_time': get_setting('auto_submit_time', '10:00')
    }
    
    return render_template('admin/system_settings.html', 
                         settings=settings,
                         now=datetime.now())


def get_setting(key, default=None):
    """Get system setting value"""
    from app.models import SystemSetting
    setting = SystemSetting.query.filter_by(setting_key=key).first()
    return setting.setting_value if setting else default


@admin_bp.route('/force-auto-submit')
@login_required
@admin_required
def force_auto_submit():
    """Manually trigger auto-submit without date/time checks."""
    from app.utils.scheduler import create_batches_for_current_month
    create_batches_for_current_month()  # new function
    flash('Auto-submit manually triggered.', 'info')
    return redirect(url_for('admin.system_settings'))



from app.models import TaxFeeConfig

@admin_bp.route('/tax-fee-config', methods=['GET', 'POST'])
@login_required
@admin_required
def tax_fee_config():
    config = TaxFeeConfig.get_config()
    
    if request.method == 'POST':
        try:
            config.handling_fee_percent = float(request.form.get('handling_fee_percent', 0))
            config.finance_fee_percent = float(request.form.get('finance_fee_percent', 0))
            config.updated_by = current_user.id
            db.session.commit()
            flash('Tax & Fee configuration updated successfully!', 'success')
        except Exception as e:
            db.session.rollback()
            flash(f'Error: {str(e)}', 'danger')
        return redirect(url_for('admin.tax_fee_config'))
    
    return render_template('admin/tax_fee_config.html', config=config)
    