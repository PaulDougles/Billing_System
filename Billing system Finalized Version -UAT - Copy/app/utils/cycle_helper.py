# app/utils/cycle_helper.py

from datetime import datetime, date, timedelta
from calendar import monthrange
import calendar
import pytz


def get_setting(key, default=None):
    """Get system setting value - defined here to avoid circular import"""
    from app.models import SystemSetting
    setting = SystemSetting.query.filter_by(setting_key=key).first()
    return setting.setting_value if setting else default


def get_sri_lanka_today():
    """Get today's date in Sri Lanka timezone (UTC+5:30)"""
    tz = pytz.timezone('Asia/Colombo')
    return datetime.now(tz).date()


def get_sri_lanka_now():
    """Get current datetime in Sri Lanka timezone (UTC+5:30)"""
    tz = pytz.timezone('Asia/Colombo')
    return datetime.now(tz)


class CycleHelper:
    """Handles 4-week cycle logic with configurable auto-submit day"""
    
    @staticmethod
    def get_auto_submit_day():
        """Get auto-submit day from settings (default 26)"""
        try:
            day = int(get_setting('auto_submit_day', '26'))
            return min(max(day, 1), 28)  # Ensure between 1-28
        except:
            return 26
    
    @staticmethod
    def get_auto_submit_date():
        """Get the next auto-submit date based on configured day"""
        auto_submit_day = CycleHelper.get_auto_submit_day()
        today = get_sri_lanka_today()
        
        # Calculate the auto-submit date for the current period.
        # If today is before the auto_submit_day, it's in the current month.
        # If today is the auto_submit_day, it's today.
        # If today is after the auto_submit_day, it's in the next month.
        if today.day < auto_submit_day:
            return date(today.year, today.month, auto_submit_day)
        elif today.day == auto_submit_day:
            return today
        else: # today.day > auto_submit_day
            if today.month == 12:
                return date(today.year + 1, 1, auto_submit_day)
            else:
                return date(today.year, today.month + 1, auto_submit_day)
    
    @staticmethod
    def get_max_past_days():
        """Get max past days from settings (default 14)"""
        try:
            days = int(get_setting('max_past_days', '14'))
            return min(max(days, 1), 30)  # Ensure between 1-30
        except:
            return 14
    
    @staticmethod
    def get_allow_future_dates():
        """Get allow future dates setting (default False)"""
        setting = get_setting('allow_future_dates', 'false')
        return setting.lower() == 'true'
    
    @staticmethod
    def get_auto_submit_time():
        """Get auto-submit time from settings (default '17:00')"""
        setting = get_setting('auto_submit_time', '17:00')
        return setting
    
    @staticmethod
    def get_current_cycle(target_date=None):
        """
        Get the current cycle based on date.
        
        Cycle runs from auto_submit_day of current month to (auto_submit_day-1) of next month.
        
        Example: auto_submit_day = 26
        - Cycle 1: May 26 to June 25
        - Cycle 2: June 26 to July 25
        - Cycle 3: July 26 to August 25
        
        This works for ANY auto_submit_day value (1-28)
        """
        if target_date is None:
            target_date = get_sri_lanka_today()
        
        auto_submit_day = CycleHelper.get_auto_submit_day()
        
        # Determine cycle
        if target_date.day >= auto_submit_day:
            # On or after auto_submit_day: cycle started this month on auto_submit_day
            cycle_start_date = date(target_date.year, target_date.month, auto_submit_day)
            
            # Cycle ends on (auto_submit_day - 1) of NEXT month
            if target_date.month == 12:
                cycle_end_date = date(target_date.year + 1, 1, auto_submit_day - 1)
            else:
                cycle_end_date = date(target_date.year, target_date.month + 1, auto_submit_day - 1)
        else:
            # Before auto_submit_day: cycle started last month on auto_submit_day
            if target_date.month == 1:
                cycle_start_date = date(target_date.year - 1, 12, auto_submit_day)
            else:
                cycle_start_date = date(target_date.year, target_date.month - 1, auto_submit_day)
            
            # Cycle ends on (auto_submit_day - 1) of current month
            cycle_end_date = date(target_date.year, target_date.month, auto_submit_day - 1)
        
        cycle_name = f"{cycle_start_date.strftime('%b %d')} - {cycle_end_date.strftime('%b %d')}"
        
        # Generate EXACTLY 4 weeks
        weeks = CycleHelper.get_cycle_weeks_fixed(cycle_start_date, cycle_end_date)
        
        return {
            'cycle_start': cycle_start_date,
            'cycle_end': cycle_end_date,
            'cycle_name': cycle_name,
            'weeks': weeks,
            'is_active': target_date <= cycle_end_date
        }
    
    @staticmethod
    def get_cycle_weeks_fixed(cycle_start, cycle_end):
        """
        Split cycle into EXACTLY 4 fixed weeks.
        """
        weeks = []
        cycle_length = (cycle_end - cycle_start).days + 1
        week_size = cycle_length // 4
        remainder = cycle_length % 4
        
        week_boundaries = []
        current_day = 1
        
        for i in range(4):
            extra = 1 if i < remainder else 0
            week_end = current_day + week_size + extra - 1
            week_boundaries.append((current_day, week_end))
            current_day = week_end + 1
        
        for week_num, (start_day, end_day) in enumerate(week_boundaries, 1):
            start_offset = start_day - 1
            end_offset = min(end_day - 1, cycle_length - 1)
            
            week_start = cycle_start + timedelta(days=start_offset)
            week_end = cycle_start + timedelta(days=end_offset)
            
            weeks.append({
                'week_number': week_num,
                'start_date': week_start,
                'end_date': week_end,
                'start_day': start_day,
                'end_day': end_day,
                'date_range': f"{week_start.strftime('%b %d')} - {week_end.strftime('%b %d')}"
            })
        
        return weeks
    
    @staticmethod
    def get_week_number_for_date(target_date):
        """
        Get the correct week number (1-4) for a given date based on the 4-week cycle.
        """
        cycle = CycleHelper.get_current_cycle(target_date)
        day_number = (target_date - cycle['cycle_start']).days + 1
        
        if day_number <= 7:
            return 1
        elif day_number <= 14:
            return 2
        elif day_number <= 21:
            return 3
        else:
            return 4
    
    @staticmethod
    def get_week_for_date(target_date):
        """
        Determine which week (1-4) a date falls into within the cycle.
        """
        cycle = CycleHelper.get_current_cycle(target_date)
        day_number = (target_date - cycle['cycle_start']).days + 1
        
        if day_number <= 7:
            week_number = 1
        elif day_number <= 14:
            week_number = 2
        elif day_number <= 21:
            week_number = 3
        else:
            week_number = 4
        
        for week in cycle['weeks']:
            if week['week_number'] == week_number:
                return {
                    'week_number': week_number,
                    'week_start': week['start_date'],
                    'week_end': week['end_date']
                }
        
        return {'week_number': 4, 'week_start': None, 'week_end': None}
    
    @staticmethod
    def get_cycle_weeks_summary(cycle=None):
        """Get formatted weeks for display - ALWAYS 4 weeks"""
        if cycle is None:
            cycle = CycleHelper.get_current_cycle()
        
        weeks = []
        for week in cycle['weeks'][:4]:
            weeks.append({
                'week_number': week['week_number'],
                'start_date': week['start_date'].strftime('%b %d'),
                'end_date': week['end_date'].strftime('%b %d'),
                'full_start': week['start_date'],
                'full_end': week['end_date'],
                'date_range': f"{week['start_date'].strftime('%b %d')} - {week['end_date'].strftime('%b %d')}"
            })
        
        return weeks
    
    @staticmethod
    def is_in_cycle(target_date):
        """Check if date is within current cycle"""
        cycle = CycleHelper.get_current_cycle()
        return cycle['cycle_start'] <= target_date <= cycle['cycle_end']
    
    @staticmethod
    def get_next_cycle_start():
        """Get start date of next cycle"""
        auto_submit_day = CycleHelper.get_auto_submit_day()
        today = get_sri_lanka_today()
        if today.day >= auto_submit_day:
            if today.month == 12:
                return date(today.year + 1, 1, auto_submit_day)
            else:
                return date(today.year, today.month + 1, auto_submit_day)
        else:
            return date(today.year, today.month, auto_submit_day)
    
    @staticmethod
    def format_cycle_for_display():
        """Get formatted cycle info for UI display"""
        cycle = CycleHelper.get_current_cycle()
        weeks = CycleHelper.get_cycle_weeks_summary(cycle)
        auto_submit_day = CycleHelper.get_auto_submit_day()
        today = get_sri_lanka_today()
        
        return {
            'cycle_period': f"{cycle['cycle_start'].strftime('%B %d')} - {cycle['cycle_end'].strftime('%B %d')}",
            'cycle_start': cycle['cycle_start'].strftime('%Y-%m-%d'),
            'cycle_end': cycle['cycle_end'].strftime('%Y-%m-%d'),
            'weeks': weeks,
            'current_week': CycleHelper.get_week_for_date(today)['week_number'],
            'days_until_submit': (cycle['cycle_end'] - today).days,
            'auto_submit_day': auto_submit_day
        }
    
    @staticmethod
    def get_allowed_date_range_for_bu():
        """
        Get allowed date range for BU users.
        
        Rules:
        1. ALWAYS show today + past (max_past_days - 1) days (total max_past_days days)
        2. Date range must be within the current cycle
        3. NEVER show future dates
        4. TODAY must ALWAYS be selectable
        
        Cycle: auto_submit_day to (auto_submit_day - 1) of next month
        This works for ANY auto_submit_day value from database
        """
        today = get_sri_lanka_today()
        auto_submit_day = CycleHelper.get_auto_submit_day()
        max_past_days = CycleHelper.get_max_past_days()  # Default 14
        allow_future = CycleHelper.get_allow_future_dates()
        
        # Get cycle based on auto_submit_day
        cycle = CycleHelper.get_current_cycle(today)
        cycle_start = cycle['cycle_start']  # This will be auto_submit_day
        cycle_end = cycle['cycle_end']      # This will be (auto_submit_day - 1) of next month
        
        # ========== Calculate max_past_days including today ==========
        # Today - (max_past_days - 1) days
        range_start = today - timedelta(days=max_past_days - 1)
        
        # ========== Apply cycle boundary ==========
        # Minimum date = max(cycle_start, range_start)
        min_allowed = max(cycle_start, range_start)
        
        # ========== IMPORTANT: Maximum date MUST be TODAY ==========
        # This ensures today is always selectable
        max_allowed = today
        
        # Ensure min doesn't exceed max
        if min_allowed > max_allowed:
            min_allowed = max_allowed
        
        # ========== Default date = TODAY ==========
        # Today should always be the default selection
        default_date = today
        
        # Calculate how many days are selectable in this cycle
        selectable_days = (max_allowed - min_allowed).days + 1 if min_allowed <= max_allowed else 0
        
        # Get week info for display
        current_week = CycleHelper.get_week_for_date(today)
        current_week_start = current_week['week_start']
        current_week_end = current_week['week_end']
        
        previous_week_start = current_week_start - timedelta(days=7)
        previous_week_end = current_week_end - timedelta(days=7)
        
        return {
            'min_date': min_allowed,
            'max_date': max_allowed,
            'default_date': default_date,
            'cycle_start': cycle_start,
            'cycle_end': cycle_end,
            'current_week_start': current_week_start,
            'current_week_end': current_week_end,
            'previous_week_start': previous_week_start,
            'previous_week_end': previous_week_end,
            'range_start': range_start,
            'selectable_days': selectable_days,
            'max_past_days': max_past_days,
            'auto_submit_day': auto_submit_day,
            'allow_future_dates': allow_future,
            'can_select': min_allowed <= max_allowed,
            'is_cycle_start_day': today.day == auto_submit_day,
            'is_cycle_end_day': today.day == (auto_submit_day - 1) if auto_submit_day > 1 else False,
            'range_type': 'past_days_within_cycle'
        }
    
    @staticmethod
    def validate_bu_date(selected_date):
        """
        Validate if a date is allowed for BU user selection.
        
        Rules:
        1. No future dates
        2. Within last max_past_days days (including today)
        3. Within current cycle (auto_submit_day to auto_submit_day-1 of next month)
        """
        today = get_sri_lanka_today()
        max_past_days = CycleHelper.get_max_past_days()
        auto_submit_day = CycleHelper.get_auto_submit_day()
        allow_future = CycleHelper.get_allow_future_dates()
        
        # Get cycle boundaries
        cycle = CycleHelper.get_current_cycle(today)
        cycle_start = cycle['cycle_start']
        cycle_end = cycle['cycle_end']
        
        # ========== Check 1: Future dates ==========
        if selected_date > today:
            if allow_future:
                # Future dates allowed, but must be within cycle
                if selected_date > cycle_end:
                    return False, f"Future dates must be within the current cycle (ends {cycle_end.strftime('%Y-%m-%d')})."
                return True, None
            else:
                return False, f"Future dates are not allowed. Please select today or a past date."
        
        # ========== Check 2: Must be within max_past_days days including today ==========
        range_start = today - timedelta(days=max_past_days - 1)
        if selected_date < range_start:
            return False, f"You can only select dates from the last {max_past_days} days (including today). The earliest allowed date is {range_start.strftime('%Y-%m-%d')}."
        
        # ========== Check 3: Must be within cycle ==========
        if selected_date < cycle_start:
            return False, f"This date is before the current cycle start ({cycle_start.strftime('%Y-%m-%d')}). The cycle started on {cycle_start.strftime('%Y-%m-%d')}."
        
        if selected_date > cycle_end:
            return False, f"This date is after the current cycle end ({cycle_end.strftime('%Y-%m-%d')})."
        
        return True, None
    
    @staticmethod
    def get_week_dates_for_display():
        """
        Get formatted week dates for display in UI
        """
        today = get_sri_lanka_today()
        
        current_week = CycleHelper.get_week_for_date(today)
        current_week_start = current_week['week_start']
        current_week_end = current_week['week_end']
        
        previous_week_start = current_week_start - timedelta(days=7)
        previous_week_end = current_week_end - timedelta(days=7)
        
        return {
            'current_week': {
                'start': current_week_start.strftime('%Y-%m-%d'),
                'end': current_week_end.strftime('%Y-%m-%d'),
                'display': f"{current_week_start.strftime('%b %d')} - {current_week_end.strftime('%b %d')}"
            },
            'previous_week': {
                'start': previous_week_start.strftime('%Y-%m-%d'),
                'end': previous_week_end.strftime('%Y-%m-%d'),
                'display': f"{previous_week_start.strftime('%b %d')} - {previous_week_end.strftime('%b %d')}"
            }
        }