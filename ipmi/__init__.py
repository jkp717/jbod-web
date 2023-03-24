import os
import logging
from flask import Flask
from flask.logging import default_handler
from flask_admin import Admin
from apscheduler.events import EVENT_JOB_MISSED, EVENT_JOB_ERROR, EVENT_JOB_EXECUTED, EVENT_JOB_ADDED, \
    EVENT_JOB_REMOVED, EVENT_JOB_SUBMITTED


# configuration defaults
config_defaults = {
    'timezone': 'America/Chicago',
    'console_port': '/dev/ttyS0',
    'baud_rate': '115600',
    'console_timeout': 1,
    'truenas_api_key': None,
    'truenas_url': None,
    'max_chassis_temp': 65,
    'min_chassis_temp': 20,
    'min_fan_pwm': 20,
    'max_fan_pwm': 100,
    'acpi_shutdown_script': None,
    'acpi_startup_script': None
}

# task scheduler defaults
scheduler_jobs = [
    {
        'job_id': 'query_disk_properties',
        'func': 'ipmi.jobs:query_disk_properties',
        'job_name': 'Query Disk Properties',
        'description': 'TrueNAS API call to get disk properties.',
        'hours': 1
    }, {
        'job_id': 'query_disk_temperatures',
        'func': 'ipmi.jobs:query_disk_temperatures',
        'job_name': 'Query Disk Temperatures',
        'description': 'TrueNAS API call to get disk temperatures.',
        'minutes': 1
    }, {
        'job_id': 'poll_setpoints',
        'func': 'ipmi.jobs:poll_setpoints',
        'job_name': 'Poll Fan Setpoints',
        'description': 'Poll chassis temperature and set fan(s) PWM according to defined setpoint.',
        'minutes': 2
    }, {
        'job_id': 'poll_fan_rpm',
        'func': 'ipmi.jobs:poll_fan_rpm',
        'job_name': 'Poll Fan RPMs',
        'description': 'Poll controller to get latest fan RPM(s).',
        'seconds': 30
    }, {
        'job_id': 'database_cleanup',
        'func': 'ipmi.jobs:database_cleanup',
        'job_name': 'Database Cleanup',
        'description': 'Removes old data from database',
        'hours': 2
    },
]


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
    from ipmi.helpers import initial_db_setup, get_config_value, generate_key, \
        disk_tooltip_html, svg_html_converter

    # initialize flask addons
    db.init_app(app)
    jobs.scheduler.init_app(app)

    # Create the database if it doesn't already exist
    if not os.path.exists(app.config['SQLALCHEMY_DATABASE_URI']):
        with app.app_context():
            db.create_all()
            initial_db_setup(config_defaults, scheduler_jobs)

    with app.app_context():
        _ceph = generate_key(app.config['SECRET_KEY'], app.config['SECRET_KEY_SALT'])
        # setup encrypt & decrypt methods in app instance
        app.__setattr__('encrypt', _ceph.encrypt)
        app.__setattr__('decrypt', _ceph.decrypt)

        # populate app configuration with system jobs from database
        app.config['TIMEZONE'] = db.session.query(SysConfig.value).where(SysConfig.key == 'timezone').first()[0]
        for job in db.session.query(SysJob).where(SysJob.active == True).all():  # noqa
            app.config['SCHEDULER_JOBS'].append(job.job_dict)

        # setup console threads for read/writes
        jobs.get_console()

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

    return app

