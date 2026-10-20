# run.py
from app import create_app
import os

# Ensure instance folder exists
os.makedirs('instance', exist_ok=True)

# Use config from environment or default to production
config_name = os.environ.get('FLASK_CONFIG', 'production')
app = create_app(config_name)

if __name__ == '__main__':
    host = os.environ.get('HOST', '0.0.0.0')
    port = int(os.environ.get('PORT', 5000))
    debug = os.environ.get('FLASK_DEBUG', 'False').lower() == 'true'
    
    print("=" * 60)
    print("🚀 Invoice System - Complete Billing System")
    print("=" * 60)
    print(f"🌐 Server: http://localhost:{port} (Accessible on network via http://{host}:{port})")
    print("=" * 60)
    
    # Start the scheduler with app context
    try:
        from app.utils.scheduler import start_scheduler
        start_scheduler(app)
        print("✅ Auto-submit scheduler started")
    except Exception as e:
        print(f"⚠️ Scheduler could not start: {e}")
    
    # Run with use_reloader=False to prevent duplicate scheduler
    app.run(host=host, port=port, debug=debug, use_reloader=False)