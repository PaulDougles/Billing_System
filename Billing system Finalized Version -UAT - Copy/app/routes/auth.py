# app/routes/auth.py

from flask import Blueprint, render_template, redirect, url_for, request, flash, session, jsonify
from flask_login import login_user, logout_user, login_required, current_user
from app.models import User
from app.extensions import db
from app.utils.email_helper import send_otp_email
from datetime import datetime

auth_bp = Blueprint('auth', __name__)


@auth_bp.route('/login', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated:
        return redirect(url_for('dashboard.index'))

    if request.method == 'POST':
        email = request.form.get('email', '').strip()
        if not email:
            flash('Please enter your email address.', 'danger')
            return redirect(url_for('auth.login'))

        user = User.query.filter_by(email=email).first()
        if not user or not user.is_active:
            flash('No active account found with that email.', 'danger')
            return redirect(url_for('auth.login'))

        # Store email in session
        session['login_email'] = email
        
        # Check if user has multiple roles available (finance controller with admin access)
        available_roles = user.get_available_roles()
        
        if len(available_roles) > 1:
            # Finance controller has admin access - show role selection page
            session['user_id_for_roles'] = user.id
            session['available_roles'] = available_roles
            return redirect(url_for('auth.select_role'))
        else:
            # Single role - send OTP directly
            otp = user.generate_otp(expires_in=10)
            session['otp_email'] = email
            session['selected_role'] = user.role
            if send_otp_email(user, otp):
                flash('A one‑time passcode has been sent to your email.', 'info')
            else:
                flash('Email delivery could not be confirmed. Check your inbox or use Resend OTP.', 'warning')
            return redirect(url_for('auth.verify_otp'))

    return render_template('auth/login.html')


@auth_bp.route('/select-role', methods=['GET', 'POST'])
def select_role():
    """Page for finance controller to select which role to use (only shown if user has admin access)"""
    if current_user.is_authenticated:
        return redirect(url_for('dashboard.index'))
    
    email = session.get('login_email')
    available_roles = session.get('available_roles', [])
    user_id = session.get('user_id_for_roles')
    
    if not email or not available_roles:
        flash('Session expired. Please login again.', 'warning')
        return redirect(url_for('auth.login'))
    
    user = User.query.get(user_id)
    if not user:
        flash('User not found. Please login again.', 'danger')
        return redirect(url_for('auth.login'))
    
    if request.method == 'POST':
        selected_role = request.form.get('selected_role')
        
        if selected_role not in available_roles:
            flash('Invalid role selected.', 'danger')
            return redirect(url_for('auth.select_role'))
        
        # Store selected role in session
        session['selected_role'] = selected_role
        
        # Generate OTP and continue
        otp = user.generate_otp(expires_in=10)
        session['otp_email'] = email
        if send_otp_email(user, otp):
            flash(f'Role selected: {selected_role.title()}. Please check your email for OTP.', 'info')
        else:
            flash('Email delivery could not be confirmed. Check your inbox or use Resend OTP.', 'warning')
        return redirect(url_for('auth.verify_otp'))
    
    # Role descriptions for display
    role_descriptions = {
        'finance_controller': {
            'title': 'Finance Controller',
            'icon': 'bi-calculator-fill',
            'color': '#10B981',
            'description': 'Manage financial approvals, review service charges, and oversee billing processes.',
            'badge': 'Primary Role'
        },
        'admin': {
            'title': 'System Administrator',
            'icon': 'bi-shield-lock-fill',
            'color': '#EF4444',
            'description': 'Full system access: manage users, companies, units, resource types, and system settings.',
            'badge': 'Elevated Access'
        }
    }
    
    return render_template('auth/select_role.html', 
                         email=email,
                         available_roles=available_roles,
                         role_descriptions=role_descriptions,
                         user=user)


@auth_bp.route('/verify-otp', methods=['GET', 'POST'])
def verify_otp():
    if current_user.is_authenticated:
        return redirect(url_for('dashboard.index'))

    email = session.get('otp_email')
    if not email:
        flash('Please start login again.', 'warning')
        return redirect(url_for('auth.login'))

    user = User.query.filter_by(email=email).first()
    if not user:
        session.pop('otp_email', None)
        flash('Invalid email. Please login again.', 'danger')
        return redirect(url_for('auth.login'))

    if request.method == 'POST':
        otp = request.form.get('otp', '').strip()
        if not otp:
            flash('Please enter the OTP.', 'danger')
            return render_template('auth/verify_otp.html', email=email)

        if user.verify_otp(otp):
            # Get the selected role from session
            selected_role = session.get('selected_role', user.role)
            
            # Login the user
            login_user(user)
            user.update_last_login()
            
            # Store the active role in session
            session['active_role'] = selected_role
            
            # Clean up login-related session variables
            for key in ['otp_email', 'selected_role', 'login_email', 'available_roles', 'user_id_for_roles']:
                session.pop(key, None)
            
            # Redirect to central dashboard index which handles role-based routing correctly.
            # This prevents users from being sent to restricted dashboards (like finance) 
            # that would flash "Access denied" before they eventually reach their destination.
            return redirect(url_for('dashboard.index'))
        else:
            remaining_attempts = user.get_otp_remaining_attempts()
            if remaining_attempts <= 0:
                session.pop('otp_email', None)
                flash('Too many failed attempts. Please request a new OTP.', 'danger')
                return redirect(url_for('auth.login'))
            flash(f'Invalid OTP. {remaining_attempts} attempt(s) remaining.', 'danger')
            return render_template('auth/verify_otp.html', email=email)

    return render_template('auth/verify_otp.html', email=email)


@auth_bp.route('/switch-role')
@login_required
def switch_role():
    """Switch role during active session (for finance controller with admin access)"""
    current_role = session.get('active_role', current_user.role)
    
    # Check if user can switch roles
    if current_user.role == 'finance_controller' and hasattr(current_user, 'has_admin_access') and current_user.has_admin_access:
        if current_role == 'finance_controller':
            session['active_role'] = 'admin'
            flash('Switched to Admin mode. You now have full system access.', 'info')
            return redirect(url_for('dashboard.index'))
        else:
            session['active_role'] = 'finance_controller'
            flash('Switched to Finance Controller mode.', 'info')
            return redirect(url_for('finance_controller.dashboard'))
    
    flash('You do not have permission to switch roles.', 'danger')
    return redirect(url_for('dashboard.index'))


@auth_bp.route('/resend-otp', methods=['POST'])
def resend_otp():
    email = request.form.get('email')
    if not email:
        return jsonify({'error': 'Email required'}), 400
    user = User.query.filter_by(email=email).first()
    if not user or not user.is_active:
        return jsonify({'error': 'User not found'}), 404
    # Generate new OTP
    otp = user.generate_otp(expires_in=10)
    if send_otp_email(user, otp):
        session['otp_email'] = email
        return jsonify({'success': True})
    else:
        return jsonify({'error': 'Failed to send OTP'}), 500


@auth_bp.route('/logout')
@login_required
def logout():
    # Clear session
    session.clear()
    logout_user()
    flash('You have been logged out.', 'info')
    return redirect(url_for('auth.login'))


@auth_bp.route('/profile')
@login_required
def profile():
    """View user profile (read‑only, no password change)"""
    return render_template('auth/profile.html', user=current_user)