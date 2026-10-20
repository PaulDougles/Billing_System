# app/utils/week_helper.py
from datetime import datetime, date, timedelta
from calendar import monthrange


def get_setting(key, default=None):
    """Get system setting value"""
    from app.models import SystemSetting
    setting = SystemSetting.query.filter_by(setting_key=key).first()
    return setting.setting_value if setting else default


def get_current_week_info():
    """Get current week information based on 4-week cycle"""
    from app.utils.cycle_helper import CycleHelper  # Import here to avoid circular import
    
    cycle = CycleHelper.get_current_cycle()
    today = datetime.now().date()
    
    current_week = 1
    week_start = cycle['cycle_start']
    week_end = cycle['cycle_end']
    
    for week in cycle['weeks']:
        if week['start_date'] <= today <= week['end_date']:
            current_week = week['week_number']
            week_start = week['start_date']
            week_end = week['end_date']
            break
    
    return {
        'week_number': current_week,
        'year': today.year,
        'week_start': week_start,
        'week_end': week_end,
        'is_current_week': True,
        'cycle_start': cycle['cycle_start'],
        'cycle_end': cycle['cycle_end']
    }

