import os
import base64
from flask import Flask
from logging.config import dictConfig
from sqlalchemy.exc import IntegrityError
from flask_admin import Admin
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from apscheduler.events import EVENT_JOB_MISSED, EVENT_JOB_ERROR, EVENT_JOB_EXECUTED, EVENT_JOB_ADDED, \
    EVENT_JOB_REMOVED, EVENT_JOB_SUBMITTED

from ipmi.config import config_defaults, scheduler_jobs, logging_config


# run the following to start server
# sudo gunicorn -w 1 -b 0.0.0.0 'ipmi:create_app()' --threads 3


dictConfig(logging_config)


def setup_flask_admin(app_instance, session):

    from ipmi import models as mdl
    from ipmi import views as vw

    admin = Admin(
        name='JBOD',
        template_mode='bootstrap4',
        base_template='jbod_base.html'
    )
    admin.init_app(app_instance, index_view=vw.IndexView(name='Home', url='/'))
    # admin.add_view(vw.TestView(endpoint='test'))
    admin.add_view(vw.DiskView(mdl.Disk, session))
    admin.add_view(vw.ChassisView(mdl.Chassis, session))
    admin.add_view(vw.ControllerView(mdl.Controller, session))
    admin.add_view(vw.FanView(mdl.Fan, session, name='Fans', endpoint='fan'))
    admin.add_view(vw.SysConfigView(mdl.SysConfig, session, name='Settings', endpoint='settings', category='Settings'))
    admin.add_view(vw.TaskView(mdl.SysJob, session, name='Scheduled Tasks', endpoint='jobs', category='Settings'))
    admin.add_view(vw.SetpointView(mdl.FanSetpoint, session, name='setpoints', endpoint='setpoints'))
    admin.add_view(vw.NewSetupView(name='Setup', endpoint='setup'))

    return app_instance


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


def create_app(test_config=None):
    # create and configure the app
    app = Flask(__name__, instance_relative_config=True)
    app.jinja_env.trim_blocks = True
    app.config.from_mapping(
        DEBUG=True,
        TESTING=True,
        SCHEDULER_API_ENABLED=True,
        SECRET_KEY='y2ruDSrohycg8PA9YO1Vtjt5s7fdDakM5giqBgNKHGs=',
        SECRET_KEY_SALT=b'\x1c_\xd3\\z\xedf\xe4\xe6Y\x9az\x05\xbf7\xdf',
        FLASK_ADMIN_FLUID_LAYOUT=True,
        SQLALCHEMY_DATABASE_URI="sqlite:///%s" % os.path.normpath(os.path.join(app.instance_path, 'ipmi.db')),
        SCHEDULER_JOBS=[],  # APScheduler Jobs
    )
    if test_config is None:
        # load the instance config, if it exists, when not testing
        app.config.from_pyfile('config.py', silent=True)
    else:
        # load the test config if passed in
        app.config.from_mapping(test_config)
    # ensure the instance folder exists
    try:
        os.makedirs(app.instance_path)
    except OSError:
        pass

    from ipmi import jobs
    from ipmi.models import db, SysConfig, SysJob
    from ipmi.helpers import get_config_value, disk_tooltip_html, svg_html_converter

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

    jobs.scheduler.init_app(app)
    app = setup_flask_admin(app, db.session)

    # setup apscheduler and event listeners
    jobs.scheduler.start()
    jobs.scheduler.add_listener(jobs.ev.job_missed_listener, EVENT_JOB_MISSED)
    jobs.scheduler.add_listener(jobs.ev.job_error_listener, EVENT_JOB_ERROR)
    jobs.scheduler.add_listener(jobs.ev.job_executed_listener, EVENT_JOB_EXECUTED)
    jobs.scheduler.add_listener(jobs.ev.job_added_listener, EVENT_JOB_ADDED)
    jobs.scheduler.add_listener(jobs.ev.job_removed_listener, EVENT_JOB_REMOVED)
    jobs.scheduler.add_listener(jobs.ev.job_submitted_listener, EVENT_JOB_SUBMITTED)

    # add custom functions to jinja environment
    app.jinja_env.globals.update(truenas_connection_info=jobs.truenas_connection_info)
    app.jinja_env.globals.update(get_serial_connection=jobs.console_connection_check)
    app.jinja_env.globals.update(disk_tooltip_html=disk_tooltip_html)
    app.jinja_env.globals.update(svg_html_converter=svg_html_converter)
    # app.jinja_env.globals.update(debug=debug)
    return app

