import uuid
import serial
from flask import current_app, jsonify, request, redirect, flash, Markup
from flask_admin import expose, BaseView
from flask_admin.form import rules
from flask_admin.contrib.sqla import ModelView
from flask_admin.model.template import LinkRowAction

from apscheduler.events import EVENT_JOB_EXECUTED, EVENT_JOB_ERROR
from wtforms.widgets import PasswordInput
from requests.exceptions import MissingSchema
from serial.tools.list_ports import comports

from webapp import helpers
from webapp.models import db, PhySlot, FanSetpoint, Fan, Controller, SysConfig, Chassis, SysJob, Alert
from webapp.jobs import scheduler, query_disk_properties, query_controller_properties, \
    truenas_connection_info, get_console, ping_controllers, console_connection_check, activate_sys_job
from webapp.jobs.events import fan_calibration_job_listener


class JBODBaseView(ModelView):
    form_excluded_columns = ['create_date', 'modify_date']
    column_exclude_list = form_excluded_columns
    column_type_formatters = helpers.get_config_formatters()
    named_filter_urls = True
    can_view_details = True

    def get_list_columns(self):
        return self.get_column_names(
            only_columns=self.column_list or self.scaffold_list_columns() + ['last_update'],
            excluded_columns=self.column_exclude_list,
        )


class IndexView(BaseView):

    @expose('/')
    def index(self):
        setup_required = {
            'truenas': truenas_connection_info() is not None,
            'controller': console_connection_check(),
            'chassis': helpers.get_model_by_id(Chassis, 1) is not None,
            'jobs': db.session.query(SysJob).where(SysJob.active == False).all() is None,  # noqa
        }
        jbods = db.session.query(Chassis).where(Chassis.controller_id is not None).all()  # noqa
        return self.render(
            'index.html',
            setup_complete=not any(setup_required),
            setup_required=setup_required,
            jbods=jbods,
            disk_tooltip=['name', 'serial', 'temperature']
        )


class NewSetupView(BaseView):
    @expose('/')
    def index(self):
        # placeholder to prevent Flask-Admin error
        return 404

    @expose('/truenas', methods=['POST'])
    def truenas(self):
        """Truenas initial setup"""
        if request.method == 'POST':
            content = request.get_json(force=True)
            for k, v in content.items():
                model = db.session.query(SysConfig).where(SysConfig.key == k).first()
                model.value = v if not model.encrypt else current_app.encrypt(v.encode())
            db.session.commit()
            return jsonify({"result": "success", "msg": "Connection successful."}), 200
        return jsonify({"result": "error", "msg": "method not allowed"}), 405

    @expose('/controller', methods=['GET', 'POST'])
    def controller(self):
        """Controller(s) initial setup"""
        if request.method == 'POST':
            content = request.get_json(force=True)
            for k, v in content.items():
                model = db.session.query(SysConfig).where(SysConfig.key == k).first()
                # handle changes to serial port and baudrate
                if k in ['console_port', 'baud_rate']:
                    tty = get_console()
                    if tty:
                        if v is None:
                            tty.close()
                            model.value = None
                        elif k == 'console_port':
                            try:
                                tty.change_port(v)
                            except serial.SerialException:
                                model.value = None
                                db.session.commit()
                                return jsonify({
                                    "result": "error",
                                    "msg": "Error occurred while attempting to change serial port. Please check"
                                           "port provided is accessible to program."
                                }), 400
                        elif k == 'baud_rate':
                            try:
                                tty.change_baudrate(int(v))
                            except serial.SerialException:
                                model.value = None
                                db.session.commit()
                                return jsonify({
                                    "result": "error",
                                    "msg": "Error occurred while attempting to change serial baudrate"
                                }), 400
                model.value = v if not model.encrypt else current_app.encrypt(v.encode())
            db.session.commit()
            if console_connection_check():
                return jsonify({"result": "success", "msg": "Successfully established serial connection."}), 200
            return jsonify({"result": "error", "msg": "Unable to establish connection with serial controller."}), 400
        if request.method == 'GET':
            # return a list of usable COM ports
            coms = [com.device for com in comports()]
            if current_app.config['TESTING']:
                coms.append('loop://')
            return jsonify({"avail_ports": coms}), 200

    def is_visible(self):
        return False


class SysConfigView(JBODBaseView):
    column_exclude_list = JBODBaseView.column_exclude_list + ['encrypt']
    column_editable_list = ['value', 'encrypt']

    def on_model_change(self, form, model, is_created):
        # encrypt API key
        if model.encrypt:
            model.value = current_app.encrypt(model.value.encode())

    def after_model_change(self, form, model, is_created):
        # update console if params change
        if model.key in ['console_port', 'baud_rate']:
            tty = get_console()
            if tty:
                if model.value is None:
                    tty.close()
                elif model.key == 'console_port':
                    tty.change_port(model.value)
                elif model.key == 'baud_rate':
                    tty.change_baudrate(model.value)

    def on_form_prefill(self, form, id):
        if form.encrypt.data:
            form.value.widget = PasswordInput(hide_value=True)


class FanView(JBODBaseView):
    can_create = False
    can_delete = False
    list_template = 'list.html'
    edit_template = 'fan/edit.html'
    column_exclude_list = JBODBaseView.column_exclude_list + [
        'calibration_job_uuid', 'calibration_status'
    ]
    column_editable_list = ['description', 'pwm']
    form_excluded_columns = JBODBaseView.form_excluded_columns + [
        'setpoints', 'rpm', 'active', 'four_pin', 'port_num', 'controller', 'min_rpm', 'max_rpm',
        'calibration_job_uuid', 'calibration_status', 'pwm', 'rpm_deviation'
    ]
    form_rules = [rules.Header('Fan Setpoints')]
    column_filters = ['controller.id', 'controller.chassis', 'controller.chassis.id', 'rpm', 'active']
    column_extra_row_actions = [helpers.FanCalibrationRowAction()]

    def on_model_change(self, form, model, is_created):
        old_model = helpers.get_model_by_id(Fan, model.id)
        # check for changing PWM value
        if old_model.pwm != model.pwm:
            tty = get_console()
            if tty:
                tty.command_write(tty.cmd.PWM, Fan.controller_id, model.pwm)
                current_app.logger.info(f"Manually adjusting PWM value on fan {model.id} to {model.pwm}")
            else:
                current_app.logger.error("Cannot adjust PWM; no controllers are currently connected!")
                model.pwm = old_model.pwm
                db.session.commit()

    @expose('/calibrate/', methods=['GET'])
    def calibrate(self):
        fan_id = int(request.args.get('id'))
        if not fan_id:
            return jsonify({'result': 'error', 'message': 'fan id required'}), 400
        job_uuid = uuid.uuid4()
        calibration_job = {
            "id": str(job_uuid),
            "name": "fan_calibration",
            "func": "webapp.jobs:fan_calibration",
            "replace_existing": True,
            "args": (fan_id,),
            # omit trigger to run immediately
        }
        fan = helpers.get_model_by_id(Fan, int(fan_id))
        fan.calibration_job_uuid = str(job_uuid)
        fan.calibration_status = helpers.StatusFlag.RUNNING
        db.session.commit()
        scheduler.add_job(**calibration_job)
        # fan_calibration_job_listener removes itself once job is complete
        scheduler.add_listener(fan_calibration_job_listener, EVENT_JOB_EXECUTED | EVENT_JOB_ERROR)
        return jsonify({'result': 'running', 'job': calibration_job}), 200

    @expose('/calibrate/status', methods=['GET'])
    def calibrate_status(self):
        fan_id = int(request.args.get('id'))
        if not fan_id:
            return jsonify({
                "status": helpers.StatusFlag.ERROR,
                "message": "Fan ID required"
            }), 400
        fan = helpers.get_model_by_id(Fan, fan_id)
        if fan.calibration_status == helpers.StatusFlag.RUNNING:
            return jsonify({
                "status": helpers.StatusFlag.RUNNING,
                "message": "Warning: Calibration is taking longer than expected to complete."
            })
        if fan.calibration_status == helpers.StatusFlag.COMPLETE:
            # add default setpoints to PWM Fans
            if fan.four_pin:
                helpers.cascade_add_setpoints(fan_id)
            return jsonify({
                "status": helpers.StatusFlag.COMPLETE,
                "message": Markup(f"""Fan calibration complete! 
                    <a href="{self.get_url('.index_view')}" class="alert-link">Reload </a>to see results.
            """)
            })
        if fan.calibration_status == helpers.StatusFlag.FAIL:
            return jsonify({
                "status": helpers.StatusFlag.FAIL,
                "message": "Fan Calibration failed! Check the logs and try again."
            })
        return jsonify({
            "status": int(helpers.StatusFlag.UNKNOWN),
            "message": "Oops! Fan calibration status is unknown."
        })

    @expose('/setpoints', methods=['POST', 'GET'])
    def data(self):
        if request.method == 'POST':
            content = request.get_json(force=True)
            for sp in content:
                existing_model = db.session.query(FanSetpoint).where(FanSetpoint.temp == sp['temp']).first()
                if sp['pwm'] != existing_model.pwm:
                    existing_model.pwm = sp['pwm']
            db.session.commit()
            return jsonify({"result": "success"}), 200
        fid = request.args.get('fan_id')
        if not fid:
            return jsonify({"error": "fan_id required"}), 400
        setpoints = db.session.query(FanSetpoint) \
            .where(FanSetpoint.fan_id == fid) \
            .order_by(FanSetpoint.temp) \
            .all()
        resp = {'pwm': [], 'temp': []}
        for sp in setpoints:
            resp['pwm'].append(sp.pwm)
            resp['temp'].append(sp.temp)
        return jsonify(resp)


class SetpointView(JBODBaseView):
    form_excluded_columns = JBODBaseView.form_excluded_columns + ['fan', 'pwm']
    create_template = 'fan/modal.html'

    def is_visible(self):
        return False

    @expose('/data')
    def data(self):
        fid = request.args.get('fan_id')
        if not fid:
            return "fan_id required", 400
        setpoints = db.session.query(FanSetpoint) \
            .where(FanSetpoint.fan_id == fid) \
            .order_by(FanSetpoint.temp) \
            .all()
        resp = {'pwm': [], 'temp': []}
        for sp in setpoints:
            resp['pwm'].append(sp.pwm)
            resp['temp'].append(sp.temp)
        return jsonify(resp)

    @expose('/delete', methods=['GET', 'POST'])
    def delete_one(self):
        if request.method == 'POST':
            content = request.get_json(force=True)
            sp = helpers.get_model_by_id(FanSetpoint, int(content['id']))
            db.session.delete(sp)
            db.session.commit()
            return jsonify({'result': 'success'}), 200
        fid = request.args.get('fan_id')
        setpoints = db.session.query(FanSetpoint) \
            .where(FanSetpoint.fan_id == fid) \
            .order_by(FanSetpoint.temp) \
            .all()
        return self.render('fan/delete_setpoint.html', setpoints=setpoints)

    def on_model_change(self, form, model, is_created):
        if 'fan_id' in request.args.keys():
            fid = request.args.get('fan_id')
            fan_model = helpers.get_model_by_id(Fan, fid)
            fan_setpoints = db.session.query(FanSetpoint) \
                .where(FanSetpoint.fan_id == fid) \
                .order_by(FanSetpoint.temp) \
                .all()
            if not fan_model or not fan_setpoints:
                current_app.logger.error(f"Unable to update setpoint model for fan_id = {fid}")
                raise ValueError(f"Unable to update setpoint model for fan_id = {fid}")
            else:
                model.pwm = max(sp.pwm for sp in fan_setpoints if sp.temp < model.temp) + 1 \
                    or min(sp.pwm for sp in fan_setpoints) + 1
                model.fan_id = fid
                db.session.add(model)
                db.session.commit()


class DiskView(JBODBaseView):
    can_view_details = True
    can_create = False
    can_delete = False
    can_export = True
    refresh_view = '.refresh'
    list_template = 'refresh_list.html'
    # column_editable_list = ['phy_slot']
    form_columns = ['phy_slot']
    column_editable_list = ['phy_slot']
    column_filters = ['serial', 'bus', 'type', 'size', 'phy_slot.chassis.name', 'zfs_pool']
    column_list = [
        'serial', 'zfs_pool', 'model', 'size', 'type', 'bus', 'phy_slot', 'temperature',
        'last_temp_reading', 'last_update'
    ]
    column_formatters = {'size': helpers.disk_size_formatter}
    # form_excluded_columns = JBODBaseView.form_excluded_columns + ['disk_temps', ]

    def get_empty_list_message(self):
        return Markup(f"<a href={self.get_url('.refresh')}>Request disk data from Host</a>")

    @expose('/refresh')
    def refresh(self):
        try:
            query_disk_properties()
            flash('Disk properties successfully refreshed.', 'info')
        except MissingSchema:
            flash('Disk properties failed to refresh! '
                  'Missing url schema; add http:// or https:// to TrueNAS url.', 'error')
        except Exception as err:
            current_app.logger.error('DiskView.refresh: Failed to refresh disk properties.')
            flash('Failed to refresh disk properties.', 'error')
            if current_app.config['DEBUG']:
                raise err
        return redirect(self.get_url('.index_view'))


class ChassisView(JBODBaseView):
    can_view_details = True
    list_template = 'list.html'
    form_excluded_columns = JBODBaseView.form_excluded_columns + [
        'fans', 'disks', 'phy_slots', 'psu_on'
    ]
    column_list = [
        'name', 'slot_cnt', 'populated_slots', 'active_fans', 'psu_on', 'last_update'
    ]
    column_formatters = {
        'populated_slots': helpers.disk_link_formatter,
    }
    column_extra_row_actions = [
        LinkRowAction('mdl-fan', '/fan/?flt1_controller_chassis_id_equals={row_id}'),
    ]
    column_labels = {
        'slot_cnt': 'Disk Slots',
        'Chassis.populated_slots': 'Disks',
        'populated_slots': 'Slots In-Use',
        'psu_on': 'PSU'
    }

    def after_model_change(self, form, model, is_created):
        if is_created:
            for i in range(model.slot_cnt):
                db.session.add(PhySlot(chassis_id=model.id, phy_slot=i + 1))
            db.session.commit()
        else:
            del_rows = []
            db_slots = db.session.query(db.func.count(PhySlot.id)).where(PhySlot.chassis_id == model.id).first()[0]
            if db_slots < model.slot_cnt:
                for i in range(db_slots, model.slot_cnt):
                    db.session.add(PhySlot(chassis_id=model.id, phy_slot=i))
            elif db_slots > model.slot_cnt:
                del_rows += db.session.query(PhySlot) \
                    .filter(PhySlot.phy_slot > model.slot_cnt, PhySlot.chassis_id == model.id) \
                    .all()
            for row in del_rows:
                db.session.delete(row)
            db.session.commit()


class ControllerView(JBODBaseView):
    can_create = False
    # can_create = True
    can_edit = False
    refresh_view = '.broadcast'
    list_template = 'refresh_list.html'
    column_list = ['id', 'mcu_device_id', 'firmware_version', 'fan_port_cnt', 'psu_on', 'alive']
    column_formatters = {'id': helpers.controller_id_formatter}
    column_extra_row_actions = [helpers.ControllerAlarmRowAction(), helpers.ControllerLEDRowAction()]

    def get_empty_list_message(self):
        return Markup(f"<a href={self.get_url('.broadcast')}>Search for connected Controllers</a>")

    def on_model_delete(self, model):
        if model.chassis:
            fans = db.session.query(Fan) \
                .where(Fan.controller_id == model.id) \
                .all()
            for row in fans:
                db.session.delete(row)
            db.session.commit()

    def on_model_change(self, form, model, is_created):
        helpers.cascade_controller_fan(model, form, is_created)

    @expose('/identify/<controller_id>', methods=['GET'])
    def identify(self, controller_id):
        """
        Called by row action; schedules job to toggle
        LED on for 3 seconds.
        """
        job_uuid = uuid.uuid4()
        identify_job = {
            "id": str(job_uuid),
            "name": "toggle_controller_led",
            "func": "webapp.jobs:toggle_controller_led",
            "replace_existing": True,
            "args": (controller_id,),
            # omit trigger to run immediately
        }
        scheduler.add_job(**identify_job)
        flash(f"Chassis LED active on  (Controller: {controller_id})...")
        return redirect(self.get_url('.index_view'))

    @expose('/alarm/<controller_id>', methods=['GET'])
    def alarm(self, controller_id):
        """
        Called by row action; schedules job to toggle
        alarm on for 3 seconds.
        """
        job_uuid = uuid.uuid4()
        sound_alarm_job = {
            "id": str(job_uuid),
            "name": "sound_controller_alarm",
            "func": "webapp.jobs:sound_controller_alarm",
            "replace_existing": True,
            "args": (controller_id,),
            # omit trigger to run immediately
        }
        scheduler.add_job(**sound_alarm_job)
        flash(f"Triggering alarm sound on controller {controller_id}...")
        return redirect(self.get_url('.index_view'))

    @expose('/alive', methods=['GET'])
    def alive(self):
        """
        Simple route to return True/False if controllers are alive
        """
        return jsonify({'alive': console_connection_check()})

    @expose('/broadcast', methods=['GET'])
    def broadcast(self):
        """
        Broadcasts out to serial port looking for new controllers.
        Returns either json or HTML based on request type.
        """
        if not request.args.get('id'):
            controllers = db.session.query(Controller).all()
            # send out ping to controller group (looks for new controllers)
            responses = ping_controllers()
        else:
            controllers = db.session.query(Controller).where(Controller.id == int(request.args.get('id'))).all()
            responses = ping_controllers(int(request.args.get('id')))
        db_ids = [c.id for c in controllers] if controllers else []
        r_ids = [r['id'] for r in responses] if responses else []
        # tests
        new_c = set(r_ids).difference(set(db_ids))
        dead_c = set(db_ids).difference(set(r_ids))
        ack_c = set(db_ids).intersection(set(r_ids))
        # update existing alive controllers
        for alive_id in ack_c:
            ca = helpers.get_model_by_id(Controller, alive_id)
            ca.alive = True
        # update dead controllers
        for old_id in dead_c:
            cd = helpers.get_model_by_id(Controller, old_id)
            if request.args.get('action') == 'delete':
                db.session.delete(cd)
                helpers.cascade_controller_fan(cd)
            else:
                cd.alive = False
        db.session.commit()
        if request.args.get('action') == 'add':
            for new_id in new_c:
                c_model = query_controller_properties(Controller(id=int(new_id)))
                if not c_model.alive:
                    flash("Oops! There was a problem communicating with controller. Try again.")
                    return redirect(self.get_url('.index_view')), 500
                db.session.add(c_model)
                helpers.cascade_controller_fan(c_model)
                # Turn on controller data polling job (if not already)
            db.session.commit()
            activate_sys_job('poll_controller_data')
        if request.is_json:
            return jsonify({'result': 'ready', 'controllers': {
                'acknowledged': ack_c, 'dead': dead_c, 'new': new_c}}), 200
        # handle HTML request
        flash_msg = ""
        if len(new_c) > 0:
            flash_msg += f"{len(new_c)} new controller(s) found! "
        if len(dead_c) > 0:
            flash_msg += f"{len(dead_c)} controller(s) are not responding to ping. "
        if request.args.get('action'):
            if len(new_c) > 0 or len(dead_c) > 0:
                flash_msg += "Database updated."
        elif len(new_c) > 0:
            flash_msg += Markup(f"""
            <a href="{self.get_url('.broadcast', action='add')}" class="alert-link">Add new controller?</a>
            """)
        elif len(dead_c) > 0:
            flash_msg += Markup(f"""
            <a href="{self.get_url('.broadcast', action='delete')}" class="alert-link">Remove controller?</a>
            """)
        else:
            flash_msg += f"{len(ack_c)} controllers responded. No changes needed."
        flash(flash_msg)
        return redirect(self.get_url('.index_view'))


class TaskView(JBODBaseView):
    can_create = False
    can_delete = False
    can_edit = True
    column_list = ['job_name', 'active', 'seconds', 'minutes', 'hours', 'description', 'last_update']
    column_editable_list = ['active', 'seconds', 'minutes', 'hours']

    def on_model_change(self, form, model, is_created):
        job = scheduler.get_job(model.job_id)
        if not job and model.active:
            scheduler.add_job(**model.job_dict)
        elif job and not model.active:
            scheduler.remove_job(model.job_id)


class AlertView(JBODBaseView):

    def is_visible(self):
        return False

    @expose('/delete/<alert_id>', methods=['POST', 'DELETE'])
    def delete_alert(self, alert_id):
        if str(alert_id).upper() == 'ALL':
            alerts = db.session.query(Alert).all()
            for alert in alerts:
                db.session.delete(alert)
            db.session.commit()
            return jsonify({"result": "success"}), 200
        alert = helpers.get_model_by_id(Alert, int(alert_id))
        if not alert:
            return jsonify({"result": "error", "msg": f"alert {alert_id} not found."}), 400
        db.session.delete(alert)
        db.session.commit()
        return jsonify({"result": "success"}), 200
