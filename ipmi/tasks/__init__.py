import requests
import logging
from flask import current_app
from ipmi import scheduler, db
from ipmi.models import SysConfig, Disk, DiskTemp, Chassis, Fan, FanSetpoint, Controller
from sqlalchemy.exc import IntegrityError
from ipmi.tasks.console import JBODConsoleAckException, JBODConsole
from ipmi.helpers import get_config_value


_logger = logging.getLogger(__name__)


def initial_db_setup(config_defaults: dict) -> None:
    # populate default configuration values
    for k, v in config_defaults.items():
        try:
            db.session.add(SysConfig(key=k, value=v))
            db.session.commit()
        except IntegrityError:
            db.session.rollback()


def truenas_connection_info():
    with current_app.app_context():
        api_key = db.session.execute(
            db.select(SysConfig).where(SysConfig.key == "truenas_api_key")
        ).first()
        base_url = db.session.execute(
            db.select(SysConfig).where(SysConfig.key == "truenas_url")
        ).first()
        try:
            return {
                "api_key": current_app.decrypt(getattr(api_key[0], 'value')).decode(),
                "ip": getattr(base_url[0], 'value', None).lstrip("http:").strip("/")
            }
        except TypeError:
            return None
        except AttributeError:
            return None


def _truenas_api_request(method: str, url_path: str, headers: dict = None, data: dict = None):
    if headers is None:
        headers = {}
    with current_app.app_context():
        api_key = db.session.execute(
            db.select(SysConfig).where(SysConfig.key == "truenas_api_key")
        ).first()
        base_url = db.session.execute(
            db.select(SysConfig).where(SysConfig.key == "truenas_url")
        ).first()
        if getattr(api_key[0], 'value', None) or getattr(base_url[0], 'value', None):
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
            return requests.request(method, f"{_base_url}{url_path}", headers=_headers, json=data, timeout=2)
        return None


@scheduler.task('interval', id='query_disk_properties', hours=1)
def query_disk_properties() -> None:
    with scheduler.app.app_context():
        resp = _truenas_api_request('GET', '/api/v2.0/disk')
        for disk in resp.json():
            db.session.merge(
                Disk(
                    name=disk.get('name', None),
                    devname=disk.get('devname', None),
                    model=disk.get('model', None),
                    serial=disk.get('serial', None),
                    subsystem=disk.get('subsystem', None),
                    size=disk.get('size', None),
                    rotationrate=disk.get('rotationrate', None),
                    type=disk.get('type', None),
                    bus=disk.get('bus', None),
                )
            )
        db.session.commit()
        _logger.info("query_disk_properties scheduled job complete")


@scheduler.task('interval', id='query_disk_temperatures', seconds=30)
def query_disk_temperatures() -> None:
    with scheduler.app.app_context():
        disks = db.session.execute(db.select(Disk)).all()
        resp = _truenas_api_request(
            'POST',
            '/api/v2.0/disk/temperatures',
            headers={'Content-Type': 'application/json'},
            data={"names": [disk[0].name for disk in disks], "powermode": "NEVER"}
        )
        if resp.status_code == 200:
            for _name, temp in resp.json().items():
                db.session.add(
                    DiskTemp(**{'disk_id': d[0].serial for d in disks if d[0].name == _name}, temp=temp)
                )
            db.session.commit()
            _logger.info("query_disk_temperatures scheduled job complete")
        else:
            _logger.error(f"query_disk_temperatures api response code: {resp.status_code}")


@scheduler.task('interval', id='poll_setpoints', seconds=60)
def poll_setpoints() -> None:
    """
    Polls disk temps and sets the corresponding fan PWM setpoint.
    Writes any changes to database.
    """
    with scheduler.app.app_context():
        com = None
        jbods = db.session.query(Chassis).all()
        for jbod in jbods:
            disks = db.session.query(Disk).where(Disk.chassis_id == jbod.id).all()
            if not disks:
                _logger.warning("No disks assigned to chassis %s, %s", jbod.name, jbod.id)
                continue
            fans = db.session.query(Disk).where(Fan.chassis_id == jbod.id).all()
            temp_agg = max([int(d.last_temp_reading) for d in disks if str(d.last_temp_reading).isnumeric()])
            # get all setpoint models for each fan
            for fan in fans:
                # skip fans that are not four pin (PWM)
                if not fan.four_pin:
                    _logger.debug("Skipping setpoint polling for fan %s; Not a PWM fan.", fan.id)
                    continue
                new_pwm = None
                setpoints = db.session.query(FanSetpoint)\
                    .where(FanSetpoint.fan_id == fan.id)\
                    .order_by(FanSetpoint.temp)\
                    .all()
                # if the first setpoint is higher than the temp_agg, use it
                if setpoints[0].temp > temp_agg:
                    new_pwm = setpoints[0].pwm
                else:
                    # find setpoint best matching current temp avg
                    for i, _ in enumerate(setpoints):
                        try:
                            if setpoints[i].temp <= temp_agg <= setpoints[i + 1].temp:
                                new_pwm = setpoints[i].pwm
                                continue
                        except IndexError:
                            new_pwm = setpoints[-1].pwm

                # only send changes if value has changed
                if new_pwm != fan.pwm and new_pwm is not None:
                    # send new fan pwm to console & update model
                    if not com:
                        com = JBODConsole(
                            baudrate=int(get_config_value('baud_rate')),
                            port=get_config_value('console_port'),
                            timeout=int(get_config_value('console_timeout'))
                        )
                    resp = com.send_command(com.cmd.PWM, jbod.controller.id, fan.id, new_pwm)
                    if resp[0].encode('ASCII') != com.ctrlc.ACK.encode('ASCII'):
                        raise JBODConsoleAckException(
                            command_req=com.cmd.PWM, command_args=[jbod.controller.id, fan.id, new_pwm]
                        )
                    else:
                        fan.pwm = new_pwm
                        db.session.commit()
                    # write changes to db if request is successful
                    _logger.info("Fan %s setpoint updated; New PWM: %s", fan.id, new_pwm)
                else:
                    _logger.debug("Fan %s setpoint is correct; no changes made", fan.fan_id)
        if com:
            com.close()


@scheduler.task('interval', id='poll_fan_rpm', seconds=60)
def poll_fan_rpm() -> None:
    """
    Polls controller for fan RPMs. Writes any changes to database.
    """
    with scheduler.app.app_context():
        com = None
        jbods = db.session.query(Chassis).all()
        for jbod in jbods:
            # send each fan to controller to get rpm values
            fans = db.session.query(Disk).where(Fan.chassis_id == jbod.id).all()
            for fan in fans:
                # Keep current rpm for reference later
                _old_rpm = fan.rpm
                if not com:
                    com = JBODConsole(
                        baudrate=int(get_config_value('baud_rate')),
                        port=get_config_value('console_port'),
                        timeout=int(get_config_value('console_timeout'))
                    )
                resp = com.send_command(com.cmd.RPM, jbod.controller.id, fan.id)
                if resp[0].encode('ASCII') != com.ctrlc.ACK.encode('ASCII'):
                    raise JBODConsoleAckException(
                        command_req=com.cmd.PWM, command_args=[jbod.id, fan.id]
                    )
                fan.rpm = int(resp[1])
                # Log large changes
                if abs(fan.rpm - _old_rpm) > 100:
                    _logger.info("RPM Change: FAN %s RPM %s", fan.fan_id, fan.rpm)
                else:
                    _logger.debug("No change to RPM Values on Fan: %s", fan.fan_id)
        if com:
            com.close()


def query_host_state() -> str:
    # states = ['BOOTING', 'READY', 'SHUTTING_DOWN']
    resp = _truenas_api_request('GET', '/api/v2.0/system/state')
    return resp.text


def ping_controllers() -> int:
    """
    Sends an Enquiry request to master controller, which is propagated down to
    each attached controller.

    :return: Count of acknowledgements received (controller count)
    """
    with current_app.app_context():
        com = JBODConsole(
                baudrate=int(get_config_value('baud_rate')),
                port=get_config_value('console_port'),
                timeout=int(get_config_value('console_timeout'))
        )
        responses = []
        ack = com.send_ctrlc(com.ctrlc.ENQ)
        for a in ack:
            if a[0].encode('ASCII') != com.ctrlc.ACK.encode('ASCII'):
                com.close()
                raise JBODConsoleAckException(command_req="ENQ")
            responses.append(True)
        com.close()
        return len(responses)


def query_controller_properties(controller: Controller) -> Controller:
    with current_app.app_context():
        dev_id_mapper = {
            'id': 'mcu_device_id',
            'lot': 'mcu_lot_id',
            'waf': 'mcu_wafer_id',
            'rev': 'mcu_revision_id'
        }
        com = JBODConsole(
                baudrate=int(get_config_value('baud_rate')),
                port=get_config_value('console_port'),
                timeout=int(get_config_value('console_timeout'))
        )

        # returns a json like object with id, lot, waf, rev properties
        dev_id = com.send_command(com.cmd.DEVICE_ID, controller.id)
        if dev_id[0].encode('ASCII') != com.ctrlc.ACK.encode('ASCII'):
            com.close()
            raise JBODConsoleAckException(
                command_req=com.cmd.DEVICE_ID, command_args=[controller.id]
            )
        # assign model attrs based on mapper
        for id_type in dev_id[1].strip().strip("{}").split(","):
            setattr(controller, dev_id_mapper.get(id_type.split(':')[0]), id_type.split(':')[1])

        # get total fan ports supported by controller
        fan_cnt = com.send_command(com.cmd.FAN_CNT, controller.id)
        if fan_cnt[0].encode('ASCII') != com.ctrlc.ACK.encode('ASCII'):
            com.close()
            raise JBODConsoleAckException(
                command_req=com.cmd.FAN_CNT, command_args=[controller.id]
            )
        controller.fan_port_cnt = int(fan_cnt[1])

        # get firmware version
        fw_version = com.send_command(com.cmd.FIRMWARE_VERSION, controller.id)
        if fw_version[0].encode('ASCII') != com.ctrlc.ACK.encode('ASCII'):
            com.close()
            raise JBODConsoleAckException(
                command_req=com.cmd.FIRMWARE_VERSION, command_args=[controller.id]
            )
        controller.firmware_version = fw_version[1]

        # finally close connection
        com.close()

        # return updated model
        return controller