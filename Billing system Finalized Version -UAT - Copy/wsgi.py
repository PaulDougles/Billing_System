# wsgi.py - Production & UAT WSGI entry point
import os
from app import create_app

# Load configuration from environment (defaults to 'production')
config_name = os.environ.get('FLASK_CONFIG', 'production')
app = create_app(config_name)

# Start background scheduler once if enabled
if os.environ.get('START_SCHEDULER', 'True').lower() == 'true':
    try:
        from app.utils.scheduler import start_scheduler
        start_scheduler(app)
        print("✅ Background scheduler initialized")
    except Exception as e:
        print(f"⚠️ Scheduler startup note: {e}")

if __name__ == '__main__':
    # When run directly with 'python wsgi.py', serve with Waitress if installed, or fallback to app.run
    host = os.environ.get('HOST', '0.0.0.0')
    port = int(os.environ.get('PORT', 5000))
    debug = os.environ.get('FLASK_DEBUG', 'False').lower() == 'true'

    try:
        from waitress import serve
        print(f"🚀 Serving with Waitress on http://{host}:{port}")
        serve(app, host=host, port=port)
    except ImportError:
        print(f"ℹ️ Waitress not found. Running with Flask dev server on http://{host}:{port}")
        app.run(host=host, port=port, debug=debug)
