from datetime import datetime

from app.extensions import db
from app.models import TimeEntry


def list_user_time_entries(user_id, limit=50):
    return (
        TimeEntry.query.filter_by(user_id=user_id)
        .order_by(TimeEntry.created_at.desc())
        .limit(limit)
        .all()
    )


def create_time_entry(user_id, data):
    entry = TimeEntry(
        user_id=user_id,
        service_date=datetime.now().date(),
        submission_date=datetime.utcnow(),
        project_id_external=data.get('project_id_external', ''),
        project_name=data.get('project_name', ''),
        resources=data.get('resources', []),
        description=data.get('description', ''),
        is_billable=data.get('is_billable', True),
        total_amount=data.get('total_amount', 0),
    )
    db.session.add(entry)
    db.session.commit()
    return entry


def delete_time_entry(entry):
    db.session.delete(entry)
    db.session.commit()
