import logging
from webapp import helpers, jobs
from webapp.models import db, Fan, SysJob


_logger = logging.getLogger('apscheduler_events')


def fan_calibration_job_listener(event):
    """Single trigger event listener"""
    with jobs.scheduler.app.app_context():
        fan = db.session.query(Fan).where(Fan.calibration_job_uuid == getattr(event, 'job_id')).first()
        original_rpm = fan.rpm
        if fan:
            if event.exception:
                fan.calibration_status = helpers.StatusFlag.FAIL
            else:
                fan.calibration_status = helpers.StatusFlag.COMPLETE
            # set fan rpm so SQLAlchemy onupdate on 'active' column works correctly
            fan.rpm = original_rpm
            db.session.commit()
            # remove itself after triggering on fan job
            jobs.scheduler.remove_listener(fan_calibration_job_listener)


def job_missed_listener(event):
    """Job missed event."""
    _logger.warning(f"Job {event.job_id} missed by scheduler.")


def job_error_listener(event):
    """Job error event."""
    _logger.error(f"Scheduled job {event.job_id} failed. Error: {event.exception}")


def job_executed_listener(event):
    """Job executed event."""
    _logger.info(f"Scheduled job {event.job_id} executed.")


def job_added_listener(event):
    """Job added event."""
    with jobs.scheduler.app.app_context():
        _logger.info(f"Scheduled job {event.job_id} added to job store.")
        if event.job_id == 'poll_controller_data':
            # add companion func; used to monitor dc2 responses and update controller alive state
            # must be a separate job b/c the dc2 request is sent in a separate thread and returned in callback
            job = db.session.query(SysJob).where(SysJob.job_id == getattr(event, 'job_id')).first()
            jobs.scheduler.add_job('_poll_controller_data', func="webapp.jobs:_poll_controller_data",
                                   trigger='interval', second=job.seconds, minute=job.minutes)


def job_removed_listener(event):
    """Job removed event."""
    with jobs.scheduler.app.app_context():
        _logger.info(f"Scheduled job {event.job_id} removed to job store.")
        if event.job_id == 'poll_controller_data':
            # remove companion func
            jobs.scheduler.remove_job('_poll_controller_data')


def job_submitted_listener(event):
    """Job scheduled to run event."""
    _logger.info(f"Scheduled job {event.job_id} was submitted to its executor to be run.")