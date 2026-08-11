"""Shared extension instances. Imported by __init__.py and models to avoid circular imports."""
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager
from apscheduler.schedulers.background import BackgroundScheduler

db = SQLAlchemy()
login_manager = LoginManager()
scheduler = BackgroundScheduler(daemon=True)
