import os
import math
from typing import Optional, Iterable, Union
from enum import IntEnum
from dateutil import tz
from sqlalchemy.exc import IntegrityError
from flask import Markup, current_app
from flask_admin.helpers import url_for
from datetime import datetime
from flask_admin.model import typefmt
from flask_admin.model.template import TemplateLinkRowAction
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
import base64

from ipmi.models import db, FanSetpoint, SysConfig, SysJob, Celsius, Disk


class StatusFlag(IntEnum):
    FAIL = 0
    COMPLETE = 1
    RUNNING = 2
    UNKNOWN = 3
    ERROR = 4


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


def get_config_value(config_param: str):
    with current_app.app_context():
        return db.session.query(SysConfig.value).where(SysConfig.key == config_param).first()[0]


def initial_db_setup(config_defaults: dict, scheduler_jobs: []) -> None:
    with current_app.app_context():
        # populate default configuration values
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


def disk_tooltip_html(model: Union[Disk, None]) -> str:
    if model:
        return f"""<div class=disk-tooltip-temp>{model.temperature}</div>
        <a href="{url_for('disk.details_view', id=model.serial)}" class=disk-tooltip-item>{model.serial}</a>
        """
    return f"""
    <a href="{url_for('disk.index_view', flt1_physlot_chassis_name_empty=1)}" class=disk-tooltip-item>Add Disk</a>
    """


def svg_html_converter(path: str) -> str:
    """Jinja2 function"""
    with current_app.app_context():
        root_path = str(current_app.root_path).split(os.sep)
    try:
        with open('/'.join([*root_path, *path.split('/')]), 'r') as svg:
            return Markup(svg.read())
    except OSError:
        return ""


def disk_size_formatter(view, context, model, name):
    size_bytes = getattr(model, name)
    if size_bytes == 0:
        return "0B"
    size_name = ("B", "KB", "MB", "GB", "TB", "PB", "EB", "ZB", "YB")
    i = int(math.floor(math.log(size_bytes, 1024)))
    p = math.pow(1024, i)
    s = round(size_bytes / p, 2)
    return "%s %s" % (s, size_name[i])


def disk_link_formatter(view, context, model, name):
    filter_txt = 'flt0_physlot_chassis_name_equals'
    print(getattr(model, name))
    if getattr(model, name):
        return Markup(
            f"""<a href='{url_for("disk.index_view")}?{filter_txt}={model.name}'>{getattr(model, name)}</a>"""
        )
    return getattr(model, name)


def datetime_formatter(view, value, name):
    with current_app.app_context():
        value = value.replace(tzinfo=tz.gettz('UTC'))
        return value.astimezone(tz.gettz(current_app.config['TIMEZONE'])).strftime('%m/%d/%Y %X')


def byte_formatter(view, value, name):
    return Markup("""&bull;&bull;&bull;&bull;&bull;&bull;&bull;&bull;&bull;""")


def psu_toggle_formatter(view, context, model, name):
    # psu off
    if getattr(model, name):
        return Markup(
            f"""<a href='{url_for("chassis.psu_toggle")}?id={model.id}&state=ON'>TURN-ON</a>"""
        )
    # psu on
    return Markup(
            f"""<a href='{url_for("chassis.psu_toggle")}?id={model.id}&state=OFF'>TURN-OFF</a>"""
        )


class FanCalibrationRowAction(TemplateLinkRowAction):
    def __init__(self):
        super(FanCalibrationRowAction, self).__init__('custom_row_actions.fan_calibration')


def get_model_by_id(model, id, column_name: str = 'id'):
    return db.session.query(model).where(getattr(model, column_name) == id).first()


def cascade_add_setpoints(fan_id: int):
    min_fan_pwm = int(get_config_value('min_fan_pwm'))
    max_fan_pwm = int(get_config_value('max_fan_pwm'))
    min_chassis_temp = int(get_config_value('min_chassis_temp'))
    max_chassis_temp = int(get_config_value('max_chassis_temp'))
    mid_fan_pwm = round((min_fan_pwm + max_fan_pwm) / 2)
    mid_chassis_temp = round((min_chassis_temp + max_chassis_temp) / 2)
    db.session.add(FanSetpoint(fan_id=fan_id, pwm=min_fan_pwm, temp=min_chassis_temp))
    db.session.add(FanSetpoint(fan_id=fan_id, pwm=mid_fan_pwm, temp=mid_chassis_temp))
    db.session.add(FanSetpoint(fan_id=fan_id, pwm=max_fan_pwm, temp=max_chassis_temp))


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