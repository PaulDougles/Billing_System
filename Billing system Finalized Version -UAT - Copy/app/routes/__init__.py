from flask import Flask
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager
from flask_migrate import Migrate
from flask_mail import Mail
import os

db = SQLAlchemy()
login_manager = LoginManager()
migrate = Migrate()
mail = Mail()

def setup_logging(app):
    import logging
    logging.basicConfig(level=logging.INFO)

def create_app(config_name='default'):
    app = Flask(__name__, 
                instance_relative_config=True,
                static_folder='static',
                template_folder='templates')
    
    app.config.from_object(config[config_name])
    
    try:
        os.makedirs(app.instance_path)
    except OSError:
        pass
    
    db.init_app(app)
    login_manager.init_app(app)
    migrate.init_app(app, db)
    mail.init_app(app)
    
    setup_logging(app)
    
    login_manager.login_view = 'auth.login'
    login_manager.login_message = 'Please log in to access this page.'
    
    # Import all blueprints
    from app.routes.auth import auth_bp
    from app.routes.dashboard import dashboard_bp
    from app.routes.time_tracking import time_tracking_bp
    
    from app.routes.reports import reports_bp
    from app.routes.admin import admin_bp
    from app.routes.workflow import workflow_bp
    from app.routes.api import api_bp
    from app.routes.record_assistant import record_assistant_bp
    from app.routes.bu_team import bu_team_bp
    from app.routes.head_of_it import head_of_it_bp
    from app.routes.finance_controller import finance_controller_bp
    from app.routes.payment import payment_bp
    from app.routes.supervisor import supervisor_bp
    
    # Register all blueprints
    app.register_blueprint(auth_bp)
    app.register_blueprint(dashboard_bp)
    app.register_blueprint(time_tracking_bp)
    
    app.register_blueprint(reports_bp)
    app.register_blueprint(admin_bp)
    app.register_blueprint(workflow_bp)
    app.register_blueprint(api_bp, url_prefix='/api')
    app.register_blueprint(record_assistant_bp)
    app.register_blueprint(bu_team_bp)
    app.register_blueprint(head_of_it_bp)           # THIS IS THE MISSING ONE!
    app.register_blueprint(finance_controller_bp)
    app.register_blueprint(payment_bp)
    app.register_blueprint(supervisor_bp)
    
    @app.context_processor
    def utility_processor():
        from datetime import datetime
        return {'now': datetime.now}
    
    with app.app_context():
        db.create_all()
        from app.utils.helpers import create_admin_user
        create_admin_user(app)
    
    return app