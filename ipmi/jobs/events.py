import logging
from flask.logging import default_handler
from ipmi import helpers, jobs
from ipmi.models import db, Fan

_logger = logging.getLogger(__name__)
_logger.addHandler(default_handler)


def fan_calibration_job_listener(event):
    """Single trigger event listener"""
    with jobs.scheduler.app.app_context():
        fan = db.session.query(Fan).where(Fan.calibration_job_uuid == getattr(event, 'job_id')).first()
        if fan:
            if event.exception:
                fan.calibration_status = helpers.StatusFlag.FAIL
            else:
                fan.calibration_status = helpers.StatusFlag.COMPLETE
            db.session.commit()
            # remove itself after triggering on fan job
            jobs.scheduler.remove_listener(fan_calibration_job_listener)


def job_missed_listener(event):
    """Job missed event."""
    with jobs.scheduler.app.app_context():
        _logger.warning("Job %s missed by scheduler.", event.job_id)


def job_error_listener(event):
    """Job error event."""
    with jobs.scheduler.app.app_context():
        _logger.error("Scheduled job %s failed. Error: %s", event.job_id, event.exception)


def job_executed_listener(event):
    """Job executed event."""
    with jobs.scheduler.app.app_context():
        _logger.info("Scheduled job %s executed.", event.job_id)


def job_added_listener(event):
    """Job added event."""
    with jobs.scheduler.app.app_context():
        _logger.info("Scheduled job %s added to job store.", event.job_id)


def job_removed_listener(event):
    """Job removed event."""
    with jobs.scheduler.app.app_context():
        _logger.info("Scheduled job %s removed to job store.", event.job_id)


def job_submitted_listener(event):
    """Job scheduled to run event."""
    with jobs.scheduler.app.app_context():
        _logger.info("Scheduled job %s was submitted to its executor to be run.", event.job_id)