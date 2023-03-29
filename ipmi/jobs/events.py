import logging
from ipmi import helpers, jobs
from ipmi.models import db, Fan


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
    with jobs.scheduler.app.app_context():
        jobs.scheduler.app.logger.warning(f"Job {event.job_id} missed by scheduler.")


def job_error_listener(event):
    """Job error event."""
    with jobs.scheduler.app.app_context():
        # TODO: add check for consecutive failed attempts; then remove job when exceeded
        jobs.scheduler.app.logger.error(f"Scheduled job {event.job_id} failed. Error: {event.exception}")


def job_executed_listener(event):
    """Job executed event."""
    with jobs.scheduler.app.app_context():
        jobs.scheduler.app.logger.info(f"Scheduled job {event.job_id} executed.")


def job_added_listener(event):
    """Job added event."""
    with jobs.scheduler.app.app_context():
        jobs.scheduler.app.logger.info(f"Scheduled job {event.job_id} added to job store.")


def job_removed_listener(event):
    """Job removed event."""
    with jobs.scheduler.app.app_context():
        jobs.scheduler.app.logger.info(f"Scheduled job {event.job_id} removed to job store.")


def job_submitted_listener(event):
    """Job scheduled to run event."""
    with jobs.scheduler.app.app_context():
        jobs.scheduler.app.logger.info(f"Scheduled job {event.job_id} was submitted to its executor to be run.")