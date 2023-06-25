import json
import logging
import time
from datetime import datetime, timedelta
from typing import Optional, Union

from apscheduler.job import Job
from flask import current_app
from flask_apscheduler import APScheduler
from serial import SerialException, serial_for_url
from sqlalchemy.exc import IntegrityError
from requests.exceptions import ConnectionError

from webapp import utils
from webapp.console import JBODCommand, JBODConsole, JBODConsoleException, JBODRxData, ResetEvent
from webapp.jobs import events as ev
from webapp.models import db, SysConfig, Disk, DiskTemp, Chassis, Fan, FanSetpoint, Controller, \
    PhySlot, SysJob, FanLog, ComStat

scheduler = APScheduler()
_logger = logging.getLogger("apscheduler_jobs")


def activate_sys_job(job_id: Union[str, int]) -> Optional[Job]:
    with current_app.app_context():
        job = db.session.query(SysJob).where(SysJob.job_id == job_id).first()
        if not job.active:
            aps_job = scheduler.add_job(**job.job_dict)
            job.active = True
            db.session.commit()
            return aps_job
        return None


def resume_failed_job(job_id: Union[str, int]) -> Optional[Job]:
    with current_app.app_context():
        db_job = db.session.query(SysJob).where(SysJob.job_id == job_id).first()
        if db_job.paused:
            resumed_job = scheduler.resume_job(job_id)
            db_job.paused = False
            db.session.commit()
            return resumed_job
        return None


def tty_stat_tracker():
    """
    Stores tx/rx byte tracker hourly into db from memory;
    clears memory tracker
    """
    with scheduler.app.app_context():
        tty = get_console()
        if not tty:
            return None
        curr_stat = db.session.query(ComStat).filter(ComStat.stat_date == datetime.today().date()).first()
        if not curr_stat:
            curr_stat = ComStat(stat_date=datetime.today().date())
        curr_stat.rx += tty.bytes_recv
        curr_stat.tx += tty.bytes_trans
        tty.bytes_recv = 0
        tty.bytes_trans = 0
        db.session.commit()


def get_console() -> Union[JBODConsole, None]:
    with current_app.app_context():
        if not getattr(current_app, 'console', None):
            port = utils.get_config_value('console_port')
            if not port:
                # set console to None if port is not provided
                current_app.__setattr__('console', None)
                return None
            if current_app.config['SERIAL_DEBUG'] and current_app.config['SERIAL_DEBUG_FILE']:
                port = f"spy:///{port}?file={current_app.config['SERIAL_DEBUG_FILE']}"
            try:
                tty = JBODConsole(
                    serial_for_url(
                        port,
                        baudrate=int(utils.get_config_value('baud_rate')),
                        timeout=int(utils.get_config_value('console_timeout')),
                        do_not_open=True),
                    callback=console_callback,
                )
                tty.start()
                current_app.__setattr__('console', tty)
            except SerialException as err:
                # store JBODConsole instance as app attr
                current_app.__setattr__('console', None)
                current_app.logger.error(err)
    return getattr(current_app, 'console')


def console_connection_check():
    """
    Used in jinja2 templates
    """
    tty = get_console()
    if tty is not None:
        if not tty.serial.is_open:
            return False
        controllers = db.session.query(Controller).all()
        # return true if all controllers in db show 'alive'
        if controllers:
            return len(controllers) == len([c for c in controllers if c.alive])
        # No controllers are avail on initial setup so just check for open serial port
        return True
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
        resp = utils.truenas_api_request('GET', '/api/v2.0/disk')
        if not resp:
            return
        try:
            zfs_props = _query_zfs_properties()
            db_serials = [disk.serial for disk in db.session.query(Disk).all()]
            for disk in resp.json():
                disk_zfs = zfs_props.get(disk.get('name'), {})
                if disk.get('serial') in db_serials:
                    model = db.session.query(Disk).filter(Disk.serial == disk.get('serial')).first()
                    model.name = disk.get('name', None)
                    model.devname = disk.get('devname', None)
                    model.model = disk.get('model', None)
                    model.serial = disk.get('serial', None)
                    model.subsystem = disk.get('subsystem', None)
                    model.size = disk.get('size', None)
                    model.rotationrate = disk.get('rotationrate', None)
                    model.type = disk.get('type', None)
                    model.bus = disk.get('bus', None)
                    for k, v in disk_zfs.items():
                        setattr(model, k, v)
                else:
                    db.session.add(
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
                            phy_slot_id=None,
                            **disk_zfs
                        )
                    )
            db.session.commit()
        except IntegrityError:
            db.session.rollback()


def query_disk_temperatures() -> None:
    with scheduler.app.app_context():
        disks = db.session.query(Disk).all()
        if not disks:
            _logger.warning("query_disk_temperatures scheduled job skipped. No disks to query.")
            return
        try:
            resp = utils.truenas_api_request(
                'POST',
                '/api/v2.0/disk/temperatures',
                headers={'Content-Type': 'application/json'},
                data={"names": [disk.name for disk in disks], "powermode": "NEVER"}
            )
            if resp.status_code == 200:
                _logger.debug(f"query_disk_temperatures received a valid response from host; {resp}")
                for _name, temp in resp.json().items():
                    for disk in disks:
                        if disk.name == _name:
                            disk.temperature = temp
                            disk.last_temp_reading = datetime.utcnow()
                            break
                db.session.commit()
            else:
                Exception(f"query_disk_temperatures api response code != 200: {resp.status_code}")
        except ConnectionError as err:
            _logger.warning("query_disk_temperatures: unable to connect to Truenas. Is host running?")
            raise err


def _query_zfs_properties() -> dict:
    """Ran inside query disk properties job"""
    resp = utils.truenas_api_request('GET', '/api/v2.0/pool')
    if resp.status_code == 200:
        _logger.debug(f"_query_zfs_properties received a valid response from host; {resp}")
        _logger.info(f"_query_zfs_properties: retrieving ZFS properties; {resp}")
        data = resp.json()
        disks = {}
        for pool in data:
            # will return a list of attribute string paths to {type: DISK}
            # root path is zfs pool > topology > [data,log,cache,spare,special,dedup] > device
            # device path is index > children[] > device (can have multiple device hierarchies)
            for path in list(utils.json_path_generator('type', 'DISK', pool)):
                disk_props = {}
                # topology.data.0.children.0.type
                disk = utils.resolve_string_attr(pool, path)  # returns disk object
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
    _logger.error(f"_query_zfs_properties received a invalid response from host; {resp}")


def query_host_state() -> str:
    # states = ['BOOTING', 'READY', 'SHUTTING_DOWN']
    resp = utils.truenas_api_request('GET', '/api/v2.0/system/state')
    if resp:
        return resp.text
    return 'UNKNOWN'


def poll_setpoints() -> None:
    # TODO: Figure out a better way of dealing with serial race condition
    with scheduler.app.app_context():
        scheduler.pause_job('poll_controller_data')
        # give read thread loop time to clear buffer
        time.sleep(0.2)
        try:
            _poll_setpoints()
        finally:
            scheduler.resume_job('poll_controller_data')


def _poll_setpoints() -> None:
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
            _logger.warning("No chassis defined, skipping job: poll_setpoints")
            return None
        # get disk temps grouped by chassis
        for jbod in jbods:
            disks = db.session.query(Disk).join(PhySlot).filter(PhySlot.chassis_id == jbod.id).all()
            # disks = db.session.query(Disk).where(Disk.chassis_id == jbod.id).all()
            _logger.debug(f"poll_setpoints: chassis: {jbod.name or jbod.id}; Disks: {disks}")
            if not len(disks) > 0:
                _logger.warning("No disks assigned to chassis %s, %s", jbod.name, jbod.id)
                continue
            fans = db.session.query(Fan).where(
                Fan.controller_id == jbod.controller.id,
                Fan.active == True,
                Fan.four_pin == True
            ).all()
            if not fans:
                _logger.warning(f"poll_setpoints: No active pwm fans found for {jbod.name or jbod.id} chassis;")
                continue
            try:
                temp_agg = max([int(d.temperature) for d in disks if d.temperature])
            except ValueError:
                _logger.warning(f"poll_setpoints: No numeric temperature values for chassis {jbod.name or jbod.id}; "
                                f"temperature: {[d.temperature for d in disks]}")
                continue
            # get all setpoint models for each fan
            for fan in fans:
                # skip fans that are not four pin (PWM)
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
                    tty.command_write(JBODCommand.PWM, jbod.controller.id, fan.port_num, new_pwm)
                    fan.pwm = new_pwm
                    db.session.commit()
                    # write changes to db if request is successful
                    _logger.info("Fan %s setpoint updated; New PWM: %s", fan.id, new_pwm)
                else:
                    _logger.debug("Fan %s setpoint is correct; no changes made", fan.id)


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
                # using DEVICE_ID rather than PING to get mcu id from response
                dev_id = tty.command_write(JBODCommand.DEVICE_ID, next_id)
                resp.append({"id": next_id, "mcu_device_id": str(dev_id.data)})
            except JBODConsoleException as err:
                if next_id == 1:
                    current_app.logger.error(f"Controller on {tty.serial.port} did not respond to ID request. {err}")
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
            # returns a UUID of device id
            dev_id = tty.command_write(JBODCommand.DEVICE_ID, controller.id)
            controller.mcu_device_id = str(dev_id.data)

            # get total fan ports supported by controller
            fc = tty.command_write(JBODCommand.FAN_CNT, controller.id)
            try:
                controller.fan_port_cnt = int(fc.data)
            except ValueError:
                _logger.error(f"Received a non-integer value for fan_port_cnt: {fc}")
                controller.fan_port_cnt = 0

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
        filter_before = datetime.utcnow() - timedelta(days=2)
        old_temps = db.session.query(DiskTemp).filter(DiskTemp.create_date <= filter_before).all()
        old_logs = db.session.query(FanLog).filter(FanLog.create_date <= filter_before).all()
        rows = old_temps.extend(old_logs or [])
        if rows:
            for r in rows:
                db.session.delete(r)
            db.session.commit()


def poll_controller_data() -> None:
    """
    Job sends a non-blocking data request to controller through
    the console write thread. Responses are handled by the console
    callback (ds2 messages).
    """
    with scheduler.app.app_context():
        tty = get_console()
        if not tty.serial.is_open:
            raise SerialException("Serial connection not established.")
        # controller responds to DC2 requests with json-like object
        tty.transmit(tty.ctrlc.DC2)


def _poll_controller_data() -> None:
    """
    Job to verify controller(s) is responding to poll_controller_data.
    Updates controllers 'alive' to false if no response is received within
    2x of poll_controller_data scheduled runtime interval.
    """
    with scheduler.app.app_context():
        job = db.session.query(SysJob).where(SysJob.job_id == 'poll_controller_data').first()
        if job.active:
            ctrlr = db.session.query(Controller).all()
            for c in ctrlr:
                if not c.last_ds2:
                    pass
                elif c.last_ds2 >= (datetime.utcnow() - timedelta(seconds=job.seconds * 2, minutes=job.minutes * 2)):
                    c.alive = True
                else:
                    c.alive = False
            db.session.commit()


def sound_controller_alarm(controller_id: int, duration: int = 3) -> None:
    with scheduler.app.app_context():
        tty = get_console()
        if not tty:
            raise SerialException("Serial connection not established.")
        # 50 is the recommended duty cycle for piezo buzzer used
        tty.command_write(JBODCommand.ALARM, controller_id, '50')
        time.sleep(duration)
        tty.command_write(JBODCommand.ALARM, controller_id, '00')


def toggle_controller_led(controller_id: int, duration: int = 10) -> None:
    with scheduler.app.app_context():
        tty = get_console()
        if not tty:
            raise SerialException("Serial connection not established.")
        # 50 is the recommended duty cycle for piezo buzzer used
        tty.command_write(JBODCommand.LED_ON, controller_id, 2)
        time.sleep(duration)
        tty.command_write(JBODCommand.LED_OFF, controller_id, 2)


def reset_controller_mcu(controller_id: int) -> None:
    with scheduler.app.app_context():
        tty = get_console()
        if not tty:
            raise SerialException("Serial connection not established.")
        tty.command_write(JBODCommand.RESET, controller_id)


def _truenas_shutdown(tty: JBODConsole):
    with scheduler.app.app_context():
        _logger.info("Shutdown request received from controller. "
                     "Attempting to shutdown host now.")
        resp = utils.truenas_api_request(
            'POST',
            '/api/v2.0/system/shutdown',
            headers={'Content-Type': 'application/json'},
            data={"delay": 0}
        )
        if resp.status_code == 200:
            _logger.info("Host confirmed shutdown request. Shutting down...")
            tty.command_write(tty.ctrlc.ACK)
        else:
            _logger.info("Host shutdown request failed! Attempting to cancel shutdown...")
            # shutdown failed; send cancellation to all controllers
            for controller in db.session.query(Controller).all():
                try:
                    tty.command_write(JBODCommand.CANCEL_SHUTDOWN, controller.id)
                except JBODConsoleException:
                    pass


def console_callback(tty: JBODConsole, rx: JBODRxData):
    """
    Function called when data is received from controller that was not
    requested by JBODConsole write_command
    """
    with scheduler.app.app_context():
        if rx.xoff:
            _truenas_shutdown(tty)
        elif rx.xon:
            _logger.warning("ACPI ON Event received but host is already on! %s", rx)
        elif rx.dc2:
            # Device Control 2 used to broadcast controller psu, rpm, and pwm data
            # response example: {466-2038344B513050-19-1003:{psu:ON,rpm:[1000,1200,0,3000],pwm:[40,30,0,20]}}
            _logger.debug("Attempting to parse rpm data: %s", rx.raw_data)
            try:
                resp = json.loads(rx.data.strip("\r\n\x00"))
                ctrlr = db.session.query(Controller).where(Controller.mcu_device_id == resp['mcu']).first()
                data = resp['data']
                _logger.debug("Controller matched to ds2: %s", ctrlr)

                # update psu status if needed
                if (ctrlr.psu_on == True and data['psu'] != "ON") or (ctrlr.psu_on == False and data['psu'] == "ON"):
                    ctrlr.psu_on = data['psu'] == "ON"
                    _logger.info("psu status for %s updated to %s", ctrlr.mcu_device_id, ctrlr.psu_on)

                # update fan(s) rpm and pwm values
                for i, rpm in enumerate(data['rpm']):
                    fan = db.session.query(Fan).where(Fan.controller_id == ctrlr.id, Fan.port_num == i + 1).first()
                    if not fan:
                        _logger.warning("Unable to find associated fan for rpm data: %s", rx)
                    else:
                        fan.pwm = data['pwm'][i]
                        fan.rpm = int(rpm)
                        _logger.debug("Stored fan[%s] rpm: %s", fan.id, fan.rpm)
                ctrlr.last_ds2 = datetime.utcnow()
                ctrlr.alive = True
                db.session.commit()
            except Exception as err:  # noqa
                _logger.error("Unable to parse controller data: %s", rx)
                _logger.error(err)
        elif rx.dc4:
            # misc controller event messages
            msg_type, msg_body = rx.data.split(":")[0], rx.data.split(":")[1]
            if msg_type == 'reset_event':
                rst_code = ResetEvent(int(msg_body)).name
                _logger.warning("Controller reset event: %s", rst_code)
            else:
                _logger.warning("Unknown ds4 event message received: %s", rx)
        else:
            _logger.warning("Uncaught console event received: %s", rx)


def cascade_controller_fan(controller_id: int):
    """checks if a four pin fan; should be called when controller is added"""
    with scheduler.app.app_context():
        FOUR_PIN_RPM_DEVIATION = int(utils.get_config_value('four_pin_rpm_deviation'))
        MAX_FAN_PWM = int(utils.get_config_value('max_fan_pwm'))
        RPM_READ_DELAY = float(utils.get_config_value('rpm_read_delay'))
        model = utils.get_model_by_id(Controller, controller_id)
        tty = get_console()
        fans = []
        for i in range(model.fan_port_cnt):
            f = Fan(controller_id=model.id, port_num=i + 1)
            db.session.add(f)
            db.session.flush()
            fans.append(f)
        db.session.commit()
        starting_rpm = []
        for fan in fans:
            ret = tty.command_write(tty.cmd.RPM, fan.controller_id, fan.port_num)
            fan.rpm = int(ret.data)
            if int(ret.data) == 0:
                fan.active = False
                continue
            fan.active = True
            starting_rpm.append(int(ret.data))
            tty.command_write(tty.cmd.PWM, fan.controller_id, fan.port_num, MAX_FAN_PWM)
        db.session.commit()
        _logger.debug(f"cascade_fan: starting_rpm: {starting_rpm}")
        time.sleep(RPM_READ_DELAY)
        finishing_rpm = []
        for fan in fans:
            if fan.active:
                ret = tty.command_write(tty.cmd.RPM, fan.controller_id, fan.port_num)
                finishing_rpm.append(int(ret.data))
                tty.command_write(tty.cmd.PWM, fan.controller_id, fan.port_num, fan.pwm)
        rpm_delta = [abs(x - y) for x, y in list(zip(starting_rpm, finishing_rpm))]
        _logger.debug(f"cascade_fan: finishing_rpm: {finishing_rpm}; delta: {rpm_delta}")
        for i, fan in enumerate([fan for fan in fans if fan.active]):
            if rpm_delta[i] > FOUR_PIN_RPM_DEVIATION:
                fan.four_pin = True
                utils.cascade_add_setpoints(fan.id)
        db.session.commit()


def fan_calibration(fan_id: Union[int, str]) -> None:
    """
    Ran to determine fan RPM to PWM curve. Should only run
    once when a fan is installed or when the fan RPM curve
    deviates outside the allowable range.
    """
    max_wait_secs = 5  # max seconds to wait for rpm to normalize
    wait_secs = 0
    with scheduler.app.app_context():
        # get config values
        MIN_FAN_PWM = int(utils.get_config_value('min_fan_pwm'))
        MAX_FAN_PWM = int(utils.get_config_value('max_fan_pwm'))
        DEFAULT_FAN_PWM = int(utils.get_config_value('default_fan_pwm'))
        # get console
        tty = get_console()
        if not tty:
            raise SerialException("Serial connection not established.")
        fan_model = utils.get_model_by_id(Fan, fan_id)
        psu_status = tty.command_write(JBODCommand.STATUS, fan_model.controller_id)
        if psu_status.data != 'ON':
            raise Exception(f"Controller {fan_model.controller_id} psu status is {psu_status.data}; "
                            f"skipping fan_calibration.")
        # pwm fan‘s speed scales broadly linear with the duty-cycle of the PWM signal between
        # maximum speed at 100% PWM and the specified minimum speed at 20% PWM
        if not fan_model.rpm:
            r = tty.command_write(JBODCommand.RPM, fan_model.controller_id, fan_model.id)
            original_rpm = int(r.data)
        else:
            original_rpm = fan_model.rpm
        original_pwm = fan_model.pwm if MIN_FAN_PWM < fan_model.pwm < MAX_FAN_PWM else DEFAULT_FAN_PWM
        _logger.debug(f"fan_calibration: initial values; rpm={original_rpm}; pwm={original_pwm}")
        tty.command_write(JBODCommand.PWM, fan_model.controller_id, fan_model.id, MIN_FAN_PWM)
        # wait for rpm value to normalize
        r = tty.command_write(JBODCommand.RPM, fan_model.controller_id, fan_model.id)
        new_rpm = int(r.data)
        prev_rpm = 0
        while abs(new_rpm - prev_rpm) > 100 and wait_secs <= max_wait_secs:
            time.sleep(1)
            prev_rpm = new_rpm
            r = tty.command_write(JBODCommand.RPM, fan_model.controller_id, fan_model.id)
            _logger.debug(f"fan_calibration: min_pwm loop: rpm value: {int(r.data)}")
            new_rpm = int(r.data)
            wait_secs += 1
        _logger.debug(f"fan_calibration: min rpm normalization took {wait_secs} secs.  "
                      f"New min rpm value is {round(new_rpm, -2)}")
        # store new readings in min_rpm; round to the nearest 100th
        fan_model.min_rpm = round(new_rpm, -2)
        # Set fan to max PWM
        tty.command_write(JBODCommand.PWM, fan_model.controller_id, fan_model.id, MAX_FAN_PWM)
        # wait for rpm value to normalize
        time.sleep(0.5)
        r = tty.command_write(JBODCommand.RPM, fan_model.controller_id, fan_model.id)
        new_rpm = int(r.data)
        wait_secs = 0
        while abs(new_rpm - prev_rpm) > 100 and wait_secs <= max_wait_secs:
            time.sleep(1)
            prev_rpm = new_rpm
            r = tty.command_write(JBODCommand.RPM, fan_model.controller_id, fan_model.id)
            new_rpm = int(r.data)
            wait_secs += 1
        _logger.debug(f"fan_calibration: max rpm normalization took {wait_secs} secs.  "
                      f"New min rpm value is {round(new_rpm, -2)}")
        fan_model.max_rpm = round(new_rpm, -2)
        # define four pin
        if fan_model.min_rpm in range(fan_model.max_rpm - 100, fan_model.max_rpm + 100):
            fan_model.four_pin = False
        else:
            fan_model.four_pin = True
        # Set fan back to original PWM
        _logger.debug("fan_calibration: Complete! Setting fan back to initial values.")
        tty.command_write(JBODCommand.PWM, fan_model.controller_id, fan_model.id, original_pwm)
        fan_model.rpm = original_rpm
        # commit changes to db
        db.session.commit()
