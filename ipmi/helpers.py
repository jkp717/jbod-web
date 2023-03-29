import os
import math
import json
import logging
import requests
from typing import Optional, Iterable, Union
from enum import IntEnum
from dateutil import tz
from flask import Markup, current_app, Flask
from flask_admin.helpers import url_for
from datetime import datetime
from flask_admin.model import typefmt
from flask_admin.model.template import TemplateLinkRowAction

from ipmi.models import db, FanSetpoint, SysConfig, Disk, Controller, Fan, Alert
from ipmi.console import JBODRxData, JBODConsole, JBODCommand, JBODConsoleException, ResetEvent

from ipmi.config import MAX_FAN_PWM, MIN_FAN_PWM


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
            return requests.request(method, f"{_base_url}{url_path}", headers=_headers, json=data, timeout=10)
        return None


def disk_tooltip_html(model: Optional[Disk]) -> str:
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


def controller_id_formatter(view, context, model, name):
    txt = 'Master' if model.id == 1 else f'Slave({model.id-1})'
    return Markup(txt)


class FanCalibrationRowAction(TemplateLinkRowAction):
    def __init__(self):
        super(FanCalibrationRowAction, self).__init__('custom_row_actions.fan_calibration')


class ControllerAlarmRowAction(TemplateLinkRowAction):
    def __init__(self):
        super(ControllerAlarmRowAction, self).__init__('custom_row_actions.controller_alarm')


def get_model_by_id(model, id: Union[str, int], column_name: Optional[str] = 'id'):
    return db.session.query(model).where(getattr(model, column_name) == id).first()


def cascade_add_setpoints(fan_id: int):
    min_chassis_temp = int(get_config_value('min_chassis_temp'))
    max_chassis_temp = int(get_config_value('max_chassis_temp'))
    mid_fan_pwm = round((int(MIN_FAN_PWM) + int(MAX_FAN_PWM)) / 2)
    mid_chassis_temp = round((min_chassis_temp + max_chassis_temp) / 2)
    db.session.add(FanSetpoint(fan_id=fan_id, pwm=int(MIN_FAN_PWM), temp=min_chassis_temp))
    db.session.add(FanSetpoint(fan_id=fan_id, pwm=mid_fan_pwm, temp=mid_chassis_temp))
    db.session.add(FanSetpoint(fan_id=fan_id, pwm=int(MAX_FAN_PWM), temp=max_chassis_temp))


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


def _truenas_shutdown(cxt: Flask, tty: JBODConsole):
    cxt.logger.info("Shutdown request received from controller. "
                    "Attempting to shutdown host now.")
    resp = truenas_api_request(
        'POST',
        '/api/v2.0/system/shutdown',
        headers={'Content-Type': 'application/json'},
        data={"delay": 0}
    )
    if resp.status_code == 200:
        cxt.logger.info("Host confirmed shutdown request. Shutting down...")
        tty.command_write(tty.ctrlc.ACK)
    else:
        cxt.logger.info("Host shutdown request failed! Attempting to cancel shutdown...")
        # shutdown failed; send cancellation to all controllers
        for controller in db.session.query(Controller).all():
            try:
                tty.command_write(JBODCommand.CANCEL_SHUTDOWN, controller.id)
            except JBODConsoleException:
                pass


def console_callback(tty: JBODConsole, rx: JBODRxData, cxt: Flask):
    """
    Function called when data is received from controller that was not
    requested by JBODConsole write_command
    """
    with cxt.app_context():
        if rx.xoff:
            _truenas_shutdown(cxt, tty)
        elif rx.xon:
            cxt.logger.warning("ACPI ON Event received but host is already on! %s", rx)
        elif rx.dc2:
            # Device Control 2 used to broadcast controller psu, rpm, and pwm data
            # response example: {466-2038344B513050-19-1003:{psu:ON,rpm:[1000,1200,0,3000],pwm:[40,30,0,20]}}
            cxt.logger.debug("Attempting to parse rpm data: %s", rx)
            try:
                resp = json.loads(rx.data)
                ctrlr = db.session.query(Controller).where(Controller.mcu_device_id == resp['mcu']).first()

                # update psu status if needed
                if (ctrlr.psu_on == True and resp['psu'] != "ON") or (ctrlr.psu_on == False and resp['psu'] == "ON"):
                    ctrlr.psu_on = resp['psu'] == "ON"
                    cxt.logger.info("psu status for %s updated to %s", ctrlr.mcu_device_id, ctrlr.psu_on)
                    db.session.commit()

                # update fan(s) rpm and pwm values
                for i, rpm in enumerate(resp['data']['rpm']):
                    fan = db.session.query(Fan).where(Fan.controller_uuid == ctrlr.id, Fan.port_num == i+1).first()
                    if not fan:
                        cxt.logger.warning("Unable to find associated fan for rpm data: %s", rx)
                    else:
                        fan.pwm = resp['data']['pwm'][i]
                        # remove noise by only recording changes +/- 50 rpm
                        if fan.rpm in range(int(rpm)-50, int(rpm)+50):
                            fan.rpm = int(rpm)
                            fan.rpm_deviation = fan_rpm_deviation(fan)
                            cxt.logger.debug("Stored fan[%s] rpm: %s; Deviation: %s",
                                             fan.id, fan.rpm, fan.rpm_deviation)
                        db.session.commit()
            except Exception:  # noqa
                cxt.logger.error("Unable to parse controller data: %s", rx)
        elif rx.dc4:
            # misc controller event messages
            msg_type, msg_body = rx.data.split(":")[0], rx.data.split(":")[1]
            if msg_type == 'reset_event':
                rst_code = ResetEvent(int(msg_body)).name
                cxt.logger.warning("Controller reset event: %s", rst_code)
            else:
                cxt.logger.warning("Unknown ds4 event message received: %s", rx)
        else:
            cxt.logger.warning("Uncaught console event received: %s", rx)


def cascade_controller_fan(model: Controller, *args, **kwargs):
    db_fans = db.session.query(db.func.count(Fan.id)).where(Fan.controller_id == model.id).first()[0]
    if db_fans < model.fan_port_cnt:
        for i in range(db_fans, model.fan_port_cnt):
            db.session.flush()  # flush to populate autoincrement id
            f = Fan(controller_id=model.id, port_num=i+1)
            db.session.add(f)
            cascade_add_setpoints(f.id)  # create default setpoints for new fans
    elif db_fans > model.fan_port_cnt:
        del_rows = db.session.query(Fan) \
            .filter(Fan.port_num > model.fan_port_cnt, Fan.controller_id == model.id) \
            .all()
        for row in del_rows:
            db.session.delete(row)
        db.session.commit()


def fan_rpm_deviation(fan: Fan, pwm: Optional[int] = None) -> int:
    """
    Watchdog calculates expected RPM based on provided PWM.  Should be
    ran each time rpm values are provided by controller.
    @param fan: Fan Model
    @param pwm: PWM to calculate (Model pwm used if not provided)
    @return: Absolute RPM Deviation
    """
    # (pwm, rpm) = (x, y)
    # TODO: Alert user of potential issues with fan
    if fan.min_rpm and fan.max_rpm and fan.four_pin:
        pwm = fan.pwm if not pwm else pwm
        p1 = (int(MIN_FAN_PWM), fan.min_rpm)
        p2 = (int(MAX_FAN_PWM), fan.max_rpm)
        m = (p2[1] - p1[1]) / (p2[0] - p1[0])  # slope
        y = p1[1] - m * p1[0]  # y-intercept
        calc_rpm = (m * pwm) + y
        return abs(fan.rpm - calc_rpm)
    return 0


class AlertLogHandler(logging.Handler):

    def __init__(self, alert_model, app_context: Flask, db_session):
        logging.Handler.__init__(self)
        self.app_context = app_context
        self.db_session = db_session
        self.alert_model = alert_model
        self.log_msg = None

    def emit(self, record):
        # Clear the log message so it can be put to db via sql (escape quotes)
        self.log_msg = record.msg.strip().replace('\'', '\'\'')
        # Make the SQL insert
        with self.app_context.app_context():
            self.db_session.add(self.alert_model(category=record.levelname, content=self.log_msg))
            self.db_session.commit()