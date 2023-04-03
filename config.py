import os
from dotenv import load_dotenv


basedir = os.path.abspath(os.path.dirname(__file__))
load_dotenv(os.path.join(basedir, '.env'))


class Config:
    SCHEDULER_API_ENABLED = True
    SECRET_KEY = os.environ.get('SECRET_KEY')
    SECRET_KEY_SALT = os.environ.get('SECRET_KEY_SALT').encode()
    FLASK_ADMIN_FLUID_LAYOUT = True
    SQLALCHEMY_DATABASE_URI = f"sqlite:///{os.path.join(basedir, 'instance', 'ipmi.db')}"
    SCHEDULER_JOBS = []  # APScheduler Jobs


class DevConfig(Config):
    DEBUG = True
    TESTING = True


class ProdConfig(Config):
    DEBUG = False
    TESTING = False