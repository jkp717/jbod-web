import time
import json
from typing import Optional, Union
from flask import current_app
from sqlalchemy.sql import text
from flask_apscheduler import APScheduler
from serial import SerialException, serial_for_url
from ipmi.models import db, SysConfig, Disk, DiskTemp, Chassis, Fan, FanSetpoint, Controller, PhySlot
from ipmi.console import JBODCommand, JBODConsole, JBODConsoleException
from ipmi import helpers
from ipmi.jobs import events as ev
from ipmi.config import MIN_FAN_PWM, MAX_FAN_PWM


scheduler = APScheduler()


def get_console() -> Union[JBODConsole, None]:
    with current_app.app_context():
        if not getattr(current_app, 'console', None):
            port = helpers.get_config_value('console_port')
            if not port:
                # set console to None if port is not provided
                current_app.__setattr__('console', None)
                return None
            try:
                tty = JBODConsole(
                    serial_for_url(
                        port,
                        baudrate=int(helpers.get_config_value('baud_rate')),
                        timeout=int(helpers.get_config_value('console_timeout')),
                        do_not_open=True),
                    callback=helpers.console_callback,
                    cxt=current_app
                )
                tty.start()
                current_app.__setattr__('console', tty)
            except SerialException as err:
                # store JBODConsole instance as app attr
                current_app.__setattr__('console', None)
                current_app.logger.error(err)
    return getattr(current_app, 'console', None)


def console_connection_check():
    tty = get_console()
    if tty is not None:
        return tty.serial.is_open
    return False


def truenas_connection_info():
    """
    Used in jinja2 templates
    """
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


def query_disk_properties() -> None:
    with scheduler.app.app_context():
        resp = helpers.truenas_api_request('GET', '/api/v2.0/disk')
        if resp:
            zfs_props = _query_zfs_properties()
            for disk in resp.json():
                disk_zfs = zfs_props.get(disk.get('name'), {})
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
                        **disk_zfs
                    )
                )
            db.session.commit()


def query_disk_temperatures() -> None:
    with scheduler.app.app_context():
        disks = db.session.query(Disk).all()
        if not disks:
            raise Exception("query_disk_temperatures scheduled job skipped. No disks to query.")
        resp = helpers.truenas_api_request(
            'POST',
            '/api/v2.0/disk/temperatures',
            headers={'Content-Type': 'application/json'},
            data={"names": [disk.name for disk in disks], "powermode": "NEVER"}
        )
        if resp.status_code == 200:
            for _name, temp in resp.json().items():
                db.session.add(
                    DiskTemp(**{'disk_serial': d.serial for d in disks if d.name == _name}, temp=temp)
                )
            db.session.commit()
        else:
            Exception(f"query_disk_temperatures api response code != 200: {resp.status_code}")


def _query_zfs_properties() -> dict:
    """Ran inside query disk properties job"""
    resp = helpers.truenas_api_request('GET', '/api/v2.0/pool')
    if resp.status_code == 200:
        data = resp.json()
        disks = {}
        for pool in data:
            # will return a list of attribute string paths to {type: DISK}
            # root path is zfs pool > topology > [data,log,cache,spare,special,dedup] > device
            # device path is index > children[] > device (can have multiple device hierarchies)
            for path in list(helpers.json_path_generator('type', 'DISK', pool)):
                disk_props = {}
                # topology.data.0.children.0.type
                disk = helpers.resolve_string_attr(pool, path)  # returns disk object
                disk_props['zfs_pool'] = pool['name']
                disk_props['zfs_topology'] = path.split('.')[1]
                disk_props['zfs_device_path'] = disk['path']
                # stats
                disk_props['read_errors'] = disk['stats']['read_errors']
                disk_props['write_errors'] = disk['stats']['write_errors']
                disk_props['checksum_errors'] = disk['stats']['checksum_errors']
                # disk name
                disks[disk['disk']] = disk_props
        return disks


def query_host_state() -> str:
    # states = ['BOOTING', 'READY', 'SHUTTING_DOWN']
    resp = helpers.truenas_api_request('GET', '/api/v2.0/system/state')
    if resp:
        return resp.text
    return 'UNKNOWN'


def poll_setpoints() -> None:
    """
    Polls disk temps and sets the corresponding fan PWM setpoint.
    Writes any changes to database.
    """
    with scheduler.app.app_context():
        tty = get_console()
        if not tty:
            raise SerialException("Serial connection not established.")
        jbods = db.session.query(Chassis).where(Chassis.controller_id != None).all()  # noqa
        # stop process if no chassis is defined
        if not jbods:
            scheduler.app.logger.warning("No chassis defined, skipping job: poll_setpoints")
            return None
        # get disk temps grouped by chassis
        for jbod in jbods:
            disks = db.session.query(Disk).join(PhySlot).filter(PhySlot.chassis_id == jbod.id).all()
            # disks = db.session.query(Disk).where(Disk.chassis_id == jbod.id).all()
            if not disks:
                scheduler.app.logger.warning("No disks assigned to chassis %s, %s", jbod.name, jbod.id)
                continue
            fans = db.session.query(Fan).where(Fan.controller_id == jbod.controller.id).all()
            temp_agg = max([int(d.last_temp_reading) for d in disks if str(d.last_temp_reading).isnumeric()])
            # get all setpoint models for each fan
            for fan in fans:
                # skip fans that are not four pin (PWM)
                if not fan.four_pin:
                    scheduler.app.logger.debug("Skipping setpoint polling for fan %s; Not a PWM fan.", fan.id)
                    continue
                new_pwm = None
                setpoints = db.session.query(FanSetpoint) \
                    .where(FanSetpoint.fan_id == fan.id) \
                    .order_by(FanSetpoint.temp) \
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
                    new_pwm = tty.command_write(JBODCommand.PWM, jbod.controller.id, fan.id, new_pwm)
                    fan.pwm = int(new_pwm.data)
                    db.session.commit()
                    # write changes to db if request is successful
                    scheduler.app.logger.info("Fan %s setpoint updated; New PWM: %s", fan.id, new_pwm)
                else:
                    scheduler.app.logger.debug("Fan %s setpoint is correct; no changes made", fan.fan_id)


def ping_controllers(controller_id: Optional[int] = None) -> Union[list[dict], list[None]]:
    """
    Loop increments id calling DEVICE_ID command until either NAK or no response
    @param controller_id: will only ping this controller if provided
    @return: list of responding device ids
    """
    with current_app.app_context():
        resp = []
        next_id = controller_id or 1
        tty = get_console()
        while True:
            # continue to loop until either NAK or no response
            try:
                dev_id = tty.command_write(JBODCommand.DEVICE_ID, next_id)
                resp.append({"id": next_id, "mcu_device_id": str(dev_id.data)})
            except JBODConsoleException:
                if next_id == 1:
                    current_app.logger.error(f"Controller on {tty.serial.port} did not respond to ID request.")
                break
            if next_id == controller_id:
                break
            next_id += 1
        return resp


def query_controller_properties(controller: Controller) -> Controller:
    with scheduler.app.app_context():
        tty = get_console()
        if not tty:
            raise SerialException("Serial connection not established.")
        try:
            # returns a json like object with id, lot, waf, rev properties
            dev_id = tty.command_write(JBODCommand.DEVICE_ID, controller.id)
            controller.mcu_device_id = str(dev_id.data)

            # get total fan ports supported by controller
            fc = tty.command_write(JBODCommand.FAN_CNT, controller.id)
            controller.fan_port_cnt = int(fc.data)

            # get firmware version
            fw = tty.command_write(JBODCommand.FIRMWARE_VERSION, controller.id)
            controller.firmware_version = fw.data

            # get PSU status
            psu = tty.command_write(JBODCommand.STATUS, controller.id)
            controller.psu_on = psu.data == 'ON'

            controller.alive = True
        except JBODConsoleException:
            controller.alive = False
        # return updated model
        return controller


def database_cleanup():
    with scheduler.app.app_context():
        db.session.excute(
            text("""DELETE FROM disk_temp WHERE create_date < now() - INTERVAL 2 DAY;""")
        )
        db.session.commit()


def poll_controller_data() -> None:
    """
    Job sends a non-blocking data request to controller through
    the console write thread. Responses are handled by the console
    callback (ds2 messages).
    """
    with scheduler.app.app_context():
        with scheduler.app.app_context():
            tty = get_console()
            if not tty.serial.is_open:
                raise SerialException("Serial connection not established.")
            # controller responds to DC2 requests with json-like object
            tty.transmit(tty.ctrlc.DC2)


def fan_calibration(fan_id: int) -> None:
    """
    Ran to determine fan RPM to PWM curve. Should only run
    once when a fan is installed or when the fan RPM curve
    deviates outside the allowable range. Returns updated
    Fan model.
    """
    max_wait_secs = 5  # max seconds to wait for rpm to normalize
    wait_secs = 0
    with scheduler.app.app_context():
        tty = get_console()
        if not tty:
            raise SerialException("Serial connection not established.")
        fan_model = helpers.get_model_by_id(Fan, fan_id)
        psu_status = tty.command_write(JBODCommand.STATUS, fan_model.controller_id)
        if psu_status.data != 'ON':
            raise Exception(f"Controller {fan_model.controller_id} psu status is {psu_status.data}; "
                            f"skipping fan_calibration.")
        # pwm fan‘s speed scales broadly linear with the duty-cycle of the PWM signal between
        # maximum speed at 100% PWM and the specified minimum speed at 20% PWM
        if not fan_model.pwm:
            r = tty.command_write(JBODCommand.RPM, fan_model.controller_id, fan_model.id)
            original_rpm = int(r.data)
        else:
            original_rpm = fan_model.rpm
        original_pwm = fan_model.pwm if fan_model.pwm < MAX_FAN_PWM else MAX_FAN_PWM
        tty.command_write(JBODCommand.PWM, fan_model.controller_id, fan_model.id, MIN_FAN_PWM)
        # wait for rpm value to normalize
        r = tty.command_write(JBODCommand.RPM, fan_model.controller_id, fan_model.id)
        new_rpm = int(r.data)
        prev_rpm = 0
        while abs(new_rpm - prev_rpm) > 100 or wait_secs >= max_wait_secs:
            time.sleep(1)
            prev_rpm = new_rpm
            r = tty.command_write(JBODCommand.RPM, fan_model.controller_id, fan_model.id)
            new_rpm = int(r.data)
            wait_secs += 1
        # store new readings in min_rpm; round to the nearest 100th
        fan_model.min_rpm = round(new_rpm, -2)
        # Set fan to max PWM
        tty.command_write(JBODCommand.PWM, fan_model.controller_id, fan_model.id, MAX_FAN_PWM)
        # wait for rpm value to normalize
        time.sleep(0.5)
        r = tty.command_write(JBODCommand.RPM, fan_model.controller_id, fan_model.id)
        new_rpm = int(r.data)
        wait_secs = 0
        while abs(new_rpm - prev_rpm) > 100 or wait_secs >= max_wait_secs:
            time.sleep(1)
            prev_rpm = new_rpm
            new_rpm = int(tty.command_write(JBODCommand.RPM, fan_model.controller_id, fan_model.id).data)
            wait_secs += 1
        fan_model.max_rpm = round(new_rpm, -2)
        # define four pin
        if fan_model.min_rpm in range(fan_model.max_rpm-100, fan_model.max_rpm+100):
            fan_model.four_pin = False
        else:
            fan_model.four_pin = True
        # Set fan back to original PWM
        tty.command_write(JBODCommand.PWM, fan_model.controller_id, fan_model.id, original_pwm)
        fan_model.rpm = original_rpm
        # commit changes to db
        db.session.commit()


def test_serial_job() -> None:
    with current_app.app_context():
        print("writing test started")
        tty = get_console()
        if not tty:
            raise SerialException("Serial connection not established.")
        resp = tty.command_write(JBODCommand.DEVICE_ID, 1)
        print(resp)


def test_fan_job(fan_id) -> None:
    with scheduler.app.app_context():
        from random import randint
        time.sleep(randint(7, 10))
        scheduler.app.logger.debug(f"test_fan_job ran! fan_id: {fan_id}")

