from app.models import SystemAudit
from app.extensions import db
from flask import request
from datetime import datetime
import traceback


class SystemAuditLogger:
    """Centralized audit logging for system activities"""
    
    @staticmethod
    def log(user, action_type, action_category, target_type=None, target_id=None, 
            target_name=None, details=None, status='success', error_message=None):
        """Log any system activity"""
        try:
            audit = SystemAudit(
                user_id=user.id if user else None,
                user_email=user.email if user else 'system',
                user_role=user.role if user else 'system',
                action_type=action_type,
                action_category=action_category,
                target_type=target_type,
                target_id=target_id,
                target_name=target_name,
                details=details or {},
                ip_address=request.remote_addr if request else None,
                user_agent=request.headers.get('User-Agent') if request else None,
                status=status,
                error_message=error_message
            )
            db.session.add(audit)
            db.session.commit()
            return audit
        except Exception as e:
            print(f"Error logging system audit: {e}")
            db.session.rollback()
            return None
    
    @staticmethod
    def log_auth(user, action, status='success', error=None):
        """Log authentication activities"""
        return SystemAuditLogger.log(
            user=user,
            action_type=action,
            action_category='authentication',
            details={'login_method': 'password' if 'password' in action else 'otp'},
            status=status,
            error_message=error
        )
    
    @staticmethod
    def log_user_management(admin, action, target_user, details=None, status='success', error=None):
        """Log user management activities"""
        return SystemAuditLogger.log(
            user=admin,
            action_type=action,
            action_category='user_management',
            target_type='user',
            target_id=target_user.id if target_user else None,
            target_name=target_user.email if target_user else None,
            details=details,
            status=status,
            error_message=error
        )
    
    @staticmethod
    def log_company_management(admin, action, company, details=None, status='success', error=None):
        """Log company management activities"""
        return SystemAuditLogger.log(
            user=admin,
            action_type=action,
            action_category='company_management',
            target_type='company',
            target_id=company.id if company else None,
            target_name=company.name if company else None,
            details=details,
            status=status,
            error_message=error
        )
    
    @staticmethod
    def log_team_management(admin, action, team, details=None, status='success', error=None):
        """Log team management activities"""
        return SystemAuditLogger.log(
            user=admin,
            action_type=action,
            action_category='team_management',
            target_type='team',
            target_id=team.id if team else None,
            target_name=team.name if team else None,
            details=details,
            status=status,
            error_message=error
        )
    
    @staticmethod
    def log_report_generation(user, report_type, details=None, status='success', error=None):
        """Log report generation activities"""
        return SystemAuditLogger.log(
            user=user,
            action_type='report_generated',
            action_category='reporting',
            target_type='report',
            target_name=report_type,
            details=details,
            status=status,
            error_message=error
        )
    
    @staticmethod
    def log_invoice_generation(user, company, invoice_number, services_count, total_amount, status='success', error=None):
        """Log invoice generation activities"""
        return SystemAuditLogger.log(
            user=user,
            action_type='invoice_generated',
            action_category='invoicing',
            target_type='company',
            target_id=company.id if company else None,
            target_name=company.name if company else None,
            details={
                'invoice_number': invoice_number,
                'services_count': services_count,
                'total_amount': total_amount
            },
            status=status,
            error_message=error
        )