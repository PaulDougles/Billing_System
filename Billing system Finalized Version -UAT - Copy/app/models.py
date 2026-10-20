from app.extensions import db
from flask_login import UserMixin
from datetime import datetime, timedelta
import secrets
import hashlib

# ============ ASSOCIATION TABLES (Define FIRST) ============

# Association table for Unit-Company many-to-many relationship
unit_companies = db.Table('unit_companies',
    db.Column('unit_id', db.Integer, db.ForeignKey('units.id'), primary_key=True),
    db.Column('company_id', db.Integer, db.ForeignKey('companies.id'), primary_key=True)
)


# ============ COMPANY MODEL ============

class Company(db.Model):
    __tablename__ = 'companies'
    
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False, unique=True)
    code = db.Column(db.String(20), unique=True)
    address = db.Column(db.String(200))
    phone = db.Column(db.String(20))
    email = db.Column(db.String(120))
    is_active = db.Column(db.Boolean, default=True)
    is_parent_company = db.Column(db.Boolean, nullable=False, default=False)
    company_type = db.Column(db.String(50), nullable=False, default='external')
    has_full_access = db.Column(db.Boolean, nullable=False, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    
    @property
    def short_name(self):
        """First sentence of company name (text before the first period)."""
        if not self.name:
            return 'N/A'
        return self.name.split('.')[0].strip() or self.name.strip()
    
    @property
    def showback_label(self):
        return f'{self.short_name} Showback'
    
    @property
    def showback_label_short(self):
        return f'{self.short_name} '
    
    def __repr__(self):
        return f'<Company {self.name}>'


# ============ UNIT MODEL ============

class Unit(db.Model):
    __tablename__ = 'units'
    
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(50), nullable=False, unique=True)
    description = db.Column(db.String(200))
    
    # Unit has a supervisor
    supervisor_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    
    # Many-to-many relationship with companies - NO backref here
    companies = db.relationship('Company', secondary=unit_companies, backref='units_list')
    
    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    
    # Relationships
    supervisor = db.relationship('User', foreign_keys=[supervisor_id], backref='supervised_units')
    members = db.relationship('UnitMember', back_populates='unit', cascade='all, delete-orphan')
    
    def __repr__(self):
        return f'<Unit {self.name}>'
    
    @property
    def member_count(self):
        return UnitMember.query.filter_by(unit_id=self.id, is_active=True).count()
    
    @property
    def company_names(self):
        return ', '.join([c.name for c in self.companies])


# ============ USER MODEL ============

class User(UserMixin, db.Model):
    __tablename__ = 'users'
    
    id = db.Column(db.Integer, primary_key=True)
    
    # Personal Information
    email = db.Column(db.String(120), unique=True, nullable=False, index=True)
    employee_id = db.Column(db.String(50), unique=True, nullable=False, index=True)
    nic = db.Column(db.String(20), unique=True, nullable=False)
    company_id = db.Column(db.Integer, db.ForeignKey('companies.id'), nullable=True)
    first_name = db.Column(db.String(50), nullable=False)
    last_name = db.Column(db.String(50), nullable=False)
    
    # Role Management
    role = db.Column(db.String(50), nullable=False)
    
    # For BU users - they belong to a unit
    unit_id = db.Column(db.Integer, db.ForeignKey('units.id'), nullable=True)
    
    # Account Status
    is_active = db.Column(db.Boolean, default=True)
    is_verified = db.Column(db.Boolean, default=False)   # optional, can be used to track first login
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    last_login = db.Column(db.DateTime, nullable=True)
    
    # OTP Fields
    otp_code = db.Column(db.String(6), nullable=True)
    otp_expiry = db.Column(db.DateTime, nullable=True)
    otp_attempts = db.Column(db.Integer, default=0)
    
    # Relationships
    company = db.relationship('Company', foreign_keys=[company_id])
    unit = db.relationship('Unit', foreign_keys=[unit_id], backref='users')

    has_admin_access = db.Column(db.Boolean, default=False)  # Does this finance controller have admin access?
    preferred_role = db.Column(db.String(50), default=None)  # Store last used role preference
    
    def can_access_admin(self):
        """Check if user has admin access capability"""
        if self.role == 'admin':
            return True
        if self.role == 'finance_controller' and self.has_admin_access:
            return True
        return False
    
    def get_available_roles(self):
        """Get list of roles this user can access"""
        if self.role == 'finance_controller' and self.has_admin_access:
            return ['finance_controller', 'admin']
        return [self.role]
    
    def __repr__(self):
        return f'<User {self.email} - {self.role}>'
    
    @property
    def full_name(self):
        return f"{self.first_name} {self.last_name}"
    
    @property
    def username(self):
        return self.email.split('@')[0]
    
    @property
    def accessible_companies(self):
        """Get companies this user can access based on their unit"""
        if self.role == 'admin':
            return Company.query.filter_by(is_active=True).all()
        elif self.role == 'bu_user' and self.unit:
            return self.unit.companies
        elif self.role == 'head_of_it' and self.company_id:
            return [self.company]
        elif self.role == 'finance_controller':
            return Company.query.filter_by(is_active=True).all()
        elif self.role == 'record_assistant' and self.company_id:  
            return [self.company]
        elif (
            self.role == 'payment_company_user'
            and self.company
            and (self.company.is_parent_company or self.company.company_type == 'parent')
        ):
            return Company.query.all()
        elif self.role in ['payment_lolc_technologies', 'payment_company_user']:
            if self.company_id:
                return [self.company]
            return Company.query.filter_by(is_active=True).all()
        return []
    
    def generate_otp(self, expires_in=None):
        """Generate 6-digit OTP with optional expiry in minutes."""
        import secrets
        self.otp_code = f"{secrets.randbelow(900000) + 100000}"
        if expires_in is not None:
            self.otp_expiry = datetime.utcnow() + timedelta(minutes=expires_in)
        else:
            self.otp_expiry = None
        self.otp_attempts = 0
        db.session.commit()
        return self.otp_code
    
    def is_otp_expired(self):
        """Check if OTP has expired."""
        if self.otp_expiry is None:
            return False
        return datetime.utcnow() > self.otp_expiry
    
    def get_otp_remaining_time(self):
        """Get remaining time for OTP in seconds."""
        if self.otp_expiry is None:
            return 86400 * 365
        remaining = (self.otp_expiry - datetime.utcnow()).total_seconds()
        return max(0, int(remaining))
    
    def clear_expired_otp(self):
        """Clear OTP if expired."""
        if self.otp_expiry is not None and datetime.utcnow() > self.otp_expiry:
            self.otp_code = None
            self.otp_expiry = None
            self.otp_attempts = 0
            db.session.commit()
            return True
        return False
    
    def verify_otp(self, otp):
        """Verify OTP and log user in."""
        if not self.otp_code or not self.otp_expiry:
            return False
        if self.otp_attempts >= 5:
            return False
        if datetime.utcnow() > self.otp_expiry:
            return False
        if self.otp_code != otp:
            self.otp_attempts += 1
            db.session.commit()
            return False
        # OTP is valid
        self.otp_code = None
        self.otp_expiry = None
        self.otp_attempts = 0
        self.is_verified = True   # optional: mark as verified when first OTP used
        db.session.commit()
        return True
    
    def get_otp_remaining_attempts(self):
        """Get remaining OTP attempts"""
        return max(0, 5 - self.otp_attempts)
    
    def update_last_login(self):
        """Update last login timestamp."""
        self.last_login = datetime.utcnow()
        db.session.commit()

# ============ UNIT MEMBER MODEL ============

class UnitMember(db.Model):
    __tablename__ = 'unit_members'
    
    id = db.Column(db.Integer, primary_key=True)
    unit_id = db.Column(db.Integer, db.ForeignKey('units.id'), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    joined_at = db.Column(db.DateTime, default=datetime.utcnow)
    is_active = db.Column(db.Boolean, default=True)
    
    # Relationships
    unit = db.relationship('Unit', back_populates='members')
    user = db.relationship('User', foreign_keys=[user_id])
    
    def __repr__(self):
        return f'<UnitMember Unit:{self.unit_id} User:{self.user_id}>'


# ============ SYSTEM SETTINGS ============




# ============ TIME ENTRY MODELS ============

class TimeEntry(db.Model):
    __tablename__ = 'time_entries'
    
    id = db.Column(db.Integer, primary_key=True)
    
    # Basic Info
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    submission_date = db.Column(db.DateTime, default=datetime.utcnow)
    unit_id = db.Column(db.Integer, db.ForeignKey('units.id'), nullable=True)
    
    # Week Tracking
    week_number = db.Column(db.Integer, nullable=True)
    year = db.Column(db.Integer, nullable=True)
   
    is_locked = db.Column(db.Boolean, default=False)
    
    # Service Date (user selectable)
    service_date = db.Column(db.Date, nullable=True, default=datetime.utcnow)
    project_id_external = db.Column(db.String(100), nullable=True)
    project_name = db.Column(db.String(200), nullable=True)
    resources = db.Column(db.JSON, default=list)
    description = db.Column(db.Text, nullable=True)
    total_amount = db.Column(db.Numeric(15, 2), nullable=False, default=0)
    assigned_payment_team = db.Column(db.String(50), nullable=True)
    
    # Payment Team Assignment (if still needed at entry level – optional, can also be per service)
    
    
    # Invoice fields (entry‑level invoice flag might still be useful)
   
    
    # Legacy fields (keep for compatibility with old code)
    status = db.Column(db.String(20), default='draft')

    is_billable = db.Column(db.Boolean, default=True)
    invoice_generated = db.Column(db.Boolean, nullable=False, default=False)
    invoice_number = db.Column(db.String(50), nullable=True)
    invoice_pdf = db.Column(db.LargeBinary, nullable=True)
    invoice_pdf_filename = db.Column(db.String(255), nullable=True)
    invoice_generated_by = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    invoice_generated_at = db.Column(db.DateTime, nullable=True)

    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    # Relationships
    user = db.relationship('User', foreign_keys=[user_id])
    service_items = db.relationship('ServiceItem', back_populates='time_entry', lazy='dynamic', cascade='all, delete-orphan')
    
    def __repr__(self):
        return f'<TimeEntry {self.id} - Week {self.week_number}>'

    @property
    def date(self):
        return self.service_date

    @date.setter
    def date(self, value):
        self.service_date = value

    @property
    def created_by(self):
        return self.user

    @property
    def can_edit(self):
        """Check if entry can be edited (only lock status matters)"""
        return not self.is_locked
    
    @property
    def current_status(self):
        """Get current workflow status based on service items (aggregated)"""
        # This can be computed on the fly by checking all service items
        # For simplicity, return the entry's status field
        return self.status
    
    def get_total_amount(self):
        total = 0
        items = ServiceItem.query.filter_by(time_entry_id=self.id).all()
        for item in items:
            total += float(item.total_amount or 0)
        return total


class ServiceItem(db.Model):
    """Individual service item within a time entry"""
    __tablename__ = 'service_items'
    
    id = db.Column(db.Integer, primary_key=True)
    time_entry_id = db.Column(db.Integer, db.ForeignKey('time_entries.id', ondelete='CASCADE'), nullable=False)
    company_id = db.Column(db.Integer, db.ForeignKey('companies.id'), nullable=False)
    
    # Service details
    service_description = db.Column(db.Text, nullable=False)
    resources = db.Column(db.JSON, default=[])
    total_amount = db.Column(db.Numeric(10, 2), default=0)
    chargeback_total = db.Column(db.Numeric(15, 2), default=0)
    showback_total = db.Column(db.Numeric(15, 2), default=0)
    
    # Supervisor approval columns
    supervisor_approval_status = db.Column(db.String(20), default='pending')
    supervisor_comment = db.Column(db.Text, nullable=True)
    supervisor_approved_by = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    supervisor_approved_at = db.Column(db.DateTime, nullable=True)
    supervisor_rejected_by = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    supervisor_rejected_at = db.Column(db.DateTime, nullable=True)
    
    # Head of IT Approval
    head_approval_status = db.Column(db.String(20), default='pending')
    head_approval_comment = db.Column(db.Text, nullable=True)
    head_approved_by = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    head_approved_at = db.Column(db.DateTime, nullable=True)
    head_rejected_by = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    head_rejected_at = db.Column(db.DateTime, nullable=True)
    
    # Finance Controller Approval
    finance_approval_status = db.Column(db.String(20), default='pending')
    finance_approval_comment = db.Column(db.Text, nullable=True)
    finance_approved_by = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    finance_approved_at = db.Column(db.DateTime, nullable=True)
    finance_rejected_by = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    finance_rejected_at = db.Column(db.DateTime, nullable=True)
    
    # Payment Team Assignment
    assigned_payment_team = db.Column(db.String(50), nullable=True)
    
    # Invoice fields
    invoice_generated = db.Column(db.Boolean, default=False)
    invoice_generated_at = db.Column(db.DateTime, nullable=True)  # ADD THIS LINE
    invoice_generated_by = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    invoice_number = db.Column(db.String(50), nullable=True)
    invoice_pdf = db.Column(db.LargeBinary, nullable=True)
    invoice_pdf_filename = db.Column(db.String(255), nullable=True)
    
    
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    
    # Relationships
    company = db.relationship('Company', foreign_keys=[company_id])
    time_entry = db.relationship('TimeEntry', back_populates='service_items')
    
    def __repr__(self):
        return f'<ServiceItem {self.id}>'


# ============ RESOURCE TYPES ============

class ResourceType(db.Model):
    __tablename__ = 'resource_types'
    
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    code = db.Column(db.String(50), unique=True, nullable=False)
    description = db.Column(db.Text)
    category = db.Column(db.String(50))
    unit_id = db.Column(db.Integer, db.ForeignKey('units.id'), nullable=False)

    # Relationships
    unit = db.relationship('Unit', foreign_keys=[unit_id], backref='resource_types')

    tax_applied = db.Column(db.Boolean, default=False)
    tax_company_name = db.Column(db.String(50), nullable=True) 
    base_cost = db.Column(db.Numeric(15,2), default=0.0)
    
    # NEW FIELDS for manual mode (LOIT components)
    loit_chargeback = db.Column(db.Numeric(15,2), default=0.0)  
    loit_showback = db.Column(db.Numeric(15,2), default=0.0)    
    
    # For hourly/fixed/per_user/per_unit simple models
    chargeback_rate = db.Column(db.Numeric(15,2), default=0)    
    showback_rate = db.Column(db.Numeric(15,2), default=0)      
    showback_applied = db.Column(db.Boolean, default=False)
    unit_of_measure = db.Column(db.String(50))

    document_required = db.Column(db.Boolean, default=False)  
    
    is_active = db.Column(db.Boolean, default=True)
    display_order = db.Column(db.Integer, default=0)

    # Timestamps
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    def __repr__(self):
        return f'<ResourceType {self.code} - {self.name}>'
    
    @property
    def current_rate(self):
        """Get current rate (backward compatibility)"""
        return float(self.chargeback_rate)




class Document(db.Model):
    __tablename__ = 'documents'
    
    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey('companies.id'), nullable=False)
    service_item_id = db.Column(db.Integer, db.ForeignKey('service_items.id'), nullable=True)
    resource_id = db.Column(db.String(100), nullable=True)
    uploaded_by = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    

    original_filename = db.Column(db.String(255), nullable=False)
    
    # NEW: store binary content directly in DB
    file_data = db.Column(db.LargeBinary, nullable=False)   # <-- ADD THIS
    
    # Keep these for metadata (optional, you can keep file_path but make it nullable)
    file_path = db.Column(db.String(500), nullable=True) # <-- make nullable
    file_size = db.Column(db.Integer, default=0)
    file_type = db.Column(db.String(100), nullable=False)
    
    
    version = db.Column(db.Integer, default=1)
    is_active = db.Column(db.Boolean, default=True)
    
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    uploader = db.relationship('User', foreign_keys=[uploaded_by])
    
   
    
    def __repr__(self):
        return f'<Document {self.document_name}>'



# ============ AUDIT MODELS ============

# In models.py - ActivityLog is already there, but let's ensure it has all fields
class ActivityLog(db.Model):
    __tablename__ = 'activity_logs'
    
    id = db.Column(db.Integer, primary_key=True)
    time_entry_id = db.Column(db.Integer, db.ForeignKey('time_entries.id', ondelete='CASCADE'), nullable=False)
    service_item_id = db.Column(db.Integer, db.ForeignKey('service_items.id'), nullable=True)  # NEW
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    action = db.Column(db.String(50), nullable=False)
    status_from = db.Column(db.String(50), nullable=True)  # NEW
    status_to = db.Column(db.String(50), nullable=True)    # NEW
    details = db.Column(db.JSON, nullable=True)
    ip_address = db.Column(db.String(45), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    
    # Relationships
    time_entry = db.relationship('TimeEntry', foreign_keys=[time_entry_id])
    service_item = db.relationship('ServiceItem', foreign_keys=[service_item_id])  # NEW
    user = db.relationship('User', foreign_keys=[user_id])


class Notification(db.Model):
    __tablename__ = 'notifications'
    
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    time_entry_id = db.Column(db.Integer, db.ForeignKey('time_entries.id', ondelete='CASCADE'), nullable=False)
    notification_type = db.Column(db.String(50), nullable=False)
    subject = db.Column(db.String(200), nullable=False)
    message = db.Column(db.Text, nullable=False)
    sent_at = db.Column(db.DateTime, default=datetime.utcnow)
    read_at = db.Column(db.DateTime, nullable=True)
    
    def __repr__(self):
        return f'<Notification {self.notification_type}>'


class SystemAudit(db.Model):
    """Audit trail for ALL system activities"""
    __tablename__ = 'system_audits'
    
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    user_email = db.Column(db.String(120))
    user_role = db.Column(db.String(50))
    action_type = db.Column(db.String(50), nullable=False)
    action_category = db.Column(db.String(30), nullable=False)
    target_type = db.Column(db.String(50))
    target_id = db.Column(db.Integer)
    target_name = db.Column(db.String(200))
    details = db.Column(db.JSON)
    ip_address = db.Column(db.String(45))
    user_agent = db.Column(db.String(500))
    status = db.Column(db.String(20), default='success')
    error_message = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    
    user = db.relationship('User', foreign_keys=[user_id])
    
    def __repr__(self):
        return f'<SystemAudit {self.action_type}>'






class MonthlyBatch(db.Model):
    """Track monthly batches sent to Head of IT"""
    __tablename__ = 'monthly_batches'
    
    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey('companies.id'), nullable=False)
    year = db.Column(db.Integer, nullable=False)
    month = db.Column(db.Integer, nullable=False)
    batch_date = db.Column(db.DateTime, default=datetime.utcnow)
    status = db.Column(db.String(20), default='pending')  # pending, sent_to_head, head_approved, head_rejected
    sent_to_head_at = db.Column(db.DateTime, nullable=True)
    head_approved_at = db.Column(db.DateTime, nullable=True)
    head_rejected_at = db.Column(db.DateTime, nullable=True)
    
    # Relationships
    company = db.relationship('Company', foreign_keys=[company_id])
    entries = db.relationship('MonthlyBatchEntry', back_populates='batch', cascade='all, delete-orphan')
    
    def __repr__(self):
        return f'<MonthlyBatch {self.company.name} - {self.year}/{self.month}>'


class MonthlyBatchEntry(db.Model):
    """Individual time entries within a monthly batch"""
    __tablename__ = 'monthly_batch_entries'
    
    id = db.Column(db.Integer, primary_key=True)
    batch_id = db.Column(db.Integer, db.ForeignKey('monthly_batches.id'), nullable=False)
    time_entry_id = db.Column(db.Integer, db.ForeignKey('time_entries.id'), nullable=False)
    week_number = db.Column(db.Integer, nullable=False)
    status = db.Column(db.String(20), default='pending')  # pending, approved, rejected
    
    # Relationships
    batch = db.relationship('MonthlyBatch', back_populates='entries')
    time_entry = db.relationship('TimeEntry')
    
    def __repr__(self):
        return f'<MonthlyBatchEntry Batch:{self.batch_id} Entry:{self.time_entry_id}>'


class SystemSetting(db.Model):
    """System settings - already exists, add these keys"""
    __tablename__ = 'system_settings'
    
    id = db.Column(db.Integer, primary_key=True)
    setting_key = db.Column(db.String(100), unique=True, nullable=False)
    setting_value = db.Column(db.Text, nullable=False)
    description = db.Column(db.Text)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    def __repr__(self):
        return f'<SystemSetting {self.setting_key}={self.setting_value}>'
    

class MonthlyMasterInvoice(db.Model):
    __tablename__ = 'monthly_master_invoices'
    
    id = db.Column(db.Integer, primary_key=True)
    year = db.Column(db.Integer, nullable=False)
    month = db.Column(db.Integer, nullable=False)  # 1-12
    invoice_number = db.Column(db.String(50), unique=True, nullable=False)
    invoice_pdf = db.Column(db.LargeBinary, nullable=True)
    pdf_filename = db.Column(db.String(255), nullable=True)
    generated_by = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    generated_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    total_amount = db.Column(db.Numeric(15, 2), default=0)
    period_start = db.Column(db.Date, nullable=True)
    period_end = db.Column(db.Date, nullable=True)
    
    generator = db.relationship('User', foreign_keys=[generated_by])
    
    def __repr__(self):
        return f'<MonthlyMasterInvoice {self.invoice_number}>'



class TaxFeeConfig(db.Model):
    """Global tax and fee configuration (single row)"""
    __tablename__ = 'tax_fee_config'
    
    id = db.Column(db.Integer, primary_key=True)
    handling_fee_percent = db.Column(db.Numeric(5,2), default=0.0, nullable=False)
    finance_fee_percent = db.Column(db.Numeric(5,2), default=0.0, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    updated_by = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    
    @classmethod
    def get_config(cls):
        config = cls.query.first()
        if not config:
            config = cls(handling_fee_percent=0.0, finance_fee_percent=0.0)
            db.session.add(config)
            db.session.commit()
        return config