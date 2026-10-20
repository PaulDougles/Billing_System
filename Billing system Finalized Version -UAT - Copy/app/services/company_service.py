from app.extensions import db
from app.models import Company

VALID_COMPANY_TYPES = {'parent', 'external'}


def get_parent_company():
    return Company.query.filter_by(is_parent_company=True).first()


def get_payment_team_for_company(company):
    if company is None:
        return None
    if company.company_type == 'parent':
        return 'payment_lolc_technologies'
    return 'payment_company_user'


def is_parent_company_user(user):
    return bool(
        user
        and user.company
        and (user.company.is_parent_company or user.company.company_type == 'parent')
    )


def can_access_company(user, company_id):
    if (
        not user
        or user.role != 'payment_company_user'
        or company_id is None
        or user.company_id is None
    ):
        return False
    return is_parent_company_user(user) or user.company_id == company_id


def list_companies(active_only=False):
    query = Company.query
    if active_only:
        query = query.filter_by(is_active=True)
    return query.order_by(Company.name).all()


def create_company(*, name, code=None, address=None, phone=None, email=None,
                   company_type='external', is_parent_company=False,
                   has_full_access=False):
    if not name or not name.strip():
        raise ValueError('Company name is required')
    if Company.query.filter_by(name=name.strip()).first():
        raise ValueError('Company already exists')
    if company_type == 'finance':
        company_type = 'external'
    if company_type not in VALID_COMPANY_TYPES:
        raise ValueError('Invalid company type')
    is_parent_company = is_parent_company or company_type == 'parent'
    if is_parent_company:
        company_type = 'parent'
    company = Company(
        name=name.strip(),
        code=code,
        address=address,
        phone=phone,
        email=email,
        company_type=company_type,
        is_parent_company=is_parent_company,
        has_full_access=is_parent_company,
    )
    db.session.add(company)
    db.session.commit()
    return company


def update_company(company, *, name, code=None, address=None, phone=None,
                   email=None, company_type=None, is_parent_company=None,
                   has_full_access=None):
    if not name or not name.strip():
        raise ValueError('Company name is required')
    duplicate = Company.query.filter(
        Company.name == name.strip(),
        Company.id != company.id,
    ).first()
    if duplicate:
        raise ValueError('Company already exists')
    if company_type is not None and company_type not in VALID_COMPANY_TYPES:
        if company_type == 'finance':
            company_type = 'external'
        else:
            raise ValueError('Invalid company type')
    company.name = name.strip()
    company.code = code
    company.address = address
    company.phone = phone
    company.email = email
    if company_type is not None:
        company.company_type = company_type
        company.is_parent_company = company_type == 'parent'
    elif is_parent_company is not None:
        company.is_parent_company = is_parent_company
    if company.is_parent_company:
        company.company_type = 'parent'
        company.has_full_access = True
    else:
        company.company_type = 'external'
        company.has_full_access = False
    db.session.commit()
    return company


def toggle_company_status(company):
    old_status = company.is_active
    company.is_active = not old_status
    db.session.commit()
    return old_status, company.is_active
