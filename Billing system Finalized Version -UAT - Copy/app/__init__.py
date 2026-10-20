from flask import Flask
from config import config
from app.extensions import db, login_manager, migrate, mail, setup_logging
import os

def create_app(config_name='default'):
    app = Flask(__name__, 
                instance_relative_config=True,
                static_folder='static',
                template_folder='templates')
    
    # Load configuration
    app.config.from_object(config[config_name])
    
    # Ensure instance folder exists
    try:
        os.makedirs(app.instance_path)
    except OSError:
        pass
    
    # Initialize extensions
    db.init_app(app)
    login_manager.init_app(app)
    migrate.init_app(app, db)
    mail.init_app(app)
    
    # Setup logging
    setup_logging(app)
    
    # Configure login
    login_manager.login_view = 'auth.login'
    login_manager.login_message = 'Please log in to access this page.'
    
    # Import and register blueprints - MOVED INSIDE THE FUNCTION
    from app.routes.auth import auth_bp
    from app.routes.admin import admin_bp
    from app.routes.api import api_bp
    from app.routes.bu_team import bu_team_bp
    from app.routes.head_of_it import head_of_it_bp
    from app.routes.finance_controller import finance_controller_bp
    from app.routes.payment import payment_bp
    from app.routes.dashboard import dashboard_bp
    from app.routes.supervisor import supervisor_bp
    from app.routes.record_assistant import record_assistant_bp 
    
    app.register_blueprint(auth_bp)
    app.register_blueprint(admin_bp, url_prefix='/admin')
    app.register_blueprint(api_bp, url_prefix='/api')
    app.register_blueprint(bu_team_bp, url_prefix='/bu-team')
    app.register_blueprint(head_of_it_bp, url_prefix='/head-of-it')
    app.register_blueprint(finance_controller_bp, url_prefix='/finance-controller')
    app.register_blueprint(payment_bp, url_prefix='/payment')
    app.register_blueprint(dashboard_bp)
    app.register_blueprint(supervisor_bp)
    app.register_blueprint(record_assistant_bp, url_prefix='/record-assistant') 
    
    # Template context processor
    @app.context_processor
    def utility_processor():
        from datetime import datetime
        from app.utils.labels import get_company_showback_label, get_company_showback_label_short, get_company_short_name
        return {
            'now': datetime.now,
            'company_short_name': get_company_short_name,
            'company_showback_label': get_company_showback_label,
            'company_showback_label_short': get_company_showback_label_short,
        }
    
    @app.template_filter('company_short_name')
    def company_short_name_filter(name):
        from app.utils.labels import get_company_short_name
        return get_company_short_name(name)
    
    @app.template_filter('company_showback_label')
    def company_showback_label_filter(name):
        from app.utils.labels import get_company_showback_label
        return get_company_showback_label(name)
    
    @app.template_filter('company_showback_label_short')
    def company_showback_label_short_filter(name):
        from app.utils.labels import get_company_showback_label_short
        return get_company_showback_label_short(name)
    
    # Apply ProxyFix for reverse proxy support (Nginx / ALB / Cloudflare)
    from werkzeug.middleware.proxy_fix import ProxyFix
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)
    
    # Create database tables safely
    with app.app_context():
        db.create_all()
        from app.utils.schema_migrations import apply_schema_migrations
        apply_schema_migrations()

        try:
            # Create initial data
            from app.utils.helpers import create_initial_data
            create_initial_data(app)
        except Exception as e:
            app.logger.warning(f"Initial data setup failed: {e}")
    
    return app