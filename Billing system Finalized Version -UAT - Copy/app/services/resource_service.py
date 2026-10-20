from app.models import ResourceType


def list_resource_types(unit_id=None, active_only=False):
    query = ResourceType.query
    if unit_id is not None:
        query = query.filter_by(unit_id=unit_id)
    if active_only and hasattr(ResourceType, 'is_active'):
        query = query.filter_by(is_active=True)
    return query.order_by(ResourceType.name).all()


def calculate_resource_rates(resources, resource_types):
    resource_by_id = {str(item.id): item for item in resource_types}
    totals = {'chargeback': 0.0, 'showback': 0.0}
    details = []
    for resource in resources or []:
        resource_type = resource_by_id.get(str(resource.get('type', '')))
        quantity = float(resource.get('quantity') or resource.get('hours') or 0)
        chargeback_rate = float(resource_type.chargeback_rate or 0) if resource_type else 0.0
        showback_rate = float(resource_type.showback_rate or 0) if resource_type else 0.0
        chargeback = quantity * chargeback_rate
        showback = quantity * showback_rate
        totals['chargeback'] += chargeback
        totals['showback'] += showback
        details.append({
            'resource_type': resource_type,
            'quantity': quantity,
            'chargeback_rate': chargeback_rate,
            'showback_rate': showback_rate,
            'chargeback_amount': chargeback,
            'showback_amount': showback,
        })
    return {'totals': totals, 'details': details}
