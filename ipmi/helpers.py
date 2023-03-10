import math
from flask import Markup
from flask_admin.helpers import url_for
from datetime import datetime
from flask_admin.model import typefmt
from flask_admin.model.template import TemplateLinkRowAction
from ipmi import db
from ipmi.models import FanSetpoint, SysConfig


def get_config_value(config_param: str):
    return db.session.query(SysConfig.value).where(SysConfig.key == config_param).first()[0]


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
    return value.strftime('%d/%m/%Y %H:%M')


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


class PSUToggleRowAction(TemplateLinkRowAction):
    def __init__(self):
        super(PSUToggleRowAction, self).__init__('custom_row_actions.psu_toggle')


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


CONFIG_FORMATTERS = dict(typefmt.BASE_FORMATTERS)
CONFIG_FORMATTERS.update({
    bytes: byte_formatter,
    datetime: datetime_formatter
})