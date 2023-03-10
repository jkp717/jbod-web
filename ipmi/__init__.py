import os
from flask import Flask
from flask_sqlalchemy import SQLAlchemy
from flask_admin import Admin
from flask_apscheduler import APScheduler
from cryptography.fernet import Fernet


config_defaults = {
    'console_port': '/dev/ttyS0',
    'baud_rate': '115600',
    'console_timeout': 1,
    'truenas_api_key': None,
    'truenas_url': None,
    'max_chassis_temp': 60,
    'min_chassis_temp': 20,
    'min_fan_pwm': 20,
    'max_fan_pwm': 100,
}


db = SQLAlchemy()
admin = Admin(name='JBOD', template_mode='bootstrap4', base_template='jbod_base.html')
scheduler = APScheduler()


def create_app(test_config=None):
    # create and configure the app
    app = Flask(__name__, instance_relative_config=True)
    app.config.from_mapping(
        DEBUG=True,
        TESTING=True,
        SECRET_KEY='y2ruDSrohycg8PA9YO1Vtjt5s7fdDakM5giqBgNKHGs=',
        FLASK_ADMIN_FLUID_LAYOUT=True,
        SQLALCHEMY_DATABASE_URI="sqlite:///%s" % os.path.normpath(os.path.join(app.instance_path, 'ipmi.db')),
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

    _ceph = Fernet(app.config['SECRET_KEY'])
    app.__setattr__('encrypt', _ceph.encrypt)
    app.__setattr__('decrypt', _ceph.decrypt)

    # initialize flask addons
    db.init_app(app)
    admin.init_app(app)
    scheduler.init_app(app)
    scheduler.start()

    # Create the database if it doesn't already exist
    if not os.path.exists(app.config['SQLALCHEMY_DATABASE_URI']):
        with app.app_context():
            from ipmi.tasks import initial_db_setup
            db.create_all()
            initial_db_setup(config_defaults)

    from ipmi.views import (
        DiskView, FanView, SysConfigView, ChassisView,
        SetpointView, ControllerView
    )
    from ipmi.models import (
        Disk, PhySlot, Fan, SysConfig, Chassis, FanSetpoint,
        Controller
    )
    from ipmi.tasks import truenas_connection_info

    admin.add_view(DiskView(Disk, db.session))
    admin.add_view(ChassisView(Chassis, db.session))
    admin.add_view(ControllerView(Controller, db.session))
    admin.add_view(FanView(Fan, db.session, name='Fans', endpoint='fan'))
    admin.add_view(SysConfigView(SysConfig, db.session, name='Settings', endpoint='settings'))
    admin.add_view(SetpointView(FanSetpoint, db.session, name='setpoints', endpoint='setpoints'))
    app.jinja_env.globals.update(truenas_connection_info=truenas_connection_info)

    return app

