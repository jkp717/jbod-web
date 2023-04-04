import logging
import os
import base64
from flask import Flask
from flask.logging import default_handler
from logging.handlers import RotatingFileHandler
from sqlalchemy.exc import IntegrityError
from flask_admin import Admin
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from apscheduler.events import EVENT_JOB_MISSED, EVENT_JOB_ERROR, EVENT_JOB_EXECUTED, EVENT_JOB_ADDED, \
    EVENT_JOB_REMOVED, EVENT_JOB_SUBMITTED

from webapp.config import config_defaults, scheduler_jobs


# run the following to start server
# sudo gunicorn -w 1 -b 0.0.0.0 'webapp:create_app()' --threads 10


def setup_flask_admin(app_instance, session):

    from webapp import models as mdl
    from webapp import views as vw

    admin = Admin(
        name='JBOD',
        template_mode='bootstrap4',
        base_template='jbod_base.html'
    )
    admin.init_app(app_instance, index_view=vw.IndexView(name='Home', url='/'))
    admin.add_view(vw.DiskView(mdl.Disk, session))
    admin.add_view(vw.ChassisView(mdl.Chassis, session))
    admin.add_view(vw.ControllerView(mdl.Controller, session))
    admin.add_view(vw.FanView(mdl.Fan, session, name='Fans', endpoint='fan'))
    admin.add_view(vw.SysConfigView(mdl.SysConfig, session, name='Settings', endpoint='settings', category='Settings'))
    admin.add_view(vw.TaskView(mdl.SysJob, session, name='Scheduled Tasks', endpoint='jobs', category='Settings'))
    admin.add_view(vw.SetpointView(mdl.FanSetpoint, session, name='setpoints', endpoint='setpoints'))
    admin.add_view(vw.NewSetupView(name='Setup', endpoint='setup'))
    admin.add_view(vw.AlertView(mdl.Alert, session, endpoint='alerts'))

    return app_instance


def setup_logger(file_path: str, app_instance: Flask, level: int):
    if not os.path.exists(file_path):
        if file_path.endswith(('/', '\\')):
            os.mkdir(file_path)
            file_path = os.path.normpath(os.path.join(file_path, 'ipmi.log'))
        else:
            file_path = os.path.normpath(file_path)
    file_handler = RotatingFileHandler(filename=file_path, mode='a', maxBytes=100000, backupCount=0)
    file_handler.setLevel(level)
    for logger in (app_instance.logger, logging.getLogger('apscheduler_events')):
        logger.setLevel(level)
        logger.addHandler(file_handler)
        logger.addHandler(default_handler)


def generate_key(salt, token) -> Fernet:
    if not isinstance(salt, bytes):
        salt = salt.encode()
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=480000,
    )
    if not isinstance(token, bytes):
        token = token.encode()
    key = base64.urlsafe_b64encode(kdf.derive(token))
    return Fernet(key)


def create_app(dev=False):
    # create and configure the app
    app = Flask(__name__)
    app.jinja_env.trim_blocks = True

    # create a .env if one doesn't already exist
    basedir = os.path.abspath(os.path.dirname(os.path.dirname(__file__)))
    if not os.path.exists(os.path.join(basedir, '.env')):
        with open(os.path.join(basedir, '.env'), 'w') as f:
            f.write(f"SECRET_KEY={Fernet.generate_key().decode()}\n")
            f.write(f"SECRET_KEY_SALT={base64.b64encode(os.urandom(16)).decode()}\n")

    if not dev:
        app.config.from_object('config.ProdConfig')
    else:
        app.config.from_object('config.DevConfig')
    # ensure the instance folder exists
    try:
        os.makedirs(app.instance_path)
    except OSError:
        pass

    from webapp import jobs
    from webapp.models import db, SysConfig, SysJob, Alert
    from webapp import helpers

    # initialize flask addons
    db.init_app(app)

    with app.app_context():
        # Create the database if it doesn't already exist
        if not os.path.exists(app.config['SQLALCHEMY_DATABASE_URI']):
            db.create_all()
            for k, v in config_defaults.items():
                try:
                    if k.endswith('_key'):
                        db.session.add(SysConfig(key=k, value=v, encrypt=True))
                    else:
                        db.session.add(SysConfig(key=k, value=v, encrypt=False))
                    db.session.commit()
                except IntegrityError:
                    db.session.rollback()
            # add db placeholders for system jobs
            for j in scheduler_jobs:
                try:
                    db.session.add(SysJob(**j))
                    db.session.commit()
                except IntegrityError:
                    db.session.rollback()

        # setup logger
        log_path = helpers.get_config_value('log_path')
        if not log_path:
            log_path = os.path.normpath(os.path.join(app.instance_path, 'ipmi.log'))
        setup_logger(file_path=log_path, app_instance=app, level=app.config['LOGGING_LEVEL'])
        alert_handler = helpers.AlertLogHandler(alert_model=Alert, app_context=app, db_session=db.session)
        alert_handler.setLevel(logging.WARNING)  # prevent overflow of alerts
        app.logger.addHandler(alert_handler)

        # setup encrypt & decrypt methods in app instance
        _ceph = generate_key(app.config['SECRET_KEY'], app.config['SECRET_KEY_SALT'])
        app.__setattr__('encrypt', _ceph.encrypt)
        app.__setattr__('decrypt', _ceph.decrypt)

        # populate app configuration with system jobs from database
        app.config['TIMEZONE'] = db.session.query(SysConfig.value).where(SysConfig.key == 'timezone').first()[0]
        for job in db.session.query(SysJob).where(SysJob.active == True).all():  # noqa
            app.config['SCHEDULER_JOBS'].append(job.job_dict)

        # setup console threads for read/writes
        jobs.get_console()

    app = setup_flask_admin(app, db.session)
    jobs.scheduler.init_app(app)

    # setup apscheduler and event listeners
    jobs.scheduler.add_listener(jobs.ev.job_missed_listener, EVENT_JOB_MISSED)
    jobs.scheduler.add_listener(jobs.ev.job_error_listener, EVENT_JOB_ERROR)
    jobs.scheduler.add_listener(jobs.ev.job_executed_listener, EVENT_JOB_EXECUTED)
    jobs.scheduler.add_listener(jobs.ev.job_added_listener, EVENT_JOB_ADDED)
    jobs.scheduler.add_listener(jobs.ev.job_removed_listener, EVENT_JOB_REMOVED)
    jobs.scheduler.add_listener(jobs.ev.job_submitted_listener, EVENT_JOB_SUBMITTED)
    jobs.scheduler.start()

    # add custom functions to jinja environment
    app.jinja_env.globals.update(truenas_connection_info=jobs.truenas_connection_info)
    app.jinja_env.globals.update(get_serial_connection=jobs.console_connection_check)
    app.jinja_env.globals.update(disk_tooltip_html=helpers.disk_tooltip_html)
    app.jinja_env.globals.update(svg_html_converter=helpers.svg_html_converter)
    app.jinja_env.globals.update(get_alerts=helpers.get_alerts)

    return app

