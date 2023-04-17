#
# Flask Configurations
#

import os
import base64
from dotenv import load_dotenv
import logging


basedir = os.path.abspath(os.path.dirname(__file__))
load_dotenv(os.path.join(basedir, '.env'))


class Config:
    SCHEDULER_API_ENABLED = True
    SECRET_KEY = os.environ.get('SECRET_KEY')
    SECRET_KEY_SALT = base64.b64decode(bytes(os.environ.get('SECRET_KEY_SALT'), "utf-8"))
    FLASK_ADMIN_FLUID_LAYOUT = True
    SQLALCHEMY_DATABASE_URI = f"sqlite:///{os.path.join(basedir, 'instance', 'jbod.db')}"
    SERIAL_DEBUG_FILE = os.path.join(basedir, 'instance', 'serial.log')
    UPLOAD_DIR = os.path.join(basedir, 'instance', 'uploads')
    SCHEDULER_JOBS = []  # APScheduler Jobs


class DevConfig(Config):
    DEBUG = True
    TESTING = True
    SERIAL_DEBUG = True
    LOGGING_LEVEL = logging.DEBUG


class ProdConfig(Config):
    DEBUG = False
    TESTING = False
    SERIAL_DEBUG = False
    LOGGING_LEVEL = logging.INFO