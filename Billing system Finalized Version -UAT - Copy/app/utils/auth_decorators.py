from functools import wraps

from flask import flash, jsonify, redirect, request, url_for
from flask_login import current_user


def role_required(*roles):
    allowed_roles = frozenset(roles)

    def decorator(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            json_request = (
                request.is_json
                or request.path.startswith('/api/')
                or request.accept_mimetypes.best == 'application/json'
            )
            if not current_user.is_authenticated:
                if json_request:
                    return jsonify({'success': False, 'error': 'Authentication required'}), 401
                flash('Please log in.', 'danger')
                return redirect(url_for('auth.login'))
            if current_user.role not in allowed_roles:
                if json_request:
                    return jsonify({'success': False, 'error': 'Unauthorized'}), 403
                flash('Access denied.', 'danger')
                return redirect(url_for('dashboard.index'))
            return view(*args, **kwargs)
        return wrapped
    return decorator
