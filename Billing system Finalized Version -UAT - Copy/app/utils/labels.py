"""Display labels derived from company names and service resource data."""


def get_company_short_name(company_name):
    """First sentence of company name (text before the first period)."""
    if not company_name:
        return 'N/A'
    return str(company_name).split('.')[0].strip() or str(company_name).strip()


def get_company_showback_label(company_name, include_rs=False):
    """Dynamic showback column label, e.g. 'Browns Showback'."""
    short = get_company_short_name(company_name)
    label = f'{short} Showback'
    if include_rs:
        label = f'{label} (Rs)'
    return label


def get_company_showback_label_short(company_name, include_rs=False):
    """Abbreviated showback label, e.g. 'Browns SB'."""
    short = get_company_short_name(company_name)
    label = f'{short} SB'
    if include_rs:
        label = f'{label} (Rs)'
    return label


def combine_resource_descriptions(resources):
    """Build service-level description from per-resource descriptions."""
    if not resources:
        return 'N/A'
    parts = []
    for resource in resources:
        desc = (resource.get('description') or '').strip()
        if desc and desc not in parts:
            parts.append(desc)
    return ' | '.join(parts) if parts else 'N/A'


def get_company_showback_rate_label(company_name):
    return f'{get_company_short_name(company_name)} Showback Rate (Rs)'


def get_company_showback_amount_label(company_name):
    return f'{get_company_short_name(company_name)} Showback Amount (Rs)'


def resource_description(resource, fallback=''):
    """Description for a single resource, with optional fallback."""
    desc = (resource.get('description') or '').strip()
    return desc or fallback
