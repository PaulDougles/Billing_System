# app/routes/dashboard.py

from flask import Blueprint, render_template, redirect, url_for, session
from flask_login import login_required, current_user
from app.models import User, TimeEntry, ServiceItem, Company, Unit
from app.extensions import db
from datetime import datetime

dashboard_bp = Blueprint('dashboard', __name__)


@dashboard_bp.route('/')
@dashboard_bp.route('/dashboard')
@login_required
def index():
    """Main dashboard - shows different content based on role"""
    
    # Get active role from session (set during login for finance controller with admin access)
    active_role = session.get('active_role', current_user.role)
    
    # For Record Assistant - redirect to their dashboard (ADD THIS FIRST)
    if current_user.role == 'record_assistant':
        return redirect(url_for('record_assistant.dashboard'))
    
    # For admin or finance controller in admin mode - show system-wide statistics
    if active_role == 'admin' or current_user.role == 'admin':
        total_users = User.query.count()
        total_entries = TimeEntry.query.count()
        pending_approvals = TimeEntry.query.filter_by(status='submitted').count()
        
        # Calculate total revenue from invoiced entries
        invoiced_entries = TimeEntry.query.filter_by(status='invoiced').all()
        total_revenue = sum([e.get_total_amount() for e in invoiced_entries])
        
        # Count invoiced services
        invoiced_count = ServiceItem.query.filter_by(invoice_generated=True).count()
        
        # Get counts by role
        bu_users = User.query.filter_by(role='bu_user').count()
        head_of_it_users = User.query.filter_by(role='head_of_it').count()
        finance_controllers = User.query.filter_by(role='finance_controller').count()
        payment_teams = User.query.filter(User.role.like('payment_%')).count()
        supervisors = User.query.filter_by(role='supervisor').count()
        
        # Get companies count
        total_companies = Company.query.filter_by(is_active=True).count()
        active_companies = Company.query.filter_by(is_active=True).count()
        
        # Get units count
        total_units = Unit.query.count()
        
        return render_template('dashboard.html',
                             total_users=total_users,
                             total_entries=total_entries,
                             pending_approvals=pending_approvals,
                             total_revenue=total_revenue,
                             invoiced_count=invoiced_count,
                             bu_users=bu_users,
                             head_of_it_users=head_of_it_users,
                             finance_controllers=finance_controllers,
                             payment_teams=payment_teams,
                             supervisors=supervisors,
                             active_companies=active_companies, 
                             total_companies=total_companies,
                             total_units=total_units)
    
    # For Finance Controller in normal mode - redirect to their dashboard
    elif active_role == 'finance_controller' or current_user.role == 'finance_controller':
        return redirect(url_for('finance_controller.dashboard'))
    
    # For BU Team users - redirect to their dashboard
    elif current_user.role == 'bu_user':
        return redirect(url_for('bu_team.dashboard'))
    
    # For Head of IT - redirect to their dashboard
    elif current_user.role == 'head_of_it':
        return redirect(url_for('head_of_it.dashboard'))
    
    # For Supervisor - redirect to supervisor dashboard
    elif current_user.role == 'supervisor':
        return redirect(url_for('supervisor.dashboard'))
    
    elif current_user.role == 'super_admin':
        return redirect(url_for('payment.dashboard'))
    
    # For ALL Payment Team roles - redirect to payment dashboard
    elif current_user.role in ['payment_company_user']:
        return redirect(url_for('payment.company_dashboard'))
    
    # Default fallback for any other role
    else:
        return render_template('dashboard.html', 
                             total_users=0, 
                             total_entries=0, 
                             pending_approvals=0, 
                             total_revenue=0,
                             invoiced_count=0,
                             bu_users=0,
                             head_of_it_users=0,
                             finance_controllers=0,
                             payment_teams=0,
                             supervisors=0,
                             total_companies=0,
                             total_units=0)