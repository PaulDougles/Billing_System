from flask import Blueprint, render_template, request, redirect, url_for, flash
from flask_login import login_required, current_user
from app.models import Project, TimeEntry
from app.extensions import db
from datetime import datetime

time_tracking_bp = Blueprint('time_tracking', __name__)

@time_tracking_bp.route('/entry')
@login_required
def entry():
    """Page for entering new time entries"""
    projects = Project.query.filter_by(status='active').all()
    return render_template('time_tracking/entry.html', projects=projects)


@time_tracking_bp.route('/create', methods=['POST'])
@login_required
def create():
    """Create a new time entry"""
    project_id = request.form.get('project_id')
    date = datetime.strptime(request.form.get('date'), '%Y-%m-%d').date()
    hours = float(request.form.get('hours'))
    description = request.form.get('description')
    is_billable = 'is_billable' in request.form
    
    if not project_id:
        flash('Please select a project', 'danger')
        return redirect(url_for('time_tracking.entry'))
    
    if hours <= 0:
        flash('Hours must be greater than 0', 'danger')
        return redirect(url_for('time_tracking.entry'))
    
    # Get project to verify it exists
    project = Project.query.get_or_404(project_id)
    
    # Create time entry
    entry = TimeEntry(
        user_id=current_user.id,
        project_id=project_id,
        date=date,
        hours=hours,
        description=description,
        is_billable=is_billable
    )
    
    db.session.add(entry)
    db.session.commit()
    
    flash(f'Time entry logged successfully! {hours} hours at Rs {project.hourly_rate}/hr', 'success')
    return redirect(url_for('time_tracking.list'))


@time_tracking_bp.route('/list')
@login_required
def list():
    """Page for viewing all time entries"""
    entries = TimeEntry.query\
        .filter_by(user_id=current_user.id)\
        .order_by(TimeEntry.date.desc())\
        .all()
    
    # Calculate totals
    total_hours = sum(float(e.hours) for e in entries)
    total_amount = sum(e.amount for e in entries)
    
    return render_template('time_tracking/list.html', 
                         entries=entries,
                         total_hours=total_hours,
                         total_amount=total_amount)


@time_tracking_bp.route('/<int:entry_id>/delete', methods=['POST'])
@login_required
def delete(entry_id):
    """Delete a time entry"""
    entry = TimeEntry.query.get_or_404(entry_id)
    
    if entry.user_id != current_user.id:
        flash('You can only delete your own entries', 'danger')
        return redirect(url_for('time_tracking.list'))
    
    if entry.invoice_id:
        flash('Cannot delete entry that is already invoiced', 'danger')
        return redirect(url_for('time_tracking.list'))
    
    db.session.delete(entry)
    db.session.commit()
    
    flash('Time entry deleted successfully', 'success')
    return redirect(url_for('time_tracking.list'))