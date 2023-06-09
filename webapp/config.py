#
# Webapp Configurations
#

DEFAULT_FAN_PWM = 50

# configuration defaults
config_defaults = {
    'timezone': 'America/Chicago',
    'console_port': None,
    'baud_rate': '115600',
    'console_timeout': 1,
    'truenas_api_key': None,
    'truenas_url': None,
    'max_chassis_temp': 65,
    'min_chassis_temp': 20,
    'max_fan_pwm': 99,
    'min_fan_pwm': 20,
    'default_fan_pwm': DEFAULT_FAN_PWM,
    'log_path': None,
    # abs rpm difference after testing pwm +/- 10
    # fan is a four_pin if > this value
    'four_pin_rpm_deviation': 100,
    # how long to wait after changing PWM
    # before checking rpm for changes
    'rpm_read_delay': 0.3,
    # number of consecutive job failures when job schedule is paused
    'job_max_failures': 3,
    # minutes until the failed job is resumed
    'job_paused_minutes': 60,
    'http_requests_timeout': 15,
    'fan_alert_after_seconds': 120
}

scheduler_job_defaults = {
    'coalesce': True,
    'max_instances': 1,
    'misfire_grace_time': 1
}

# task scheduler defaults
scheduler_jobs = [
    {
        'job_id': 'query_disk_properties',
        'func': 'webapp.jobs:query_disk_properties',
        'job_name': 'Query Disk Properties',
        'description': 'TrueNAS API call to get disk properties.',
        'hours': 1,
        'can_edit': True,
    }, {
        'job_id': 'query_disk_temperatures',
        'func': 'webapp.jobs:query_disk_temperatures',
        'job_name': 'Query Disk Temperatures',
        'description': 'TrueNAS API call to get disk temperatures.',
        'minutes': 1,
        'can_edit': True
    }, {
        'job_id': 'poll_setpoints',
        'func': 'webapp.jobs:poll_setpoints',
        'job_name': 'Poll Fan Setpoints',
        'description': 'Poll chassis temperature and set fan(s) PWM according to defined setpoint.',
        'minutes': 2,
        'can_edit': True
    }, {
        'job_id': 'poll_controller_data',
        'func': 'webapp.jobs:poll_controller_data',
        'job_name': 'Poll Controller Data',
        'description': 'Poll controller(s) to get latest fan RPM and PWM, as well as PSU Status.',
        'seconds': 30,
        'can_edit': True
    }, {
        'job_id': 'database_cleanup',
        'func': 'webapp.jobs:database_cleanup',
        'job_name': 'Database Cleanup',
        'description': 'Removes old data from database',
        'hours': 2,
        'can_edit': True
    },
    {
        'job_id': 'tty_stat_tracker',
        'func': 'webapp.jobs:tty_stat_tracker',
        'job_name': 'Stat Tracker',
        'description': 'Stores Tx/Rx byte counts',
        'hours': 1,
        'can_edit': False,  # prevents user from being able to change times
        'active': True
    }
]