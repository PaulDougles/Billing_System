# app/utils/ai_predictor.py
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from statsmodels.tsa.holtwinters import ExponentialSmoothing
from statsmodels.tsa.arima.model import ARIMA
import warnings
warnings.filterwarnings('ignore')

class RevenuePredictor:
    """Revenue forecasting using statistical methods"""
    
    @staticmethod
    def get_historical_data(months=60, company_id=None):
        """Fetch historical revenue data from database"""
        from app.models import ServiceItem
        from app.extensions import db
        from sqlalchemy import func
        
        dialect = db.engine.name
        if dialect == 'postgresql':
            month_col = func.to_char(ServiceItem.finance_approved_at, 'YYYY-MM').label('month')
        else:
            month_col = func.strftime('%Y-%m', ServiceItem.finance_approved_at).label('month')

        query = db.session.query(
            month_col,
            func.sum(ServiceItem.total_amount).label('revenue')
        ).filter(
            ServiceItem.finance_approval_status == 'approved',
            ServiceItem.finance_approved_at >= datetime.now() - timedelta(days=months*30)
        )

        if company_id is not None:
            query = query.filter(ServiceItem.company_id == company_id)
        
        data = query.group_by('month').order_by('month').all()
        
        return [{'month': d.month, 'revenue': float(d.revenue)} for d in data]
    
    @staticmethod
    def exponential_smoothing_forecast(historical_data, periods=6):
        """Forecast using Exponential Smoothing (Holt-Winters)"""
        if len(historical_data) < 12:
            return None
        
        df = pd.DataFrame(historical_data)
        df['month'] = pd.to_datetime(df['month'])
        df = df.set_index('month')
        df = df.asfreq('MS')
        df['revenue'] = df['revenue'].fillna(method='ffill')
        
        try:
            # Try with seasonal component if enough data
            if len(df) >= 24:
                model = ExponentialSmoothing(
                    df['revenue'],
                    seasonal_periods=12,
                    trend='add',
                    seasonal='add',
                    initialization_method='estimated'
                )
            else:
                model = ExponentialSmoothing(
                    df['revenue'],
                    trend='add',
                    initialization_method='estimated'
                )
            
            fit = model.fit()
            forecast = fit.forecast(periods)
            
            return [max(0, round(val, 2)) for val in forecast]
        except Exception as e:
            print(f"Exponential smoothing error: {e}")
            return None
    
    @staticmethod
    def arima_forecast(historical_data, periods=6):
        """Forecast using ARIMA model"""
        if len(historical_data) < 10:
            return None
        
        df = pd.DataFrame(historical_data)
        df['month'] = pd.to_datetime(df['month'])
        df = df.set_index('month')
        df = df.asfreq('MS')
        df['revenue'] = df['revenue'].fillna(method='ffill')
        
        try:
            # Auto-select ARIMA parameters
            model = ARIMA(df['revenue'], order=(1, 1, 1))
            fit = model.fit()
            forecast = fit.forecast(steps=periods)
            
            return [max(0, round(val, 2)) for val in forecast]
        except Exception as e:
            print(f"ARIMA error: {e}")
            return None
    
    @staticmethod
    def simple_moving_average(historical_data, periods=6, window=3):
        """Simple moving average forecast"""
        if len(historical_data) < window:
            return None
        
        revenues = [d['revenue'] for d in historical_data[-window*2:]]
        last_avg = np.mean(revenues[-window:])
        
        # Calculate trend
        if len(revenues) >= window*2:
            prev_avg = np.mean(revenues[-window*2:-window])
            trend = last_avg - prev_avg
        else:
            trend = 0
        
        forecast = []
        for i in range(periods):
            next_val = last_avg + trend * (i + 1)
            forecast.append(max(0, round(next_val, 2)))
        
        return forecast
    
    @staticmethod
    def combine_forecasts(historical_data, periods=6):
        """Combine multiple methods for better accuracy"""
        forecasts = []
        
        # Get predictions from different methods
        es_forecast = RevenuePredictor.exponential_smoothing_forecast(historical_data, periods)
        arima_forecast = RevenuePredictor.arima_forecast(historical_data, periods)
        sma_forecast = RevenuePredictor.simple_moving_average(historical_data, periods)
        
        if es_forecast:
            forecasts.append(es_forecast)
        if arima_forecast:
            forecasts.append(arima_forecast)
        if sma_forecast:
            forecasts.append(sma_forecast)
        
        if not forecasts:
            return None
        
        # Average the forecasts
        combined = []
        for i in range(periods):
            values = [f[i] for f in forecasts if i < len(f)]
            combined.append(round(np.mean(values), 2))
        
        return combined
    
    @staticmethod
    def calculate_growth(historical_data, forecast):
        """Calculate growth percentage between last year and next year"""
        if len(historical_data) < 12 or not forecast or len(forecast) < 12:
            return 0
        
        last_year_avg = np.mean([d['revenue'] for d in historical_data[-12:]])
        next_year_avg = np.mean(forecast[:12]) if len(forecast) >= 12 else np.mean(forecast)
        
        if last_year_avg == 0:
            return 0
        
        growth = ((next_year_avg - last_year_avg) / last_year_avg) * 100
        return round(growth, 2)
    
    @staticmethod
    def get_confidence_level(historical_data):
        """Calculate confidence level based on data consistency"""
        if len(historical_data) < 12:
            return 65
        
        revenues = [d['revenue'] for d in historical_data]
        cv = np.std(revenues) / np.mean(revenues) if np.mean(revenues) > 0 else 1
        
        # Lower CV = higher confidence
        if cv < 0.2:
            return 90
        elif cv < 0.4:
            return 80
        elif cv < 0.6:
            return 70
        else:
            return 60


# Standalone functions for easy import
def get_revenue_forecast(months=6, years_history=5, company_id=None):
    """Main function to get revenue forecast"""
    historical = RevenuePredictor.get_historical_data(months=years_history*12, company_id=company_id)
    
    if not historical:
        return None
    
    if len(historical) < 3:
        last_revenue = historical[-1]['revenue']
        forecast = [round(last_revenue, 2)] * months
        growth = 0
        confidence = 50 if len(historical) == 1 else 55
    else:
        forecast = RevenuePredictor.combine_forecasts(historical, months)
        if not forecast:
            last_revenue = historical[-1]['revenue']
            forecast = [round(last_revenue, 2)] * months
        growth = RevenuePredictor.calculate_growth(historical, forecast)
        confidence = RevenuePredictor.get_confidence_level(historical)
    
    return {
        'historical': historical,
        'forecast': forecast,
        'growth_percent': growth,
        'confidence_level': confidence,
        'next_month_prediction': forecast[0] if forecast else 0,
        'next_3_months_total': sum(forecast[:3]) if forecast and len(forecast) >= 3 else 0,
        'next_6_months_total': sum(forecast[:6]) if forecast and len(forecast) >= 6 else 0
    }