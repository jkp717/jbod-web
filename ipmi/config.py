

# Max should not be 100, controller only supports two digits
MAX_FAN_PWM = 99

# Lowest supported value by most PWM fans
MIN_FAN_PWM = 20

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
    'rpm_watchdog_window': 200,
    'log_path': None
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
        'description': 'Poll controller(s) to get latest fan RPM.',
        'seconds': 30
    }, {
        'job_id': 'database_cleanup',
        'func': 'ipmi.jobs:database_cleanup',
        'job_name': 'Database Cleanup',
        'description': 'Removes old data from database',
        'hours': 2
    },
]