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
    'four_pin_rpm_deviation': 50
}

# task scheduler defaults
scheduler_jobs = [
    {
        'job_id': 'query_disk_properties',
        'func': 'webapp.jobs:query_disk_properties',
        'job_name': 'Query Disk Properties',
        'description': 'TrueNAS API call to get disk properties.',
        'hours': 1
    }, {
        'job_id': 'query_disk_temperatures',
        'func': 'webapp.jobs:query_disk_temperatures',
        'job_name': 'Query Disk Temperatures',
        'description': 'TrueNAS API call to get disk temperatures.',
        'minutes': 1
    }, {
        'job_id': 'poll_setpoints',
        'func': 'webapp.jobs:poll_setpoints',
        'job_name': 'Poll Fan Setpoints',
        'description': 'Poll chassis temperature and set fan(s) PWM according to defined setpoint.',
        'minutes': 2
    }, {
        'job_id': 'poll_controller_data',
        'func': 'webapp.jobs:poll_controller_data',
        'job_name': 'Poll Controller Data',
        'description': 'Poll controller(s) to get latest fan RPM and PWM, as well as PSU Status.',
        'seconds': 30
    }, {
        'job_id': 'database_cleanup',
        'func': 'webapp.jobs:database_cleanup',
        'job_name': 'Database Cleanup',
        'description': 'Removes old data from database',
        'hours': 2
    },
]
