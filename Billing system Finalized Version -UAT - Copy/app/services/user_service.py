import re

from app.extensions import db
from app.models import UnitMember, User


EMAIL_PATTERN = re.compile(r'^[^\s@]+@([^\s@]+\.)+[^\s@]+$')


def validate_user_fields(email, employee_id, nic, first_name, last_name, role):
    if not all((email, employee_id, nic, first_name, last_name, role)):
        return 'All fields are required'
    if not EMAIL_PATTERN.match(email):
        return 'Please enter a valid email address'
    existing = User.query.filter(
        (User.email == email) |
        (User.employee_id == employee_id) |
        (User.nic == nic)
    ).first()
    if existing:
        return 'User with this email, employee ID, or NIC already exists'
    return None


def add_user_to_session(*, email, employee_id, nic, first_name, last_name, role,
                        company_id=None, unit_id=None, has_admin_access=False):
    validation_error = validate_user_fields(
        email, employee_id, nic, first_name, last_name, role
    )
    if validation_error:
        raise ValueError(validation_error)
    user = User(
        email=email,
        employee_id=employee_id,
        nic=nic,
        first_name=first_name,
        last_name=last_name,
        role=role,
        company_id=company_id,
        unit_id=unit_id,
        has_admin_access=has_admin_access if role == 'finance_controller' else False,
        is_verified=False,
        is_active=True,
    )
    db.session.add(user)
    db.session.flush()
    if role == 'bu_user' and unit_id:
        db.session.add(UnitMember(unit_id=unit_id, user_id=user.id))
    return user


def create_user(*, email, employee_id, nic, first_name, last_name, role,
                company_id=None, unit_id=None, has_admin_access=False):
    user = add_user_to_session(
        email=email,
        employee_id=employee_id,
        nic=nic,
        first_name=first_name,
        last_name=last_name,
        role=role,
        company_id=company_id,
        unit_id=unit_id,
        has_admin_access=has_admin_access,
    )
    db.session.commit()
    return user
