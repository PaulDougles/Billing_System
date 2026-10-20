import os
from flask import Blueprint, jsonify, request, send_file, Response, flash, redirect, url_for, render_template, current_app
from flask_login import login_required, current_user
from app.utils.cycle_helper import CycleHelper
from datetime import datetime, date
from calendar import monthrange
from sqlalchemy import func, or_, and_

from app.models import (
    TimeEntry, ServiceItem, Company, User, ActivityLog, Notification,
    Unit, ResourceType, Document,
    SystemSetting, UnitMember, MonthlyBatch, MonthlyBatchEntry, MonthlyMasterInvoice
)
from app.models import UnitMember
from sqlalchemy.orm import joinedload
from app.utils.system_audit import SystemAuditLogger
from app.extensions import db, mail
from flask_mail import Message
from datetime import datetime, timedelta
import traceback
import io
import json
import csv
from sqlalchemy import func
from app.utils.pdf_generator import generate_invoice_from_entry
from app.utils.email_helper import send_approval_email
from app.utils.labels import (
    combine_resource_descriptions, get_company_showback_label,
    get_company_showback_rate_label, get_company_showback_amount_label,
    resource_description,
)
from app.services import company_service, invoice_service, service_item_service, time_entry_service
from app.services.notification_service import send_notification
from app.utils.auth_decorators import role_required

api_bp = Blueprint('api', __name__)

@api_bp.before_request
def before_request():
    """Ensure user is authenticated for all API routes"""
    if not current_user.is_authenticated:
        return jsonify({'success': False, 'error': 'Authentication required'}), 401

# ============ TIME ENTRIES ENDPOINTS ============

@api_bp.route('/time-entries', methods=['GET'])
@login_required
def get_time_entries():
    """Get all time entries for current user"""
    try:
        entries = time_entry_service.list_user_time_entries(current_user.id)
        
        result = []
        for entry in entries:
            service_items = ServiceItem.query.filter_by(time_entry_id=entry.id).all()
            total_amount = sum(float(item.total_amount or 0) for item in service_items)
            
            result.append({
                'id': entry.id,
                'date': entry.service_date.strftime('%Y-%m-%d') if entry.service_date else '',
                'week_number': entry.week_number,
                'service_items_count': len(service_items),
                'total_amount': total_amount,
                'is_billable': entry.is_billable,
                'invoice_generated': entry.invoice_generated or False,
                'invoice_number': entry.invoice_number,
                'has_pdf': entry.invoice_pdf is not None
            })
        
        return jsonify(result)
    except Exception as e:
        print("Error in get_time_entries:", str(e))
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500
    

@api_bp.route('/time-entries', methods=['POST'])
@login_required
def add_time_entry():
    """Create a new time entry"""
    try:
        data = request.get_json()
        print("Received data:", data)
        
        if not data:
            return jsonify({'success': False, 'error': 'No data provided'}), 400
        
        entry = time_entry_service.create_time_entry(current_user.id, data)
        
        return jsonify({'success': True, 'id': entry.id})
        
    except Exception as e:
        db.session.rollback()
        print("Error in add_time_entry:", str(e))
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500

@api_bp.route('/time-entries/<int:entry_id>', methods=['DELETE'])
@login_required
def delete_time_entry(entry_id):
    """Delete a time entry"""
    try:
        entry = TimeEntry.query.get_or_404(entry_id)
        
        if entry.user_id != current_user.id:
            return jsonify({'success': False, 'error': 'Unauthorized'}), 403
        
        time_entry_service.delete_time_entry(entry)
        
        return jsonify({'success': True})
    except Exception as e:
        db.session.rollback()
        print("Error in delete_time_entry:", str(e))
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500

# ============ SUMMARY ENDPOINT ============


@api_bp.route('/service-items/<int:service_item_id>', methods=['GET'])
@login_required
def get_service_item_details(service_item_id):
    """Get details for a specific service item"""
    try:
        service_item = ServiceItem.query.get_or_404(service_item_id)
        entry = TimeEntry.query.get(service_item.time_entry_id)
        
        # ========== AUTHORIZATION CHECKS ==========
        is_authorized = False

        # Admin can access everything
        if current_user.role == 'admin':
            is_authorized = True

        elif current_user.role == 'super_admin':
            is_authorized = True
        
        # RECORD ASSISTANT can access services for their assigned company
        elif current_user.role == 'record_assistant':
            if current_user.company_id and current_user.company_id == service_item.company_id:
                is_authorized = True
        
        # BU user can access their own entries
        elif entry.user_id == current_user.id:
            is_authorized = True
        
        # Head of IT can access their company's services
        elif current_user.role == 'head_of_it' and service_item.company_id == current_user.company_id:
            is_authorized = True
        
        # Finance Controller can access all
        elif current_user.role == 'finance_controller':
            is_authorized = True
        
        
        # Payment company user
        elif current_user.role == 'payment_company_user':
            if company_service.can_access_company(current_user, service_item.company_id):
                is_authorized = True
        
        # Supervisor can access their unit members' services
        elif current_user.role == 'supervisor':
            bu_user = User.query.get(entry.user_id)
            if bu_user and bu_user.unit and bu_user.unit.supervisor_id == current_user.id:
                is_authorized = True

        if not is_authorized:
            print(f"Unauthorized: User {current_user.email} (role={current_user.role}) tried to access service {service_item_id}")
            return jsonify({'success': False, 'error': 'Unauthorized'}), 403
        
        # ========== GET COMPANY AND BU USER INFO ==========
        company_name = 'N/A'
        if service_item.company_id:
            company = Company.query.get(service_item.company_id)
            if company:
                company_name = company.name
        
        bu_user = User.query.get(entry.user_id) if entry else None
        unit_name = 'N/A'
        if bu_user and bu_user.unit_id:
            unit = Unit.query.get(bu_user.unit_id)
            if unit:
                unit_name = unit.name
        
        # ========== PROCESS RESOURCES ==========
        all_resource_types = ResourceType.query.all()
        resource_by_id = {str(rt.id): rt for rt in all_resource_types}
        
        processed_resources = []
        # Track totals for the three separate categories
        total_loit_chargeback = 0
        total_loit_showback = 0
        total_lolc_showback = 0
        
        for resource in service_item.resources or []:
            resource_type_id = str(resource.get('type', ''))
            rt = resource_by_id.get(resource_type_id)
            
            quantity = float(resource.get('quantity') or resource.get('hours') or 0)
            
            if rt:
                resource_name = rt.name
                resource_code = rt.code
                uom = rt.unit_of_measure or 'units'
                
                # ========== GET THE THREE SEPARATE VALUES FROM RESOURCE TYPE ==========
                loit_chargeback_rate = float(rt.loit_chargeback) if rt.loit_chargeback else 0
                loit_showback_rate = float(rt.loit_showback) if rt.loit_showback else 0
                lolc_showback_rate = float(rt.showback_rate) if rt.showback_rate else 0
                
                # Calculate amounts
                loit_chargeback_amount = quantity * loit_chargeback_rate
                loit_showback_amount = quantity * loit_showback_rate
                lolc_showback_amount = quantity * lolc_showback_rate
                
                # Add to totals
                total_loit_chargeback += loit_chargeback_amount
                total_loit_showback += loit_showback_amount
                total_lolc_showback += lolc_showback_amount
                
                processed_resources.append({
                    'id': rt.id,
                    'name': resource_name,
                    'code': resource_code,
                    'quantity': quantity,
                    'uom': uom,
                    'description': resource_description(
                        resource, service_item.service_description or ''
                    ),
                    # LOIT Chargeback - from rt.loit_chargeback
                    'loit_chargeback_rate': loit_chargeback_rate,
                    'loit_chargeback_amount': loit_chargeback_amount,
                    # LOIT Showback - from rt.loit_showback
                    'loit_showback_rate': loit_showback_rate,
                    'loit_showback_amount': loit_showback_amount,
                    # LOLC Showback - from rt.showback_rate
                    'lolc_showback_rate': lolc_showback_rate,
                    'lolc_showback_amount': lolc_showback_amount,
                    # Legacy fields for backward compatibility
                    'chargeback_rate': loit_chargeback_rate + loit_showback_rate,
                    'showback_rate': lolc_showback_rate,
                    'chargeback_amount': loit_chargeback_amount + loit_showback_amount,
                    'showback_amount': lolc_showback_amount
                })
            else:
                # Fallback for resources without type
                processed_resources.append({
                    'id': None,
                    'name': resource_type_id.replace('_', ' ').title(),
                    'code': '',
                    'quantity': quantity,
                    'uom': 'units',
                    'loit_chargeback_rate': 0,
                    'loit_chargeback_amount': 0,
                    'loit_showback_rate': 0,
                    'loit_showback_amount': 0,
                    'lolc_showback_rate': 0,
                    'lolc_showback_amount': 0,
                    'chargeback_rate': 0,
                    'showback_rate': 0,
                    'chargeback_amount': 0,
                    'showback_amount': 0
                })
        
        # ========== CALCULATE TOTALS ==========
        # Use stored values if they exist, otherwise use calculated
        if service_item.chargeback_total is not None:
            chargeback_total = float(service_item.chargeback_total)
        else:
            chargeback_total = total_loit_chargeback + total_loit_showback
        
        if service_item.showback_total is not None:
            showback_total = float(service_item.showback_total)
        else:
            showback_total = total_lolc_showback
        
        total_amount = chargeback_total + showback_total
        
        # ========== DETERMINE STATUS ==========
        if service_item.finance_approval_status == 'approved':
            status = 'finance_approved'
        elif service_item.head_approval_status == 'approved':
            status = 'head_approved'
        elif service_item.head_approval_status == 'rejected':
            status = 'head_rejected'
        elif service_item.supervisor_approval_status == 'rejected':
            status = 'supervisor_rejected'
        else:
            status = 'pending'
        
        # ========== BUILD RESPONSE ==========
        result = {
            'success': True,
            'service_item': {
                'id': service_item.id,
                'entry_id': entry.id,
                'submission_date': entry.submission_date.strftime('%Y-%m-%d %H:%M') if entry.submission_date else entry.created_at.strftime('%Y-%m-%d %H:%M'),
                'company_name': company_name,
                'company_showback_label': get_company_showback_label(company_name),
                'description': service_item.service_description,
                'resources': processed_resources,
                # NEW TOTALS - The three separate categories
                'loit_chargeback_total': total_loit_chargeback,
                'loit_showback_total': total_loit_showback,
                'lolc_showback_total': total_lolc_showback,
                # Legacy totals
                'chargeback_total': chargeback_total,
                'showback_total': showback_total,
                'total_amount': total_amount,
                'status': status,
                'head_approval_status': service_item.head_approval_status,
                'head_approval_comment': service_item.head_approval_comment,
                'finance_approval_status': service_item.finance_approval_status,
                'finance_approval_comment': service_item.finance_approval_comment,
                'invoice_generated': service_item.invoice_generated or False,
                'invoice_number': service_item.invoice_number,
                'bu_user': {
                    'full_name': bu_user.full_name if bu_user else 'Unknown',
                    'employee_id': bu_user.employee_id if bu_user else 'N/A',
                    'team_name': unit_name
                }
            }
        }
        return jsonify(result)
        
    except Exception as e:
        print("Error in get_service_item_details:", str(e))
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500
        
    
@api_bp.route('/summary', methods=['GET'])
@login_required
def get_summary():
    """Get summary statistics for current user"""
    try:
        today = datetime.now().date()
        week_ago = today - timedelta(days=7)
        month_start = today.replace(day=1)
        
        # Today's entries
        today_entries = TimeEntry.query\
            .filter(TimeEntry.user_id == current_user.id)\
            .filter(TimeEntry.date == today)\
            .all()
        
        today_hours = 0
        for entry in today_entries:
            if entry.resources:
                for resource in entry.resources:
                    today_hours += float(resource.get('hours', 0))
        
        # This week's entries
        week_entries = TimeEntry.query\
            .filter(TimeEntry.user_id == current_user.id)\
            .filter(TimeEntry.date >= week_ago)\
            .all()
        
        week_hours = 0
        for entry in week_entries:
            if entry.resources:
                for resource in entry.resources:
                    week_hours += float(resource.get('hours', 0))
        
        # This month's billable entries
        month_entries = TimeEntry.query\
            .filter(TimeEntry.user_id == current_user.id)\
            .filter(TimeEntry.date >= month_start)\
            .filter(TimeEntry.is_billable == True)\
            .all()
        
        billable_hours = 0
        revenue = 0
        for entry in month_entries:
            if entry.resources:
                for resource in entry.resources:
                    hours = float(resource.get('hours', 0))
                    rate = float(resource.get('rate', 0))
                    billable_hours += hours
                    revenue += hours * rate
        
        return jsonify({
            'today': float(today_hours),
            'week': float(week_hours),
            'billable': float(billable_hours),
            'revenue': float(revenue)
        })
    except Exception as e:
        print("Error in get_summary:", str(e))
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500




# ============ DASHBOARD STATISTICS ============

@api_bp.route('/dashboard/stats', methods=['GET'])
@login_required
def get_dashboard_stats():
    """Get dashboard statistics"""
    try:
        # Invoice statistics
        total_invoices = Invoice.query.filter_by(user_id=current_user.id).count()
        draft_invoices = Invoice.query.filter_by(user_id=current_user.id, status='draft').count()
        paid_invoices = Invoice.query.filter_by(user_id=current_user.id, status='paid').count()
        
        # Revenue statistics
        revenue_total = db.session.query(func.sum(Invoice.total_amount)).filter(
            Invoice.user_id == current_user.id,
            Invoice.status == 'paid'
        ).scalar() or 0
        
        revenue_month = db.session.query(func.sum(Invoice.total_amount)).filter(
            Invoice.user_id == current_user.id,
            Invoice.status == 'paid',
            Invoice.payment_date >= datetime.now().date().replace(day=1)
        ).scalar() or 0
        
        # Time entry statistics
        total_hours = 0
        time_entries = TimeEntry.query.filter_by(user_id=current_user.id).all()
        for entry in time_entries:
            if entry.resources:
                for resource in entry.resources:
                    total_hours += float(resource.get('hours', 0))
        
        return jsonify({
            'success': True,
            'total_invoices': total_invoices,
            'draft_invoices': draft_invoices,
            'paid_invoices': paid_invoices,
            'revenue_total': float(revenue_total),
            'revenue_month': float(revenue_month),
            'total_hours': total_hours
        })
        
    except Exception as e:
        print("Error in get_dashboard_stats:", str(e))
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500

# ============ APPROVAL WORKFLOW ENDPOINTS ============

@api_bp.route('/time-entries/submit', methods=['POST'])
@login_required
def submit_time_entry():
    """Submit a time entry with multiple service items"""
    try:
        data = request.get_json()
        print("Received submission data:", data)
        
        # Get service date from request
        service_date_str = data.get('service_date')
        service_date = None
        if service_date_str:
            try:
                service_date = datetime.strptime(service_date_str, '%Y-%m-%d').date()
            except ValueError:
                service_date = datetime.utcnow().date()
        else:
            service_date = datetime.utcnow().date()
        
        # Create time entry with service_date
        entry = TimeEntry(
            user_id=current_user.id,
            status='submitted',
            submission_date=datetime.utcnow(),
            service_date=service_date  # NEW FIELD
        )
        db.session.add(entry)
        db.session.flush()
        
        # Create service items
        for item_data in data.get('service_items', []):
            resources_list = []
            for r in item_data.get('resources', []):
                resources_list.append({
                    'type': r.get('type'),
                    'quantity': r.get('quantity', r.get('hours', 0)),
                    'description': (r.get('description') or '').strip()
                })
            service_description = combine_resource_descriptions(resources_list)
            if service_description == 'N/A' and item_data.get('description'):
                service_description = item_data['description']
            
            service_item = ServiceItem(
                time_entry_id=entry.id,
                company_id=item_data['company_id'],
                service_description=service_description,
                resources=resources_list or item_data.get('resources', []),
                total_amount=item_data['total_amount']
            )
            db.session.add(service_item)
            print(f"Added service item for company: {item_data['company_id']}")
        
        db.session.commit()
        
        # Create activity log
        log = ActivityLog(
            time_entry_id=entry.id,
            user_id=current_user.id,
            action='submitted',
            details={'ip': request.remote_addr, 'service_date': str(service_date)}
        )
        db.session.add(log)
        db.session.commit()
        
        # Send notification to Head of IT for each company
        service_items = ServiceItem.query.filter_by(time_entry_id=entry.id).all()
        notified_companies = set()
        
        for item in service_items:
            if item.company_id not in notified_companies:
                head_of_it = User.query.filter_by(
                    role='head_of_it',
                    company_id=item.company_id
                ).first()
                
                if head_of_it:
                    send_notification(
                        user=head_of_it,
                        entry=entry,
                        service_item=item,
                        type='pending_approval',
                        url=f'/head-of-it/dashboard'
                    )
                    notified_companies.add(item.company_id)
                    print(f"Notification sent to Head of IT for company {item.company_id}")
        
        return jsonify({'success': True, 'entry_id': entry.id})
        
    except Exception as e:
        db.session.rollback()
        print("Error in submit_time_entry:", str(e))
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500


@api_bp.route('/time-entries/submit-to-supervisor', methods=['POST'])
@login_required
def submit_to_supervisor():
    try:
        data = request.get_json()
        
        if current_user.role != 'bu_user':
            return jsonify({'success': False, 'error': 'Only BU users can submit entries'}), 403
        
        # Determine user's unit
        unit_id = current_user.unit_id
        if not unit_id:
            membership = UnitMember.query.filter_by(user_id=current_user.id).first()
            if membership:
                unit_id = membership.unit_id
        
        if not unit_id:
            return jsonify({'success': False, 'error': 'You are not assigned to any unit'}), 400
        
        unit = Unit.query.get(unit_id)
        if not unit or not unit.supervisor_id:
            return jsonify({'success': False, 'error': 'No supervisor assigned to your unit'}), 400
        
        # Parse service date
        service_date_str = data.get('service_date')
        if service_date_str:
            try:
                service_date = datetime.strptime(service_date_str, '%Y-%m-%d').date()
            except:
                service_date = datetime.utcnow().date()
        else:
            service_date = datetime.utcnow().date()
        
        # Create time entry
        entry = TimeEntry(
            user_id=current_user.id,
            unit_id=unit_id,
            status='submitted',
            submission_date=datetime.utcnow(),
            week_number=data.get('week_number'),
            year=data.get('year'),
            service_date=service_date
        )
        db.session.add(entry)
        db.session.flush()
        
        service_item_ids_map = {}
        
        # Create service items
        for item_data in data.get('service_items', []):
            resources_list = []
            for r in item_data.get('resources', []):
                resources_list.append({
                    'type': r.get('type'),
                    'quantity': r.get('quantity', 0),
                    'description': (r.get('description') or '').strip(),
                    'resource_id': r.get('resource_id')  # <-- Store resource_id
                })
            
            chargeback_total = float(item_data.get('chargeback_total', 0))
            showback_total = float(item_data.get('showback_total', 0))
            total_amount = chargeback_total + showback_total
            
            service_item = ServiceItem(
                time_entry_id=entry.id,
                company_id=item_data['company_id'],
                service_description=combine_resource_descriptions(resources_list),
                resources=resources_list,
                total_amount=total_amount,
                chargeback_total=chargeback_total,
                showback_total=showback_total
            )
            db.session.add(service_item)
            db.session.flush()
            
            # Store mapping from frontend ID to database ID
            if 'frontend_id' in item_data:
                service_item_ids_map[item_data['frontend_id']] = service_item.id
            
            # ========== LINK DOCUMENTS TO SERVICE ITEM ==========
            for resource in resources_list:
                resource_id = resource.get('resource_id')
                if resource_id:
                    # Find documents with this resource_id and company_id
                    docs = Document.query.filter_by(
                        company_id=item_data['company_id'],
                        resource_id=resource_id,
                        service_item_id=None,
                        is_active=True # Only link active documents
                    ).all()
                    for doc in docs:
                        doc.service_item_id = service_item.id
                        print(f"Linked document {doc.id} to service item {service_item.id}")
        
        # Create activity log
        log = ActivityLog(
            time_entry_id=entry.id,
            user_id=current_user.id,
            action='submitted',
            details={
                'service_items_count': len(data.get('service_items', [])),
                'ip': request.remote_addr,
                'week_number': data.get('week_number'),
                'service_date': str(service_date)
            }
        )
        db.session.add(log)
        db.session.commit()
        
        # Send notification to supervisor
        supervisor = User.query.get(unit.supervisor_id)
        if supervisor:
            from app.utils.email_helper import send_submission_email
            first_service = ServiceItem.query.filter_by(time_entry_id=entry.id).first()
            send_submission_email(supervisor, entry, first_service)
        
        return jsonify({'success': True, 'entry_id': entry.id, 'service_item_ids': service_item_ids_map})
        
    except Exception as e:
        db.session.rollback()
        print("Error in submit_to_supervisor:", str(e))
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500





@api_bp.route('/resource-types/by-unit', methods=['GET'])
@login_required
def get_resource_types_by_unit():
    """Get resource types available for the current user's unit"""
    unit_id = current_user.unit_id
    if not unit_id:
        membership = UnitMember.query.filter_by(user_id=current_user.id).first()
        if membership:
            unit_id = membership.unit_id
    
    if not unit_id:
        return jsonify([])
    
    resource_types = ResourceType.query.filter_by(unit_id=unit_id, is_active=True).order_by(ResourceType.display_order).all()
    
    result = []
    for rt in resource_types:
        result.append({
            'id': rt.id,
            'code': rt.code,
            'name': rt.name,
            'chargeback_rate': float(rt.chargeback_rate),
            'showback_rate': float(rt.showback_rate),
            'unit_of_measure': rt.unit_of_measure,
            'display_order': rt.display_order,
            'document_required': rt.document_required  # <-- ADD THIS
        })
    return jsonify(result)
    


    


    
@api_bp.route('/entries/<int:entry_id>', methods=['GET'])
@login_required
def get_entry_details(entry_id):
    """Get detailed information for a specific entry"""
    try:
        entry = TimeEntry.query.get_or_404(entry_id)
        
        # Check authorization
        is_authorized = (
            entry.user_id == current_user.id or 
            current_user.role == 'admin' or
            (current_user.role == 'head_of_it' and any(item.company_id == current_user.company_id for item in entry.service_items)) or
            current_user.role == 'finance_controller'
            
        )
        
        if not is_authorized:
            return jsonify({'success': False, 'error': 'Unauthorized'}), 403
        
        # Get the user who created this entry
        created_by_user = User.query.get(entry.user_id)
        
        # Get unit name safely
        Unit_name = 'N/A'
        if created_by_user and created_by_user.unit:
            Unit_name = created_by_user.unit.name
        
        # Prepare entry data
        entry_data = {
            'id': entry.id,
            'submission_date': entry.submission_date.strftime('%Y-%m-%d %H:%M') if entry.submission_date else entry.created_at.strftime('%Y-%m-%d %H:%M'),
            'status': entry.status,
            'total_amount': float(entry.get_total_amount()),
            'head_approval_comment': entry.head_approval_comment,
            'finance_approval_comment': entry.finance_approval_comment,
            'invoice_generated': entry.invoice_generated,
            'invoice_number': entry.invoice_number,
            'created_by': {
                'id': created_by_user.id if created_by_user else None,
                'full_name': created_by_user.full_name if created_by_user else 'Unknown',
                'employee_id': created_by_user.employee_id if created_by_user else 'N/A',
                'team_name': Unit_name
            },
            'service_items': []
        }
        
        # Add service items
        for item in entry.service_items:
            # Safely get company name
            company_name = 'N/A'
            if hasattr(item, 'company') and item.company:
                company_name = item.company.name
            elif item.company_id:
                company = Company.query.get(item.company_id)
                if company:
                    company_name = company.name
            
            item_dict = {
                'id': item.id,
                'company_name': company_name,
                'company_id': item.company_id,
                'description': item.service_description,
                
                'total_amount': float(item.total_amount),
                'resources': item.resources if item.resources else []
            }
            entry_data['service_items'].append(item_dict)
        
        # Add approval info if available
        if entry.head_approved_by:
            head_approved_user = User.query.get(entry.head_approved_by)
            if head_approved_user:
                entry_data['head_approved_by'] = head_approved_user.full_name
        
        if entry.finance_approved_by:
            finance_approved_user = User.query.get(entry.finance_approved_by)
            if finance_approved_user:
                entry_data['finance_approved_by'] = finance_approved_user.full_name
        
        return jsonify({'success': True, 'entry': entry_data})
        
    except Exception as e:
        print("Error in get_entry_details:", str(e))
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500


@api_bp.route('/head-of-it/entries/<int:entry_id>/service-items', methods=['GET'])
@login_required
def get_entry_service_items(entry_id):
    """Get all service items for an entry (for Head of IT)"""
    if current_user.role != 'head_of_it':
        return jsonify({'success': False, 'error': 'Unauthorized'}), 403
    
    try:
        entry = TimeEntry.query.get_or_404(entry_id)
        
        # Get service items for this company only
        service_items = ServiceItem.query.filter_by(
            time_entry_id=entry_id,
            company_id=current_user.company_id
        ).all()
        
        result = []
        for item in service_items:
            result.append({
                'id': item.id,
                'company_id': item.company_id,
                'description': item.service_description,
                
                'resources': item.resources,
                
                'total_amount': float(item.total_amount),
                'head_approval_status': item.head_approval_status,
                'head_approval_comment': item.head_approval_comment
            })
        
        return jsonify({'success': True, 'service_items': result})
        
    except Exception as e:
        print("Error in get_entry_service_items:", str(e))
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500
    

@api_bp.route('/head-of-it/service-items/<int:service_item_id>/approve', methods=['POST'])
@login_required
def head_of_it_approve_service(service_item_id):
    """Head of IT approves a specific service item"""
    if current_user.role != 'head_of_it':
        return jsonify({'success': False, 'error': 'Unauthorized'}), 403
    
    try:
        service_item = ServiceItem.query.get_or_404(service_item_id)
        entry = TimeEntry.query.get(service_item.time_entry_id)
        
        if service_item.company_id != current_user.company_id:
            return jsonify({'success': False, 'error': 'Not authorized for this company'}), 403
        
        data = request.get_json()
        comment = data.get('comment', '')
        
        # Update service item - HEAD APPROVAL
        service_item.head_approval_status = 'approved'
        service_item.head_approval_comment = comment
        service_item.head_approved_by = current_user.id
        service_item.head_approved_at = datetime.utcnow()
        
        # If this was previously rejected by Finance, reset finance status
        if service_item.finance_approval_status == 'rejected':
            service_item.finance_approval_status = 'pending'
            service_item.finance_approval_comment = None
            service_item.finance_rejected_by = None
            service_item.finance_rejected_at = None
        
        # --- UPDATE MONTHLY BATCH ENTRY STATUS ---
        from app.utils.cycle_helper import CycleHelper
        cycle = CycleHelper.get_current_cycle()
        batch_year = cycle['cycle_end'].year
        batch_month = cycle['cycle_end'].month
        
        batch = MonthlyBatch.query.filter_by(
            company_id=service_item.company_id,
            year=batch_year,
            month=batch_month
        ).first()
        
        if batch:
            batch_entry = MonthlyBatchEntry.query.filter_by(
                batch_id=batch.id,
                time_entry_id=entry.id
            ).first()
            if batch_entry:
                batch_entry.status = 'approved'
        
        # Check if all services in the entry are approved
        all_items = ServiceItem.query.filter_by(time_entry_id=entry.id).all()
        all_approved = all(i.head_approval_status == 'approved' for i in all_items)
        
        if all_approved:
            entry.status = 'head_approved'
            entry.head_approved_at = datetime.utcnow()
            entry.head_approved_by = current_user.id
        else:
            entry.status = 'submitted'
        
        # --- CHECK IF ALL ENTRIES IN THE BATCH ARE APPROVED ---
        if batch:
            # Get all batch entries for this batch
            batch_entries = MonthlyBatchEntry.query.filter_by(batch_id=batch.id).all()
            # For each batch entry, check if all its service items are approved
            all_batch_approved = True
            for be in batch_entries:
                # Get the time entry
                te = TimeEntry.query.get(be.time_entry_id)
                if not te:
                    continue
                services = ServiceItem.query.filter_by(time_entry_id=te.id).all()
                if not all(s.head_approval_status == 'approved' for s in services):
                    all_batch_approved = False
                    break
            if all_batch_approved and not batch.head_approved_at:
                batch.head_approved_at = datetime.utcnow()
        
        # Create activity log
        log = ActivityLog(
            time_entry_id=entry.id,
            user_id=current_user.id,
            action='head_approved',
            details={'service_item_id': service_item.id, 'comment': comment, 'ip': request.remote_addr}
        )
        db.session.add(log)
        db.session.commit()
        
        # Send notification to BU user
        bu_user = User.query.get(entry.user_id)
        if bu_user:
            send_notification(bu_user, entry, service_item, 'head_approved', '/bu-team/dashboard')
        
        # If all approved, notify finance controller
        if entry.status == 'head_approved':
            finance_controller = User.query.filter_by(role='finance_controller').first()
            if finance_controller:
                send_notification(finance_controller, entry, None, 'pending_finance', '/finance-controller/dashboard')
        
        return jsonify({'success': True, 'message': 'Service approved'})
        
    except Exception as e:
        db.session.rollback()
        print("Error in head_of_it_approve_service:", str(e))
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500
    



@api_bp.route('/head-of-it/service-items/<int:service_item_id>/reject', methods=['POST'])
@login_required
def head_of_it_reject_service(service_item_id):
    """Head of IT rejects a service - goes back to Supervisor"""
    if current_user.role != 'head_of_it':
        return jsonify({'success': False, 'error': 'Unauthorized'}), 403
    
    try:
        service_item = ServiceItem.query.get_or_404(service_item_id)
        entry = TimeEntry.query.get(service_item.time_entry_id)
        
        if service_item.company_id != current_user.company_id:
            return jsonify({'success': False, 'error': 'Not authorized'}), 403
        
        data = request.get_json()
        comment = data.get('comment', '')
        
        # Update service item
        service_item.head_approval_status = 'rejected'
        service_item.head_approval_comment = comment
        service_item.head_rejected_by = current_user.id
        service_item.head_rejected_at = datetime.utcnow()
        # Reset supervisor approval so it goes back to supervisor for re-review
        service_item.supervisor_approval_status = 'pending'
        
        # Update entry status (no supervisor approval on entry)
        entry.status = 'head_rejected_supervisor_pending'
        
        # --- UPDATE MONTHLY BATCH ENTRY STATUS ---
        from app.utils.cycle_helper import CycleHelper
        cycle = CycleHelper.get_current_cycle()
        batch_year = cycle['cycle_end'].year
        batch_month = cycle['cycle_end'].month
        
        batch = MonthlyBatch.query.filter_by(
            company_id=service_item.company_id,
            year=batch_year,
            month=batch_month
        ).first()
        
        if batch:
            batch_entry = MonthlyBatchEntry.query.filter_by(
                batch_id=batch.id,
                time_entry_id=entry.id
            ).first()
            if batch_entry:
                batch_entry.status = 'rejected'
                if not batch.head_rejected_at:
                    batch.head_rejected_at = datetime.utcnow()
        
        # Create activity log
        log = ActivityLog(
            time_entry_id=entry.id,
            user_id=current_user.id,
            action='head_rejected',
            details={'service_item_id': service_item.id, 'comment': comment, 'ip': request.remote_addr}
        )
        db.session.add(log)
        db.session.commit()
        
        # Send email to supervisor (find supervisor from the BU user's unit)
        bu_user = User.query.get(entry.user_id)
        if bu_user and bu_user.unit_id:
            unit = Unit.query.get(bu_user.unit_id)
            if unit and unit.supervisor_id:
                supervisor = User.query.get(unit.supervisor_id)
                if supervisor:
                    from app.utils.email_helper import send_supervisor_rejection_email
                    send_supervisor_rejection_email(supervisor, entry, service_item, comment)
        
        return jsonify({'success': True, 'message': 'Service rejected and returned to Supervisor for review'})
        
    except Exception as e:
        db.session.rollback()
        print("Error in head_of_it_reject_service:", str(e))
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500
    

@api_bp.route('/head-of-it/batch-approve', methods=['POST'])
@login_required
def head_of_it_batch_approve():
    """Head of IT approves multiple services at once (same as single approve)"""
    if current_user.role != 'head_of_it':
        return jsonify({'success': False, 'error': 'Unauthorized'}), 403

    try:
        data = request.get_json()
        service_ids = data.get('service_ids', [])
        if not service_ids:
            service_ids = data.get('entry_ids', [])
        comment = data.get('comment', '')

        if not service_ids:
            return jsonify({'success': False, 'error': 'No services selected'}), 400

        from app.utils.cycle_helper import CycleHelper
        cycle = CycleHelper.get_current_cycle()
        batch_year = cycle['cycle_end'].year
        batch_month = cycle['cycle_end'].month

        approved_count = 0
        entries_to_check = set()
        companies_affected = set()

        for service_id in service_ids:
            service_item = ServiceItem.query.get(service_id)
            if not service_item:
                continue
            if service_item.company_id != current_user.company_id:
                continue

            entry = TimeEntry.query.get(service_item.time_entry_id)
            if not entry:
                continue

            # Update service item
            service_item.head_approval_status = 'approved'
            service_item.head_approval_comment = comment
            service_item.head_approved_by = current_user.id
            service_item.head_approved_at = datetime.utcnow()

            if service_item.finance_approval_status == 'rejected':
                service_item.finance_approval_status = 'pending'
                service_item.finance_approval_comment = None
                service_item.finance_rejected_by = None
                service_item.finance_rejected_at = None

            # Update MonthlyBatchEntry
            batch = MonthlyBatch.query.filter_by(
                company_id=service_item.company_id,
                year=batch_year,
                month=batch_month
            ).first()
            if batch:
                batch_entry = MonthlyBatchEntry.query.filter_by(
                    batch_id=batch.id,
                    time_entry_id=entry.id
                ).first()
                if batch_entry:
                    batch_entry.status = 'approved'
                    companies_affected.add(batch.id)

            approved_count += 1
            entries_to_check.add(entry.id)

        # After all updates, check each entry for completeness
        for entry_id in entries_to_check:
            entry = TimeEntry.query.get(entry_id)
            if not entry:
                continue
            all_items = ServiceItem.query.filter_by(time_entry_id=entry.id).all()
            all_approved = all(i.head_approval_status == 'approved' for i in all_items)
            if all_approved:
                entry.status = 'head_approved'
                entry.head_approved_at = datetime.utcnow()
                entry.head_approved_by = current_user.id
            else:
                entry.status = 'submitted'
            db.session.add(entry)

        # For each affected batch, check if all entries are now approved
        for batch_id in companies_affected:
            batch = MonthlyBatch.query.get(batch_id)
            if batch and not batch.head_approved_at:
                batch_entries = MonthlyBatchEntry.query.filter_by(batch_id=batch.id).all()
                all_approved = True
                for be in batch_entries:
                    te = TimeEntry.query.get(be.time_entry_id)
                    if not te:
                        continue
                    services = ServiceItem.query.filter_by(time_entry_id=te.id).all()
                    if not all(s.head_approval_status == 'approved' for s in services):
                        all_approved = False
                        break
                if all_approved:
                    batch.head_approved_at = datetime.utcnow()
                    db.session.add(batch)

        db.session.commit()

        # Send notifications
        for entry_id in entries_to_check:
            entry = TimeEntry.query.get(entry_id)
            if not entry:
                continue
            bu_user = User.query.get(entry.user_id)
            if bu_user:
                service_item = ServiceItem.query.filter_by(time_entry_id=entry.id).first()
                send_notification(bu_user, entry, service_item, 'head_approved', '/bu-team/dashboard')
            if entry.status == 'head_approved':
                finance_controller = User.query.filter_by(role='finance_controller').first()
                if finance_controller:
                    send_notification(finance_controller, entry, None, 'pending_finance', '/finance-controller/dashboard')

        return jsonify({'success': True, 'message': f'Approved {approved_count} services successfully'})

    except Exception as e:
        db.session.rollback()
        print(f"Error in batch approve: {str(e)}")
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500


@api_bp.route('/head-of-it/batch-reject', methods=['POST'])
@login_required
def head_of_it_batch_reject():
    """Head of IT rejects multiple services at once (same as single reject)"""
    if current_user.role != 'head_of_it':
        return jsonify({'success': False, 'error': 'Unauthorized'}), 403

    try:
        data = request.get_json()
        service_ids = data.get('service_ids', [])
        if not service_ids:
            service_ids = data.get('entry_ids', [])
        comment = data.get('comment', '')

        if not service_ids:
            return jsonify({'success': False, 'error': 'No services selected'}), 400
        if not comment:
            return jsonify({'success': False, 'error': 'Comment required for rejection'}), 400

        from app.utils.cycle_helper import CycleHelper
        cycle = CycleHelper.get_current_cycle()
        batch_year = cycle['cycle_end'].year
        batch_month = cycle['cycle_end'].month

        rejected_count = 0
        entries_to_update = set()
        companies_affected = set()

        for service_id in service_ids:
            service_item = ServiceItem.query.get(service_id)
            if not service_item:
                continue
            if service_item.company_id != current_user.company_id:
                continue

            entry = TimeEntry.query.get(service_item.time_entry_id)
            if not entry:
                continue

            # Update service item
            service_item.head_approval_status = 'rejected'
            service_item.head_approval_comment = comment
            service_item.head_rejected_by = current_user.id
            service_item.head_rejected_at = datetime.utcnow()
            # Reset supervisor approval so it goes back to supervisor
            service_item.supervisor_approval_status = 'pending'

            # Update entry status (no supervisor approval on entry)
            entry.status = 'head_rejected_supervisor_pending'

            # Update MonthlyBatchEntry
            batch = MonthlyBatch.query.filter_by(
                company_id=service_item.company_id,
                year=batch_year,
                month=batch_month
            ).first()
            if batch:
                batch_entry = MonthlyBatchEntry.query.filter_by(
                    batch_id=batch.id,
                    time_entry_id=entry.id
                ).first()
                if batch_entry:
                    batch_entry.status = 'rejected'
                    companies_affected.add(batch.id)

            rejected_count += 1
            entries_to_update.add(entry.id)

        # For each affected batch, set head_rejected_at if not already set
        for batch_id in companies_affected:
            batch = MonthlyBatch.query.get(batch_id)
            if batch and not batch.head_rejected_at:
                batch.head_rejected_at = datetime.utcnow()
                db.session.add(batch)

        db.session.commit()

        # Send notifications to supervisors (avoid duplicates)
        supervisors_notified = set()
        for entry_id in entries_to_update:
            entry = TimeEntry.query.get(entry_id)
            if not entry:
                continue
            # Find supervisor from the entry's BU user's unit
            bu_user = User.query.get(entry.user_id)
            if bu_user and bu_user.unit_id:
                unit = Unit.query.get(bu_user.unit_id)
                if unit and unit.supervisor_id and unit.supervisor_id not in supervisors_notified:
                    supervisor = User.query.get(unit.supervisor_id)
                    if supervisor:
                        from app.utils.email_helper import send_supervisor_rejection_email
                        send_supervisor_rejection_email(supervisor, entry, None, comment)
                        supervisors_notified.add(supervisor.id)

        return jsonify({'success': True, 'message': f'Rejected {rejected_count} services. Returned to Supervisor.'})

    except Exception as e:
        db.session.rollback()
        print(f"Error in batch reject: {str(e)}")
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500


    
# ============ FINANCE CONTROLLER ENDPOINTS ============
@api_bp.route('/finance-controller/entries', methods=['GET'])
@login_required
@role_required('finance_controller')
def get_finance_controller_entries():
    """Get entries pending finance review"""
    try:
        entries = TimeEntry.query\
            .filter(TimeEntry.status == 'head_approved')\
            .order_by(TimeEntry.head_approved_at.desc())\
            .all()
        
        result = []
        for entry in entries:
            result.append({
                'id': entry.id,
                'submission_date': entry.submission_date.strftime('%Y-%m-%d %H:%M:%S'),
                'head_approved_at': entry.head_approved_at.strftime('%Y-%m-%d %H:%M:%S') if entry.head_approved_at else None,
                'created_by': {
                    'id': entry.created_by.id,
                    'full_name': entry.created_by.full_name,
                    'employee_id': entry.created_by.employee_id,
                    'team_name': entry.created_by.unit.name if entry.created_by.unit else None
                },
                'head_approved_by': {
                    'id': entry.head_approved_by.id,
                    'full_name': entry.head_approved_by.full_name
                } if entry.head_approved_by else None,
                'service_items': [{
                    'id': item.id,
                    'company_name': item.company.name,
                    'description': item.service_description,
                    
                    'resources': item.resources,
                    'flat_fee': float(item.flat_fee),
                    'total_amount': float(item.total_amount)
                } for item in entry.service_items],
                'total_amount': entry.get_total_amount()
            })
        
        return jsonify({'success': True, 'entries': result})
        
    except Exception as e:
        print("Error in get_finance_controller_entries:", str(e))
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500

# ============ FINANCE CONTROLLER ENDPOINTS ============

@api_bp.route('/finance-controller/entries/<int:entry_id>/approve', methods=['POST'])
@login_required
@role_required('finance_controller')
def finance_controller_approve(entry_id):
    """Finance Controller approves an entry"""
    try:
        entry = TimeEntry.query.get_or_404(entry_id)
        data = request.get_json()
        
        # Update entry status
        entry.status = 'finance_approved'
        entry.finance_approval_status = 'approved'
        entry.finance_approval_comment = data.get('comment', '')
        entry.finance_approved_by = current_user.id
        entry.finance_approved_at = datetime.utcnow()
        
        # Update all service items in this entry
        items = ServiceItem.query.filter_by(time_entry_id=entry.id).all()
        for item in items:
            item.finance_approval_status = 'approved'
            item.finance_approved_by = current_user.id
            item.finance_approved_at = datetime.utcnow()
            
            # Determine which payment team gets this service item
            company = Company.query.get(item.company_id)
            if company:
                item.assigned_payment_team = company_service.get_payment_team_for_company(company)
        
        # Create activity log
        log = ActivityLog(
            time_entry_id=entry.id,
            user_id=current_user.id,
            action='finance_approved',
            details={'comment': data.get('comment', ''), 'ip': request.remote_addr}
        )
        db.session.add(log)
        
        db.session.commit()
        
        # Send notification to BU user
        bu_user = User.query.get(entry.user_id)
        if bu_user:
            send_notification(bu_user, entry, None, 'finance_approved', '/bu-team/dashboard')
        
        # Send notifications to payment teams
        for item in items:
            if item.assigned_payment_team:
                payment_team = User.query.filter_by(role=item.assigned_payment_team).first()
                if payment_team:
                    send_notification(payment_team, entry, item, 'ready_for_invoice', '/payment/dashboard')
        
        return jsonify({'success': True, 'message': 'Entry approved and sent to payment team'})
        
    except Exception as e:
        db.session.rollback()
        print("Error in finance_controller_approve:", str(e))
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500


@api_bp.route('/finance-controller/entries/<int:entry_id>/reject', methods=['POST'])
@login_required
@role_required('finance_controller')
def finance_controller_reject(entry_id):
    """Finance Controller rejects an entry - returns to Head of IT"""
    try:
        entry = TimeEntry.query.get_or_404(entry_id)
        data = request.get_json()
        
        # Update entry status
        entry.status = 'finance_rejected'
        entry.finance_approval_status = 'rejected'
        entry.finance_approval_comment = data.get('comment', '')
        entry.finance_rejected_by = current_user.id
        entry.finance_rejected_at = datetime.utcnow()
        
        # Update all service items in this entry
        items = ServiceItem.query.filter_by(time_entry_id=entry.id).all()
        for item in items:
            item.finance_approval_status = 'rejected'
            item.finance_approval_comment = data.get('comment', '')
            item.finance_rejected_by = current_user.id
            item.finance_rejected_at = datetime.utcnow()
            # Reset head approval so Head of IT can review again
            item.head_approval_status = 'pending'
        
        # Create activity log
        log = ActivityLog(
            time_entry_id=entry.id,
            user_id=current_user.id,
            action='finance_rejected',
            details={'comment': data.get('comment', ''), 'ip': request.remote_addr}
        )
        db.session.add(log)
        
        db.session.commit()
        
        # Send notification to Head of IT
        head_of_it = User.query.filter_by(id=entry.head_approved_by).first()
        if head_of_it:
            send_notification(head_of_it, entry, None, 'finance_rejected', '/head-of-it/dashboard')
        
        return jsonify({'success': True, 'message': 'Entry rejected and returned to Head of IT'})
        
    except Exception as e:
        db.session.rollback()
        print("Error in finance_controller_reject:", str(e))
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500
    

# ============ PAYMENT TEAM ENDPOINTS ============



@api_bp.route('/payment/entries/<int:entry_id>/generate-invoice', methods=['POST'])
@login_required
@role_required('payment_lolc_technologies', 'payment_lolc_finance')
def payment_generate_invoice(entry_id):
    """Generate invoice for payment team"""
    try:
        entry = TimeEntry.query.get_or_404(entry_id)
        
        if entry.assigned_payment_team != current_user.role:
            return jsonify({'success': False, 'error': 'Not authorized for this entry'}), 403
        
        # Generate invoice number
        invoice_number = f"INV-{entry.id}-{datetime.now().strftime('%Y%m%d%H%M%S')}"
        
        # Generate PDF bytes (using your existing function)
        pdf_bytes, invoice_number = generate_invoice_from_entry(entry, invoice_number)
        
        # Update entry
        entry.invoice_generated = True
        entry.invoice_number = invoice_number
        entry.invoice_pdf = pdf_bytes
        entry.invoice_pdf_filename = f"{invoice_number}.pdf"
        entry.invoice_generated_by = current_user.id
        entry.invoice_generated_at = datetime.utcnow()
        entry.status = 'invoiced'
        
        # Create activity log
        log = ActivityLog(
            time_entry_id=entry.id,
            user_id=current_user.id,
            action='invoice_generated',
            details={'invoice_number': invoice_number, 'ip': request.remote_addr}
        )
        db.session.add(log)
        
        db.session.commit()
        
        # Send notification to BU user
        send_notification(
            user=entry.created_by,
            entry=entry,
            type='invoiced',
            url=f'/bu-team/dashboard'
        )
        
        return jsonify({
            'success': True,
            'invoice_number': invoice_number,
            'message': 'Invoice generated successfully'
        })
        
    except Exception as e:
        db.session.rollback()
        print("Error in payment_generate_invoice:", str(e))
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500

# ============ BU TEAM DASHBOARD ENDPOINTS ============

@api_bp.route('/bu-team/entries', methods=['GET'])
@login_required
def get_bu_team_entries():
    """Get all entries for BU team user with status"""
    try:
        if current_user.role != 'bu_user':
            return jsonify({'success': False, 'error': 'Unauthorized'}), 403
        
        entries = TimeEntry.query\
            .filter_by(user_id=current_user.id)\
            .order_by(TimeEntry.submission_date.desc())\
            .all()
        
        result = []
        for entry in entries:
            result.append({
                'id': entry.id,
                'submission_date': entry.submission_date.strftime('%Y-%m-%d %H:%M:%S'),
                'status': entry.status,
                'head_approval_status': entry.head_approval_status,
                'head_approval_comment': entry.head_approval_comment,
                'finance_approval_status': entry.finance_approval_status,
                'finance_approval_comment': entry.finance_approval_comment,
                'service_items': [{
                    'id': item.id,
                    'company_name': item.company.name,
                    'company_id': item.company_id, # Add company_id here
                    'description': item.service_description,
                    'total_amount': float(item.total_amount)
                } for item in entry.service_items],
                'total_amount': entry.get_total_amount(),
                'invoice_generated': entry.invoice_generated,
                'invoice_number': entry.invoice_number
            })
        
        return jsonify({'success': True, 'entries': result})
        
    except Exception as e:
        print("Error in get_bu_team_entries:", str(e))
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500

# ============ REPORT GENERATION ENDPOINTS ============



@api_bp.route('/service-items/<int:service_item_id>/workflow', methods=['GET'])
@login_required
def get_service_item_workflow(service_item_id):
    """Get COMPLETE workflow history for a service item - REAL ACTIONS ONLY (no duplicates)"""
    try:
        service_item = ServiceItem.query.get_or_404(service_item_id)
        entry = TimeEntry.query.get(service_item.time_entry_id)
        
        # Check if user is payment_company_user with LOLC Technologies (full access)
        is_lolc_tech_payment = (
            current_user.role == 'payment_company_user' and 
            current_user.company and 
            company_service.is_parent_company_user(current_user)
        )
        
        # Check if user is payment_company_user from another company
        is_other_company_payment = (
            current_user.role == 'payment_company_user' and 
            current_user.company and 
            not company_service.is_parent_company_user(current_user)
        )
        
        # Authorization logic
        is_authorized = False
        
        # Admin can view everything
        if current_user.role == 'admin':
            is_authorized = True
        
        elif current_user.role == 'super_admin':
            is_authorized = True
        
        # BU user can view their own entries
        elif entry.user_id == current_user.id:
            is_authorized = True
        
        # Head of IT can view their company's services
        elif current_user.role == 'head_of_it' and service_item.company_id == current_user.company_id:
            is_authorized = True
        
        # Finance Controller can view all
        elif current_user.role == 'finance_controller':
            is_authorized = True
        
        
        
        # LOLC Technologies payment_company_user can view all
        elif is_lolc_tech_payment:
            is_authorized = True
            
        # Supervisor can view entries for their unit members
        elif current_user.role == 'supervisor':
            if entry.user and entry.user.unit and entry.user.unit.supervisor_id == current_user.id:
                is_authorized = True
        
        # Other company payment_company_user can only view their own company
        elif is_other_company_payment and service_item.company_id == current_user.company_id:
            is_authorized = True
        
        if not is_authorized:
            return jsonify({'success': False, 'error': 'Unauthorized'}), 403
        
        # Get ALL activity logs for this entry
        logs = ActivityLog.query.filter(
            ActivityLog.time_entry_id == entry.id
        ).order_by(ActivityLog.created_at.asc()).all()
        
        # Also get service_item specific logs
        service_logs = ActivityLog.query.filter(
            ActivityLog.service_item_id == service_item.id
        ).order_by(ActivityLog.created_at.asc()).all()
        
        # Combine and deduplicate
        all_logs = logs + service_logs
        unique_logs = {}
        for log in all_logs:
            if log.id not in unique_logs:
                unique_logs[log.id] = log
        all_logs = list(unique_logs.values())
        all_logs.sort(key=lambda x: x.created_at)
        
        # Build workflow timeline - REAL ACTIONS ONLY (no duplicates)
        workflow = []
        
        # Track seen actions to prevent duplicates
        seen_actions = set()
        
        # 1. Initial submission (REAL action) - ONLY add if no submission log exists
        submission_exists_in_logs = any(log.action == 'submitted' for log in all_logs)
        
        if entry.submission_date and not submission_exists_in_logs:
            user = User.query.get(entry.user_id)
            workflow.append({
                'action': 'submitted',
                'action_display': 'Submitted',
                'user_name': user.full_name if user else 'Unknown',
                'user_role': user.role if user else 'bu_user',
                'comment': None,
                'status_from': 'draft',
                'status_to': 'submitted',
                'created_at': entry.submission_date.strftime('%Y-%m-%d %H:%M:%S'),
                'icon': 'bi-send',
                'color': 'primary'
            })
            seen_actions.add(f"submitted_{entry.submission_date.strftime('%Y-%m-%d %H:%M:%S')}")
        
        # 2. Process ONLY real activity logs (skip any "awaiting" entries)
        for log in all_logs:
            # SKIP any fake/placeholder entries or redundant status updates
            if 'awaiting' in log.action.lower() or log.action == 'entry_status_updated':
                continue
            
            # Create a unique key for this action to prevent duplicates
            action_key = f"{log.action}_{log.created_at.strftime('%Y-%m-%d %H:%M:%S')}"
            if action_key in seen_actions:
                continue
            seen_actions.add(action_key)
            
            user = User.query.get(log.user_id)
            
            # Map actions to display names and styling
            action_map = {
                'submitted': {
                    'display': 'Submitted', 
                    'icon': 'bi-send', 
                    'color': 'primary'
                },
                'supervisor_approved': {
                    'display': 'Supervisor Approved', 
                    'icon': 'bi-check-circle', 
                    'color': 'success'
                },
                'supervisor_rejected': {
                    'display': 'Supervisor Rejected', 
                    'icon': 'bi-x-circle', 
                    'color': 'danger'
                },
                'head_approved': {
                    'display': 'Head of IT Approved', 
                    'icon': 'bi-check-circle', 
                    'color': 'success'
                },
                'head_rejected': {
                    'display': 'Head of IT Rejected', 
                    'icon': 'bi-x-circle', 
                    'color': 'danger'
                },
                'head_status_reset': {
                    'display': 'Head Status Reset', 
                    'icon': 'bi-arrow-repeat', 
                    'color': 'warning'
                },
                'finance_approved': {
                    'display': 'Finance Controller Approved', 
                    'icon': 'bi-check-circle', 
                    'color': 'success'
                },
                'finance_rejected': {
                    'display': 'Finance Controller Rejected', 
                    'icon': 'bi-x-circle', 
                    'color': 'danger'
                },
                'invoice_generated': {
                    'display': 'Invoice Generated', 
                    'icon': 'bi-file-pdf', 
                    'color': 'info'
                },
                'resubmitted': {
                    'display': 'Resubmitted After Rejection', 
                    'icon': 'bi-arrow-repeat', 
                    'color': 'warning'
                },
                'entry_status_updated': {
                    'display': 'Status Updated', 
                    'icon': 'bi-pencil', 
                    'color': 'secondary'
                }
            }
            
            action_info = action_map.get(log.action, {
                'display': log.action.replace('_', ' ').title(),
                'icon': 'bi-info-circle',
                'color': 'secondary'
            })
            
            workflow.append({
                'id': log.id,
                'action': log.action,
                'action_display': action_info['display'],
                'user_name': user.full_name if user else 'System',
                'user_role': user.role if user else 'system',
                'comment': log.details.get('comment') if log.details else None,
                'status_from': log.status_from,
                'status_to': log.status_to,
                'created_at': log.created_at.strftime('%Y-%m-%d %H:%M:%S'),
                'icon': action_info['icon'],
                'color': action_info['color'],
                'details': log.details,
                'is_batch': log.details.get('batch_approve') or log.details.get('batch_reject') if log.details else False
            })
        
        # 3. Add supervisor approval from service_item (if not already in workflow)
        if service_item.supervisor_approved_at:
            action_key = f"supervisor_approved_{service_item.supervisor_approved_at.strftime('%Y-%m-%d %H:%M:%S')}"
            if action_key not in seen_actions:
                user = User.query.get(service_item.supervisor_approved_by)
                workflow.append({
                    'action': 'supervisor_approved',
                    'action_display': 'Supervisor Approved',
                    'user_name': user.full_name if user else 'Unknown',
                    'user_role': 'supervisor',
                    'comment': service_item.supervisor_comment,
                    'created_at': service_item.supervisor_approved_at.strftime('%Y-%m-%d %H:%M:%S'),
                    'icon': 'bi-check-circle',
                    'color': 'success',
                    'details': {'service_item_id': service_item.id}
                })
                seen_actions.add(action_key)
        
        # 4. Add supervisor rejection from service_item (if not already in workflow)
        if service_item.supervisor_rejected_at:
            action_key = f"supervisor_rejected_{service_item.supervisor_rejected_at.strftime('%Y-%m-%d %H:%M:%S')}"
            if action_key not in seen_actions:
                user = User.query.get(service_item.supervisor_rejected_by)
                workflow.append({
                    'action': 'supervisor_rejected',
                    'action_display': 'Supervisor Rejected',
                    'user_name': user.full_name if user else 'Unknown',
                    'user_role': 'supervisor',
                    'comment': service_item.supervisor_comment,
                    'created_at': service_item.supervisor_rejected_at.strftime('%Y-%m-%d %H:%M:%S'),
                    'icon': 'bi-x-circle',
                    'color': 'danger',
                    'details': {'service_item_id': service_item.id}
                })
                seen_actions.add(action_key)
        
        # 5. Add Head of IT approval (if not already in workflow)
        if service_item.head_approved_at:
            action_key = f"head_approved_{service_item.head_approved_at.strftime('%Y-%m-%d %H:%M:%S')}"
            if action_key not in seen_actions:
                user = User.query.get(service_item.head_approved_by)
                workflow.append({
                    'action': 'head_approved',
                    'action_display': 'Head of IT Approved',
                    'user_name': user.full_name if user else 'Unknown',
                    'user_role': user.role if user else 'head_of_it',
                    'comment': service_item.head_approval_comment,
                    'created_at': service_item.head_approved_at.strftime('%Y-%m-%d %H:%M:%S'),
                    'icon': 'bi-check-circle',
                    'color': 'success',
                    'details': {'service_item_id': service_item.id, 'company_id': service_item.company_id}
                })
                seen_actions.add(action_key)
        
        # 6. Add Head of IT rejection (if not already in workflow)
        if service_item.head_rejected_at:
            action_key = f"head_rejected_{service_item.head_rejected_at.strftime('%Y-%m-%d %H:%M:%S')}"
            if action_key not in seen_actions:
                user = User.query.get(service_item.head_rejected_by)
                workflow.append({
                    'action': 'head_rejected',
                    'action_display': 'Head of IT Rejected',
                    'user_name': user.full_name if user else 'Unknown',
                    'user_role': user.role if user else 'head_of_it',
                    'comment': service_item.head_approval_comment,
                    'created_at': service_item.head_rejected_at.strftime('%Y-%m-%d %H:%M:%S'),
                    'icon': 'bi-x-circle',
                    'color': 'danger',
                    'details': {'service_item_id': service_item.id, 'company_id': service_item.company_id}
                })
                seen_actions.add(action_key)
        
        # 7. Add Finance Controller approval (if not already in workflow)
        if service_item.finance_approved_at:
            action_key = f"finance_approved_{service_item.finance_approved_at.strftime('%Y-%m-%d %H:%M:%S')}"
            if action_key not in seen_actions:
                user = User.query.get(service_item.finance_approved_by)
                workflow.append({
                    'action': 'finance_approved',
                    'action_display': 'Finance Controller Approved',
                    'user_name': user.full_name if user else 'Unknown',
                    'user_role': user.role if user else 'finance_controller',
                    'comment': service_item.finance_approval_comment,
                    'created_at': service_item.finance_approved_at.strftime('%Y-%m-%d %H:%M:%S'),
                    'icon': 'bi-check-circle',
                    'color': 'success',
                    'details': {'service_item_id': service_item.id, 'total_amount': float(service_item.total_amount)}
                })
                seen_actions.add(action_key)
        
        # 8. Add Finance Controller rejection (if not already in workflow)
        if service_item.finance_rejected_at:
            action_key = f"finance_rejected_{service_item.finance_rejected_at.strftime('%Y-%m-%d %H:%M:%S')}"
            if action_key not in seen_actions:
                user = User.query.get(service_item.finance_rejected_by)
                workflow.append({
                    'action': 'finance_rejected',
                    'action_display': 'Finance Controller Rejected',
                    'user_name': user.full_name if user else 'Unknown',
                    'user_role': user.role if user else 'finance_controller',
                    'comment': service_item.finance_approval_comment,
                    'created_at': service_item.finance_rejected_at.strftime('%Y-%m-%d %H:%M:%S'),
                    'icon': 'bi-x-circle',
                    'color': 'danger',
                    'details': {'service_item_id': service_item.id, 'company_id': service_item.company_id}
                })
                seen_actions.add(action_key)
        
      
        # 9. Add invoice generation (if not already in workflow)
            if service_item.invoice_generated and service_item.invoice_number:
               inv_datetime = service_item.invoice_generated_at or datetime.utcnow()
               created_at_str = inv_datetime.strftime('%Y-%m-%d %H:%M:%S') if inv_datetime else datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')
               action_key = f"invoice_generated_{created_at_str}"
            if action_key not in seen_actions:
                user = User.query.get(service_item.invoice_generated_by)
                workflow.append({
                    'action': 'invoice_generated',
                    'action_display': 'Invoice Generated',
                    'user_name': user.full_name if user else 'Payment Team',
                    'user_role': user.role if user else 'payment_team',
                    'comment': f'Invoice {service_item.invoice_number} generated',
                    'created_at': created_at_str,
                    'icon': 'bi-file-pdf',
                    'color': 'info',
                    'details': {'invoice_number': service_item.invoice_number, 'total_amount': float(service_item.total_amount)}
                })
                seen_actions.add(action_key)
        
        # Sort by date
        workflow.sort(key=lambda x: x['created_at'])
        
        return jsonify({'success': True, 'workflow': workflow})
        
    except Exception as e:
        print("Error in get_service_item_workflow:", str(e))
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500


@api_bp.route('/workflow/view/<int:service_item_id>', methods=['GET'])
@login_required
def view_workflow(service_item_id):
    """View workflow timeline for a service item - REAL ACTIONS ONLY (no fake awaiting entries)"""
    try:
        service_item = ServiceItem.query.get_or_404(service_item_id)
        entry = TimeEntry.query.get(service_item.time_entry_id)
        
        # Check if user is payment_company_user with LOLC Technologies (full access)
        is_lolc_tech_payment = (
            current_user.role == 'payment_company_user' and 
            current_user.company and 
            company_service.is_parent_company_user(current_user)
        )
        
        # Check if user is payment_company_user from another company
        is_other_company_payment = (
            current_user.role == 'payment_company_user' and 
            current_user.company and 
            not company_service.is_parent_company_user(current_user)
        )
        
        # Authorization logic
        is_authorized = False
        
        # Admin can view everything
        if current_user.role == 'admin':
            is_authorized = True
        
        elif current_user.role == 'super_admin':
            is_authorized = True
        
        # BU user can view their own entries
        elif entry.user_id == current_user.id:
            is_authorized = True
        
        # Head of IT can view their company's services
        elif current_user.role == 'head_of_it' and service_item.company_id == current_user.company_id:
            is_authorized = True
            
        elif current_user.role == 'record_assistant':
            if current_user.company_id == service_item.company_id:
                is_authorized = True
        
        # Finance Controller can view all
        elif current_user.role == 'finance_controller':
            is_authorized = True
        
        
        
        # LOLC Technologies payment_company_user can view all
        elif is_lolc_tech_payment:
            is_authorized = True
            
        # Supervisor can view entries for their unit members
        elif current_user.role == 'supervisor':
            if entry.user and entry.user.unit and entry.user.unit.supervisor_id == current_user.id:
                is_authorized = True
        
        # Other company payment_company_user can only view their own company
        elif is_other_company_payment and service_item.company_id == current_user.company_id:
            is_authorized = True
        
        if not is_authorized:
            flash('You are not authorized to view this workflow.', 'danger')
            return redirect(url_for('dashboard.index'))
        
        # Build workflow timeline data - REAL ACTIONS ONLY
        workflow_logs = []
        
        # Track seen actions to prevent duplicates
        seen_actions = set()
        
        # Get activity logs from database (real logs only)
        activity_logs = ActivityLog.query.filter(
            ActivityLog.time_entry_id == entry.id
        ).order_by(ActivityLog.created_at.asc()).all()
        
        # 1. Submission event
        # Check if 'submitted' action already exists in activity logs to avoid duplication
        submission_logged = any(log.action == 'submitted' for log in activity_logs)
        
        if entry.submission_date and not submission_logged:
            user = User.query.get(entry.user_id)
            action_key = f"submitted_{entry.submission_date.strftime('%Y-%m-%d %H:%M:%S')}_{entry.user_id}"
            workflow_logs.append({
                'action': 'submitted',
                'action_display': 'Submitted',
                'user_name': user.full_name if user else 'Unknown',
                'user_role': user.role if user else 'bu_user',
                'comment': None,
                'created_at': entry.submission_date.strftime('%Y-%m-%d %H:%M:%S'),
                'icon': 'bi-send',
                'color': 'primary',
                'details': {'entry_id': entry.id, 'service_description': service_item.service_description[:100]}
            })
            seen_actions.add(action_key)
        
        # 2. Get activity logs from database (real logs only)
        for log in activity_logs:
            # Skip any fake/placeholder entries or redundant status updates
            if 'awaiting' in log.action.lower() or log.action == 'entry_status_updated':
                continue
            
            # Create a unique key for this action to prevent duplicates
            action_key = f"{log.action}_{log.created_at.strftime('%Y-%m-%d %H:%M:%S')}_{log.user_id}"
            if action_key in seen_actions:
                continue
            
            user = User.query.get(log.user_id)
            
            # Map action to display
            action_map = {
                'supervisor_approved': {'display': 'Supervisor Approved', 'icon': 'bi-check-circle', 'color': 'success'},
                'supervisor_rejected': {'display': 'Supervisor Rejected', 'icon': 'bi-x-circle', 'color': 'danger'},
                'head_approved': {'display': 'Head of IT Approved', 'icon': 'bi-check-circle', 'color': 'success'},
                'head_rejected': {'display': 'Head of IT Rejected', 'icon': 'bi-x-circle', 'color': 'danger'},
                'finance_approved': {'display': 'Finance Controller Approved', 'icon': 'bi-check-circle', 'color': 'success'},
                'finance_rejected': {'display': 'Finance Controller Rejected', 'icon': 'bi-x-circle', 'color': 'danger'},
                'invoice_generated': {'display': 'Invoice Generated', 'icon': 'bi-file-pdf', 'color': 'info'},
                'resubmitted': {'display': 'Resubmitted', 'icon': 'bi-arrow-repeat', 'color': 'warning'},
                'entry_status_updated': {'display': 'Status Updated', 'icon': 'bi-pencil', 'color': 'secondary'},
                'head_status_reset': {'display': 'Head Status Reset', 'icon': 'bi-arrow-repeat', 'color': 'warning'}
            }
            
            action_info = action_map.get(log.action, {
                'display': log.action.replace('_', ' ').title(),
                'icon': 'bi-info-circle',
                'color': 'secondary'
            })
            
            workflow_logs.append({
                'action': log.action,
                'action_display': action_info['display'],
                'user_name': user.full_name if user else 'System',
                'user_role': user.role if user else 'system',
                'comment': log.details.get('comment') if log.details else None,
                'created_at': log.created_at.strftime('%Y-%m-%d %H:%M:%S'),
                'icon': action_info['icon'],
                'color': action_info['color'],
                'details': log.details
            })
            seen_actions.add(action_key)
        
        # 3. Supervisor approval from service_item (if not already in logs)
        if service_item.supervisor_approved_at:
            action_key = f"supervisor_approved_{service_item.supervisor_approved_at.strftime('%Y-%m-%d %H:%M:%S')}_{service_item.supervisor_approved_by}"
            if action_key not in seen_actions:
                user = User.query.get(service_item.supervisor_approved_by)
                workflow_logs.append({
                    'action': 'supervisor_approved',
                    'action_display': 'Supervisor Approved',
                    'user_name': user.full_name if user else 'Unknown',
                    'user_role': 'supervisor',
                    'comment': service_item.supervisor_comment,
                    'created_at': service_item.supervisor_approved_at.strftime('%Y-%m-%d %H:%M:%S'),
                    'icon': 'bi-check-circle',
                    'color': 'success',
                    'details': {'service_item_id': service_item.id}
                })
                seen_actions.add(action_key)
        
        # 4. Supervisor rejection from service_item
        if service_item.supervisor_rejected_at:
            action_key = f"supervisor_rejected_{service_item.supervisor_rejected_at.strftime('%Y-%m-%d %H:%M:%S')}_{service_item.supervisor_rejected_by}"
            if action_key not in seen_actions:
                user = User.query.get(service_item.supervisor_rejected_by)
                workflow_logs.append({
                    'action': 'supervisor_rejected',
                    'action_display': 'Supervisor Rejected',
                    'user_name': user.full_name if user else 'Unknown',
                    'user_role': 'supervisor',
                    'comment': service_item.supervisor_comment,
                    'created_at': service_item.supervisor_rejected_at.strftime('%Y-%m-%d %H:%M:%S'),
                    'icon': 'bi-x-circle',
                    'color': 'danger',
                    'details': {'service_item_id': service_item.id}
                })
                seen_actions.add(action_key)
        
        # 5. Head of IT approval
        if service_item.head_approved_at:
            action_key = f"head_approved_{service_item.head_approved_at.strftime('%Y-%m-%d %H:%M:%S')}_{service_item.head_approved_by}"
            if action_key not in seen_actions:
                user = User.query.get(service_item.head_approved_by)
                workflow_logs.append({
                    'action': 'head_approved',
                    'action_display': 'Head of IT Approved',
                    'user_name': user.full_name if user else 'Unknown',
                    'user_role': user.role if user else 'head_of_it',
                    'comment': service_item.head_approval_comment,
                    'created_at': service_item.head_approved_at.strftime('%Y-%m-%d %H:%M:%S'),
                    'icon': 'bi-check-circle',
                    'color': 'success',
                    'details': {'service_item_id': service_item.id, 'company_id': service_item.company_id}
                })
                seen_actions.add(action_key)
        
        # 6. Head of IT rejection
        if service_item.head_rejected_at:
            action_key = f"head_rejected_{service_item.head_rejected_at.strftime('%Y-%m-%d %H:%M:%S')}_{service_item.head_rejected_by}"
            if action_key not in seen_actions:
                user = User.query.get(service_item.head_rejected_by)
                workflow_logs.append({
                    'action': 'head_rejected',
                    'action_display': 'Head of IT Rejected',
                    'user_name': user.full_name if user else 'Unknown',
                    'user_role': user.role if user else 'head_of_it',
                    'comment': service_item.head_approval_comment,
                    'created_at': service_item.head_rejected_at.strftime('%Y-%m-%d %H:%M:%S'),
                    'icon': 'bi-x-circle',
                    'color': 'danger',
                    'details': {'service_item_id': service_item.id, 'company_id': service_item.company_id}
                })
                seen_actions.add(action_key)
        
        # 7. Finance Controller approval
        if service_item.finance_approved_at:
            action_key = f"finance_approved_{service_item.finance_approved_at.strftime('%Y-%m-%d %H:%M:%S')}_{service_item.finance_approved_by}"
            if action_key not in seen_actions:
                user = User.query.get(service_item.finance_approved_by)
                workflow_logs.append({
                    'action': 'finance_approved',
                    'action_display': 'Finance Controller Approved',
                    'user_name': user.full_name if user else 'Unknown',
                    'user_role': user.role if user else 'finance_controller',
                    'comment': service_item.finance_approval_comment,
                    'created_at': service_item.finance_approved_at.strftime('%Y-%m-%d %H:%M:%S'),
                    'icon': 'bi-check-circle',
                    'color': 'success',
                    'details': {'service_item_id': service_item.id, 'total_amount': float(service_item.total_amount)}
                })
                seen_actions.add(action_key)
        
        # 8. Finance Controller rejection
        if service_item.finance_rejected_at:
            action_key = f"finance_rejected_{service_item.finance_rejected_at.strftime('%Y-%m-%d %H:%M:%S')}_{service_item.finance_rejected_by}"
            if action_key not in seen_actions:
                user = User.query.get(service_item.finance_rejected_by)
                workflow_logs.append({
                    'action': 'finance_rejected',
                    'action_display': 'Finance Controller Rejected',
                    'user_name': user.full_name if user else 'Unknown',
                    'user_role': user.role if user else 'finance_controller',
                    'comment': service_item.finance_approval_comment,
                    'created_at': service_item.finance_rejected_at.strftime('%Y-%m-%d %H:%M:%S'),
                    'icon': 'bi-x-circle',
                    'color': 'danger',
                    'details': {'service_item_id': service_item.id, 'company_id': service_item.company_id}
                })
                seen_actions.add(action_key)
        
        # 9. Invoice generation event - Simplified: Just show if invoice was generated
        if service_item.invoice_generated:
            already_logged = any(
                l.get('action') == 'invoice_generated' 
                for l in workflow_logs
            )
            if not already_logged:
                user = None
                if service_item.invoice_generated_by:
                    user = User.query.get(service_item.invoice_generated_by)
                
                inv_datetime = service_item.invoice_generated_at or datetime.utcnow()
                created_at_str = inv_datetime.strftime('%Y-%m-%d %H:%M:%S')
                action_key = f"invoice_generated_{created_at_str}"
                
                workflow_logs.append({
                    'action': 'invoice_generated',
                    'action_display': 'Invoice Generated',
                    'user_name': user.full_name if user else 'Payment Team',
                    'user_role': user.role if user else 'payment_team',
                    'comment': f'Invoice {service_item.invoice_number} generated' if service_item.invoice_number else 'Invoice generated',
                    'created_at': created_at_str,
                    'icon': 'bi-file-pdf',
                    'color': 'info',
                    'details': {'invoice_number': service_item.invoice_number} if service_item.invoice_number else {}
                })
        
        # Sort by date
        workflow_logs.sort(key=lambda x: x['created_at'])
        
        # Get company name
        company_name = 'N/A'
        if service_item.company_id:
            company = Company.query.get(service_item.company_id)
            if company:
                company_name = company.name
        
        # Get BU user details
        bu_user = User.query.get(entry.user_id)
        unit_name = 'N/A'
        if bu_user and bu_user.unit_id:
            unit = Unit.query.get(bu_user.unit_id)
            if unit:
                unit_name = unit.name
        
        # Prepare service data for template
        service_data = {
            'id': service_item.id,
            'entry_id': entry.id,
            'description': service_item.service_description,
            'total_amount': float(service_item.total_amount),
            'resources': service_item.resources if service_item.resources else [],
            'company_name': company_name,
            'submission_date': entry.submission_date.strftime('%Y-%m-%d %H:%M') if entry.submission_date else entry.created_at.strftime('%Y-%m-%d %H:%M'),
            'status': service_item.head_approval_status,
            'bu_user': {
                'full_name': bu_user.full_name if bu_user else 'Unknown',
                'employee_id': bu_user.employee_id if bu_user else 'N/A',
                'unit_name': unit_name
            }
        }
        
        return render_template('shared/workflow_view.html', 
                             service=service_data,
                             workflow=workflow_logs,
                             entry_id=entry.id)
        
    except Exception as e:
        print("Error in view_workflow:", str(e))
        traceback.print_exc()
        flash('Error loading workflow history. Please try again.', 'danger')
        return redirect(url_for('dashboard.index'))


# ============ PAYMENT TEAM API ENDPOINTS ============

@api_bp.route('/payment/entries', methods=['GET'])
@login_required
@role_required('payment_lolc_technologies', 'payment_lolc_finance')
def payment_get_entries():
    """Get entries for payment team dashboard"""
    try:
        # Get service items assigned to this payment team that are finance approved
        service_items = ServiceItem.query.filter_by(
            assigned_payment_team=current_user.role,
            finance_approval_status='approved'
        ).options(joinedload(ServiceItem.company))\
         .order_by(ServiceItem.created_at.desc()).all()
        
        # Pre‑fetch resource types for lookup
        all_resource_types = ResourceType.query.all()
        resource_by_id = {str(rt.id): rt for rt in all_resource_types}
        
        result = []
        for item in service_items:
            entry = TimeEntry.query.get(item.time_entry_id)
            if not entry:
                continue
            
            bu_user = User.query.get(entry.user_id)
            company = Company.query.get(item.company_id)
            
            # Process resources – convert stored JSON to displayable format
            processed_resources = []
            for resource in item.resources or []:
                resource_type_id = str(resource.get('type', ''))
                rt = resource_by_id.get(resource_type_id)
                if rt:
                    resource_name = rt.name
                    uom = rt.unit_of_measure or 'units'
                    chargeback_rate = float(rt.chargeback_rate) if rt.chargeback_rate else 0
                    showback_rate = float(rt.showback_rate) if rt.showback_rate else 0
                else:
                    resource_name = f"Unknown (ID {resource_type_id})"
                    uom = 'units'
                    chargeback_rate = 0
                    showback_rate = 0
                
                quantity = float(resource.get('quantity') or resource.get('hours') or 0)
                processed_resources.append({
                    'name': resource_name,
                    'quantity': quantity,
                    'hours': quantity, # Alias for frontend compatibility
                    'uom': uom,
                    'chargeback_rate': chargeback_rate,
                    'showback_rate': showback_rate,
                    'chargeback_amount': quantity * chargeback_rate,
                    'showback_amount': quantity * showback_rate
                })
            
            result.append({
                'id': entry.id,
                'service_item_id': item.id,
                'submission_date': entry.submission_date.strftime('%Y-%m-%d %H:%M:%S') if entry.submission_date else entry.created_at.strftime('%Y-%m-%d %H:%M:%S'),
                'bu_user': {
                    'full_name': bu_user.full_name if bu_user else 'Unknown',
                    'employee_id': bu_user.employee_id if bu_user else 'N/A'
                },
                'company_name': company.name if company else 'N/A',
                'description': item.service_description,
                'total_amount': float(item.total_amount),
                
                'resources': processed_resources,
                'invoice_generated': item.invoice_generated or False,
                'invoice_number': item.invoice_number
            })
        
        return jsonify({'success': True, 'entries': result})
        
    except Exception as e:
        print("Error in payment_get_entries:", str(e))
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500

@api_bp.route('/payment/invoices', methods=['GET'])
@login_required
@role_required('payment_lolc_technologies', 'payment_lolc_finance')
def payment_get_invoices():
    """Get invoices for payment team"""
    try:
        # Get service items that have invoices generated
        service_items = ServiceItem.query.filter_by(
            assigned_payment_team=current_user.role,
            invoice_generated=True
        ).order_by(ServiceItem.invoice_generated_at.desc()).all()
        
        invoices = []
        for item in service_items:
            entry = TimeEntry.query.get(item.time_entry_id)
            bu_user = User.query.get(entry.user_id) if entry else None
            company = Company.query.get(item.company_id)
            
            invoices.append({
                'id': item.id,
                'invoice_number': item.invoice_number,
                'invoice_date': item.invoice_generated_at.strftime('%Y-%m-%d') if item.invoice_generated_at else '',
                'total_amount': float(item.total_amount),
                'bu_user': bu_user.full_name if bu_user else 'Unknown',
                'company_name': company.name if company else 'N/A',
                'description': item.service_description[:50]
            })
        
        return jsonify({'success': True, 'invoices': invoices})
        
    except Exception as e:
        print("Error in payment_get_invoices:", str(e))
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500


@api_bp.route('/payment/bulk-invoice', methods=['POST'])
@login_required
@role_required('payment_lolc_technologies', 'payment_lolc_finance')
def payment_bulk_invoice():
    """Generate invoices for multiple service items"""
    try:
        data = request.get_json()
        service_item_ids = data.get('service_item_ids', [])
        
        if not service_item_ids:
            return jsonify({'success': False, 'error': 'No items selected'}), 400
        
        generated_count = 0
        errors = []
        generated_service_item_ids = []
        
        for service_item_id in service_item_ids:
            try:
                service_item = ServiceItem.query.get(service_item_id)
                if not service_item:
                    errors.append(f"Service item {service_item_id} not found")
                    continue
                
                if service_item.assigned_payment_team != current_user.role:
                    errors.append(f"Service item {service_item_id} not assigned to your team")
                    continue
                
                if service_item.invoice_generated:
                    errors.append(f"Service item {service_item_id} already invoiced")
                    continue
                
                invoice_service.create_service_item_invoice(
                    service_item, current_user.id
                )
                generated_count += 1
                generated_service_item_ids.append(service_item.id)
                
            except Exception as e:
                errors.append(f"Service item {service_item_id}: {str(e)}")
        
        db.session.commit()
        
        return jsonify({
            'success': True,
            'count': generated_count,
            'errors': errors,
            'service_item_ids': generated_service_item_ids,
            'pdf_generated': False,
        })
        
    except Exception as e:
        db.session.rollback()
        print("Error in payment_bulk_invoice:", str(e))
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500


@api_bp.route('/payment/send-notifications', methods=['POST'])
@login_required
@role_required('payment_lolc_technologies', 'payment_lolc_finance')
def payment_send_notifications():
    """Send email notifications for selected service items"""
    try:
        data = request.get_json()
        service_item_ids = data.get('service_item_ids', [])
        
        if not service_item_ids:
            return jsonify({'success': False, 'error': 'No items selected'}), 400
        
        sent_count = 0
        errors = []
        
        for service_item_id in service_item_ids:
            try:
                service_item = ServiceItem.query.get(service_item_id)
                if not service_item:
                    errors.append(f"Service item {service_item_id} not found")
                    continue
                
                entry = TimeEntry.query.get(service_item.time_entry_id)
                bu_user = User.query.get(entry.user_id)
                
                if bu_user:
                    msg = Message(
                        subject=f'Invoice {service_item.invoice_number} Ready',
                        recipients=[bu_user.email]
                    )
                    msg.body = f"""
Dear {bu_user.full_name},

Your invoice {service_item.invoice_number} has been generated.

Service Details:
- Company: {service_item.company.name if service_item.company else 'N/A'}
- Description: {service_item.service_description}
- Amount: Rs {service_item.total_amount}

You can view and download the invoice from your dashboard.

Thank you,
Invoice System Team
                    """
                    mail.send(msg)
                    sent_count += 1
                    
                    # Store notification
                    notification = Notification(
                        user_id=bu_user.id,
                        time_entry_id=entry.id,
                        notification_type='invoice_ready',
                        subject=f'Invoice {service_item.invoice_number} Ready',
                        message=f'Your invoice has been generated'
                    )
                    db.session.add(notification)
                    
            except Exception as e:
                errors.append(f"Service item {service_item_id}: {str(e)}")
        
        db.session.commit()
        
        return jsonify({
            'success': True,
            'count': sent_count,
            'errors': errors
        })
        
    except Exception as e:
        db.session.rollback()
        print("Error in payment_send_notifications:", str(e))
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500
    

@api_bp.route('/finance-controller/services/<int:service_id>/approve', methods=['POST'])
@login_required
@role_required('finance_controller')
def finance_controller_approve_service(service_id):
    """Finance Controller approves a specific service"""
    try:
        service_item = service_item_service.get_service_item(service_id)
        entry = TimeEntry.query.get(service_item.time_entry_id)
        
        data = request.get_json() or {}
        comment = data.get('comment', '')
        try:
            service_item_service.approve_service_item(
                service_item,
                user_id=current_user.id,
                comment=comment,
                remote_addr=request.remote_addr,
            )
        except ValueError as error:
            return jsonify({'success': False, 'error': str(error)}), 400
        company = service_item.company
        
        db.session.commit()
        
        # ========== SEND NOTIFICATIONS ==========
        
        # 1. Send notification to BU User (the one who submitted)
        bu_user = User.query.get(entry.user_id)
        if bu_user:
            send_notification(
                user=bu_user,
                entry=entry,
                service_item=service_item,
                type='finance_approved',
                url='/bu-team/dashboard'
            )
            print(f"Notification sent to BU User: {bu_user.email}")
        
        # 2. Send notification to the SPECIFIC payment user for this company
        # Find payment user assigned to this specific company
        company_payment_user = User.query.filter(
            User.role == 'payment_company_user',
            User.company_id == service_item.company_id
        ).first()
        
        if company_payment_user:
            send_notification(
                user=company_payment_user,
                entry=entry,
                service_item=service_item,
                type='ready_for_invoice',
                url='/payment/company-dashboard'
            )
            print(f"Notification sent to Company Payment User: {company_payment_user.email} for company {company.name if company else 'N/A'}")
        else:
            print(f"No specific payment user found for company {company.name if company else 'N/A'}")
        
        # 3. Also send notification to LOLC Technologies payment users (for oversight)
        # This ensures the central payment team is also aware
        lolc_tech_payment_users = User.query.filter(
            User.role == 'payment_lolc_technologies'
        ).all()
        
        # Also include payment_company_user with LOLC Technologies company
        lolc_tech_company_users = User.query.filter(
            User.role == 'payment_company_user',
            User.company.has(Company.is_parent_company.is_(True))
        ).all()
        
        all_lolc_tech_users = lolc_tech_payment_users + lolc_tech_company_users
        
        for payment_user in all_lolc_tech_users:
            if payment_user and payment_user.id != current_user.id:
                # Skip if this is the same as the company-specific user (to avoid duplicate)
                if company_payment_user and payment_user.id == company_payment_user.id:
                    continue
                send_notification(
                    user=payment_user,
                    entry=entry,
                    service_item=service_item,
                    type='ready_for_invoice',
                    url='/payment/dashboard'
                )
                print(f"Notification sent to LOLC Tech Payment User: {payment_user.email}")
        
        # 4. Send to admin users for oversight
        admin_users = User.query.filter_by(role='admin').all()
        for admin in admin_users:
            if admin and admin.id != current_user.id:
                send_notification(
                    user=admin,
                    entry=entry,
                    service_item=service_item,
                    type='ready_for_invoice',
                    url='/payment/dashboard'
                )
                print(f"Notification sent to Admin: {admin.email}")
        
        return jsonify({'success': True, 'message': 'Service approved and sent to payment team'})
        
    except Exception as e:
        db.session.rollback()
        print("Error in finance_controller_approve_service:", str(e))
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500


@api_bp.route('/finance-controller/services/<int:service_id>/reject', methods=['POST'])
@login_required
def finance_controller_reject_service(service_id):
    """Finance Controller rejects a specific service - returns to Head of IT"""
    if current_user.role != 'finance_controller':
        return jsonify({'success': False, 'error': 'Unauthorized'}), 403
    
    try:
        service_item = ServiceItem.query.get_or_404(service_id)
        entry = TimeEntry.query.get(service_item.time_entry_id)
        data = request.get_json()
        
        comment = data.get('comment', '')
        
        if not comment:
            return jsonify({'success': False, 'error': 'Comment required for rejection'}), 400
        
        # Update service item - FINANCE REJECTION
        service_item.finance_approval_status = 'rejected'
        service_item.finance_approval_comment = comment
        service_item.finance_rejected_by = current_user.id
        service_item.finance_rejected_at = datetime.utcnow()
        
        # CRITICAL: Reset HEAD approval to pending so Head of IT can review again
        service_item.head_approval_status = 'pending'
        service_item.head_approval_comment = None  # Clear previous comment
        service_item.head_approved_by = None
        service_item.head_approved_at = None
        
        # Update entry status
        all_items = ServiceItem.query.filter_by(time_entry_id=entry.id).all()
        any_pending = any(i.head_approval_status == 'pending' for i in all_items)
        any_rejected = any(i.head_approval_status == 'rejected' for i in all_items)
        
        if any_pending:
            entry.status = 'submitted'
        elif any_rejected:
            entry.status = 'head_rejected'
        else:
            entry.status = 'head_approved'
        
        # Create activity log
        log = ActivityLog(
            time_entry_id=entry.id,
            user_id=current_user.id,
            action='finance_rejected',
            details={
                'service_item_id': service_item.id,
                'comment': comment,
                'ip': request.remote_addr
            }
        )
        db.session.add(log)
        
        db.session.commit()
        
        print(f"\n=== Finance Rejection ===")
        print(f"Service ID: {service_id}")
        print(f"Head approval status reset to: {service_item.head_approval_status}")
        print(f"Entry {entry.id} status: {entry.status}")
        
        # Send notification to Head of IT
        head_of_it = User.query.filter_by(role='head_of_it', company_id=service_item.company_id).first()
        if head_of_it:
            send_notification(
                user=head_of_it,
                entry=entry,
                service_item=service_item,
                type='finance_rejected',
                url='/head-of-it/dashboard'
            )
            print(f"Notification sent to Head of IT: {head_of_it.email}")
        
        # Send notification to BU user
        bu_user = User.query.get(entry.user_id)
        if bu_user:
            send_notification(
                user=bu_user,
                entry=entry,
                service_item=service_item,
                type='finance_rejected',
                url='/bu-team/dashboard'
            )
        
        return jsonify({'success': True, 'message': 'Service rejected and returned to Head of IT for re-review'})
        
    except Exception as e:
        db.session.rollback()
        print("Error in finance_controller_reject_service:", str(e))
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500
    
@api_bp.route('/finance-controller/batch-approve', methods=['POST'])
@login_required
def finance_controller_batch_approve():
    """Finance Controller approves multiple services at once (same as single approve)"""
    if current_user.role != 'finance_controller':
        return jsonify({'success': False, 'error': 'Unauthorized'}), 403

    try:
        data = request.get_json()
        service_ids = data.get('service_ids', [])
        comment = data.get('comment', '')

        if not service_ids:
            return jsonify({'success': False, 'error': 'No services selected'}), 400

        approved_count = 0
        # Keep track of entries that become fully approved
        entries_to_update = set()

        for service_id in service_ids:
            service_item = ServiceItem.query.get(service_id)
            if not service_item:
                continue
            # Only allow if head_approval_status is 'approved'
            if service_item.head_approval_status != 'approved':
                continue

            # Update service item (same as single approve)
            service_item.finance_approval_status = 'approved'
            service_item.finance_approval_comment = comment
            service_item.finance_approved_by = current_user.id
            service_item.finance_approved_at = datetime.utcnow()

            # Determine payment team based on company (same as single approve)
            company = Company.query.get(service_item.company_id)
            if company:
                service_item.assigned_payment_team = company_service.get_payment_team_for_company(company)

            approved_count += 1
            entries_to_update.add(service_item.time_entry_id)

        # After updating all services, check each parent entry
        for entry_id in entries_to_update:
            entry = TimeEntry.query.get(entry_id)
            if not entry:
                continue
            # Check if all services in this entry are now finance-approved
            all_services = ServiceItem.query.filter_by(time_entry_id=entry.id).all()
            if all(s.finance_approval_status == 'approved' for s in all_services):
                entry.status = 'finance_approved'
                db.session.add(entry)

        db.session.commit()

        # Send notifications (same as single approve)
        # For each entry that became fully approved, notify BU user and payment team
        for entry_id in entries_to_update:
            entry = TimeEntry.query.get(entry_id)
            if not entry or entry.status != 'finance_approved':
                continue

            # Notify BU user
            bu_user = User.query.get(entry.user_id)
            if bu_user:
                send_notification(
                    user=bu_user,
                    entry=entry,
                    service_item=None,
                    type='finance_approved',
                    url='/bu-team/dashboard'
                )

            # Notify payment teams for each service item
            services = ServiceItem.query.filter_by(time_entry_id=entry.id).all()
            for item in services:
                if item.assigned_payment_team:
                    payment_team = User.query.filter_by(role=item.assigned_payment_team).first()
                    if payment_team:
                        send_notification(
                            user=payment_team,
                            entry=entry,
                            service_item=item,
                            type='ready_for_invoice',
                            url='/payment/dashboard'
                        )

        return jsonify({'success': True, 'message': f'Approved {approved_count} services'})

    except Exception as e:
        db.session.rollback()
        print(f"Error in batch approve: {str(e)}")
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500


@api_bp.route('/finance-controller/batch-reject', methods=['POST'])
@login_required
def finance_controller_batch_reject():
    """Finance Controller rejects multiple services at once (same as single reject)"""
    if current_user.role != 'finance_controller':
        return jsonify({'success': False, 'error': 'Unauthorized'}), 403

    try:
        data = request.get_json()
        service_ids = data.get('service_ids', [])
        comment = data.get('comment', '')

        if not service_ids:
            return jsonify({'success': False, 'error': 'No services selected'}), 400

        if not comment:
            return jsonify({'success': False, 'error': 'Comment required for rejection'}), 400

        rejected_count = 0
        entries_to_update = set()

        for service_id in service_ids:
            service_item = ServiceItem.query.get(service_id)
            if not service_item:
                continue

            # Same as single reject
            service_item.finance_approval_status = 'rejected'
            service_item.finance_approval_comment = comment
            service_item.finance_rejected_by = current_user.id
            service_item.finance_rejected_at = datetime.utcnow()

            # CRITICAL: Reset head_approval_status so Head of IT can review again
            service_item.head_approval_status = 'pending'
            service_item.head_approval_comment = None
            service_item.head_approved_by = None
            service_item.head_approved_at = None

            rejected_count += 1
            entries_to_update.add(service_item.time_entry_id)

        # Update parent entries
        for entry_id in entries_to_update:
            entry = TimeEntry.query.get(entry_id)
            if not entry:
                continue
            # Determine new entry status based on service items
            all_items = ServiceItem.query.filter_by(time_entry_id=entry.id).all()
            any_pending = any(i.head_approval_status == 'pending' for i in all_items)
            any_rejected = any(i.head_approval_status == 'rejected' for i in all_items)

            if any_pending:
                entry.status = 'submitted'
            elif any_rejected:
                entry.status = 'head_rejected'
            else:
                entry.status = 'head_approved'

        db.session.commit()

        # Send notifications to Head of IT (same as single reject)
        # Group by company to avoid duplicate notifications
        companies_notified = set()
        for service_id in service_ids:
            service_item = ServiceItem.query.get(service_id)
            if not service_item:
                continue
            if service_item.company_id in companies_notified:
                continue
            head_of_it = User.query.filter_by(
                role='head_of_it',
                company_id=service_item.company_id
            ).first()
            if head_of_it:
                send_notification(
                    user=head_of_it,
                    entry=TimeEntry.query.get(service_item.time_entry_id),
                    service_item=service_item,
                    type='finance_rejected',
                    url='/head-of-it/dashboard'
                )
                companies_notified.add(service_item.company_id)

            # Also notify BU user
            entry = TimeEntry.query.get(service_item.time_entry_id)
            if entry:
                bu_user = User.query.get(entry.user_id)
                if bu_user:
                    send_notification(
                        user=bu_user,
                        entry=entry,
                        service_item=service_item,
                        type='finance_rejected',
                        url='/bu-team/dashboard'
                    )

        return jsonify({'success': True, 'message': f'Rejected {rejected_count} services'})

    except Exception as e:
        db.session.rollback()
        print(f"Error in batch reject: {str(e)}")
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500


@api_bp.route('/payment/service-items/<int:service_item_id>/generate-invoice', methods=['POST'])
@login_required
@role_required('payment_lolc_technologies', 'payment_lolc_finance')
def payment_generate_invoice_single(service_item_id):
    """Generate invoice for a specific service item"""
    try:
        service_item = invoice_service.get_service_invoice(service_item_id)
        entry = TimeEntry.query.get(service_item.time_entry_id)
        
        # Check authorization
        if service_item.assigned_payment_team != current_user.role:
            return jsonify({'success': False, 'error': 'Not authorized for this service'}), 403
        
        invoice_number = invoice_service.create_service_item_invoice(
            service_item, current_user.id
        )
        
        db.session.commit()
        
        return jsonify({
            'success': True,
            'invoice_number': invoice_number,
            'message': 'Invoice record created successfully',
            'pdf_generated': False,
        })
        
    except Exception as e:
        db.session.rollback()
        print("Error in payment_generate_invoice_single:", str(e))
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500
    

@api_bp.route('/invoices/<int:service_item_id>/generate-pdf', methods=['POST'])
@login_required
def generate_service_invoice_pdf(service_item_id):
    """Generate and store the PDF for an existing service-item invoice."""
    service_item = invoice_service.get_service_invoice(service_item_id)
    if not (
        current_user.role == 'super_admin'
        or (
            current_user.role == service_item.assigned_payment_team
            and current_user.role != 'payment_company_user'
        )
        or company_service.can_access_company(current_user, service_item.company_id)
    ):
        return jsonify({'success': False, 'error': 'Unauthorized'}), 403

    try:
        pdf_bytes = invoice_service.render_service_item_invoice_pdf(service_item)
        service_item.invoice_pdf = pdf_bytes
        service_item.invoice_pdf_filename = f'{service_item.invoice_number}.pdf'
        db.session.commit()
        bu_user = User.query.get(service_item.time_entry.user_id)
        if bu_user:
            send_notification(
                bu_user,
                service_item.time_entry,
                service_item,
                'invoiced',
                '/bu-team/dashboard',
            )
        return send_file(
            io.BytesIO(pdf_bytes),
            mimetype='application/pdf',
            as_attachment=True,
            download_name=service_item.invoice_pdf_filename,
        )
    except ValueError as error:
        db.session.rollback()
        return jsonify({'success': False, 'error': str(error)}), 400
    except Exception as error:
        db.session.rollback()
        current_app.logger.exception('Failed to generate PDF for service item %s', service_item_id)
        return jsonify({'success': False, 'error': str(error)}), 500


@api_bp.route('/invoices/<int:service_item_id>/download-pdf', methods=['GET'])
@login_required
def download_service_invoice_pdf(service_item_id):
    """Download a previously generated service-item invoice PDF."""
    service_item = invoice_service.get_service_invoice(service_item_id)
    if not (
        current_user.role == 'super_admin'
        or current_user.role == 'admin'
        or (
            current_user.role == service_item.assigned_payment_team
            and current_user.role != 'payment_company_user'
        )
        or company_service.can_access_company(current_user, service_item.company_id)
        or (
            current_user.role == 'bu_user'
            and service_item.time_entry
            and service_item.time_entry.user_id == current_user.id
        )
    ):
        return jsonify({'success': False, 'error': 'Unauthorized'}), 403
    if not service_item.invoice_pdf:
        return jsonify({'success': False, 'error': 'Invoice PDF has not been generated'}), 404
    return send_file(
        io.BytesIO(service_item.invoice_pdf),
        mimetype='application/pdf',
        as_attachment=True,
        download_name=service_item.invoice_pdf_filename or f'{service_item.invoice_number}.pdf',
    )





@api_bp.route('/payment/generate-all-company-invoices', methods=['POST'])
@login_required
@role_required('super_admin')
def payment_generate_all_company_invoices():
    """Generate monthly master invoice for the selected month/year."""
    
    try:
        from calendar import monthrange
        from datetime import date, datetime

        data = request.get_json() or {}
        year = data.get('year')
        month = data.get('month')

        if year is None or month is None:
            today = datetime.now().date()
            year = today.year
            month = today.month
        else:
            year = int(year)
            month = int(month)

        start_date = date(year, month, 1)
        end_date = date(year, month, monthrange(year, month)[1])

        services = ServiceItem.query.join(TimeEntry).filter(
            ServiceItem.finance_approval_status == 'approved',
            TimeEntry.service_date >= start_date,
            TimeEntry.service_date <= end_date
        ).all()

        if not services:
            return jsonify({'success': False, 'error': f'No approved services for {year}-{month:02d}'}), 400

        all_resource_types = ResourceType.query.all()
        resource_by_id = {str(rt.id): rt for rt in all_resource_types}

        companies_dict = {}
        for item in services:
            company = Company.query.get(item.company_id)
            if not company:
                continue
            if company.id not in companies_dict:
                companies_dict[company.id] = {
                    'name': company.name,
                    'services': [],
                    'total_amount': 0
                }
            entry = TimeEntry.query.get(item.time_entry_id)
            bu_user = User.query.get(entry.user_id) if entry else None
            unit_name = 'N/A'
            if bu_user and bu_user.unit_id:
                unit = Unit.query.get(bu_user.unit_id)
                if unit:
                    unit_name = unit.name

            processed_resources = []
            for resource in item.resources or []:
                resource_type_id = str(resource.get('type', ''))
                rt = resource_by_id.get(resource_type_id)
                if rt:
                    res_name = rt.name
                    uom = rt.unit_of_measure or 'units'
                    chargeback_rate = float(rt.chargeback_rate) if rt.chargeback_rate else 0
                else:
                    res_name = f"Unknown ({resource_type_id})"
                    uom = 'units'
                    chargeback_rate = 0
                quantity = resource.get('quantity', 0)
                processed_resources.append({
                    'name': res_name,
                    'quantity': quantity,
                    'uom': uom,
                    'chargeback_rate': chargeback_rate,
                    'chargeback_amount': quantity * chargeback_rate
                })

            amount = float(item.total_amount)
            companies_dict[company.id]['total_amount'] += amount
            companies_dict[company.id]['services'].append({
                'submission_date': entry.submission_date.strftime('%Y-%m-%d %H:%M') if entry and entry.submission_date else entry.created_at.strftime('%Y-%m-%d %H:%M'),
                'bu_user': bu_user.full_name if bu_user else 'Unknown',
                'employee_id': bu_user.employee_id if bu_user else 'N/A',
                'team_name': unit_name,
                'description': item.service_description,
                'resources': processed_resources,
                'amount': amount
            })

        companies_list = list(companies_dict.values())
        total_amount = sum(c['total_amount'] for c in companies_list)

        invoice, is_new = invoice_service.get_or_create_monthly_invoice(
            year, month, current_user.id
        )

        from app.utils.pdf_generator import generate_master_invoice_pdf
        invoice_date = datetime.now().strftime('%Y-%m-%d')
        period = f"{start_date.strftime('%Y-%m-%d')} to {end_date.strftime('%Y-%m-%d')}"
        pdf_bytes = generate_master_invoice_pdf(companies_list, invoice.invoice_number, invoice_date, period)

        invoice_service.store_monthly_invoice_pdf(
            invoice,
            pdf_bytes,
            total_amount,
            f"monthly_invoice_{year}_{month:02d}.pdf",
        )
        invoice_service.mark_monthly_services_invoiced(services, invoice.invoice_number)
        db.session.commit()

        return jsonify({
            'success': True,
            'count': 1,
            'invoice_number': invoice.invoice_number,
            'message': f'Monthly invoice generated for {year}-{month:02d} and {len(services)} service items marked as invoiced.'
        })

    except Exception as e:
        db.session.rollback()
        print("Error in generate monthly invoice:", str(e))
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500
    

@api_bp.route('/payment/company-invoice', methods=['POST'])
@login_required
@role_required('payment_lolc_technologies', 'payment_lolc_finance')
def payment_company_invoice():
    """Generate a single invoice for all services of a company"""
    try:
        data = request.get_json()
        company_id = data.get('company_id')
        
        if not company_id:
            return jsonify({'success': False, 'error': 'Company ID required'}), 400
        
        company = Company.query.get(company_id)
        if not company:
            return jsonify({'success': False, 'error': 'Company not found'}), 404
        
        # Get all services for this company that are finance approved and not yet invoiced
        services = ServiceItem.query.filter(
            ServiceItem.company_id == company_id,
            ServiceItem.finance_approval_status == 'approved',
            ServiceItem.invoice_generated == False
        ).all()
        
        if not services:
            return jsonify({'success': False, 'error': 'No pending services found for this company'}), 400
        
        invoice_number, _pdf_bytes = invoice_service.create_company_invoice(
            services, company, current_user.id
        )
        
        db.session.commit()
        
        return jsonify({
            'success': True,
            'invoice_number': invoice_number,
            'message': f'Invoice generated for {company.name}'
        })
        
    except Exception as e:
        db.session.rollback()
        print("Error in payment_company_invoice:", str(e))
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500

@api_bp.route('/payment/master-invoice', methods=['GET'])
@login_required
@role_required('super_admin')
def payment_master_invoice():
    """Return stored monthly master invoice data (for preview modal)."""
    
    today = datetime.now().date()
    year = request.args.get('year', today.year, type=int)
    month = request.args.get('month', today.month, type=int)

    invoice = MonthlyMasterInvoice.query.filter_by(year=year, month=month).first()
    if not invoice:
        return jsonify({'success': False, 'error': f'No invoice found for {year}-{month}'}), 404

    start_date = date(year, month, 1)
    end_date = date(year, month, monthrange(year, month)[1])
    services = ServiceItem.query.join(TimeEntry).filter(
        ServiceItem.finance_approval_status == 'approved',
        TimeEntry.service_date >= start_date,
        TimeEntry.service_date <= end_date
    ).all()

    all_resource_types = ResourceType.query.all()
    resource_by_id = {str(rt.id): rt for rt in all_resource_types}

    companies_dict = {}
    for item in services:
        company = Company.query.get(item.company_id)
        if not company:
            continue

        if company.id not in companies_dict:
            companies_dict[company.id] = {
                'name': company.name,
                'services': [],
                'total_amount': 0.0
            }

        entry = TimeEntry.query.get(item.time_entry_id)
        if not entry:
            continue

        bu_user = User.query.get(entry.user_id) if entry else None
        unit_name = 'N/A'
        if bu_user and bu_user.unit_id:
            unit = Unit.query.get(bu_user.unit_id)
            if unit:
                unit_name = unit.name

        processed_resources = []
        for resource in item.resources or []:
            resource_type_id = str(resource.get('type', ''))
            rt = resource_by_id.get(resource_type_id)
            if rt:
                res_name = rt.name
                uom = rt.unit_of_measure or 'units'
                chargeback_rate = float(rt.chargeback_rate) if rt.chargeback_rate else 0
            else:
                res_name = f"Unknown ({resource_type_id})"
                uom = 'units'
                chargeback_rate = 0
            quantity = resource.get('quantity', 0)
            processed_resources.append({
                'name': res_name,
                'quantity': quantity,
                'uom': uom,
                'chargeback_rate': chargeback_rate,
                'chargeback_amount': quantity * chargeback_rate
            })

        amount = float(item.total_amount)
        companies_dict[company.id]['services'].append({
            'submission_date': entry.submission_date.strftime('%Y-%m-%d %H:%M:%S') if entry.submission_date else entry.created_at.strftime('%Y-%m-%d %H:%M:%S'),
            'bu_user': bu_user.full_name if bu_user else 'Unknown',
            'employee_id': bu_user.employee_id if bu_user else 'N/A',
            'team_name': unit_name,
            'description': item.service_description,
            'resources': processed_resources,
            'amount': amount
        })
        companies_dict[company.id]['total_amount'] += amount

    companies_list = list(companies_dict.values())
    grand_total = sum(c['total_amount'] for c in companies_list)

    return jsonify({
        'success': True,
        'invoice': {
            'invoice_number': invoice.invoice_number,
            'date': invoice.generated_at.strftime('%Y-%m-%d'),
            'period': f"{invoice.period_start.strftime('%Y-%m-%d')} to {invoice.period_end.strftime('%Y-%m-%d')}",
            'companies': companies_list,
            'grand_total': grand_total
        }
    })
    



@api_bp.route('/payment/master-invoice/download', methods=['GET'])
@login_required
@role_required('super_admin')
def download_master_invoice_pdf():
    """Download the stored monthly master invoice PDF."""
    
    today = datetime.now().date()
    year = request.args.get('year', today.year, type=int)
    month = request.args.get('month', today.month, type=int)

    invoice = MonthlyMasterInvoice.query.filter_by(year=year, month=month).first()
    if not invoice or not invoice.invoice_pdf:
        return jsonify({'success': False, 'error': 'PDF not found – please generate invoice first'}), 404

    return send_file(
        io.BytesIO(invoice.invoice_pdf),
        mimetype='application/pdf',
        as_attachment=True,
        download_name=invoice.pdf_filename or f"monthly_invoice_{year}_{month:02d}.pdf"
    )


    

# ============ PAYMENT COMPANY USER REPORTS (Non-LOLC Technologies) ============

@api_bp.route('/reports/company-preview', methods=['GET'])
@login_required
def company_reports_preview():
    """Preview report data for non-LOLC Technologies payment users"""
    if current_user.role != 'payment_company_user':
        return jsonify({'success': False, 'error': 'Unauthorized'}), 403
    
    if not current_user.company_id:
        return jsonify({'success': False, 'error': 'No company assigned'}), 400
    
    try:
        date_from = request.args.get('from')
        date_to = request.args.get('to')
        is_parent_payment_user = company_service.is_parent_company_user(current_user)
        requested_company_id = request.args.get('company_id', type=int)
        if (
            not is_parent_payment_user
            and requested_company_id
            and not company_service.can_access_company(current_user, requested_company_id)
        ):
            return jsonify({'success': False, 'error': 'Unauthorized company'}), 403
        company_id = None if is_parent_payment_user else current_user.company_id
        selected_company = Company.query.get(company_id) if company_id else None
        
        query = ServiceItem.query.filter(
            ServiceItem.finance_approval_status == 'approved'
        )
        if company_id:
            query = query.filter(ServiceItem.company_id == company_id)
        
        if date_from:
            query = query.filter(ServiceItem.created_at >= date_from)
        if date_to:
            query = query.filter(ServiceItem.created_at <= date_to + ' 23:59:59')
        
        items = query.all()
        
        # Get resource types for lookup
        all_resource_types = ResourceType.query.all()
        resource_by_id = {str(rt.id): rt for rt in all_resource_types}
        
        records = []
        total_amount = 0
        total_loit = 0
        total_lolc = 0
        
        for item in items:
            entry = TimeEntry.query.get(item.time_entry_id)
            if not entry:
                continue
            
            company = Company.query.get(item.company_id)
            bu_user = User.query.get(entry.user_id)
            
            unit_name = 'N/A'
            if bu_user and bu_user.unit_id:
                unit = Unit.query.get(bu_user.unit_id)
                if unit:
                    unit_name = unit.name
            
            # Calculate LOIT values and collect resource details
            loit_cb = 0
            loit_sb = 0
            lolc_sb = 0
            resource_details = []
            
            for resource in item.resources or []:
                resource_type_id = str(resource.get('type', ''))
                rt = resource_by_id.get(resource_type_id)
                quantity = float(resource.get('quantity') or resource.get('hours') or 0)
                
                if rt:
                    loit_cb_rate = float(rt.loit_chargeback or 0)
                    loit_sb_rate = float(rt.loit_showback or 0)
                    lolc_sb_rate = float(rt.showback_rate or 0)
                    
                    loit_cb += quantity * loit_cb_rate
                    loit_sb += quantity * loit_sb_rate
                    lolc_sb += quantity * lolc_sb_rate
                    
                    resource_details.append({
                        'name': rt.name,
                        'quantity': quantity,
                        'uom': rt.unit_of_measure or 'units',
                        'loit_cb_rate': loit_cb_rate,
                        'loit_sb_rate': loit_sb_rate,
                        'lolc_sb_rate': lolc_sb_rate
                    })
            
            loit_total = loit_cb + loit_sb
            grand_total = loit_total + lolc_sb
            
            total_loit += loit_total
            total_lolc += lolc_sb
            total_amount += grand_total
            
            record = {
                'company_name': company.name if company else 'N/A',
                'date': entry.service_date.strftime('%Y-%m-%d') if entry.service_date else entry.created_at.strftime('%Y-%m-%d'),
                'submission_date': entry.submission_date.strftime('%Y-%m-%d %H:%M') if entry.submission_date else entry.created_at.strftime('%Y-%m-%d %H:%M'),
                'bu_user': bu_user.full_name if bu_user else 'Unknown',
                'employee_id': bu_user.employee_id if bu_user else 'N/A',
                'team': unit_name,
                'description': item.service_description,
                'resources': resource_details,
                'loit_total': loit_total,
                'lolc_sb': lolc_sb,
                'grand_total': grand_total,
                'head_status': item.head_approval_status,
                'finance_status': item.finance_approval_status,
                'invoice_number': item.invoice_number or 'N/A'
            }
            records.append(record)
        
        return jsonify({
            'success': True,
            'data': {
                'records': records,
                'total_loit': total_loit,
                'total_lolc': total_lolc,
                'total_amount': total_amount,
                'company_name': selected_company.name if selected_company else 'All Companies'
            }
        })
        
    except Exception as e:
        print("Error in company_reports_preview:", str(e))
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500



@api_bp.route('/reports/company-export', methods=['GET'])
@login_required
def company_reports_export():
    """Export report data for non-LOLC Technologies payment users - Simplified view"""
    if current_user.role != 'payment_company_user':
        return jsonify({'success': False, 'error': 'Unauthorized'}), 403
    
    if not current_user.company_id:
        return jsonify({'success': False, 'error': 'No company assigned'}), 400
    
    try:
        date_from = request.args.get('from')
        date_to = request.args.get('to')
        format_type = request.args.get('format', 'excel')
        is_parent_payment_user = company_service.is_parent_company_user(current_user)
        requested_company_id = request.args.get('company_id', type=int)
        if (
            not is_parent_payment_user
            and requested_company_id
            and not company_service.can_access_company(current_user, requested_company_id)
        ):
            return jsonify({'success': False, 'error': 'Unauthorized company'}), 403
        company_id = None if is_parent_payment_user else current_user.company_id
        selected_company = Company.query.get(company_id) if company_id else None
        
        # Build query for service items that are finance approved
        query = ServiceItem.query.filter(
            ServiceItem.finance_approval_status == 'approved'
        )
        if company_id:
            query = query.filter(ServiceItem.company_id == company_id)
        
        if date_from:
            query = query.filter(ServiceItem.created_at >= date_from)
        if date_to:
            query = query.filter(ServiceItem.created_at <= date_to + ' 23:59:59')
        
        items = query.all()
        
        # Get resource types for lookup
        all_rt = ResourceType.query.all()
        rt_by_id = {str(rt.id): rt for rt in all_rt}
        
        report_name = selected_company.name if selected_company else 'All Companies'
        
        data = []
        for item in items:
            entry = TimeEntry.query.get(item.time_entry_id)
            if not entry:
                continue
            
            company = Company.query.get(item.company_id)
            bu_user = User.query.get(entry.user_id)
            
            unit_name = 'N/A'
            if bu_user and bu_user.unit_id:
                unit = Unit.query.get(bu_user.unit_id)
                if unit:
                    unit_name = unit.name
            
            submission_date = entry.service_date.strftime('%Y-%m-%d') if entry.service_date else entry.created_at.strftime('%Y-%m-%d')
            
            # Calculate total quantity from all resources
            total_quantity = 0
            for resource in item.resources or []:
                quantity = float(resource.get('quantity') or resource.get('hours') or 0)
                total_quantity += quantity
            
            # ========== SUPERVISOR DETAILS ==========
            supervisor_name = ''
            supervisor_status = item.supervisor_approval_status or ''
            supervisor_approval_date = ''
            supervisor_comment = item.supervisor_comment or ''
            
            if item.supervisor_approved_at:
                supervisor_approval_date = item.supervisor_approved_at.strftime('%Y-%m-%d %H:%M:%S')
            elif item.supervisor_rejected_at:
                supervisor_approval_date = item.supervisor_rejected_at.strftime('%Y-%m-%d %H:%M:%S')
            
            if item.supervisor_approved_by:
                supervisor = User.query.get(item.supervisor_approved_by)
                supervisor_name = supervisor.full_name if supervisor else ''
            elif item.supervisor_rejected_by:
                supervisor = User.query.get(item.supervisor_rejected_by)
                supervisor_name = supervisor.full_name if supervisor else ''
            
            # ========== HEAD OF IT DETAILS ==========
            head_of_it_name = ''
            head_status = item.head_approval_status or ''
            head_approval_date = ''
            head_comment = item.head_approval_comment or ''
            
            if item.head_approved_at:
                head_approval_date = item.head_approved_at.strftime('%Y-%m-%d %H:%M:%S')
            elif item.head_rejected_at:
                head_approval_date = item.head_rejected_at.strftime('%Y-%m-%d %H:%M:%S')
            
            if item.head_approved_by:
                head_of_it = User.query.get(item.head_approved_by)
                head_of_it_name = head_of_it.full_name if head_of_it else ''
            elif item.head_rejected_by:
                head_of_it = User.query.get(item.head_rejected_by)
                head_of_it_name = head_of_it.full_name if head_of_it else ''
            
            # ========== FINANCE CONTROLLER DETAILS ==========
            finance_name = ''
            finance_status = item.finance_approval_status or ''
            finance_approval_date = ''
            finance_comment = item.finance_approval_comment or ''
            
            if item.finance_approved_at:
                finance_approval_date = item.finance_approved_at.strftime('%Y-%m-%d %H:%M:%S')
            elif item.finance_rejected_at:
                finance_approval_date = item.finance_rejected_at.strftime('%Y-%m-%d %H:%M:%S')
            
            if item.finance_approved_by:
                finance_controller = User.query.get(item.finance_approved_by)
                finance_name = finance_controller.full_name if finance_controller else ''
            elif item.finance_rejected_by:
                finance_controller = User.query.get(item.finance_rejected_by)
                finance_name = finance_controller.full_name if finance_controller else ''
            
            # Calculate LOIT values
            loit_total = 0
            lolc_sb = 0
            
            for resource in item.resources or []:
                resource_type_id = str(resource.get('type', ''))
                rt = rt_by_id.get(resource_type_id)
                
                if rt:
                    quantity = float(resource.get('quantity') or resource.get('hours') or 0)
                    loit_cb = quantity * float(rt.loit_chargeback or 0)
                    loit_sb = quantity * float(rt.loit_showback or 0)
                    loit_total += loit_cb + loit_sb
                    lolc_sb += quantity * float(rt.showback_rate or 0)
            
            grand_total = loit_total + lolc_sb
            
            showback_label = get_company_showback_label(company.name if company else 'N/A', include_rs=True)
            data.append({
                'Company': company.name if company else 'N/A',
                'Date': submission_date,
                'BU User': bu_user.full_name if bu_user else 'Unknown',
                'Employee ID': bu_user.employee_id if bu_user else 'N/A',
                'Unit': unit_name,
                'Service Description': item.service_description,
                'Quantity': total_quantity,
                # Supervisor details
                'Supervisor': supervisor_name,
                'Supervisor Approval Status': supervisor_status,
                'Supervisor Approval Date': supervisor_approval_date,
                'Supervisor Comment': supervisor_comment,
                # Head of IT details
                'Head of IT': head_of_it_name,
                'Head Approval Status': head_status,
                'Head Approval Date': head_approval_date,
                'Head Comment': head_comment,
                # Finance Controller details
                'Finance Controller': finance_name,
                'Finance Approval Status': finance_status,
                'Finance Approval Date': finance_approval_date,
                'Finance Comment': finance_comment,
                # Invoice details
                'Invoice Number': item.invoice_number or 'N/A',
                # Financials
                'LOIT Total (Rs)': loit_total,
                'showback_total': lolc_sb,
                'showback_label': showback_label,
                'Grand Total (Rs)': grand_total,
                'Status': item.head_approval_status
            })
        
        if format_type == 'excel':
            return generate_company_complete_excel_report(data, report_name)
        elif format_type == 'csv':
            return generate_company_simple_csv_report(data, report_name)
        else:
            return jsonify({'success': False, 'error': 'Unsupported format'}), 400
        
    except Exception as e:
        print("Error in company_reports_export:", str(e))
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500


def generate_company_complete_excel_report(data, company_name):
    """Generate complete Excel report for a specific company with all details including Quantity"""
    import openpyxl
    from openpyxl.styles import Font, Alignment, PatternFill, Border, Side
    from openpyxl.utils import get_column_letter
    import io
    from flask import Response
    from datetime import datetime

    showback_header = get_company_showback_label(company_name, include_rs=True)

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = f"{company_name[:31]} Report"

    headers = [
        'Company', 'Date', 'BU User', 'Employee ID', 'Unit', 'Service Description', 'Quantity',
        'Supervisor', 'Supervisor Approval Status', 'Supervisor Approval Date', 'Supervisor Comment',
        'Head of IT', 'Head Approval Status', 'Head Approval Date', 'Head Comment',
        'Finance Controller', 'Finance Approval Status', 'Finance Approval Date', 'Finance Comment',
        'Invoice Number',
        'LOIT Total (Rs)', showback_header, 'Grand Total (Rs)', 'Status'
    ]

    header_fill = PatternFill(start_color="1E3A5F", end_color="1E3A5F", fill_type="solid")
    header_font = Font(color="FFFFFF", bold=True)
    header_alignment = Alignment(horizontal="center", vertical="center")
    thin_border = Border(left=Side(style='thin'), right=Side(style='thin'),
                         top=Side(style='thin'), bottom=Side(style='thin'))

    # Title
    ws.merge_cells('A1:W1')
    title_cell = ws['A1']
    title_cell.value = f"Service Report - {company_name}"
    title_cell.font = Font(size=16, bold=True)
    title_cell.alignment = Alignment(horizontal="center")

    ws['A2'] = f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}"
    ws['A2'].font = Font(italic=True, size=9)

    # Headers
    header_row = 4
    for col, header in enumerate(headers, 1):
        cell = ws.cell(row=header_row, column=col, value=header)
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = header_alignment
        cell.border = thin_border

    # Write data rows
    current_row = header_row + 1
    total_loit = 0
    total_lolc = 0
    total_grand = 0

    for record in data:
        ws.cell(row=current_row, column=1, value=record.get('Company', company_name)).border = thin_border
        ws.cell(row=current_row, column=2, value=record.get('Date', '')).border = thin_border
        ws.cell(row=current_row, column=3, value=record.get('BU User', '')).border = thin_border
        ws.cell(row=current_row, column=4, value=record.get('Employee ID', '')).border = thin_border
        ws.cell(row=current_row, column=5, value=record.get('Unit', '')).border = thin_border
        ws.cell(row=current_row, column=6, value=record.get('Service Description', '')).border = thin_border
        ws.cell(row=current_row, column=7, value=record.get('Quantity', 0)).border = thin_border
        ws.cell(row=current_row, column=8, value=record.get('Supervisor', '')).border = thin_border
        ws.cell(row=current_row, column=9, value=record.get('Supervisor Approval Status', '')).border = thin_border
        ws.cell(row=current_row, column=10, value=record.get('Supervisor Approval Date', '')).border = thin_border
        ws.cell(row=current_row, column=11, value=record.get('Supervisor Comment', '')).border = thin_border
        ws.cell(row=current_row, column=12, value=record.get('Head of IT', '')).border = thin_border
        ws.cell(row=current_row, column=13, value=record.get('Head Approval Status', '')).border = thin_border
        ws.cell(row=current_row, column=14, value=record.get('Head Approval Date', '')).border = thin_border
        ws.cell(row=current_row, column=15, value=record.get('Head Comment', '')).border = thin_border
        ws.cell(row=current_row, column=16, value=record.get('Finance Controller', '')).border = thin_border
        ws.cell(row=current_row, column=17, value=record.get('Finance Approval Status', '')).border = thin_border
        ws.cell(row=current_row, column=18, value=record.get('Finance Approval Date', '')).border = thin_border
        ws.cell(row=current_row, column=19, value=record.get('Finance Comment', '')).border = thin_border
        ws.cell(row=current_row, column=20, value=record.get('Invoice Number', '')).border = thin_border
        ws.cell(row=current_row, column=21, value=record.get('LOIT Total (Rs)', 0)).border = thin_border
        ws.cell(row=current_row, column=22, value=record.get('showback_total', 0)).border = thin_border
        ws.cell(row=current_row, column=23, value=record.get('Grand Total (Rs)', 0)).border = thin_border
        ws.cell(row=current_row, column=24, value=record.get('Status', '')).border = thin_border

        total_loit += record.get('LOIT Total (Rs)', 0)
        total_lolc += record.get('showback_total', 0)
        total_grand += record.get('Grand Total (Rs)', 0)

        current_row += 1

    # Totals row
    ws.cell(row=current_row, column=20, value="TOTAL:").font = Font(bold=True)
    ws.cell(row=current_row, column=21, value=total_loit).font = Font(bold=True)
    ws.cell(row=current_row, column=22, value=total_lolc).font = Font(bold=True)
    ws.cell(row=current_row, column=23, value=total_grand).font = Font(bold=True)

    # Auto-size columns
    for col in range(1, len(headers) + 1):
        column_letter = get_column_letter(col)
        max_length = 0
        for row in range(1, current_row + 1):
            cell_value = ws.cell(row=row, column=col).value
            if cell_value:
                max_length = max(max_length, len(str(cell_value)))
        ws.column_dimensions[column_letter].width = min(max_length + 2, 25)

    output = io.BytesIO()
    wb.save(output)
    output.seek(0)
    return Response(
        output.getvalue(),
        mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        headers={'Content-Disposition': f'attachment; filename=service_report_{company_name}.xlsx'}
    )


def generate_company_simple_csv_report(data, company_name):
    """Generate simplified CSV report for a specific company"""
    import csv
    import io
    from flask import Response
    from datetime import datetime

    showback_header = get_company_showback_label(company_name, include_rs=True)

    output = io.StringIO()
    headers = [
        'Company', 'Date', 'BU User', 'Employee ID', 'Unit', 'Service Description', 'Quantity',
        'Supervisor', 'Supervisor Approval Status', 'Supervisor Approval Date', 'Supervisor Comment',
        'Head of IT', 'Head Approval Status', 'Head Approval Date', 'Head Comment',
        'Finance Controller', 'Finance Approval Status', 'Finance Approval Date', 'Finance Comment',
        'Invoice Number',
        'LOIT Total (Rs)', showback_header, 'Grand Total (Rs)', 'Status'
    ]

    writer = csv.DictWriter(output, fieldnames=headers)
    output.write(f"# Service Report - {company_name}\n")
    output.write(f"# Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}\n\n")
    writer.writeheader()

    total_loit = 0
    total_lolc = 0
    total_grand = 0

    for record in data:
        row = {header: record.get(header, '') for header in headers}
        row[showback_header] = record.get('showback_total', 0)
        writer.writerow(row)
        total_loit += record.get('LOIT Total (Rs)', 0)
        total_lolc += record.get('showback_total', 0)
        total_grand += record.get('Grand Total (Rs)', 0)

    output.write(f"\nTOTAL,,,,,,,,,,,,,,,,,,,{total_loit},{total_lolc},{total_grand},\n")

    return Response(
        output.getvalue(),
        mimetype='text/csv',
        headers={'Content-Disposition': f'attachment; filename=service_report_{company_name}.csv'}
    )


# ============ LOLC TECHNOLOGIES REPORTS (Full Details with Rates) ============

@api_bp.route('/reports/lolc-preview', methods=['GET'])
@login_required
def lolc_reports_preview():
    """Preview report data for LOLC Technologies - Full details with rates"""
    # Check authorization
    if current_user.role != 'super_admin':
        return jsonify({'success': False, 'error': 'Unauthorized'}), 403
    
    try:
        date_from = request.args.get('from')
        date_to = request.args.get('to')
        company_id = request.args.get('company_id')
        
        # Build query for service items that are finance approved
        query = ServiceItem.query.filter(
            ServiceItem.finance_approval_status == 'approved'
        )
        
        if company_id and company_id != 'all':
            query = query.filter(ServiceItem.company_id == company_id)
        
        if date_from:
            query = query.filter(ServiceItem.created_at >= date_from)
        if date_to:
            query = query.filter(ServiceItem.created_at <= date_to + ' 23:59:59')
        
        items = query.all()
        
        # Get resource types for lookup
        all_resource_types = ResourceType.query.all()
        resource_by_id = {str(rt.id): rt for rt in all_resource_types}
        
        records = []
        summary = {}
        total_loit_cb = 0
        total_loit_sb = 0
        total_loit = 0
        total_lolc = 0
        total_grand = 0
        
        for item in items:
            entry = TimeEntry.query.get(item.time_entry_id)
            if not entry:
                continue
            
            company = Company.query.get(item.company_id)
            if not company:
                continue
            
            bu_user = User.query.get(entry.user_id)
            unit_name = 'N/A'
            if bu_user and bu_user.unit_id:
                unit = Unit.query.get(bu_user.unit_id)
                if unit:
                    unit_name = unit.name
            
            # Calculate LOIT values and collect resource details
            loit_cb = 0
            loit_sb = 0
            lolc_sb = 0
            resource_details = []
            
            for resource in item.resources or []:
                resource_type_id = str(resource.get('type', ''))
                rt = resource_by_id.get(resource_type_id)
                quantity = float(resource.get('quantity') or resource.get('hours') or 0)
                
                if rt:
                    loit_cb_rate = float(rt.loit_chargeback or 0)
                    loit_sb_rate = float(rt.loit_showback or 0)
                    lolc_sb_rate = float(rt.showback_rate or 0)
                    
                    loit_cb += quantity * loit_cb_rate
                    loit_sb += quantity * loit_sb_rate
                    lolc_sb += quantity * lolc_sb_rate
                    
                    resource_details.append({
                        'name': rt.name,
                        'quantity': quantity,
                        'uom': rt.unit_of_measure or 'units',
                        'loit_cb_rate': loit_cb_rate,
                        'loit_sb_rate': loit_sb_rate,
                        'lolc_sb_rate': lolc_sb_rate,
                        'loit_cb_amount': quantity * loit_cb_rate,
                        'loit_sb_amount': quantity * loit_sb_rate,
                        'lolc_sb_amount': quantity * lolc_sb_rate
                    })
            
            amount = loit_cb + loit_sb + lolc_sb
            total_loit_cb += loit_cb
            total_loit_sb += loit_sb
            total_loit += loit_cb + loit_sb
            total_lolc += lolc_sb
            total_grand += amount
            
            record = {
                'date': entry.service_date.strftime('%Y-%m-%d') if entry.service_date else entry.created_at.strftime('%Y-%m-%d'),
                'company_name': company.name,
                'company_id': company.id,
                'bu_user': bu_user.full_name if bu_user else 'Unknown',
                'employee_id': bu_user.employee_id if bu_user else 'N/A',
                'team': unit_name,
                'description': item.service_description,
                'resources': resource_details,
                'loit_cb': loit_cb,
                'loit_sb': loit_sb,
                'loit_total': loit_cb + loit_sb,
                'lolc_sb': lolc_sb,
                'grand_total': amount,
                'head_status': item.head_approval_status,
                'finance_status': item.finance_approval_status,
                'invoice_number': item.invoice_number or 'N/A'
            }
            records.append(record)
            
            # Build summary
            company_key = company.name
            if company_key not in summary:
                summary[company_key] = {'count': 0, 'loit_cb': 0, 'loit_sb': 0, 'loit_total': 0, 'lolc_sb': 0, 'grand_total': 0}
            summary[company_key]['count'] += 1
            summary[company_key]['loit_cb'] += loit_cb
            summary[company_key]['loit_sb'] += loit_sb
            summary[company_key]['loit_total'] += loit_cb + loit_sb
            summary[company_key]['lolc_sb'] += lolc_sb
            summary[company_key]['grand_total'] += amount
        
        return jsonify({
            'success': True,
            'data': {
                'records': records,
                'summary': summary,
                'totals': {
                    'loit_cb': total_loit_cb,
                    'loit_sb': total_loit_sb,
                    'loit_total': total_loit,
                    'lolc_sb': total_lolc,
                    'grand_total': total_grand
                }
            }
        })
        
    except Exception as e:
        print("Error in lolc_reports_preview:", str(e))
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500


@api_bp.route('/reports/lolc-export', methods=['GET'])
@login_required
def lolc_reports_export():
    """Export report data for LOLC Technologies - Full details with rates per resource"""
   

    if current_user.role != 'super_admin':
        return jsonify({'success': False, 'error': 'Unauthorized'}), 403
    
    try:
        date_from = request.args.get('from')
        date_to = request.args.get('to')
        company_id = request.args.get('company_id')
        format_type = request.args.get('format', 'excel')
        
        # Determine if single company or all companies
        is_single_company = company_id and company_id != 'all'
        
        # Build query for service items that are finance approved
        query = ServiceItem.query.filter(
            ServiceItem.finance_approval_status == 'approved'
        )
        
        if is_single_company:
            query = query.filter(ServiceItem.company_id == company_id)
        if date_from:
            query = query.filter(ServiceItem.created_at >= date_from)
        if date_to:
            query = query.filter(ServiceItem.created_at <= date_to + ' 23:59:59')
        
        items = query.all()
        
        # Get resource types for lookup
        all_rt = ResourceType.query.all()
        rt_by_id = {str(rt.id): rt for rt in all_rt}
        
        data = []
        for item in items:
            entry = TimeEntry.query.get(item.time_entry_id)
            if not entry:
                continue
            
            company = Company.query.get(item.company_id)
            bu_user = User.query.get(entry.user_id)
            
            unit_name = 'N/A'
            if bu_user and bu_user.unit_id:
                unit = Unit.query.get(bu_user.unit_id)
                if unit:
                    unit_name = unit.name
            
            submission_date = entry.service_date.strftime('%Y-%m-%d') if entry.service_date else entry.created_at.strftime('%Y-%m-%d')
            invoice_date = item.invoice_generated_at.strftime('%Y-%m-%d') if item.invoice_generated_at else ''
            
            # Get approval details
            supervisor_name = ''
            supervisor_status = item.supervisor_approval_status or ''
            supervisor_approval_date = item.supervisor_approved_at.strftime('%Y-%m-%d %H:%M:%S') if item.supervisor_approved_at else ''
            supervisor_comment = item.supervisor_comment or ''
            
            if item.supervisor_approved_by:
                supervisor = User.query.get(item.supervisor_approved_by)
                supervisor_name = supervisor.full_name if supervisor else ''
            
            head_of_it_name = ''
            head_status = item.head_approval_status or ''
            head_approval_date = item.head_approved_at.strftime('%Y-%m-%d %H:%M:%S') if item.head_approved_at else ''
            head_comment = item.head_approval_comment or ''
            head_rejection_date = item.head_rejected_at.strftime('%Y-%m-%d %H:%M:%S') if item.head_rejected_at else ''
            
            if item.head_approved_by:
                head_of_it = User.query.get(item.head_approved_by)
                head_of_it_name = head_of_it.full_name if head_of_it else ''
            elif item.head_rejected_by:
                head_of_it = User.query.get(item.head_rejected_by)
                head_of_it_name = head_of_it.full_name if head_of_it else ''
            
            finance_name = ''
            finance_status = item.finance_approval_status or ''
            finance_approval_date = item.finance_approved_at.strftime('%Y-%m-%d %H:%M:%S') if item.finance_approved_at else ''
            finance_comment = item.finance_approval_comment or ''
            finance_rejection_date = item.finance_rejected_at.strftime('%Y-%m-%d %H:%M:%S') if item.finance_rejected_at else ''
            
            if item.finance_approved_by:
                finance_controller = User.query.get(item.finance_approved_by)
                finance_name = finance_controller.full_name if finance_controller else ''
            elif item.finance_rejected_by:
                finance_controller = User.query.get(item.finance_rejected_by)
                finance_name = finance_controller.full_name if finance_controller else ''
            
            # For each resource, create a separate row
            for resource in item.resources or []:
                resource_type_id = str(resource.get('type', ''))
                rt = rt_by_id.get(resource_type_id)
                quantity = float(resource.get('quantity') or resource.get('hours') or 0)
                
                if rt:
                    loit_cb_rate = float(rt.loit_chargeback or 0)
                    loit_sb_rate = float(rt.loit_showback or 0)
                    lolc_sb_rate = float(rt.showback_rate or 0)
                    
                    loit_cb_amount = quantity * loit_cb_rate
                    loit_sb_amount = quantity * loit_sb_rate
                    lolc_sb_amount = quantity * lolc_sb_rate
                    loit_total = loit_cb_amount + loit_sb_amount
                    grand_total = loit_total + lolc_sb_amount
                    
                    resource_desc = resource_description(resource, item.service_description or '')
                    data.append({
                        'Company': company.name if company else 'N/A',
                        'Date': submission_date,
                        'BU User': bu_user.full_name if bu_user else 'Unknown',
                        'Employee ID': bu_user.employee_id if bu_user else 'N/A',
                        'Unit': unit_name,
                        'Service Description': resource_desc,
                        'Resource Type': rt.name,
                        'Quantity': f"{quantity} {rt.unit_of_measure or 'units'}",
                        'LOIT Chargeback Rate (Rs)': loit_cb_rate,
                        'LOIT Chargeback Amount (Rs)': loit_cb_amount,
                        'LOIT Showback Rate (Rs)': loit_sb_rate,
                        'LOIT Showback Amount (Rs)': loit_sb_amount,
                        'LOIT Total (Rs)': loit_total,
                        'showback_rate': lolc_sb_rate,
                        'showback_amount': lolc_sb_amount,
                        'Grand Total (Rs)': grand_total,
                        'Supervisor': supervisor_name,
                        'Supervisor Approval Status': supervisor_status,
                        'Supervisor Approval Date': supervisor_approval_date,
                        'Supervisor Comment': supervisor_comment,
                        'Head of IT': head_of_it_name,
                        'Head Approval Status': head_status,
                        'Head Approval Date': head_approval_date if head_status == 'approved' else head_rejection_date,
                        'Head Comment': head_comment,
                        'Finance Controller': finance_name,
                        'Finance Approval Status': finance_status,
                        'Finance Approval Date': finance_approval_date if finance_status == 'approved' else finance_rejection_date,
                        'Finance Comment': finance_comment,
                        'Invoice Number': item.invoice_number or 'N/A',
                        'Invoice Date': invoice_date,
                        'Status': item.head_approval_status
                    })
        
        data.sort(key=lambda x: (x['Company'], x['Date']))
        
        if format_type == 'excel':
            # Pass is_single_company flag to determine report format
            return generate_lolc_dynamic_excel_report(data, is_single_company)
        elif format_type == 'csv':
            return generate_lolc_dynamic_csv_report(data, is_single_company)
        else:
            return jsonify({'success': False, 'error': 'Unsupported format'}), 400
        
    except Exception as e:
        print("Error in lolc_reports_export:", str(e))
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500


def generate_lolc_dynamic_excel_report(data, is_single_company):
    """Generate Excel report - All companies in ONE sheet, stacked vertically with separators"""
    import openpyxl
    from openpyxl.styles import Font, Alignment, PatternFill, Border, Side
    from openpyxl.utils import get_column_letter
    import io
    from flask import Response
    from datetime import datetime
    from collections import defaultdict

    wb = openpyxl.Workbook()
    
    # Use the active sheet (remove default sheet if we want to rename)
    ws = wb.active
    ws.title = "Service Report"
    
    base_headers = [
        'Date', 'BU User', 'Employee ID', 'Unit', 'Service Description', 'Resource Type', 'Quantity',
        'Supervisor', 'Supervisor Approval Status', 'Supervisor Approval Date', 'Supervisor Comment',
        'Head of IT', 'Head Approval Status', 'Head Approval Date', 'Head Comment',
        'Finance Controller', 'Finance Approval Status', 'Finance Approval Date', 'Finance Comment',
        'Invoice Number', 'Invoice Date',
        'LOIT Chargeback Rate (Rs)', 'LOIT Chargeback Amount (Rs)',
        'LOIT Showback Rate (Rs)', 'LOIT Showback Amount (Rs)',
        'LOIT Total (Rs)',
        'Grand Total (Rs)', 'Status'
    ]
    max_headers = base_headers[:26] + [
        get_company_showback_rate_label('Company'),
        get_company_showback_amount_label('Company'),
    ] + base_headers[26:]

    # Define styles
    header_fill = PatternFill(start_color="1E3A5F", end_color="1E3A5F", fill_type="solid")
    header_font = Font(color="FFFFFF", bold=True)
    header_alignment = Alignment(horizontal="center", vertical="center")
    thin_border = Border(left=Side(style='thin'), right=Side(style='thin'),
                         top=Side(style='thin'), bottom=Side(style='thin'))
    company_header_fill = PatternFill(start_color="2C5282", end_color="2C5282", fill_type="solid")
    company_header_font = Font(color="FFFFFF", bold=True, size=12)
    summary_fill = PatternFill(start_color="F8FAFC", end_color="F8FAFC", fill_type="solid")
    total_font = Font(bold=True)

    # Group data by company
    companies_data = defaultdict(list)
    for record in data:
        companies_data[record['Company']].append(record)
    
    # Sort companies alphabetically
    sorted_companies = sorted(companies_data.keys())
    
    # Title
    if is_single_company and len(companies_data) == 1:
        title = f"Service Report - {sorted_companies[0]}"
    else:
        title = f"Service Report - ALL COMPANIES ({datetime.now().strftime('%Y-%m-%d')})"
    
    ws.merge_cells(f'A1:{get_column_letter(len(max_headers))}1')
    title_cell = ws['A1']
    title_cell.value = title
    title_cell.font = Font(size=16, bold=True)
    title_cell.alignment = Alignment(horizontal="center")
    
    ws['A2'] = f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
    ws['A2'].font = Font(italic=True, size=9)
    
    # Track current row position
    current_row = 4
    grand_total_loit_cb = 0
    grand_total_loit_sb = 0
    grand_total_loit = 0
    grand_total_lolc = 0
    grand_total_grand = 0
    
    # Process each company
    for idx, company_name in enumerate(sorted_companies):
        company_records = companies_data[company_name]
        showback_rate_header = get_company_showback_rate_label(company_name)
        showback_amount_header = get_company_showback_amount_label(company_name)
        headers = base_headers[:26] + [showback_rate_header, showback_amount_header] + base_headers[26:]
        
        # Add company separator header (blank row + company title)
        if idx > 0:
            current_row += 1  # Add blank row between companies
        
        # Company header row with background
        ws.merge_cells(f'A{current_row}:{get_column_letter(len(headers))}{current_row}')
        company_cell = ws.cell(row=current_row, column=1, value=f"🏢 {company_name}")
        company_cell.font = company_header_font
        company_cell.fill = company_header_fill
        company_cell.alignment = Alignment(horizontal="center", vertical="center")
        current_row += 1
        
        # Write headers for this company section
        for col, header in enumerate(headers, 1):
            cell = ws.cell(row=current_row, column=col, value=header)
            cell.fill = header_fill
            cell.font = header_font
            cell.alignment = header_alignment
            cell.border = thin_border
        current_row += 1
        
        # Write data rows for this company
        company_total_loit_cb = 0
        company_total_loit_sb = 0
        company_total_loit = 0
        company_total_lolc = 0
        company_total_grand = 0
        record_count = 0
        
        for record in company_records:
            ws.cell(row=current_row, column=1, value=record.get('Date', '')).border = thin_border
            ws.cell(row=current_row, column=2, value=record.get('BU User', '')).border = thin_border
            ws.cell(row=current_row, column=3, value=record.get('Employee ID', '')).border = thin_border
            ws.cell(row=current_row, column=4, value=record.get('Unit', '')).border = thin_border
            ws.cell(row=current_row, column=5, value=record.get('Service Description', '')).border = thin_border
            ws.cell(row=current_row, column=6, value=record.get('Resource Type', '')).border = thin_border
            ws.cell(row=current_row, column=7, value=record.get('Quantity', '')).border = thin_border
            ws.cell(row=current_row, column=8, value=record.get('Supervisor', '')).border = thin_border
            ws.cell(row=current_row, column=9, value=record.get('Supervisor Approval Status', '')).border = thin_border
            ws.cell(row=current_row, column=10, value=record.get('Supervisor Approval Date', '')).border = thin_border
            ws.cell(row=current_row, column=11, value=record.get('Supervisor Comment', '')).border = thin_border
            ws.cell(row=current_row, column=12, value=record.get('Head of IT', '')).border = thin_border
            ws.cell(row=current_row, column=13, value=record.get('Head Approval Status', '')).border = thin_border
            ws.cell(row=current_row, column=14, value=record.get('Head Approval Date', '')).border = thin_border
            ws.cell(row=current_row, column=15, value=record.get('Head Comment', '')).border = thin_border
            ws.cell(row=current_row, column=16, value=record.get('Finance Controller', '')).border = thin_border
            ws.cell(row=current_row, column=17, value=record.get('Finance Approval Status', '')).border = thin_border
            ws.cell(row=current_row, column=18, value=record.get('Finance Approval Date', '')).border = thin_border
            ws.cell(row=current_row, column=19, value=record.get('Finance Comment', '')).border = thin_border
            ws.cell(row=current_row, column=20, value=record.get('Invoice Number', '')).border = thin_border
            ws.cell(row=current_row, column=21, value=record.get('Invoice Date', '')).border = thin_border
            ws.cell(row=current_row, column=22, value=record.get('LOIT Chargeback Rate (Rs)', 0)).border = thin_border
            ws.cell(row=current_row, column=23, value=record.get('LOIT Chargeback Amount (Rs)', 0)).border = thin_border
            ws.cell(row=current_row, column=24, value=record.get('LOIT Showback Rate (Rs)', 0)).border = thin_border
            ws.cell(row=current_row, column=25, value=record.get('LOIT Showback Amount (Rs)', 0)).border = thin_border
            ws.cell(row=current_row, column=26, value=record.get('LOIT Total (Rs)', 0)).border = thin_border
            ws.cell(row=current_row, column=27, value=record.get('showback_rate', 0)).border = thin_border
            ws.cell(row=current_row, column=28, value=record.get('showback_amount', 0)).border = thin_border
            ws.cell(row=current_row, column=29, value=record.get('Grand Total (Rs)', 0)).border = thin_border
            ws.cell(row=current_row, column=30, value=record.get('Status', '')).border = thin_border
            
            # Accumulate company totals
            company_total_loit_cb += record.get('LOIT Chargeback Amount (Rs)', 0)
            company_total_loit_sb += record.get('LOIT Showback Amount (Rs)', 0)
            company_total_loit += record.get('LOIT Total (Rs)', 0)
            company_total_lolc += record.get('showback_amount', 0)
            company_total_grand += record.get('Grand Total (Rs)', 0)
            record_count += 1
            current_row += 1
        
        # Add company total row
        if record_count > 0:
            ws.cell(row=current_row, column=4, value=f"COMPANY TOTAL ({record_count} services):").font = total_font
            ws.cell(row=current_row, column=23, value=company_total_loit_cb).font = total_font
            ws.cell(row=current_row, column=25, value=company_total_loit_sb).font = total_font
            ws.cell(row=current_row, column=26, value=company_total_loit).font = total_font
            ws.cell(row=current_row, column=28, value=company_total_lolc).font = total_font
            ws.cell(row=current_row, column=29, value=company_total_grand).font = total_font
            current_row += 1
            
            # Add to grand totals
            grand_total_loit_cb += company_total_loit_cb
            grand_total_loit_sb += company_total_loit_sb
            grand_total_loit += company_total_loit
            grand_total_lolc += company_total_lolc
            grand_total_grand += company_total_grand
    
    # Add GRAND TOTAL row at the end
    if len(sorted_companies) > 1:
        current_row += 1  # Add blank row before grand total
        ws.cell(row=current_row, column=4, value="GRAND TOTAL (ALL COMPANIES):").font = Font(bold=True, size=11)
        ws.cell(row=current_row, column=23, value=grand_total_loit_cb).font = Font(bold=True)
        ws.cell(row=current_row, column=25, value=grand_total_loit_sb).font = Font(bold=True)
        ws.cell(row=current_row, column=26, value=grand_total_loit).font = Font(bold=True)
        ws.cell(row=current_row, column=28, value=grand_total_lolc).font = Font(bold=True)
        ws.cell(row=current_row, column=29, value=grand_total_grand).font = Font(bold=True)
    
    # Auto-size columns (for all used columns)
    for col in range(1, len(max_headers) + 1):
        column_letter = get_column_letter(col)
        max_length = 0
        for row in range(1, current_row + 1):
            cell_value = ws.cell(row=row, column=col).value
            if cell_value:
                # Handle different types
                if isinstance(cell_value, (int, float)):
                    str_val = f"{cell_value:,.2f}" if col >= 22 else str(cell_value)
                else:
                    str_val = str(cell_value)
                max_length = max(max_length, len(str_val))
        # Set width with some padding, max 35 characters
        adjusted_width = min(max_length + 3, 35)
        ws.column_dimensions[column_letter].width = adjusted_width
    
    output = io.BytesIO()
    wb.save(output)
    output.seek(0)
    
    # Generate filename
    if is_single_company and len(companies_data) == 1:
        filename = f"service_report_{list(companies_data.keys())[0]}_{datetime.now().strftime('%Y%m%d')}.xlsx"
    else:
        filename = f"lolc_technologies_report_{datetime.now().strftime('%Y%m%d')}.xlsx"
    
    return Response(
        output.getvalue(),
        mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        headers={'Content-Disposition': f'attachment; filename={filename}'}
    )


def generate_lolc_dynamic_csv_report(data, is_single_company):
    """Generate CSV report - One file for single company, separate files for multiple? 
       (CSV doesn't support multiple sheets, so one file only)"""
    import csv
    import io
    from flask import Response
    from datetime import datetime
    
    output = io.StringIO()
    showback_rate_header = get_company_showback_rate_label(
        data[0]['Company'] if data else 'Company'
    )
    showback_amount_header = get_company_showback_amount_label(
        data[0]['Company'] if data else 'Company'
    )
    headers = [
        'Company', 'Date', 'BU User', 'Employee ID', 'Unit', 'Service Description', 'Resource Type', 'Quantity',
        'Supervisor', 'Supervisor Approval Status', 'Supervisor Approval Date', 'Supervisor Comment',
        'Head of IT', 'Head Approval Status', 'Head Approval Date', 'Head Comment',
        'Finance Controller', 'Finance Approval Status', 'Finance Approval Date', 'Finance Comment',
        'Invoice Number', 'Invoice Date',
        'LOIT Chargeback Rate (Rs)', 'LOIT Chargeback Amount (Rs)',
        'LOIT Showback Rate (Rs)', 'LOIT Showback Amount (Rs)',
        'LOIT Total (Rs)',
        showback_rate_header, showback_amount_header,
        'Grand Total (Rs)', 'Status'
    ]

    writer = csv.DictWriter(output, fieldnames=headers, extrasaction='ignore')
    
    if is_single_company:
        output.write(f"# Service Report - {data[0]['Company'] if data else 'N/A'}\n")
    else:
        output.write(f"# Service Report - ALL COMPANIES\n")
    
    output.write(f"# Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}\n\n")
    writer.writeheader()
    
    for record in data:
        row = {header: record.get(header, '') for header in headers}
        row[showback_rate_header] = record.get('showback_rate', 0)
        row[showback_amount_header] = record.get('showback_amount', 0)
        writer.writerow(row)
    
    if is_single_company:
        filename = f"service_report_{data[0]['Company'] if data else 'report'}_{datetime.now().strftime('%Y%m%d')}.csv"
    else:
        filename = f"lolc_technologies_report_{datetime.now().strftime('%Y%m%d')}.csv"
    
    return Response(
        output.getvalue(),
        mimetype='text/csv',
        headers={'Content-Disposition': f'attachment; filename={filename}'}
    )



@api_bp.route('/companies', methods=['GET'])
@login_required
def get_companies():
    """Get all companies for admin"""
    try:
        companies = Company.query.filter_by(is_active=True).order_by(Company.name).all()
        
        result = [{
            'id': c.id,
            'name': c.name,
            'code': c.code,
            'email': c.email,
            'phone': c.phone,
            'address': c.address,
            'contact_first_name': c.contact_first_name,
            'contact_last_name': c.contact_last_name,
            'contact_employee_number': c.contact_employee_number,
            'is_active': c.is_active
        } for c in companies]
        
        return jsonify(result)
    except Exception as e:
        print("Error in get_companies:", str(e))
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500





@api_bp.route('/companies/<int:company_id>', methods=['GET'])
@login_required
def get_company(company_id):
    """Get single company details"""
    try:
        company = Company.query.get(company_id)
        
        if not company:
            return jsonify({'success': False, 'error': 'Company not found'}), 404
        
        result = {
            'id': company.id,
            'name': company.name,
            'code': company.code or '',
            'email': company.email or '',
            'phone': company.phone or '',
            'address': company.address or '',
            'is_active': company.is_active,
            'company_type': company.company_type,
            'is_parent_company': company.is_parent_company,
            'has_full_access': company.has_full_access,
        }
        
        return jsonify(result)
    except Exception as e:
        print("Error in get_company:", str(e))
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500
    




    import pandas as pd
from werkzeug.utils import secure_filename

ALLOWED_EXTENSIONS = {'xlsx', 'xls'}

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

@api_bp.route('/bulk-upload', methods=['POST'])
@login_required
def bulk_upload_services():
    """Bulk upload service entries from Excel file"""
    if current_user.role != 'bu_user':
        return jsonify({'success': False, 'error': 'Unauthorized. Only BU Unit users can upload.'}), 403
    
    try:
        # Check if file was uploaded
        if 'excel_file' not in request.files:
            return jsonify({'success': False, 'error': 'No file uploaded'}), 400
        
        file = request.files['excel_file']
        if file.filename == '':
            return jsonify({'success': False, 'error': 'No file selected'}), 400
        
        if not allowed_file(file.filename):
            return jsonify({'success': False, 'error': 'Invalid file type. Please upload .xlsx or .xls file'}), 400
        
        # Get default service date
        default_service_date_str = request.form.get('default_service_date')
        default_service_date = None
        if default_service_date_str:
            try:
                default_service_date = datetime.strptime(default_service_date_str, '%Y-%m-%d').date()
            except ValueError:
                default_service_date = datetime.utcnow().date()
        else:
            default_service_date = datetime.utcnow().date()
        
        # Read Excel file
        df = pd.read_excel(file)
        
        # Validate required columns
        required_columns = ['Company Name', 'Service Description']
        missing_columns = [col for col in required_columns if col not in df.columns]
        if missing_columns:
            return jsonify({'success': False, 'error': f'Missing columns: {", ".join(missing_columns)}'}), 400
        
        # Get all companies for lookup
        companies = {c.name: c.id for c in Company.query.all()}
        
        # Get resource types
        resource_types = {rt.name: rt.code for rt in ResourceType.query.all()}
        # Also add short names mapping
        resource_short_names = {
            'BA': 'ba', 'Developer': 'dev', 'QA': 'qa', 'DevOps': 'devops',
            'PM': 'pm', 'UX': 'ux', 'Arch': 'arch'
        }
        resource_types.update(resource_short_names)
        
        # Process data
        created_services = 0
        errors = []
        current_entry = None
        current_entry_services = []
        
        # Group by service (same company + description across rows)
        service_groups = {}
        
        for idx, row in df.iterrows():
            row_num = idx + 2  # Excel row number (1-indexed + header)
            try:
                company_name = str(row.get('Company Name', '')).strip()
                description = str(row.get('Service Description', '')).strip()
               
                resource_type_input = str(row.get('Resource Type', '')).strip()
                hours = row.get('Hours', 0)
                rate = row.get('Rate (Rs)', 0)
               
                service_date_val = row.get('Service Date', None)
                
                # Validate required fields
                if not company_name:
                    errors.append(f"Row {row_num}: Company Name is required")
                    continue
                
                if company_name not in companies:
                    errors.append(f"Row {row_num}: Company '{company_name}' not found. Available: {', '.join(list(companies.keys())[:5])}...")
                    continue
                
                if not description:
                    errors.append(f"Row {row_num}: Service Description is required")
                    continue
                
                
                
                # Get resource type code
                resource_code = None
                if resource_type_input:
                    # Try to find by name first
                    if resource_type_input in resource_types:
                        resource_code = resource_types[resource_type_input]
                    # Try to find by short name
                    elif resource_type_input.upper() in resource_short_names:
                        resource_code = resource_short_names[resource_type_input.upper()]
                    # Try to find by ID
                    else:
                        rt = ResourceType.query.filter(
                            (ResourceType.name == resource_type_input) |
                            (ResourceType.code == resource_type_input.lower())
                        ).first()
                        if rt:
                            resource_code = rt.code
                
                
                
                # Parse service date
                service_date = default_service_date
                if pd.notna(service_date_val):
                    try:
                        if isinstance(service_date_val, pd.Timestamp):
                            service_date = service_date_val.date()
                        else:
                            service_date = datetime.strptime(str(service_date_val), '%Y-%m-%d').date()
                    except ValueError:
                        errors.append(f"Row {row_num}: Invalid date format. Use YYYY-MM-DD")
                        continue
                
                # Create group key - group resources by company and service date
                group_key = f"{company_name}_{service_date}"
                
                if group_key not in service_groups:
                    service_groups[group_key] = {
                        'company_id': companies[company_name],
                        'service_date': service_date,
                        'resources': []
                    }
                
                # Add resource with per-resource description
                resource_obj = {
                    'type': resource_code if resource_code else 'ba',
                    'quantity': hours if hours else 0,
                    'description': description,
                    'hours': hours if hours else 0,
                    'rate': rate if rate else 0,
                    'flat_fee': flat_fee if not hours else 0,
                    'annual_cost': 0
                }
                service_groups[group_key]['resources'].append(resource_obj)
                
            except Exception as e:
                errors.append(f"Row {row_num}: {str(e)}")
        
        # Create time entries for each group
        for group_key, group_data in service_groups.items():
            try:
                # Calculate total amount
                total_amount = 0
                for r in group_data['resources']:
                    if r.get('hours'):
                        total_amount += r['hours'] * r['rate']
                    else:
                        total_amount += r['flat_fee']
                
                # Create time entry
                entry = TimeEntry(
                    user_id=current_user.id,
                    status='draft',
                    submission_date=datetime.utcnow(),
                    service_date=group_data['service_date']
                )
                db.session.add(entry)
                db.session.flush()
                
                # Create service item
                service_item = ServiceItem(
                    time_entry_id=entry.id,
                    company_id=group_data['company_id'],
                    service_description=combine_resource_descriptions(group_data['resources']),
                    resources=group_data['resources'],
                    total_amount=total_amount
                )
                db.session.add(service_item)
                
                created_services += 1
                
            except Exception as e:
                errors.append(f"Error creating service for '{group_key}': {str(e)}")
        
        db.session.commit()
        
        return jsonify({
            'success': True,
            'created_services': created_services,
            'skipped_rows': len(errors),
            'errors': errors[:20]  # Limit to first 20 errors
        })
        
    except Exception as e:
        db.session.rollback()
        print("Error in bulk_upload_services:", str(e))
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500
    





import os
from werkzeug.utils import secure_filename
from flask import send_from_directory

# Configuration
UPLOAD_FOLDER = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'uploads', 'documents')
ALLOWED_EXTENSIONS = {'pdf', 'doc', 'docx', 'xls', 'xlsx', 'jpg', 'jpeg', 'png', 'txt'}

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

def can_view_document(user, document):
    # If document is linked to a service, use service access check
    if document.service_item_id:
        return can_access_service(user, document.service_item_id)
    
    # Otherwise (company document), fallback to company-based permissions
    # Admin excluded (if you still want)
    if user.role == 'admin':
        return True # Admin should be able to view all documents
    
    if user.role == 'super_admin':
        return True
    
    # Uploader can view their own documents
    if document.uploaded_by == user.id:
        return True
    
    # Company-specific checks (same as before)
    if user.role in ('payment_lolc_technologies', 'finance_controller'):
        return True
    if user.role == 'payment_company_user' and company_service.is_parent_company_user(user):
        return True
    if user.role in ('bu_user', 'head_of_it'):
        return document.company_id == user.company_id
    if user.role == 'payment_company_user' and user.company:
        return document.company_id == user.company_id
    if user.role == 'supervisor':
        units = Unit.query.filter_by(supervisor_id=user.id).all()
        company_ids = set()
        for unit in units:
            for company in unit.companies:
                company_ids.add(company.id)
        return document.company_id in company_ids
    
    return False


def can_upload_document(user, company_id, service_item_id=None):
    # If uploading for a specific service, use service access check
    if service_item_id:
        return can_access_service(user, service_item_id)
    
    # Otherwise (company document), use company-based permissions
    if user.role == 'admin':
        return False
    
    if user.role == 'super_admin':
        return True

    if user.role == 'payment_lolc_technologies':
        return True
    if user.role == 'payment_company_user' and company_service.is_parent_company_user(user):
        return True
    if user.role == 'finance_controller':
        return True
    if user.role in ('bu_user', 'head_of_it'):
        return user.company_id == company_id
    if user.role == 'payment_company_user' and user.company:
        return user.company_id == company_id
    if user.role == 'supervisor':
        units = Unit.query.filter_by(supervisor_id=user.id).all()
        for unit in units:
            if any(c.id == company_id for c in unit.companies):
                return True
        return False
    
    return False


def can_delete_document(user, document):
    """Check if user can delete a document"""
    # Admin can delete any
    if user.role == 'admin':
        return True
    if user.role == 'super_admin':
        return True  
    # Uploader can delete their own documents
    if document.uploaded_by == user.id:
        return True
    
    return False




from app.models import Unit, TimeEntry, ServiceItem, User

def can_access_service(user, service_item_id):
    """Check if user can view/upload documents for this service."""
    service = ServiceItem.query.get(service_item_id)
    if not service:
        return False
    entry = TimeEntry.query.get(service.time_entry_id)
    if not entry:
        return False
    bu_user = User.query.get(entry.user_id)
    if not bu_user:
        return False

    # Admin: full access
    if user.role == 'admin':
        return True
    
    if user.role == 'super_admin':
        return True

    # BU user: own service
    if user.role == 'bu_user' and user.id == bu_user.id:
        return True

    # Supervisor: supervises the BU user's unit
    if user.role == 'supervisor' and bu_user.unit_id:
        unit = Unit.query.get(bu_user.unit_id)
        if unit and unit.supervisor_id == user.id:
            return True

    # Head of IT: service's company matches their company
    if user.role == 'head_of_it' and user.company_id == service.company_id:
        return True
    
    # Record Assistant: service's company matches their company
    if user.role == 'record_assistant' and user.company_id == service.company_id:
        return True

    # Finance Controller: all services
    if user.role == 'finance_controller':
        return True

    # Payment team (LOLC Technologies, LOLC Finance, or company-specific)
    if user.role == 'payment_lolc_technologies':
        return True
    if user.role == 'payment_lolc_finance':
        return True
    if company_service.can_access_company(user, service.company_id):
        return True

    return False


@api_bp.route('/documents/upload-bulk', methods=['POST'])
@login_required
def upload_bulk_documents():
    """Upload multiple documents at once when submitting entry"""
    try:
        data = request.get_json()
        documents = data.get('documents', [])
        entry_id = data.get('entry_id')
        
        if not documents:
            return jsonify({'success': False, 'error': 'No documents provided'}), 400
        
        if not entry_id:
            return jsonify({'success': False, 'error': 'Entry ID required'}), 400
        
        uploaded_count = 0
        errors = []
        
        for doc_data in documents:
            try:
                # Decode base64 file data
                import base64
                file_data = base64.b64decode(doc_data['file_data'])
                
                # Create document
                document = Document(
                    company_id=int(doc_data['company_id']),
                    service_item_id=int(doc_data['service_item_id']) if doc_data.get('service_item_id') else None,
                    resource_id=doc_data.get('resource_id'),
                    uploaded_by=current_user.id,
                    original_filename=doc_data['filename'],
                    file_data=file_data,
                    file_size=len(file_data),
                    file_type=doc_data['filename'].split('.')[-1].lower() if '.' in doc_data['filename'] else 'bin',
                )
                
                db.session.add(document)
                uploaded_count += 1
                
            except Exception as e:
                errors.append(f"Failed to upload {doc_data.get('filename', 'unknown')}: {str(e)}")
        
        db.session.commit()
        
        return jsonify({
            'success': True,
            'uploaded_count': uploaded_count,
            'errors': errors
        })
        
    except Exception as e:
        db.session.rollback()
        print("Error in upload_bulk_documents:", str(e))
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500

@api_bp.route('/documents/upload', methods=['POST'])
@login_required
def upload_document():
    """Single document upload (kept for backward compatibility)"""
    try:
        company_id = request.form.get('company_id')
        service_item_id = request.form.get('service_item_id')
        resource_info = request.form.get('resource_info')
        
        if not company_id:
            return jsonify({'success': False, 'error': 'Company ID is required'}), 400
        
        if 'file' not in request.files:
            return jsonify({'success': False, 'error': 'No file provided'}), 400
        
        file = request.files['file']
        if file.filename == '':
            return jsonify({'success': False, 'error': 'No file selected'}), 400
        
        file_data = file.read()
        if not file_data:
            return jsonify({'success': False, 'error': 'File is empty'}), 400
        
        original_filename = secure_filename(file.filename)
        file_size = len(file_data)
        file_type = original_filename.rsplit('.', 1)[1].lower() if '.' in original_filename else 'bin'
        
        # Parse resource info
        resource_id = None
        service_item_id_from_resource = None
        if resource_info:
            try:
                resource_data = json.loads(resource_info)
                resource_id = resource_data.get('resource_id')
                service_item_id_from_resource = resource_data.get('service_item_id')
            except:
                pass
        
        # If service_item_id is not provided but we have it from resource_info, use that
        if not service_item_id or service_item_id == 'null':
            service_item_id = service_item_id_from_resource
        
        # Check for existing document with the same resource_id
        existing_doc = None
        if resource_id:
            existing_doc = Document.query.filter_by(
                resource_id=resource_id,
                is_active=True
            ).first()
            
            if existing_doc:
                existing_doc.file_data = file_data
                existing_doc.original_filename = original_filename
                existing_doc.file_size = file_size
                existing_doc.file_type = file_type
                existing_doc.updated_at = datetime.utcnow()
                existing_doc.service_item_id = service_item_id if service_item_id and service_item_id != 'null' else None
                db.session.commit()
                
                return jsonify({
                    'success': True,
                    'message': 'Document updated successfully',
                    'document': {
                        'id': existing_doc.id,
                        'original_filename': existing_doc.original_filename,
                        'size_formatted': f"{existing_doc.file_size / 1024:.1f} KB",
                        'uploaded_by': User.query.get(existing_doc.uploaded_by).full_name if existing_doc.uploaded_by else 'Unknown',
                        'created_at': existing_doc.created_at.strftime('%Y-%m-%d %H:%M'),
                        'resource_id': existing_doc.resource_id,
                        'updated': True
                    }
                })
        
        # Create new document
        document = Document(
            company_id=int(company_id),
            service_item_id=int(service_item_id) if service_item_id and service_item_id != 'null' else None,
            resource_id=resource_id,
            uploaded_by=current_user.id,
            original_filename=original_filename,
            file_data=file_data,
            file_size=file_size,
            file_type=file_type,
        )
        
        db.session.add(document)
        db.session.commit()
        
        return jsonify({
            'success': True,
            'message': 'Document uploaded successfully',
            'document': {
                'id': document.id,
                'original_filename': document.original_filename,
                'size_formatted': f"{document.file_size / 1024:.1f} KB",
                'uploaded_by': User.query.get(document.uploaded_by).full_name if document.uploaded_by else 'Unknown',
                'created_at': document.created_at.strftime('%Y-%m-%d %H:%M'),
                'resource_id': resource_id,
                'updated': False
            }
        })
        
    except Exception as e:
        db.session.rollback()
        print("Error in upload_document:", str(e))
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500


@api_bp.route('/documents/company/<int:company_id>', methods=['GET'])
@login_required
def get_company_documents(company_id):
    """Get all documents for a company (company level)"""
    try:
        company = Company.query.get(company_id)
        if not company:
            return jsonify({'success': False, 'error': 'Company not found'}), 404
        
        documents = Document.query.filter_by(
            company_id=company_id,
            is_active=True
        ).order_by(Document.created_at.desc()).all()
        
        result = []
        for doc in documents:
            if can_view_document(current_user, doc):
                result.append({
                    'id': doc.id,
                    'original_filename': doc.original_filename,
                    'size': doc.file_size,
                    'size_formatted': f"{doc.file_size / 1024:.1f} KB" if doc.file_size < 1048576 else f"{doc.file_size / 1048576:.1f} MB",
                    'uploaded_by': doc.uploader.full_name if doc.uploader else 'Unknown',
                    'created_at': doc.created_at.strftime('%Y-%m-%d %H:%M'),
                    'file_type': doc.file_type
                })
        
        return jsonify({'success': True, 'documents': result})
        
    except Exception as e:
        print("Error in get_company_documents:", str(e))
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500



@api_bp.route('/documents/service/<int:service_item_id>', methods=['GET'])
@login_required
def get_service_documents(service_item_id):
    """Get all documents for a service item"""
    try:
        service_item = ServiceItem.query.get(service_item_id)
        if not service_item:
            return jsonify({'success': False, 'error': 'Service not found'}), 404
        
        # Get documents linked to this service item
        documents = Document.query.filter_by(
            service_item_id=service_item_id,
            is_active=True
        ).order_by(Document.created_at.desc()).all()
        
        result = []
        for doc in documents:
            if can_view_document(current_user, doc):
                # Get resource name if available
                resource_name = None
                if doc.resource_id and service_item.resources:
                    for r in service_item.resources:
                        if r.get('resource_id') == doc.resource_id:
                            rt = ResourceType.query.get(r.get('type'))
                            if rt:
                                resource_name = rt.name
                            break
                
                result.append({
                    'id': doc.id,
                    'original_filename': doc.original_filename,
                    'size': doc.file_size,
                    'size_formatted': f"{doc.file_size / 1024:.1f} KB" if doc.file_size < 1048576 else f"{doc.file_size / 1048576:.1f} MB",
                    'uploaded_by': doc.uploader.full_name if doc.uploader else 'Unknown',
                    'created_at': doc.created_at.strftime('%Y-%m-%d %H:%M'),
                    'file_type': doc.file_type,
                    'resource_id': doc.resource_id,
                    'resource_name': resource_name
                })
        
        return jsonify({'success': True, 'documents': result})
        
    except Exception as e:
        print("Error in get_service_documents:", str(e))
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500


@api_bp.route('/documents/download/<int:document_id>', methods=['GET'])
@login_required
def download_document(document_id):
    try:
        document = Document.query.get_or_404(document_id)
        
        if not can_view_document(current_user, document):
            return jsonify({'success': False, 'error': 'Unauthorized'}), 403
        
        return Response(
            document.file_data,
            mimetype='application/octet-stream',
            headers={'Content-Disposition': f'attachment; filename="{document.original_filename}"'}
        )
        
    except Exception as e:
        print("Error in download_document:", str(e))
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500
    


@api_bp.route('/documents/preview/<int:document_id>', methods=['GET'])
@login_required
def preview_document(document_id):
    try:
        document = Document.query.get_or_404(document_id)
        
        if not can_view_document(current_user, document):
            return jsonify({'success': False, 'error': 'Unauthorized'}), 403
        
        ext = document.file_type.lower()
        mime_map = {
            'pdf': 'application/pdf',
            'jpg': 'image/jpeg', 'jpeg': 'image/jpeg',
            'png': 'image/png', 'gif': 'image/gif',
            'txt': 'text/plain'
        }
        mimetype = mime_map.get(ext, 'application/octet-stream')
        
        # For PDF/images, serve inline; others force download
        if ext in ['pdf', 'jpg', 'jpeg', 'png', 'gif']:
            return Response(document.file_data, mimetype=mimetype)
        else:
            return Response(
                document.file_data,
                mimetype='application/octet-stream',
                headers={'Content-Disposition': f'inline; filename="{document.original_filename}"'}
            )
        
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@api_bp.route('/documents/delete/<int:document_id>', methods=['DELETE'])
@login_required
def delete_document(document_id):
    """Delete a document (soft delete)"""
    try:
        document = Document.query.get_or_404(document_id)
        
        # Only admin or the uploader can delete
        if current_user.role != 'admin' and document.uploaded_by != current_user.id:
            return jsonify({'success': False, 'error': 'Unauthorized'}), 403
        
        document.is_active = False
        db.session.commit()
        
        return jsonify({'success': True, 'message': 'Document deleted successfully'})
        
    except Exception as e:
        db.session.rollback()
        print("Error in delete_document:", str(e))
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500






@api_bp.route('/supervisor/entries', methods=['GET'])
@login_required
def get_supervisor_entries():
    """Get entries pending supervisor approval"""
    try:
        # Allow supervisor, head_of_it, and admin roles
        if current_user.role not in ['supervisor', 'head_of_it', 'admin']:
            return jsonify({'success': False, 'error': 'Unauthorized'}), 403
        
        # Get units where this user is supervisor
        units = Unit.query.filter_by(supervisor_id=current_user.id).all()
        unit_ids = [u.id for u in units]
        
        # Get users in those units
        unit_members = UnitMember.query.filter(UnitMember.unit_id.in_(unit_ids)).all()
        user_ids = [um.user_id for um in unit_members]
        
        # Get pending entries
        entries = TimeEntry.query.filter(
            TimeEntry.user_id.in_(user_ids),
            TimeEntry.supervisor_approval_status == 'pending'
        ).order_by(TimeEntry.created_at.desc()).all()
        
        result = []
        for entry in entries:
            user = User.query.get(entry.user_id)
            unit = Unit.query.get(user.unit_id) if user else None
            
            result.append({
                'id': entry.id,
                'user_name': user.full_name if user else 'Unknown',
                'employee_id': user.employee_id if user else 'N/A',
                'unit_name': unit.name if unit else 'N/A',
                'service_date': entry.service_date.strftime('%Y-%m-%d') if entry.service_date else 'N/A',
                'submission_date': entry.submission_date.strftime('%Y-%m-%d %H:%M:%S'),
                'week_number': entry.week_number,
                'total_amount': entry.get_total_amount(),
                'service_count': ServiceItem.query.filter_by(time_entry_id=entry.id).count()
            })
        
        return jsonify({'success': True, 'entries': result})
        
    except Exception as e:
        print("Error in get_supervisor_entries:", str(e))
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500


@api_bp.route('/supervisor/entries/<int:entry_id>/approve', methods=['POST'])
@login_required
def supervisor_approve_entry(entry_id):
    try:
        entry = TimeEntry.query.get_or_404(entry_id)
        user = User.query.get(entry.user_id)
        if not user or not user.unit_id:
            return jsonify({'success': False, 'error': 'User not in any unit'}), 400
        unit = Unit.query.get(user.unit_id)
        if unit.supervisor_id != current_user.id and current_user.role not in ['head_of_it', 'admin']:
            return jsonify({'success': False, 'error': 'Unauthorized'}), 403

        data = request.get_json()
        comment = data.get('comment', '')
        
        # Store old status for logging
        old_status = entry.supervisor_approval_status
        
        # Update entry
        entry.supervisor_approval_status = 'approved'
        entry.supervisor_approved_by = current_user.id
        entry.supervisor_approved_at = datetime.utcnow()
        entry.supervisor_comment = comment
        entry.status = 'supervisor_approved'

        # Reset head approval status for all service items (if previously rejected)
        service_items = ServiceItem.query.filter_by(time_entry_id=entry.id).all()
        for item in service_items:
            if item.head_approval_status == 'rejected':
                old_head_status = item.head_approval_status
                item.head_approval_status = 'pending'
                item.head_approval_comment = None
                item.head_approved_by = None
                item.head_approved_at = None
                
                # Log head status reset
                log = ActivityLog(
                    time_entry_id=entry.id,
                    service_item_id=item.id,
                    user_id=current_user.id,
                    action='head_status_reset',
                    status_from=old_head_status,
                    status_to='pending',
                    details={'reason': 'Supervisor approved after rejection', 'comment': comment}
                )
                db.session.add(log)
        
        # Create activity log for supervisor approval
        log = ActivityLog(
            time_entry_id=entry.id,
            user_id=current_user.id,
            action='supervisor_approved',
            status_from=old_status,
            status_to='approved',
            details={'comment': comment, 'supervisor_name': current_user.full_name}
        )
        db.session.add(log)
        db.session.commit()

        # Send email to BU user
        from app.utils.email_helper import send_approval_email
        send_approval_email(
            user=user,
            service_item=None,
            entry=entry,
            status='approved',
            comment=comment,
            approver_role='supervisor'
        )

        return jsonify({'success': True, 'message': 'Entry approved'})
    except Exception as e:
        db.session.rollback()
        print("Error in supervisor_approve_entry:", str(e))
        return jsonify({'success': False, 'error': str(e)}), 500


@api_bp.route('/supervisor/entries/<int:entry_id>/reject', methods=['POST'])
@login_required
def supervisor_reject_entry(entry_id):
    """Supervisor rejects a time entry - goes back to BU user for editing"""
    if current_user.role != 'supervisor':
        return jsonify({'success': False, 'error': 'Unauthorized'}), 403

    try:
        entry = TimeEntry.query.get_or_404(entry_id)

        # Check authorization
        bu_user = User.query.get(entry.user_id)
        if not bu_user or not bu_user.unit_id:
            return jsonify({'success': False, 'error': 'User not in any unit'}), 400

        unit = Unit.query.get(bu_user.unit_id)
        if unit.supervisor_id != current_user.id:
            return jsonify({'success': False, 'error': 'Unauthorized'}), 403

        data = request.get_json()
        comment = data.get('comment', '')
        if not comment:
            return jsonify({'success': False, 'error': 'Comment required for rejection'}), 400

        # Store old status for logging
        old_status = entry.supervisor_approval_status

        # Update entry
        entry.supervisor_approval_status = 'rejected'
        entry.supervisor_rejected_by = current_user.id
        entry.supervisor_rejected_at = datetime.utcnow()
        entry.supervisor_comment = comment
        entry.status = 'supervisor_rejected'

        # Create activity log for supervisor rejection
        log = ActivityLog(
            time_entry_id=entry.id,
            user_id=current_user.id,
            action='supervisor_rejected',
            status_from=old_status,
            status_to='rejected',
            details={'comment': comment, 'supervisor_name': current_user.full_name}
        )
        db.session.add(log)
        db.session.commit()

        # Send email to BU user
        from app.utils.email_helper import send_approval_email
        send_approval_email(
            user=bu_user,
            service_item=None,
            entry=entry,
            status='rejected',
            comment=comment,
            approver_role='supervisor'
        )

        return jsonify({'success': True, 'message': 'Entry rejected'})

    except Exception as e:
        db.session.rollback()
        print("Error in supervisor_reject_entry:", str(e))
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500



@api_bp.route('/supervisor/batch-approve', methods=['POST'])
@login_required
def supervisor_batch_approve():
    if current_user.role != 'supervisor':
        return jsonify({'success': False, 'error': 'Unauthorized'}), 403

    data = request.get_json()
    service_ids = data.get('service_ids', [])
    comment = data.get('comment', '')

    if not service_ids:
        return jsonify({'success': False, 'error': 'No services selected'}), 400

    approved_count = 0
    affected_entries = set()
    activity_logs = []

    for sid in service_ids:
        service_item = ServiceItem.query.get(sid)
        if not service_item:
            continue
        entry = TimeEntry.query.get(service_item.time_entry_id)
        if not entry:
            continue
        unit = Unit.query.get(entry.unit_id)
        if not unit or unit.supervisor_id != current_user.id:
            continue

        # Already approved? skip
        if service_item.supervisor_approval_status == 'approved':
            continue

        # Store old status for logging
        old_status = service_item.supervisor_approval_status

        # Approve this service (update ServiceItem, NOT TimeEntry)
        service_item.supervisor_approval_status = 'approved'
        service_item.supervisor_approved_by = current_user.id
        service_item.supervisor_approved_at = datetime.utcnow()
        service_item.supervisor_comment = comment
        
        # Create activity log for this service approval
        log = ActivityLog(
            time_entry_id=entry.id,
            service_item_id=service_item.id,
            user_id=current_user.id,
            action='supervisor_approved',
            status_from=old_status,
            status_to='approved',
            details={
                'comment': comment,
                'supervisor_name': current_user.full_name,
                'batch_approve': True,
                'service_ids_count': len(service_ids)
            },
            ip_address=request.remote_addr
        )
        activity_logs.append(log)
        
        approved_count += 1
        affected_entries.add(entry.id)
        
        # If this service was previously rejected by Head of IT, reset its status
        if service_item.head_approval_status == 'rejected':
            service_item.head_approval_status = 'pending'
            service_item.head_approval_comment = None
            service_item.head_approved_by = None
            service_item.head_approved_at = None

    # After updating all services, update the status of each affected entry
    for entry_id in affected_entries:
        entry = TimeEntry.query.get(entry_id)
        if not entry:
            continue
            
        old_entry_status = entry.status
        
        # Check all services in this entry
        all_services = ServiceItem.query.filter_by(time_entry_id=entry.id).all()
        all_approved = all(s.supervisor_approval_status == 'approved' for s in all_services)
        all_rejected = all(s.supervisor_approval_status == 'rejected' for s in all_services)
        
        # Update entry status based on service items (NOT supervisor_approval_status on TimeEntry)
        if all_approved:
            entry.status = 'supervisor_approved'
        elif all_rejected:
            entry.status = 'supervisor_rejected'
        else:
            entry.status = 'supervisor_partial'
        
        # Log entry status change
        entry_log = ActivityLog(
            time_entry_id=entry.id,
            user_id=current_user.id,
            action='entry_status_updated',
            status_from=old_entry_status,
            status_to=entry.status,
            details={
                'reason': 'batch_approve',
                'approved_services_count': approved_count,
                'total_services': len(all_services)
            },
            ip_address=request.remote_addr
        )
        activity_logs.append(entry_log)
    
    # Add all activity logs
    for log in activity_logs:
        db.session.add(log)
    
    db.session.commit()
    
    return jsonify({'success': True, 'message': f'Approved {approved_count} service(s)'})


@api_bp.route('/supervisor/batch-reject', methods=['POST'])
@login_required
def supervisor_batch_reject():
    if current_user.role != 'supervisor':
        return jsonify({'success': False, 'error': 'Unauthorized'}), 403

    data = request.get_json()
    service_ids = data.get('service_ids', [])
    comment = data.get('comment', '')

    if not service_ids:
        return jsonify({'success': False, 'error': 'No services selected'}), 400

    if not comment:
        return jsonify({'success': False, 'error': 'Comment required for rejection'}), 400

    rejected_count = 0
    affected_entries = set()
    activity_logs = []

    for sid in service_ids:
        service_item = ServiceItem.query.get(sid)
        if not service_item:
            continue
        entry = TimeEntry.query.get(service_item.time_entry_id)
        if not entry:
            continue
        unit = Unit.query.get(entry.unit_id)
        if not unit or unit.supervisor_id != current_user.id:
            continue

        if service_item.supervisor_approval_status == 'rejected':
            continue

        # Store old status for logging
        old_status = service_item.supervisor_approval_status

        service_item.supervisor_approval_status = 'rejected'
        service_item.supervisor_rejected_by = current_user.id
        service_item.supervisor_rejected_at = datetime.utcnow()
        service_item.supervisor_comment = comment
        
        # Create activity log for this service rejection
        log = ActivityLog(
            time_entry_id=entry.id,
            service_item_id=service_item.id,
            user_id=current_user.id,
            action='supervisor_rejected',
            status_from=old_status,
            status_to='rejected',
            details={
                'comment': comment,
                'supervisor_name': current_user.full_name,
                'batch_reject': True,
                'service_ids_count': len(service_ids)
            },
            ip_address=request.remote_addr
        )
        activity_logs.append(log)
        
        rejected_count += 1
        affected_entries.add(entry.id)

    for entry_id in affected_entries:
        entry = TimeEntry.query.get(entry_id)
        if not entry:
            continue
            
        old_entry_status = entry.status
        
        all_services = ServiceItem.query.filter_by(time_entry_id=entry.id).all()
        all_approved = all(s.supervisor_approval_status == 'approved' for s in all_services)
        all_rejected = all(s.supervisor_approval_status == 'rejected' for s in all_services)
        
        if all_approved:
            entry.status = 'supervisor_approved'
            entry.supervisor_approval_status = 'approved'
            entry.supervisor_approved_by = current_user.id
            entry.supervisor_approved_at = datetime.utcnow()
        elif all_rejected:
            entry.status = 'supervisor_rejected'
            entry.supervisor_approval_status = 'rejected'
            entry.supervisor_rejected_by = current_user.id
            entry.supervisor_rejected_at = datetime.utcnow()
        else:
            entry.status = 'supervisor_partial'
            entry.supervisor_approval_status = 'partial'
        
        # Log entry status change
        entry_log = ActivityLog(
            time_entry_id=entry.id,
            user_id=current_user.id,
            action='entry_status_updated',
            status_from=old_entry_status,
            status_to=entry.status,
            details={
                'reason': 'batch_reject',
                'rejected_services_count': rejected_count,
                'total_services': len(all_services),
                'comment': comment
            },
            ip_address=request.remote_addr
        )
        activity_logs.append(entry_log)
    
    # Add all activity logs
    for log in activity_logs:
        db.session.add(log)
    
    # Notify BU users for each affected entry
    for entry_id in affected_entries:
        entry = TimeEntry.query.get(entry_id)
        if entry and entry.supervisor_approval_status == 'rejected':
            bu_user = User.query.get(entry.user_id)
            if bu_user:
                send_notification(bu_user, entry, None, 'supervisor_rejected', '/bu-team/dashboard')

    db.session.commit()
    
    return jsonify({'success': True, 'message': f'Rejected {rejected_count} service(s)'})


@api_bp.route('/time-entries/<int:entry_id>/details', methods=['GET'])
@login_required
def get_time_entry_details(entry_id):
    """Get detailed information for a specific time entry including per-resource breakdown"""
    try:
        entry = TimeEntry.query.get_or_404(entry_id)
        
        # Check authorization
        user = User.query.get(entry.user_id)
        unit = Unit.query.get(user.unit_id) if user else None
        
        if current_user.role != 'admin' and current_user.role != 'head_of_it':
            if unit and unit.supervisor_id != current_user.id:
                return jsonify({'success': False, 'error': 'Unauthorized'}), 403
        
        services = ServiceItem.query.filter_by(time_entry_id=entry.id).all()
        
        # Get all resource types for lookup
        all_resource_types = ResourceType.query.all()
        resource_by_code = {rt.code: rt for rt in all_resource_types}
        resource_by_id = {str(rt.id): rt for rt in all_resource_types}
        
        services_data = []
        entry_total = 0
        
        for service in services:
            # Process resources to include all fields
            resources_list = []
            for r in service.resources or []:
                r_type_val = str(r.get('type', ''))
                # Look up ResourceType by code or ID
                rt = resource_by_code.get(r_type_val)
                if not rt:
                    rt = resource_by_id.get(r_type_val)
                
                if rt:
                    resource_name = rt.name
                    uom = rt.unit_of_measure or 'Units'
                    chargeback_rate = float(rt.chargeback_rate) if rt.chargeback_rate else 0
                    showback_rate = float(rt.showback_rate) if rt.showback_rate else 0
                else:
                    resource_name = r_type_val.replace('_', ' ').title()
                    uom = 'Units'
                    chargeback_rate = 0
                    showback_rate = 0
                
                # Support both 'quantity' and 'hours'
                qty = float(r.get('quantity', r.get('hours', 0)))
                
                # Use snapshot rates if stored, otherwise current rates
                # The stored resource may have chargeback_rate and showback_rate directly
                # If not, use the found rates
                if 'chargeback_rate' in r:
                    chargeback_rate = float(r.get('chargeback_rate', 0))
                if 'showback_rate' in r:
                    showback_rate = float(r.get('showback_rate', 0))
                
                chargeback_amount = qty * chargeback_rate
                showback_amount = qty * showback_rate
                total = chargeback_amount + showback_amount
                
                resources_list.append({
                    'name': resource_name,
                    'quantity': qty,
                    'uom': uom,
                    'chargeback_rate': chargeback_rate,
                    'showback_rate': showback_rate,
                    'chargeback_amount': chargeback_amount,
                    'showback_amount': showback_amount,
                    'total_amount': total
                })
                entry_total += total
            
            # For service-level totals (if needed)
            service_chargeback = float(service.chargeback_total) if service.chargeback_total else 0
            service_showback = float(service.showback_total) if service.showback_total else 0
            service_total = service_chargeback + service_showback
            
            services_data.append({
                'id': service.id,
                'company_name': service.company.name if service.company else 'N/A',
                'description': service.service_description,
                'total_amount': service_total,
                'chargeback_total': service_chargeback,
                'showback_total': service_showback,
                'resources': resources_list
            })
        
        # Entry-level totals
        entry_grand_total = sum(s['total_amount'] for s in services_data)
        
        return jsonify({
            'success': True,
            'entry': {
                'user_name': user.full_name if user else 'Unknown',
                'employee_id': user.employee_id if user else 'N/A',
                'unit_name': unit.name if unit else 'N/A',
                'service_date': entry.service_date.strftime('%Y-%m-%d') if entry.service_date else 'N/A',
                'week_number': entry.week_number,
                'submission_date': entry.submission_date.strftime('%Y-%m-%d %H:%M') if entry.submission_date else entry.created_at.strftime('%Y-%m-%d %H:%M'),
                'total_amount': entry_grand_total,
                'supervisor_approval_status': entry.supervisor_approval_status
            },
            'services': services_data
        })
        
    except Exception as e:
        print("Error in get_time_entry_details:", str(e))
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500
    


# ============ SUPERVISOR BATCH APPROVAL ============

@api_bp.route('/supervisor/approve-week', methods=['POST'])
@login_required
def supervisor_approve_week():
    """Supervisor approves all entries for a specific week"""
    if current_user.role != 'supervisor':
        return jsonify({'success': False, 'error': 'Unauthorized'}), 403
    
    try:
        data = request.get_json()
        week_number = data.get('week_number')
        
        if not week_number:
            return jsonify({'success': False, 'error': 'Week number required'}), 400
        
        # Get units where user is supervisor
        units = Unit.query.filter_by(supervisor_id=current_user.id).all()
        unit_ids = [u.id for u in units]
        
        # Get users in those units
        unit_members = UnitMember.query.filter(UnitMember.unit_id.in_(unit_ids)).all()
        user_ids = [um.user_id for um in unit_members]
        
        # Get current month's entries for this week
        today = datetime.now().date()
        month_start = today.replace(day=1)
        
        # Get entries for this week that are pending
        entries = TimeEntry.query.filter(
            TimeEntry.user_id.in_(user_ids),
            TimeEntry.week_number == week_number,
            TimeEntry.service_date >= month_start,
            TimeEntry.supervisor_approval_status == 'pending',
            TimeEntry.status == 'submitted'
        ).all()
        
        approved_count = 0
        for entry in entries:
            entry.supervisor_approval_status = 'approved'
            entry.supervisor_approved_by = current_user.id
            entry.supervisor_approved_at = datetime.utcnow()
            entry.status = 'supervisor_approved'
            approved_count += 1
            
            # Notify BU user
            bu_user = User.query.get(entry.user_id)
            if bu_user:
                from app.utils.email_helper import send_approval_email
                send_approval_email(bu_user, None, entry, 'approved', f'Week {week_number} approved by supervisor')
        
        db.session.commit()
        
        return jsonify({
            'success': True,
            'message': f'Approved {approved_count} entries for Week {week_number}'
        })
        
    except Exception as e:
        db.session.rollback()
        print("Error in supervisor_approve_week:", str(e))
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500


# ============ HEAD OF IT BATCH APPROVAL ============



@api_bp.route('/service-items/update', methods=['PUT'])
@login_required
def update_service_item():
    try:
        data = request.get_json()
        service_item = ServiceItem.query.get_or_404(data['service_item_id'])
        entry = TimeEntry.query.get(service_item.time_entry_id)
        
        if entry.user_id != current_user.id:
            return jsonify({'success': False, 'error': 'Unauthorized'}), 403
        
        # Only allow editing if the service item is actually rejected by supervisor
        if service_item.supervisor_approval_status != 'rejected':
            return jsonify({'success': False, 'error': 'Only rejected service items can be resubmitted'}), 400
        
        # Store old values for logging
        old_description = service_item.service_description
        old_resources = service_item.resources.copy() if service_item.resources else []
        
        # Process resources
        new_resources = []
        chargeback_total = 0
        showback_total = 0
        
        for r in data.get('resources', []):
            resource_type_id = r.get('type')
            quantity = r.get('quantity', 0)
            new_resources.append({
                'type': resource_type_id,
                'quantity': quantity,
                'description': (r.get('description') or '').strip()
            })
            rt = ResourceType.query.get(resource_type_id)
            if rt:
                chargeback_total += quantity * (rt.chargeback_rate or 0)
                showback_total += quantity * (rt.showback_rate or 0)
        
        service_item.service_description = combine_resource_descriptions(new_resources)
        
        service_item.resources = new_resources
        service_item.chargeback_total = chargeback_total
        service_item.showback_total = showback_total
        service_item.total_amount = chargeback_total + showback_total
        
        # Reset all approval statuses for this service item
        old_supervisor_status = service_item.supervisor_approval_status
        old_head_status = service_item.head_approval_status
        old_finance_status = service_item.finance_approval_status
        
        service_item.supervisor_approval_status = 'pending'
        service_item.supervisor_comment = None
        service_item.supervisor_approved_by = None
        service_item.supervisor_approved_at = None
        service_item.head_approval_status = 'pending'
        service_item.head_approval_comment = None
        service_item.head_approved_by = None
        service_item.head_approved_at = None
        service_item.finance_approval_status = 'pending'
        service_item.finance_approval_comment = None
        service_item.finance_approved_by = None
        service_item.finance_approved_at = None
        
        # Update the parent entry's overall status
        entry.status = 'submitted'
        
        # Create activity log for resubmission
        log = ActivityLog(
            time_entry_id=entry.id,
            service_item_id=service_item.id,
            user_id=current_user.id,
            action='resubmitted',
            status_from='rejected',
            status_to='pending',
            details={
                'old_description': old_description,
                'new_description': service_item.service_description,
                'old_resources': old_resources,
                'new_resources': new_resources,
                'resubmitted_by': current_user.full_name
            }
        )
        db.session.add(log)
        db.session.commit()
        
        # Send notification to supervisor
        bu_user = User.query.get(entry.user_id)
        if bu_user and bu_user.unit_id:
            unit = Unit.query.get(bu_user.unit_id)
            if unit and unit.supervisor_id:
                supervisor = User.query.get(unit.supervisor_id)
                if supervisor:
                    from app.utils.email_helper import send_submission_email
                    send_submission_email(supervisor, entry, service_item)
        
        return jsonify({'success': True, 'message': 'Service updated and resubmitted for approval'})
        
    except Exception as e:
        db.session.rollback()
        print("Error in update_service_item:", str(e))
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500



# Add these imports at the top
from app.utils.ai_predictor import get_revenue_forecast
from sqlalchemy import func

@api_bp.route('/payment/revenue-forecast', methods=['GET'])
@login_required
def payment_revenue_forecast():
    """Get AI revenue forecast for payment team"""
    if current_user.role not in ['payment_company_user', 'payment_lolc_technologies', 'payment_lolc_finance']:
        return jsonify({'success': False, 'error': 'Unauthorized'}), 403
    
    months = request.args.get('months', 6, type=int)
    years = request.args.get('years', 5, type=int)
    
    try:
        company_id = None
        if (
            current_user.role == 'payment_company_user'
            and not company_service.is_parent_company_user(current_user)
        ):
            if not current_user.company_id:
                return jsonify({'success': False, 'error': 'Company payment user has no company assigned'}), 403
            company_id = current_user.company_id

        forecast_data = get_revenue_forecast(months=months, years_history=years, company_id=company_id)
        
        if not forecast_data:
            fallback_forecast = [0] * months
            return jsonify({
                'success': True,
                'message': 'No sufficient historical data to generate a forecast',
                'historical': [],
                'forecast': fallback_forecast,
                'next_month_prediction': 0,
                'next_3_months_total': 0,
                'next_6_months_total': 0,
                'growth_percent': 0,
                'confidence_level': 50
            })
        
        return jsonify({
            'success': True,
            'historical': forecast_data['historical'],
            'forecast': forecast_data['forecast'],
            'next_month_prediction': forecast_data['next_month_prediction'],
            'next_3_months_total': forecast_data['next_3_months_total'],
            'next_6_months_total': forecast_data['next_6_months_total'],
            'growth_percent': forecast_data['growth_percent'],
            'confidence_level': forecast_data['confidence_level']
        })
        
    except Exception as e:
        print(f"Error in revenue forecast: {str(e)}")
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500


@api_bp.route('/payment/revenue-by-company', methods=['GET'])
@login_required
def payment_revenue_by_company():
    """Get revenue breakdown by company (last 12 months)"""
    if current_user.role not in ['payment_company_user', 'payment_lolc_technologies', 'payment_lolc_finance']:
        return jsonify({'success': False, 'error': 'Unauthorized'}), 403
    
    try:
        from app.models import Company
        
        query = db.session.query(
            Company.name,
            func.sum(ServiceItem.total_amount).label('revenue')
        ).join(ServiceItem).filter(
            ServiceItem.finance_approval_status == 'approved',
            ServiceItem.finance_approved_at >= datetime.now() - timedelta(days=365)
        )

        if (
            current_user.role == 'payment_company_user'
            and not company_service.is_parent_company_user(current_user)
        ):
            if not current_user.company_id:
                return jsonify({'success': False, 'error': 'Company payment user has no company assigned'}), 403
            query = query.filter(Company.id == current_user.company_id)
        
        data = query.group_by(Company.id).order_by(func.sum(ServiceItem.total_amount).desc()).all()
        
        total_revenue = sum(float(d.revenue) for d in data)
        companies = []
        for d in data:
            revenue = float(d.revenue)
            companies.append({
                'name': d.name,
                'revenue': revenue,
                'percentage': round((revenue / total_revenue) * 100, 1) if total_revenue > 0 else 0
            })
        
        return jsonify({'success': True, 'companies': companies})
        
    except Exception as e:
        print(f"Error in revenue by company: {str(e)}")
        return jsonify({'success': False, 'error': str(e)}), 500


@api_bp.route('/payment/revenue-by-unit', methods=['GET'])
@login_required
def payment_revenue_by_unit():
    """Get revenue breakdown by unit (last 12 months)"""
    if current_user.role not in ['payment_company_user', 'payment_lolc_technologies', 'payment_lolc_finance']:
        return jsonify({'success': False, 'error': 'Unauthorized'}), 403
    
    try:
        from app.models import Unit
        
        query = db.session.query(
            Unit.name,
            func.sum(ServiceItem.total_amount).label('revenue')
        ).join(User, User.unit_id == Unit.id)\
         .join(TimeEntry, TimeEntry.user_id == User.id)\
         .join(ServiceItem, ServiceItem.time_entry_id == TimeEntry.id)\
         .filter(
            ServiceItem.finance_approval_status == 'approved',
            ServiceItem.finance_approved_at >= datetime.now() - timedelta(days=365)
        )

        if (
            current_user.role == 'payment_company_user'
            and not company_service.is_parent_company_user(current_user)
        ):
            if not current_user.company_id:
                return jsonify({'success': False, 'error': 'Company payment user has no company assigned'}), 403
            query = query.filter(ServiceItem.company_id == current_user.company_id)

        data = query.group_by(Unit.id).order_by(func.sum(ServiceItem.total_amount).desc()).all()
        
        units = [{'name': d.name, 'revenue': float(d.revenue)} for d in data]
        
        return jsonify({'success': True, 'units': units})
        
    except Exception as e:
        print(f"Error in revenue by unit: {str(e)}")
        return jsonify({'success': False, 'error': str(e)}), 500


@api_bp.route('/payment/ai-insights-summary', methods=['GET'])
@login_required
def payment_ai_insights_summary():
    """Get database-backed summary metrics for the AI insights dashboard"""
    if current_user.role not in ['payment_company_user', 'payment_lolc_technologies', 'payment_lolc_finance']:
        return jsonify({'success': False, 'error': 'Unauthorized'}), 403

    if current_user.role == 'payment_company_user' and not current_user.company_id:
        return jsonify({'success': False, 'error': 'Company payment user has no company assigned'}), 403

    try:
        company_filter = None
        if (
            current_user.role == 'payment_company_user'
            and not company_service.is_parent_company_user(current_user)
        ):
            company_filter = current_user.company_id

        base_filters = [
            ServiceItem.finance_approval_status == 'approved',
        ]
        if company_filter is not None:
            base_filters.append(ServiceItem.company_id == company_filter)

        total_12m = db.session.query(func.coalesce(func.sum(ServiceItem.total_amount), 0)).filter(
            *base_filters,
            ServiceItem.finance_approved_at >= datetime.now() - timedelta(days=365)
        ).scalar() or 0

        from sqlalchemy import extract
        current_year = datetime.now().year
        total_ytd = db.session.query(func.coalesce(func.sum(ServiceItem.total_amount), 0)).filter(
            *base_filters,
            extract('year', ServiceItem.finance_approved_at) == current_year
        ).scalar() or 0

        last_month_date = datetime.now().replace(day=1) - timedelta(days=1)
        last_month_total = db.session.query(func.coalesce(func.sum(ServiceItem.total_amount), 0)).filter(
            *base_filters,
            extract('year', ServiceItem.finance_approved_at) == last_month_date.year,
            extract('month', ServiceItem.finance_approved_at) == last_month_date.month
        ).scalar() or 0

        company_data = db.session.query(
            Company.name,
            func.sum(ServiceItem.total_amount).label('revenue')
        ).join(ServiceItem).filter(
            *base_filters,
            ServiceItem.finance_approved_at >= datetime.now() - timedelta(days=365)
        ).group_by(Company.id).order_by(func.sum(ServiceItem.total_amount).desc()).first()

        unit_data = db.session.query(
            Unit.name,
            func.sum(ServiceItem.total_amount).label('revenue')
        ).join(User, User.unit_id == Unit.id)\
         .join(TimeEntry, TimeEntry.user_id == User.id)\
         .join(ServiceItem, ServiceItem.time_entry_id == TimeEntry.id)\
         .filter(
            *base_filters,
            ServiceItem.finance_approved_at >= datetime.now() - timedelta(days=365)
        ).group_by(Unit.id).order_by(func.sum(ServiceItem.total_amount).desc()).first()

        top_company = {
            'name': company_data.name if company_data else 'N/A',
            'revenue': float(company_data.revenue) if company_data else 0
        }
        top_unit = {
            'name': unit_data.name if unit_data else 'N/A',
            'revenue': float(unit_data.revenue) if unit_data else 0
        }

        return jsonify({
            'success': True,
            'total_12_months': float(total_12m),
            'total_year_to_date': float(total_ytd),
            'average_monthly_revenue': float(total_12m / 12 if total_12m else 0),
            'last_month_revenue': float(last_month_total),
            'top_company': top_company,
            'top_unit': top_unit
        })
    except Exception as e:
        print(f"Error in ai insights summary: {str(e)}")
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500
    



@api_bp.route('/companies/paginated', methods=['GET'])
@login_required
def get_companies_paginated():
    """Get paginated list of companies for modal selection"""
    page = request.args.get('page', 1, type=int)
    per_page = request.args.get('per_page', 10, type=int)
    search = request.args.get('search', '')
    
    query = Company.query.filter_by(is_active=True)
    
    if search:
        query = query.filter(
            db.or_(
                Company.name.ilike(f'%{search}%'),
                Company.code.ilike(f'%{search}%')
            )
        )
    
    pagination = query.paginate(page=page, per_page=per_page, error_out=False)
    
    items = [{
        'id': c.id,
        'name': c.name,
        'code': c.code or ''
    } for c in pagination.items]
    
    return jsonify({
        'success': True,
        'items': items,
        'total': pagination.total,
        'page': page,
        'per_page': per_page,
        'total_pages': pagination.pages
    })


@api_bp.route('/users/supervisors/paginated', methods=['GET'])
@login_required
def get_supervisors_paginated():
    """Get paginated list of users with Supervisor role only"""
    page = request.args.get('page', 1, type=int)
    per_page = request.args.get('per_page', 10, type=int)
    search = request.args.get('search', '')
    
    # ONLY supervisor role
    query = User.query.filter(User.role == 'supervisor', User.is_active == True)
    
    if search:
        query = query.filter(
            db.or_(
                User.first_name.ilike(f'%{search}%'),
                User.last_name.ilike(f'%{search}%'),
                User.email.ilike(f'%{search}%'),
                User.employee_id.ilike(f'%{search}%')
            )
        )
    
    pagination = query.paginate(page=page, per_page=per_page, error_out=False)
    
    items = [{
        'id': u.id,
        'name': u.full_name,
        'email': u.email,
        'employee_id': u.employee_id
    } for u in pagination.items]
    
    return jsonify({
        'success': True,
        'items': items,
        'total': pagination.total,
        'page': page,
        'per_page': per_page,
        'total_pages': pagination.pages
    })


@api_bp.route('/users/bu-users/paginated', methods=['GET'])
@login_required
def get_bu_users_paginated():
    """Get paginated list of BU users (unassigned)"""
    page = request.args.get('page', 1, type=int)
    per_page = request.args.get('per_page', 10, type=int)
    search = request.args.get('search', '')
    exclude_unit_id = request.args.get('exclude_unit_id', type=int)
    
    # BU users only, active, not assigned to any unit (or exclude current unit for edit)
    query = User.query.filter(
        User.role == 'bu_user',
        User.is_active == True,
        User.unit_id.is_(None)
    )
    
    if exclude_unit_id:
        # For edit page, also include users already in this unit
        query = User.query.filter(
            User.role == 'bu_user',
            User.is_active == True,
            db.or_(
                User.unit_id.is_(None),
                User.unit_id == exclude_unit_id
            )
        )
    
    if search:
        query = query.filter(
            db.or_(
                User.first_name.ilike(f'%{search}%'),
                User.last_name.ilike(f'%{search}%'),
                User.email.ilike(f'%{search}%'),
                User.employee_id.ilike(f'%{search}%')
            )
        )
    
    pagination = query.paginate(page=page, per_page=per_page, error_out=False)
    
    items = [{
        'id': u.id,
        'name': u.full_name,
        'email': u.email,
        'employee_id': u.employee_id
    } for u in pagination.items]
    
    return jsonify({
        'success': True,
        'items': items,
        'total': pagination.total,
        'page': page,
        'per_page': per_page,
        'total_pages': pagination.pages
    })


#CEO Anlytical Dashboard

# Add to app/routes/api.py

# ============ ANALYTICS DASHBOARD API ENDPOINTS ============




@api_bp.route('/payment/analytics/monthly-breakdown', methods=['GET'])
@login_required
def get_analytics_monthly_breakdown():
    """Get monthly breakdown of amounts"""
    
    if current_user.role != 'super_admin':
        return jsonify({'success': False, 'error': 'Unauthorized'}), 403
    
    try:
        date_from = request.args.get('date_from')
        date_to = request.args.get('date_to')
        company_id = request.args.get('company_id')
        
        # Build query for service items that are finance approved
        query = ServiceItem.query.join(TimeEntry).filter(
            ServiceItem.finance_approval_status == 'approved'
        )
        
        if date_from:
            query = query.filter(TimeEntry.service_date >= date_from)
        if date_to:
            query = query.filter(TimeEntry.service_date <= date_to)
        if company_id and company_id != 'all':
            query = query.filter(ServiceItem.company_id == company_id)
        
        service_items = query.all()
        
        # Group by month
        monthly_data = {}
        
        for item in service_items:
            entry = TimeEntry.query.get(item.time_entry_id)
            if not entry or not entry.service_date:
                continue
            
            month_key = entry.service_date.strftime('%Y-%m')
            
            if month_key not in monthly_data:
                monthly_data[month_key] = {
                    'month': month_key,
                    'service_count': 0,
                    'total_loit_cb': 0,
                    'total_loit_sb': 0,
                    'total_loit': 0
                }
            
            # Calculate amounts
            loit_cb = 0
            loit_sb = 0
            for resource in item.resources or []:
                resource_type_id = str(resource.get('type', ''))
                rt = ResourceType.query.get(resource_type_id)
                if rt:
                    quantity = float(resource.get('quantity') or resource.get('hours') or 0)
                    loit_cb += quantity * float(rt.loit_chargeback or 0)
                    loit_sb += quantity * float(rt.loit_showback or 0)
            
            loit_cb = loit_cb if loit_cb > 0 else float(item.chargeback_total or 0)
            loit_sb = loit_sb if loit_sb > 0 else float(item.showback_total or 0)
            
            monthly_data[month_key]['service_count'] += 1
            monthly_data[month_key]['total_loit_cb'] += loit_cb
            monthly_data[month_key]['total_loit_sb'] += loit_sb
            monthly_data[month_key]['total_loit'] += loit_cb + loit_sb
        
        # Sort by month
        result = sorted(monthly_data.values(), key=lambda x: x['month'])
        
        return jsonify({'success': True, 'monthly_data': result})
        
    except Exception as e:
        print("Error in get_analytics_monthly_breakdown:", str(e))
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500







@api_bp.route('/payment/analytics/summary', methods=['GET'])
@login_required
def get_analytics_summary():
    """Get analytics summary data with filters"""
    
    # ONLY Super Admin has access
    if current_user.role != 'super_admin':
        return jsonify({'success': False, 'error': 'Unauthorized'}), 403
    
    try:
        date_from = request.args.get('date_from')
        date_to = request.args.get('date_to')
        status = request.args.get('status')
        company_id = request.args.get('company_id')
        
        # Build query
        query = ServiceItem.query.join(TimeEntry, ServiceItem.time_entry_id == TimeEntry.id)
        
        if date_from:
            query = query.filter(TimeEntry.service_date >= date_from)
        if date_to:
            query = query.filter(TimeEntry.service_date <= date_to)
        if company_id and company_id != 'all':
            query = query.filter(ServiceItem.company_id == company_id)
        
        if status == 'finance_approved':
            query = query.filter(
                ServiceItem.head_approval_status == 'approved',
                ServiceItem.finance_approval_status == 'approved'
            )
        elif status == 'pending_finance':
            query = query.filter(
                ServiceItem.head_approval_status == 'approved',
                ServiceItem.finance_approval_status == 'pending'
            )
        else:
            query = query.filter(
                ServiceItem.head_approval_status == 'approved',
                ServiceItem.finance_approval_status == 'approved'
            )
        
        service_items = query.all()
        
        all_rt = ResourceType.query.all()
        rt_by_id = {str(rt.id): rt for rt in all_rt}
        
        total_services = len(service_items)
        total_loit_cb = 0.0
        total_loit_sb = 0.0
        total_lolc_sb = 0.0
        total_grand = 0.0
        
        for item in service_items:
            loit_cb = 0.0
            loit_sb = 0.0
            lolc_sb = 0.0
            
            if item.resources:
                for resource in item.resources:
                    resource_type_id = str(resource.get('type', ''))
                    rt = rt_by_id.get(resource_type_id)
                    if rt:
                        quantity = float(resource.get('quantity') or resource.get('hours') or 0)
                        loit_cb += quantity * float(rt.loit_chargeback or 0)
                        loit_sb += quantity * float(rt.loit_showback or 0)
                        lolc_sb += quantity * float(rt.showback_rate or 0)
            
            if loit_cb == 0 and loit_sb == 0 and lolc_sb == 0:
                chargeback_total = float(item.chargeback_total or 0)
                showback_total = float(item.showback_total or 0)
                loit_cb = chargeback_total
                loit_sb = 0.0
                lolc_sb = showback_total
            
            total_loit_cb += loit_cb
            total_loit_sb += loit_sb
            total_lolc_sb += lolc_sb
            total_grand += (loit_cb + loit_sb + lolc_sb)
        
        return jsonify({
            'success': True,
            'summary': {
                'total_services': total_services,
                'total_loit_cb': total_loit_cb,
                'total_loit_sb': total_loit_sb,
                'total_lolc_sb': total_lolc_sb,
                'total_grand': total_grand
            }
        })
        
    except Exception as e:
        print("Error in get_analytics_summary:", str(e))
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500


@api_bp.route('/payment/analytics/company-revenue', methods=['GET'])
@login_required
def get_company_revenue_analytics():
    """Get revenue breakdown by company"""
    
    # ONLY Super Admin has access
    if current_user.role != 'super_admin':
        return jsonify({'success': False, 'error': 'Unauthorized'}), 403
    
    try:
        date_from = request.args.get('date_from')
        date_to = request.args.get('date_to')
        company_id = request.args.get('company_id')
        
        query = db.session.query(
            Company.id,
            Company.name,
            func.sum(ServiceItem.total_amount).label('revenue')
        ).join(ServiceItem, ServiceItem.company_id == Company.id)\
         .join(TimeEntry, TimeEntry.id == ServiceItem.time_entry_id)\
         .filter(
            ServiceItem.head_approval_status == 'approved',
            ServiceItem.finance_approval_status == 'approved'
        )
        
        if date_from:
            query = query.filter(TimeEntry.service_date >= date_from)
        if date_to:
            query = query.filter(TimeEntry.service_date <= date_to)
        if company_id and company_id != 'all':
            query = query.filter(Company.id == company_id)
        
        results = query.group_by(Company.id).order_by(func.sum(ServiceItem.total_amount).desc()).all()
        
        companies = []
        total_revenue = 0.0
        
        for r in results:
            revenue = float(r.revenue or 0)
            total_revenue += revenue
            companies.append({
                'id': r.id,
                'name': r.name,
                'revenue': revenue
            })
        
        return jsonify({'success': True, 'companies': companies, 'total_revenue': total_revenue})
        
    except Exception as e:
        print("Error in get_company_revenue_analytics:", str(e))
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500


@api_bp.route('/payment/analytics/approval-times', methods=['GET'])
@login_required
def get_approval_times_analytics():
    """Get average approval times for each stage"""
    
    # ONLY Super Admin has access
    if current_user.role != 'super_admin':
        return jsonify({'success': False, 'error': 'Unauthorized'}), 403
    
    try:
        date_from = request.args.get('date_from')
        date_to = request.args.get('date_to')
        company_id = request.args.get('company_id')
        
        query = ServiceItem.query.join(TimeEntry, ServiceItem.time_entry_id == TimeEntry.id)
        
        if date_from:
            query = query.filter(TimeEntry.service_date >= date_from)
        if date_to:
            query = query.filter(TimeEntry.service_date <= date_to)
        if company_id and company_id != 'all':
            query = query.filter(ServiceItem.company_id == company_id)
        
        items = query.all()
        
        total_supervisor_hours = 0
        total_head_hours = 0
        total_finance_hours = 0
        count = 0
        
        for item in items:
            if item.supervisor_approved_at and item.created_at:
                hours = (item.supervisor_approved_at - item.created_at).total_seconds() / 3600
                total_supervisor_hours += hours
            
            if item.head_approved_at and item.supervisor_approved_at:
                hours = (item.head_approved_at - item.supervisor_approved_at).total_seconds() / 3600
                total_head_hours += hours
            
            if item.finance_approved_at and item.head_approved_at:
                hours = (item.finance_approved_at - item.head_approved_at).total_seconds() / 3600
                total_finance_hours += hours
            
            count += 1
        
        avg_supervisor = round(total_supervisor_hours / count, 1) if count > 0 else 0
        avg_head = round(total_head_hours / count, 1) if count > 0 else 0
        avg_finance = round(total_finance_hours / count, 1) if count > 0 else 0
        
        return jsonify({
            'success': True,
            'avg_supervisor_hours': avg_supervisor,
            'avg_head_hours': avg_head,
            'avg_finance_hours': avg_finance,
            'avg_total_hours': avg_supervisor + avg_head + avg_finance
        })
        
    except Exception as e:
        print("Error in get_approval_times_analytics:", str(e))
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500


@api_bp.route('/payment/analytics/unit-analysis', methods=['GET'])
@login_required
def get_unit_analysis():
    """Get detailed analysis for a specific unit"""
    
    # ONLY Super Admin has access
    if current_user.role != 'super_admin':
        return jsonify({'success': False, 'error': 'Unauthorized'}), 403
    
    try:
        unit_id = request.args.get('unit_id')
        date_from = request.args.get('date_from')
        date_to = request.args.get('date_to')
        
        if not unit_id:
            return jsonify({'success': False, 'error': 'Unit ID required'}), 400
        
        unit = Unit.query.get(unit_id)
        if not unit:
            return jsonify({'success': False, 'error': 'Unit not found'}), 404
        
        # Build query for service items from this unit that are finance approved
        query = ServiceItem.query.join(TimeEntry).join(
            User, User.id == TimeEntry.user_id
        ).filter(
            User.unit_id == unit_id,
            ServiceItem.finance_approval_status == 'approved'
        )
        
        if date_from:
            query = query.filter(TimeEntry.service_date >= date_from)
        if date_to:
            query = query.filter(TimeEntry.service_date <= date_to)
        
        service_items = query.all()
        
        # Pre-fetch resource types
        all_rt = ResourceType.query.all()
        rt_by_id = {str(rt.id): rt for rt in all_rt}
        
        total_services = len(service_items)
        total_loit_cb = 0.0
        total_loit_sb = 0.0
        total_lolc_sb = 0.0
        total_grand = 0.0
        
        # Resource breakdown
        resource_summary = {}
        
        for item in service_items:
            loit_cb = 0.0
            loit_sb = 0.0
            lolc_sb = 0.0
            
            # Calculate from resources if available
            if item.resources:
                for resource in item.resources:
                    resource_type_id = str(resource.get('type', ''))
                    rt = rt_by_id.get(resource_type_id)
                    if rt:
                        quantity = float(resource.get('quantity') or resource.get('hours') or 0)
                        loit_cb += quantity * float(rt.loit_chargeback or 0)
                        loit_sb += quantity * float(rt.loit_showback or 0)
                        lolc_sb += quantity * float(rt.showback_rate or 0)
                        
                        # Build resource summary
                        resource_name = rt.name
                        uom = rt.unit_of_measure or 'units'
                        
                        if resource_name not in resource_summary:
                            resource_summary[resource_name] = {
                                'name': resource_name,
                                'uom': uom,
                                'quantity': 0,
                                'loit_cb': 0,
                                'loit_sb': 0,
                                'lolc_sb': 0,
                                'total': 0
                            }
                        
                        resource_summary[resource_name]['quantity'] += quantity
                        resource_summary[resource_name]['loit_cb'] += quantity * float(rt.loit_chargeback or 0)
                        resource_summary[resource_name]['loit_sb'] += quantity * float(rt.loit_showback or 0)
                        resource_summary[resource_name]['lolc_sb'] += quantity * float(rt.showback_rate or 0)
                        resource_summary[resource_name]['total'] += quantity * (float(rt.loit_chargeback or 0) + float(rt.loit_showback or 0) + float(rt.showback_rate or 0))
            
            # If no calculated values, use stored totals
            if loit_cb == 0 and loit_sb == 0 and lolc_sb == 0:
                chargeback_total = float(item.chargeback_total or 0)
                showback_total = float(item.showback_total or 0)
                loit_cb = chargeback_total
                loit_sb = 0.0
                lolc_sb = showback_total
            
            total_loit_cb += loit_cb
            total_loit_sb += loit_sb
            total_lolc_sb += lolc_sb
            total_grand += (loit_cb + loit_sb + lolc_sb)
        
        # Monthly breakdown
        monthly_breakdown = {}
        for item in service_items:
            entry = TimeEntry.query.get(item.time_entry_id)
            if not entry or not entry.service_date:
                continue
            
            month_key = entry.service_date.strftime('%Y-%m')
            if month_key not in monthly_breakdown:
                monthly_breakdown[month_key] = {'month': month_key, 'service_count': 0, 'total': 0}
            
            monthly_breakdown[month_key]['service_count'] += 1
            monthly_breakdown[month_key]['total'] += float(item.total_amount or 0)
        
        monthly_list = sorted(monthly_breakdown.values(), key=lambda x: x['month'])
        resource_list = sorted(resource_summary.values(), key=lambda x: x['total'], reverse=True)
        
        return jsonify({
            'success': True,
            'unit_name': unit.name,
            'summary': {
                'total_services': total_services,
                'total_loit_cb': total_loit_cb,
                'total_loit_sb': total_loit_sb,
                'total_lolc_sb': total_lolc_sb,
                'total_grand': total_grand
            },
            'monthly_breakdown': monthly_list,
            'resource_breakdown': resource_list
        })
        
    except Exception as e:
        print("Error in get_unit_analysis:", str(e))
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500


@api_bp.route('/payment/analytics/user-search', methods=['GET'])
@login_required
def get_user_search():
    """Search for any user by name, email, or employee ID"""
    
   # Check authorization
    if current_user.role != 'super_admin':
       return jsonify({'success': False, 'error': 'Unauthorized'}), 403
    
    try:
        search_term = request.args.get('search', '').strip()
        date_from = request.args.get('date_from')
        date_to = request.args.get('date_to')
        
        if not search_term:
            return jsonify({'success': False, 'error': 'Please enter a search term'}), 400
        
        # Search for user
        user = None
        
        # Try exact email match
        user = User.query.filter(User.email == search_term).first()
        
        # Try exact employee ID
        if not user:
            user = User.query.filter(User.employee_id == search_term).first()
        
        # Try partial name match
        if not user:
            user = User.query.filter(
                db.or_(
                    User.first_name.ilike(f'%{search_term}%'),
                    User.last_name.ilike(f'%{search_term}%'),
                    db.func.concat(User.first_name, ' ', User.last_name).ilike(f'%{search_term}%')
                )
            ).first()
        
        if not user:
            return jsonify({'success': False, 'error': f'No user found matching "{search_term}"'}), 404
        
        # Get user's unit name
        unit_name = None
        if user.unit_id:
            unit = Unit.query.get(user.unit_id)
            if unit:
                unit_name = unit.name
        
        # Get user's company name
        company_name = None
        if user.company_id:
            company = Company.query.get(user.company_id)
            if company:
                company_name = company.name
        
        # Get all resource types for lookup
        all_rt = ResourceType.query.all()
        rt_by_id = {str(rt.id): rt for rt in all_rt}
        
        # Initialize user data
        user_data = {
            'total_services': 0,
            'total_amount': 0,
            'pending_count': 0,
            'approved_count': 0,
            'rejected_count': 0,
            'invoiced_count': 0,
            'pending_reviews': 0,
            'team_members': 0,
            'records': []
        }
        
        # ========== BU USER ==========
        if user.role == 'bu_user':
            query = ServiceItem.query.join(TimeEntry).filter(TimeEntry.user_id == user.id)
            
            if date_from:
                query = query.filter(TimeEntry.service_date >= date_from)
            if date_to:
                query = query.filter(TimeEntry.service_date <= date_to)
            
            service_items = query.all()
            
            for item in service_items:
                entry = TimeEntry.query.get(item.time_entry_id)
                company = Company.query.get(item.company_id)
                
                # Calculate amounts
                loit_cb = 0
                loit_sb = 0
                for resource in item.resources or []:
                    resource_type_id = str(resource.get('type', ''))
                    rt = rt_by_id.get(resource_type_id)
                    if rt:
                        quantity = float(resource.get('quantity') or resource.get('hours') or 0)
                        loit_cb += quantity * float(rt.loit_chargeback or 0)
                        loit_sb += quantity * float(rt.loit_showback or 0)
                item_loit_cb, item_loit_sb, item_lolc_sb = calculate_service_item_breakdown(item, rt_by_id)
                amount = item_loit_cb + item_loit_sb + item_lolc_sb
                
                loit_cb = loit_cb if loit_cb > 0 else float(item.chargeback_total or 0)
                loit_sb = loit_sb if loit_sb > 0 else float(item.showback_total or 0)
                amount = loit_cb + loit_sb
                
                # Determine status
                if item.invoice_generated:
                    status = 'Invoiced'
                    user_data['invoiced_count'] += 1
                elif item.finance_approval_status == 'approved':
                    status = 'Approved'
                    user_data['approved_count'] += 1
                elif item.head_approval_status == 'approved':
                    status = 'Head Approved'
                    user_data['approved_count'] += 1
                elif item.supervisor_approval_status == 'approved':
                    status = 'Supervisor Approved'
                    user_data['approved_count'] += 1
                elif item.supervisor_approval_status == 'rejected':
                    status = 'Rejected'
                    user_data['rejected_count'] += 1
                else:
                    status = 'Pending'
                    user_data['pending_count'] += 1
                
                user_data['total_services'] += 1
                user_data['total_amount'] += amount
                
                user_data['records'].append({
                    'id': item.id,
                    'date': entry.service_date.strftime('%Y-%m-%d') if entry and entry.service_date else entry.created_at.strftime('%Y-%m-%d'),
                    'company_name': company.name if company else 'N/A',
                    'description': item.service_description[:80] if item.service_description else '',
                    'loit_cb': loit_cb,
                    'loit_sb': loit_sb,
                    'total': amount,
                    'loit_sb': item_loit_sb,
                    'total': amount, # This total is loit_cb + loit_sb + lolc_sb
                    'status': status
                })
        
        # ========== SUPERVISOR ==========
        elif user.role == 'supervisor':
            supervised_units = Unit.query.filter_by(supervisor_id=user.id).all()
            unit_ids = [u.id for u in supervised_units]
            
            if unit_ids:
                user_data['team_members'] = User.query.filter(User.unit_id.in_(unit_ids), User.role == 'bu_user').count()
                
                query = ServiceItem.query.join(TimeEntry).join(User, TimeEntry.user_id == User.id).filter(
                    User.unit_id.in_(unit_ids)
                )
                if date_from:
                    query = query.filter(TimeEntry.service_date >= date_from)
                if date_to:
                    query = query.filter(TimeEntry.service_date <= date_to)
                
                service_items = query.all()
                
                for item in service_items:
                    entry = TimeEntry.query.get(item.time_entry_id)
                    company = Company.query.get(item.company_id)
                    bu_user = User.query.get(entry.user_id) if entry else None
                    
                    loit_cb = 0
                    loit_sb = 0
                    for resource in item.resources or []:
                        resource_type_id = str(resource.get('type', ''))
                        rt = rt_by_id.get(resource_type_id)
                        if rt:
                            quantity = float(resource.get('quantity') or resource.get('hours') or 0)
                            loit_cb += quantity * float(rt.loit_chargeback or 0)
                            loit_sb += quantity * float(rt.loit_showback or 0)
                    item_loit_cb, item_loit_sb, item_lolc_sb = calculate_service_item_breakdown(item, rt_by_id)
                    amount = item_loit_cb + item_loit_sb + item_lolc_sb

                    
                    loit_cb = loit_cb if loit_cb > 0 else float(item.chargeback_total or 0)
                    loit_sb = loit_sb if loit_sb > 0 else float(item.showback_total or 0)
                    amount = loit_cb + loit_sb
                    
                    if item.supervisor_approval_status == 'pending':
                        user_data['pending_reviews'] += 1
                        status = 'Pending Review'
                    elif item.supervisor_approval_status == 'approved':
                        user_data['approved_count'] += 1
                        status = 'Approved'
                    elif item.supervisor_approval_status == 'rejected':
                        user_data['rejected_count'] += 1
                        status = 'Rejected'
                    else:
                        status = 'Processing'
                    
                    user_data['total_services'] += 1
                    user_data['total_amount'] += amount
                    
                    user_data['records'].append({
                        'id': item.id,
                        'date': entry.service_date.strftime('%Y-%m-%d') if entry and entry.service_date else entry.created_at.strftime('%Y-%m-%d'),
                        'company_name': company.name if company else 'N/A',
                        'bu_user_name': bu_user.full_name if bu_user else 'N/A',
                        'description': item.service_description[:80] if item.service_description else '',
                        'loit_cb': loit_cb,
                        'loit_sb': loit_sb,
                    'loit_cb': item_loit_cb,
                    'loit_sb': item_loit_sb,
                        'total': amount,
                        'status': status
                    })
        
        # ========== HEAD OF IT ==========
        elif user.role == 'head_of_it':
            if user.company_id:
                query = ServiceItem.query.filter(ServiceItem.company_id == user.company_id).join(TimeEntry)
                if date_from:
                    query = query.filter(TimeEntry.service_date >= date_from)
                if date_to:
                    query = query.filter(TimeEntry.service_date <= date_to)
                
                service_items = query.all()
                
                for item in service_items:
                    entry = TimeEntry.query.get(item.time_entry_id)
                    company = Company.query.get(item.company_id)

                    item_loit_cb, item_loit_sb, item_lolc_sb = calculate_service_item_breakdown(item, rt_by_id)
                    amount = item_loit_cb + item_loit_sb + item_lolc_sb
                    
                    loit_cb = 0
                    loit_sb = 0
                    for resource in item.resources or []:
                        resource_type_id = str(resource.get('type', ''))
                        rt = rt_by_id.get(resource_type_id)
                        if rt:
                            quantity = float(resource.get('quantity') or resource.get('hours') or 0)
                            loit_cb += quantity * float(rt.loit_chargeback or 0)
                            loit_sb += quantity * float(rt.loit_showback or 0)
                    
                    loit_cb = loit_cb if loit_cb > 0 else float(item.chargeback_total or 0)
                    loit_sb = loit_sb if loit_sb > 0 else float(item.showback_total or 0)
                    amount = loit_cb + loit_sb
                    
                    if item.head_approval_status == 'pending':
                        user_data['pending_reviews'] += 1
                        status = 'Pending Review'
                    elif item.head_approval_status == 'approved':
                        user_data['approved_count'] += 1
                        status = 'Approved'
                    elif item.head_approval_status == 'rejected':
                        user_data['rejected_count'] += 1
                        status = 'Rejected'
                    else:
                        status = 'Processing'
                    
                    user_data['total_services'] += 1
                    user_data['total_amount'] += amount
                    
                    user_data['records'].append({
                        'id': item.id,
                        'date': entry.service_date.strftime('%Y-%m-%d') if entry and entry.service_date else entry.created_at.strftime('%Y-%m-%d'),
                        'company_name': company.name if company else 'N/A',
                        'description': item.service_description[:80] if item.service_description else '',
                        'loit_cb': loit_cb,
                        'loit_sb': loit_sb,
                        'total': amount,
                    'loit_sb': item_loit_sb,
                    'total': amount, # This total is loit_cb + loit_sb + lolc_sb
                        'status': status
                    })
        
        # ========== FINANCE CONTROLLER ==========
        elif user.role == 'finance_controller':
            query = ServiceItem.query.join(TimeEntry)
            if date_from:
                query = query.filter(TimeEntry.service_date >= date_from)
            if date_to:
                query = query.filter(TimeEntry.service_date <= date_to)
            
            service_items = query.all()
            
            for item in service_items:
                entry = TimeEntry.query.get(item.time_entry_id)
                company = Company.query.get(item.company_id)

                item_loit_cb, item_loit_sb, item_lolc_sb = calculate_service_item_breakdown(item, rt_by_id)
                amount = item_loit_cb + item_loit_sb + item_lolc_sb
                
                loit_cb = 0
                loit_sb = 0
                for resource in item.resources or []:
                    resource_type_id = str(resource.get('type', ''))
                    rt = rt_by_id.get(resource_type_id)
                    if rt:
                        quantity = float(resource.get('quantity') or resource.get('hours') or 0)
                        loit_cb += quantity * float(rt.loit_chargeback or 0)
                        loit_sb += quantity * float(rt.loit_showback or 0)
                
                loit_cb = loit_cb if loit_cb > 0 else float(item.chargeback_total or 0)
                loit_sb = loit_sb if loit_sb > 0 else float(item.showback_total or 0)
                amount = loit_cb + loit_sb
                
                if item.finance_approval_status == 'pending':
                    user_data['pending_reviews'] += 1
                    status = 'Pending Review'
                elif item.finance_approval_status == 'approved':
                    user_data['approved_count'] += 1
                    status = 'Approved'
                elif item.finance_approval_status == 'rejected':
                    user_data['rejected_count'] += 1
                    status = 'Rejected'
                elif item.invoice_generated:
                    user_data['invoiced_count'] += 1
                    status = 'Invoiced'
                else:
                    status = 'Processing'
                
                user_data['total_services'] += 1
                user_data['total_amount'] += amount
                
                user_data['records'].append({
                    'id': item.id,
                    'date': entry.service_date.strftime('%Y-%m-%d') if entry and entry.service_date else entry.created_at.strftime('%Y-%m-%d'),
                    'company_name': company.name if company else 'N/A',
                    'description': item.service_description[:80] if item.service_description else '',
                    'loit_cb': loit_cb,
                    'loit_sb': loit_sb,
                    'loit_cb': item_loit_cb,
                    'loit_sb': item_loit_sb,
                    'total': amount,
                    'status': status
                })
        
        # ========== OTHER ROLES (Admin, Payment, etc.) ==========
        else:
            query = ServiceItem.query.join(TimeEntry)
            if date_from:
                query = query.filter(TimeEntry.service_date >= date_from)
            if date_to:
                query = query.filter(TimeEntry.service_date <= date_to)
            
            service_items = query.all()
            
            for item in service_items:
                entry = TimeEntry.query.get(item.time_entry_id)
                company = Company.query.get(item.company_id)

                item_loit_cb, item_loit_sb, item_lolc_sb = calculate_service_item_breakdown(item, rt_by_id)
                amount = item_loit_cb + item_loit_sb + item_lolc_sb
                
                loit_cb = 0
                loit_sb = 0
                for resource in item.resources or []:
                    resource_type_id = str(resource.get('type', ''))
                    rt = rt_by_id.get(resource_type_id)
                    if rt:
                        quantity = float(resource.get('quantity') or resource.get('hours') or 0)
                        loit_cb += quantity * float(rt.loit_chargeback or 0)
                        loit_sb += quantity * float(rt.loit_showback or 0)
                
                loit_cb = loit_cb if loit_cb > 0 else float(item.chargeback_total or 0)
                loit_sb = loit_sb if loit_sb > 0 else float(item.showback_total or 0)
                amount = loit_cb + loit_sb
                
                user_data['total_services'] += 1
                user_data['total_amount'] += amount
                
                user_data['records'].append({
                    'id': item.id,
                    'date': entry.service_date.strftime('%Y-%m-%d') if entry and entry.service_date else entry.created_at.strftime('%Y-%m-%d'),
                    'company_name': company.name if company else 'N/A',
                    'description': item.service_description[:80] if item.service_description else '',
                    'loit_cb': loit_cb,
                    'loit_sb': loit_sb,
                    'loit_cb': item_loit_cb,
                    'loit_sb': item_loit_sb,
                    'total': amount,
                    'status': 'Completed'
                })
        
        # Sort records by date (newest first)
        user_data['records'].sort(key=lambda x: x['date'], reverse=True)
        
        return jsonify({
            'success': True,
            'user': {
                'id': user.id,
                'full_name': user.full_name,
                'email': user.email,
                'employee_id': user.employee_id,
                'role': user.role,
                'unit_name': unit_name,
                'company_name': company_name
            },
            'user_data': user_data
        })
        
    except Exception as e:
        print(f"Error in user search: {str(e)}")
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500

# ============================================================
# HELPER FUNCTION: Calculate Service Item Breakdown
# ============================================================

def calculate_service_item_breakdown(item, rt_by_id):
    """
    Calculate LOIT Chargeback, LOIT Showback, and LOLC Showback from a service item's resources.
    
    Args:
        item: ServiceItem object
        rt_by_id: Dictionary mapping resource type IDs to ResourceType objects
    
    Returns:
        tuple: (loit_chargeback, loit_showback, lolc_showback)
    """
    loit_cb = 0.0
    loit_sb = 0.0
    lolc_sb = 0.0
    
    for resource in item.resources or []:
        resource_type_id = str(resource.get('type', ''))
        rt = rt_by_id.get(resource_type_id)
        
        if rt:
            quantity = float(resource.get('quantity') or resource.get('hours') or 0)
            loit_cb += quantity * float(rt.loit_chargeback or 0)
            loit_sb += quantity * float(rt.loit_showback or 0)
            lolc_sb += quantity * float(rt.showback_rate or 0)
    
    return loit_cb, loit_sb, lolc_sb


@api_bp.route('/invoices/<invoice_number>/view', methods=['GET'])
@login_required
def view_invoice_pdf(invoice_number):
    """View invoice data as JSON (for modal display)"""
    try:
        print(f"Viewing invoice: {invoice_number}")
        
        # ONLY Super Admin has access
        if current_user.role != 'super_admin':
            return jsonify({'success': False, 'error': 'Unauthorized'}), 403
        
        # Check ServiceItem table first
        services = ServiceItem.query.filter_by(invoice_number=invoice_number).all()
        
        if services:
            company = Company.query.get(services[0].company_id)
            
            all_resource_types = ResourceType.query.all()
            resource_by_code = {rt.code: rt for rt in all_resource_types}
            resource_by_id = {str(rt.id): rt for rt in all_resource_types}
            
            services_data = []
            total_amount = 0
            
            for service in services:
                entry = TimeEntry.query.get(service.time_entry_id)
                bu_user = User.query.get(entry.user_id) if entry else None
                
                unit_name = 'N/A'
                if bu_user and bu_user.unit_id:
                    unit = Unit.query.get(bu_user.unit_id)
                    if unit:
                        unit_name = unit.name
                
                processed_resources = []
                for resource in service.resources or []:
                    resource_type_value = str(resource.get('type', ''))
                    rt = resource_by_code.get(resource_type_value)
                    if not rt:
                        rt = resource_by_id.get(resource_type_value)
                    
                    if rt:
                        resource_name = rt.name
                        uom = rt.unit_of_measure or 'units'
                        chargeback_rate = float(rt.chargeback_rate) if rt.chargeback_rate else 0
                        showback_rate = float(rt.showback_rate) if rt.showback_rate else 0
                    else:
                        resource_name = resource_type_value.replace('_', ' ').title()
                        uom = 'units'
                        chargeback_rate = resource.get('rate', 0)
                        showback_rate = resource.get('rate', 0)
                    
                    processed_resources.append({
                        'name': resource_name,
                        'type': resource_type_value,
                        'hours': resource.get('hours', 0),
                        'uom': uom,
                        'rate': resource.get('rate', 0),
                        'chargeback_rate': chargeback_rate,
                        'showback_rate': showback_rate,
                        'flat_fee': resource.get('flat_fee', 0)
                    })
                
                services_data.append({
                    'submission_date': entry.submission_date.strftime('%Y-%m-%d %H:%M:%S') if entry and entry.submission_date else entry.created_at.strftime('%Y-%m-%d %H:%M:%S'),
                    'bu_user': bu_user.full_name if bu_user else 'Unknown',
                    'employee_id': bu_user.employee_id if bu_user else 'N/A',
                    'team_name': unit_name,
                    'description': service.service_description,
                    'resources': processed_resources,
                    'amount': float(service.total_amount)
                })
                total_amount += float(service.total_amount)
            
            invoice_data = {
                'invoice_number': invoice_number,
                'date': services[0].invoice_generated_at.strftime('%Y-%m-%d') if services[0].invoice_generated_at else datetime.now().strftime('%Y-%m-%d'),
                'company_name': company.name if company else 'Unknown',
                'company_id': services[0].company_id,
                'services': services_data,
                'total_amount': total_amount
            }
            
            return jsonify({'success': True, 'invoice': invoice_data})
        
        return jsonify({'success': False, 'error': 'Invoice not found'}), 404
        
    except Exception as e:
        print("Error in view_invoice_pdf:", str(e))
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500


@api_bp.route('/invoices/<invoice_number>/download', methods=['GET'])
@login_required
def download_invoice_pdf(invoice_number):
    """Download invoice PDF from database"""
    try:
        print(f"Downloading invoice: {invoice_number}")
        
        # ONLY Super Admin has access
        if current_user.role != 'super_admin':
            return jsonify({'success': False, 'error': 'Unauthorized'}), 403
        
        # 1. Check TimeEntry table
        entry = TimeEntry.query.filter_by(invoice_number=invoice_number).first()
        if entry and entry.invoice_pdf:
            return send_file(
                io.BytesIO(entry.invoice_pdf),
                mimetype='application/pdf',
                as_attachment=True,
                download_name=entry.invoice_pdf_filename or f"{invoice_number}.pdf"
            )
        
        # 2. Check ServiceItem table
        service_item = ServiceItem.query.filter_by(invoice_number=invoice_number).first()
        if service_item and service_item.invoice_pdf:
            return send_file(
                io.BytesIO(service_item.invoice_pdf),
                mimetype='application/pdf',
                as_attachment=True,
                download_name=f"{invoice_number}.pdf"
            )
        
        return jsonify({'success': False, 'error': 'Invoice not found'}), 404
        
    except Exception as e:
        print("Error in download_invoice_pdf:", str(e))
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500
    


# app/routes/api.py

import pytz
from datetime import datetime
import traceback

# Add this endpoint if not already present
@api_bp.route('/date-range', methods=['GET'])
@login_required
def get_date_range():
    """Get the allowed date range for the current user based on database settings"""
    try:
        from app.utils.cycle_helper import CycleHelper, get_sri_lanka_today
        
        # Get Sri Lanka time
        tz = pytz.timezone('Asia/Colombo')
        sri_lanka_now = datetime.now(tz).date()
        
        # Check if user is BU user
        if current_user.role == 'bu_user':
            date_range = CycleHelper.get_allowed_date_range_for_bu()
            
            return jsonify({
                'success': True,
                'data': {
                    'min_date': date_range['min_date'].strftime('%Y-%m-%d'),
                    'max_date': date_range['max_date'].strftime('%Y-%m-%d'),
                    'default_date': date_range['default_date'].strftime('%Y-%m-%d'),
                    'cycle_start': date_range['cycle_start'].strftime('%Y-%m-%d'),
                    'cycle_end': date_range['cycle_end'].strftime('%Y-%m-%d'),
                    'current_week_start': date_range['current_week_start'].strftime('%Y-%m-%d'),
                    'current_week_end': date_range['current_week_end'].strftime('%Y-%m-%d'),
                    'previous_week_start': date_range['previous_week_start'].strftime('%Y-%m-%d'),
                    'previous_week_end': date_range['previous_week_end'].strftime('%Y-%m-%d'),
                    'range_start': date_range['range_start'].strftime('%Y-%m-%d'),
                    'selectable_days': date_range['selectable_days'],
                    'server_time': sri_lanka_now.strftime('%Y-%m-%d'),
                    'auto_submit_day': date_range['auto_submit_day'],
                    'max_past_days': date_range['max_past_days'],
                    'allow_future_dates': date_range['allow_future_dates'],
                    'is_cycle_start_day': date_range['is_cycle_start_day']
                }
            })
        else:
            # For other roles, allow wider range
            cycle = CycleHelper.get_current_cycle()
            today = get_sri_lanka_today()
            
            return jsonify({
                'success': True,
                'data': {
                    'min_date': cycle['cycle_start'].strftime('%Y-%m-%d'),
                    'max_date': today.strftime('%Y-%m-%d'),
                    'default_date': today.strftime('%Y-%m-%d'),
                    'cycle_start': cycle['cycle_start'].strftime('%Y-%m-%d'),
                    'cycle_end': cycle['cycle_end'].strftime('%Y-%m-%d'),
                    'server_time': today.strftime('%Y-%m-%d'),
                    'auto_submit_day': CycleHelper.get_auto_submit_day(),
                    'max_past_days': CycleHelper.get_max_past_days(),
                    'allow_future_dates': CycleHelper.get_allow_future_dates()
                }
            })
        
    except Exception as e:
        print("Error in get_date_range:", str(e))
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500


@api_bp.route('/admin/settings/auto-submit', methods=['POST'])
@login_required
def update_auto_submit_settings():
    """Update auto submit settings (admin only)"""
    if current_user.role not in ['admin', 'super_admin']:
        return jsonify({'success': False, 'error': 'Unauthorized'}), 403
    
    try:
        from app.models import SystemSetting
        from app.utils.cycle_helper import get_sri_lanka_now
        
        data = request.get_json()
        sri_lanka_now = get_sri_lanka_now()
        
        # Update auto_submit_day
        if 'auto_submit_day' in data:
            setting = SystemSetting.query.filter_by(setting_key='auto_submit_day').first()
            if not setting:
                setting = SystemSetting(setting_key='auto_submit_day', setting_value=str(data['auto_submit_day']))
                db.session.add(setting)
            else:
                setting.setting_value = str(data['auto_submit_day'])
            setting.updated_at = sri_lanka_now
            setting.updated_by = current_user.id
        
        # Update auto_submit_time
        if 'auto_submit_time' in data:
            setting = SystemSetting.query.filter_by(setting_key='auto_submit_time').first()
            if not setting:
                setting = SystemSetting(setting_key='auto_submit_time', setting_value=data['auto_submit_time'])
                db.session.add(setting)
            else:
                setting.setting_value = data['auto_submit_time']
            setting.updated_at = sri_lanka_now
            setting.updated_by = current_user.id
        
        # Update max_past_days
        if 'max_past_days' in data:
            setting = SystemSetting.query.filter_by(setting_key='max_past_days').first()
            if not setting:
                setting = SystemSetting(setting_key='max_past_days', setting_value=str(data['max_past_days']))
                db.session.add(setting)
            else:
                setting.setting_value = str(data['max_past_days'])
            setting.updated_at = sri_lanka_now
            setting.updated_by = current_user.id
        
        # Update allow_future_dates
        if 'allow_future_dates' in data:
            setting = SystemSetting.query.filter_by(setting_key='allow_future_dates').first()
            if not setting:
                setting = SystemSetting(setting_key='allow_future_dates', setting_value=str(data['allow_future_dates']).lower())
                db.session.add(setting)
            else:
                setting.setting_value = str(data['allow_future_dates']).lower()
            setting.updated_at = sri_lanka_now
            setting.updated_by = current_user.id
        
        db.session.commit()
        
        return jsonify({
            'success': True,
            'message': 'Auto submit settings updated successfully'
        })
        
    except Exception as e:
        db.session.rollback()
        print("Error in update_auto_submit_settings:", str(e))
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500


@api_bp.route('/admin/settings/auto-submit', methods=['GET'])
@login_required
def get_auto_submit_settings():
    """Get current auto submit settings (admin only)"""
    if current_user.role not in ['admin', 'super_admin']:
        return jsonify({'success': False, 'error': 'Unauthorized'}), 403
    
    try:
        from app.utils.cycle_helper import CycleHelper
        
        auto_submit_day = CycleHelper.get_auto_submit_day()
        auto_submit_time = CycleHelper.get_auto_submit_time()
        max_past_days = CycleHelper.get_max_past_days()
        allow_future_dates = CycleHelper.get_allow_future_dates()
        
        return jsonify({
            'success': True,
            'data': {
                'auto_submit_day': auto_submit_day,
                'auto_submit_time': auto_submit_time,
                'max_past_days': max_past_days,
                'allow_future_dates': allow_future_dates
            }
        })
        
    except Exception as e:
        print("Error in get_auto_submit_settings:", str(e))
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500