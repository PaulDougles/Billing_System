# app/utils/helpers.py
from app.models import User, Company, SystemSetting
from app.extensions import db


def get_setting(key, default=None):
    """Get system setting value"""
    from app.models import SystemSetting
    setting = SystemSetting.query.filter_by(setting_key=key).first()
    return setting.setting_value if setting else default


def create_initial_data(app):
    """Create ONLY admin user and system settings"""
    with app.app_context():
        # Create system settings if none exist
        if SystemSetting.query.count() == 0:
            default_settings = [
                ('auto_submit_day', '26', 'Day of month to auto-submit to Head of IT'),
                ('auto_submit_time', '01:00', 'Time to auto-submit (24hr format)'),
                ('weeks_per_month', '4', 'Number of weeks in a month'),
                ('week_lock_days', '7', 'Days after which week is locked'),
                ('week_start_day', 'Monday', 'First day of the week'),
                ('head_review_deadline_days', '5', 'Days Head of IT has to review')
            ]
            
            for key, value, desc in default_settings:
                setting = SystemSetting(setting_key=key, setting_value=value, description=desc)
                db.session.add(setting)
            
            db.session.commit()
            print("✓ Created default system settings")
        
        # Create admin user only if no users exist
        if User.query.count() == 0:
            import os
            initial_email = os.environ.get('INITIAL_ADMIN_EMAIL', 'admin@invoicesystem.com').strip()
            company = Company.query.first()
            
            admin = User(
                email=initial_email,
                employee_id='ADMIN001',
                nic='111111111V',
                company_id=company.id if company else None,
                first_name='Admin',
                last_name='User',
                role='admin',
                is_verified=True,
                is_active=True
            )
            db.session.add(admin)
            try:
                db.session.commit()
                print(f"✓ Created admin user: {initial_email}")
            except Exception as e:
                db.session.rollback()
                print(f"⚠️ Initial user creation notice: {e}")


def create_system_settings():
    """Empty - handled in create_initial_data"""
    pass