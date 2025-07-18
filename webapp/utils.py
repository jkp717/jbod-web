import logging
import math
import os
import csv
from datetime import datetime, timedelta
from enum import IntEnum
from typing import Optional, Iterable, Union

import requests
from dateutil import tz
from flask import Markup, current_app, Flask
from flask_admin.helpers import url_for
from flask_admin.model import typefmt
from flask_admin.model.template import TemplateLinkRowAction
from sqlalchemy.exc import IntegrityError

from webapp.models import db, FanSetpoint, SysConfig, Disk, Alert, Chassis, PhySlot, Fan
from webapp import jobs

_logger = logging.getLogger("utils")

class StatusFlag(IntEnum):
    FAIL = 0
    COMPLETE = 1
    RUNNING = 2
    UNKNOWN = 3
    ERROR = 4


def get_config_value(config_param: str):
    with current_app.app_context():
        return db.session.query(SysConfig.value).where(SysConfig.key == config_param).first()[0]


def get_alerts():
    with current_app.app_context():
        return db.session.query(Alert).all()


def truenas_api_request(method: str, url_path: str, headers: Optional[dict] = None, data: Optional[dict] = None):
    if headers is None:
        headers = {}
    _logger.debug(f"""
        truenas_api_request called:
        \tmethod: {method}, 
        \turl_path: {url_path}, 
        \theaders: {headers}, 
        \tdata: {data}"""
    )
    with current_app.app_context():
        api_key = db.session.execute(
            db.select(SysConfig).where(SysConfig.key == "truenas_api_key")
        ).first()
        base_url = db.session.execute(
            db.select(SysConfig).where(SysConfig.key == "truenas_url")
        ).first()
        if getattr(api_key[0], 'value', None) and getattr(base_url[0], 'value', None):
            if getattr(api_key[0], 'encrypt'):
                _api_key = current_app.decrypt(getattr(api_key[0], 'value')).decode()
            else:
                _api_key = getattr(api_key[0], 'value')
            _base_url = getattr(base_url[0], 'value')
            _headers = {
                'Authorization': f"Bearer {_api_key}",
                'accept': '*/*',
                **headers
            }
            return requests.request(method, f"{_base_url}{url_path}", headers=_headers, json=data,
                                    timeout=int(get_config_value('http_requests_timeout')))
        return None


def disk_tooltip_html(model: Optional[Disk]) -> str:
    if model:
        return f"""<div class=disk-tooltip-temp>{model.temperature}</div>
        <a href="{url_for('disk.details_view', id=model.serial)}" class=disk-tooltip-item>{model.serial}</a>
        """
    return f"""
    <a href="{url_for('disk.index_view', flt1_physlot_chassis_name_empty=1)}" class=disk-tooltip-item>Add Disk</a>
    """


def fan_tooltip_html(model: Optional[Fan]) -> str:
    if model:
        if model.active:
            cnt = len(model.logs)
            filter_txt = 'flt1_fan_fan_id_equals'
            if fan_watchdog(model):
                # watchdog triggered
                dt = datetime.utcnow() - model.last_report
                hours, remainder = divmod(dt.seconds, 3600)
                minutes, seconds = divmod(remainder, 60)
                time_dif = f'{str(int(hours)) + "h"} {str(int(minutes)) + "m"} {str(int(seconds)) + "s"}'
                return f"""<h5>Alert!</h5><div><i>Last RPM report</i></div><h6>{time_dif} ago</h6>"""
            return f"""<h5>{model.rpm} RPM</h5>
            <div><a href="{url_for('fan.details_view', id=model.id)}">View Fan</a></div>
            <div><a class="list-model-link" href='{url_for("fan/log.index_view")}?{filter_txt}={model.id}'>
                View Logs ({cnt})
            </a></div>"""
    return f"""
    <i>No active fan on this port.</i>
    """


def fan_watchdog(model: Fan) -> bool:
    """
    Fan Window Watchdog Timer - Returns bool if fan does not update within set window
    """
    with current_app.app_context():
        trigger_dt = model.last_report + timedelta(seconds=int(get_config_value('fan_alert_after_seconds')))
        if trigger_dt < datetime.utcnow():
            return True
        return False


def svg_html_converter(path: str) -> str:
    """Jinja2 function"""
    with current_app.app_context():
        root_path = str(current_app.root_path).split(os.sep)
    try:
        with open('/'.join([*root_path, *path.split('/')]), 'r') as svg:
            return Markup(svg.read())
    except OSError:
        return ""


def pwm_change_formatter(view, context, model, name):  # noqa
    return f"PWM changed from {model.old_pwm} to {model.new_pwm}"


def fan_log_formatter(view, context, model, name):  # noqa
    cnt = len(model.logs)
    filter_txt = 'flt1_fan_fan_id_equals'
    if cnt > 0:
        return Markup(
            f"""<a class="list-model-link" href='{url_for("fan/log.index_view")}?{filter_txt}={model.id}'>
                View Logs ({cnt} total)
            </a>"""
        )


def disk_size_formatter(view, context, model, name):  # noqa
    size_bytes = getattr(model, name)
    if size_bytes == 0:
        return "0B"
    size_name = ("B", "KB", "MB", "GB", "TB", "PB", "EB", "ZB", "YB")
    i = int(math.floor(math.log(size_bytes, 1024)))
    p = math.pow(1024, i)
    s = round(size_bytes / p, 2)
    return "%s %s" % (s, size_name[i])


def disk_link_formatter(view, context, model, name):  # noqa
    filter_txt = 'flt0_physlot_chassis_name_equals'
    if getattr(model, name):
        return Markup(
            f"""<a href='{url_for("disk.index_view")}?{filter_txt}={model.name}'>{getattr(model, name)}</a>"""
        )
    return getattr(model, name)


def next_job_runtime_formatter(view, context, model, name):  # noqa
    if model.active:
        job = jobs.scheduler.get_job(getattr(model, 'job_id'))
        if job:
            delta = job.next_run_time.replace(tzinfo=None) - datetime.now()
            delta_txt = ""
            if int(delta.days) > 0:
                return f"{int(delta.days)} Day(s)"
            if delta.seconds >= 3600:
                delta_txt += f"{int(delta.seconds / 3600)}h "
            if int(delta.seconds) > 60:
                delta_txt += f"{int(delta.seconds / 60)}m "
            if int(delta.seconds) > 0:
                delta_txt += f"{(delta.seconds % 60)}s "
            return delta_txt
    return None

def datetime_formatter(view, value, name):  # noqa
    with current_app.app_context():
        value = value.replace(tzinfo=tz.gettz('UTC'))
        return value.astimezone(tz.gettz(current_app.config['TIMEZONE'])).strftime('%m/%d/%Y %X')


def byte_formatter(view, value, name):  # noqa
    return Markup("""&bull;&bull;&bull;&bull;&bull;&bull;&bull;&bull;&bull;""")


def psu_toggle_formatter(view, context, model, name):  # noqa
    # psu off
    if getattr(model, name):
        return Markup(
            f"""<a href='{url_for("chassis.psu_toggle")}?id={model.id}&state=ON'>TURN-ON</a>"""
        )
    # psu on
    return Markup(
        f"""<a href='{url_for("chassis.psu_toggle")}?id={model.id}&state=OFF'>TURN-OFF</a>"""
    )


def controller_id_formatter(view, context, model, name):  # noqa
    txt = 'Master' if model.id == 1 else f'Slave({model.id - 1})'
    return Markup(txt)


class FanCalibrationRowAction(TemplateLinkRowAction):
    def __init__(self):
        super(FanCalibrationRowAction, self).__init__('custom_row_actions.fan_calibration')


class EditSetpointsRowAction(TemplateLinkRowAction):
    def __init__(self):
        super(EditSetpointsRowAction, self).__init__('custom_row_actions.edit_setpoints')


class ControllerAlarmRowAction(TemplateLinkRowAction):
    def __init__(self):
        super(ControllerAlarmRowAction, self).__init__('custom_row_actions.controller_alarm')


class ControllerLEDRowAction(TemplateLinkRowAction):
    def __init__(self):
        super(ControllerLEDRowAction, self).__init__('custom_row_actions.controller_led')


class ControllerResetRowAction(TemplateLinkRowAction):
    def __init__(self):
        super(ControllerResetRowAction, self).__init__('custom_row_actions.controller_reset')


class RunJobRowAction(TemplateLinkRowAction):
    def __init__(self):
        super(RunJobRowAction, self).__init__('custom_row_actions.run_job_now')


def get_model_by_id(model, id: Union[str, int], column_name: Optional[str] = 'id'):
    return db.session.query(model).where(getattr(model, column_name) == id).first()


def csv_upload_processor(path: str, disk_serials: list, chassis_ids: list) -> dict:
    """
    CSV Disk (Physical Slot) Location Processor
    @param path: String path to file
    @param disk_serials: list of disk serial numbers
    @param chassis_ids: list of chassis ids
    @return: dict containing added_disks, missing_disks, duplicated_disks,
    missing_chassis, skipped_slot
    """
    resp_dict = {
        'added_disks': [],
        'missing_disks': [],
        'duplicated_disks': [],
        'missing_chassis': [],
        'skipped_slot': [],
        'slot_in_use': []
    }
    _disks = []
    with open(path, newline='') as csvfile:
        csv_reader = csv.DictReader(csvfile, fieldnames=['chassis', 'disk', 'slot'])
        for i, row in enumerate(csv_reader):
            if (i == 0) or (row['disk'] == "" or row['chassis'] == "" or row['slot'] == ""):
                continue
            if row['disk'] not in disk_serials:
                resp_dict['missing_disks'].append(row['disk'])
                continue
            if row['disk'] in _disks:
                resp_dict['duplicated_disks'].append(row['disk'])
                continue
            else:
                _disks.append(row['disk'])
            disk = db.session.query(Disk).where(Disk.serial == row['disk']).first()
            if int(row['chassis']) not in chassis_ids:
                if row['chassis'] not in resp_dict['missing_chassis']:
                    resp_dict['missing_chassis'].append(row['chassis'])
                continue
            chassis = db.session.query(Chassis).where(Chassis.id == row['chassis']).first()
            if int(row['slot']) > chassis.slot_cnt:
                resp_dict['skipped_slot'].append(row['slot'])
                continue
            physlot = db.session.query(PhySlot) \
                .where(PhySlot.chassis_id == row['chassis'],
                       PhySlot.phy_slot == row['slot']) \
                .first()
            disk.phy_slot_id = physlot.id
            resp_dict['added_disks'].append(disk)
            try:
                db.session.commit()
            except IntegrityError:
                db.session.rollback()
                resp_dict['slot_in_use'].append(f"{physlot} by {physlot.disk}")
        return resp_dict


def cascade_add_setpoints(fan_id: int):
    min_chassis_temp = int(get_config_value('min_chassis_temp'))
    max_chassis_temp = int(get_config_value('max_chassis_temp'))
    min_fan_pwm = int(get_config_value('min_fan_pwm'))
    max_fan_pwm = int(get_config_value('max_fan_pwm'))
    mid_fan_pwm = round((int(min_fan_pwm) + int(max_fan_pwm)) / 2)
    mid_chassis_temp = round((min_chassis_temp + max_chassis_temp) / 2, -1)
    db.session.add(FanSetpoint(fan_id=fan_id, pwm=int(min_fan_pwm), temp=min_chassis_temp))
    db.session.add(FanSetpoint(fan_id=fan_id, pwm=mid_fan_pwm, temp=mid_chassis_temp))
    db.session.add(FanSetpoint(fan_id=fan_id, pwm=int(max_fan_pwm), temp=max_chassis_temp))


def clone_model(model, **kwargs):
    """Clone an arbitrary sqlalchemy model object without its primary key values."""
    table = model.__table__
    non_pk_columns = [k for k in table.columns.keys() if k not in table.primary_key.columns.keys()]
    data = {c: getattr(model, c) for c in non_pk_columns}
    data.update(kwargs)

    clone = model.__class__(**data)
    db.session.add(clone)
    db.session.commit()
    return clone


def get_config_formatters() -> dict:
    CONFIG_FORMATTERS = dict(typefmt.BASE_FORMATTERS)
    CONFIG_FORMATTERS.update({
        bytes: byte_formatter,
        datetime: datetime_formatter
    })
    return CONFIG_FORMATTERS


def json_path_generator(key: str, val: object, var: object) -> Iterable:
    """
    Generator that returns the path to the provided key/value
    in a JSON object. Example:
        list(json_path_generator('key_to_find', 'value_of_key', json))
    @param key: Dict key to find
    @param val: Dict key's value to match
    @param var: JSON object
    """
    if hasattr(var, 'items'):
        for k, v in var.items():
            if k == key and v == val:
                yield k
            if isinstance(v, dict):
                for result in json_path_generator(key, val, v):
                    yield f"{k}.{result}"
            elif isinstance(v, list):
                for i, d in enumerate(v):
                    for result in json_path_generator(key, val, d):
                        yield f"{k}.{i}.{result}"


def resolve_string_attr(obj: object, attr: str, level: int = None) -> Union[list, dict]:
    """
    Returns an attribute from the object passed based on the string path specified
    in the attr.  If level is provided, the object returned will be the Nth level
    of the attr string path provided.
    @param obj: object to resolve
    @param attr: dot (.) separated attribute path string i.e. "path.to.attr"
    @param level: Return Nth level of attribute path i.e. level=1 "path.to"
    @return: resolved attribute object
    """
    list_attr = attr.strip(".").split(".")[:-1 if not level else level]
    for i, name in enumerate(list_attr):
        if name.isdigit():
            obj = obj[int(name)]
        else:
            obj = obj.get(name)
    return obj


class AlertLogHandler(logging.Handler):

    def __init__(self, alert_model, app_context: Flask, db_session):
        logging.Handler.__init__(self)
        self.app_context = app_context
        self.db_session = db_session
        self.alert_model = alert_model
        self.log_msg = None

    def emit(self, record):
        # Clear the log message so that it can be put to db via sql (escape quotes)
        self.log_msg = str(record.msg).strip().replace('\'', '\'\'')
        # Make the SQL insert
        with self.app_context.app_context():
            self.db_session.add(self.alert_model(category=record.levelname, content=self.log_msg))
            self.db_session.commit()
