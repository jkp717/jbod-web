import uuid

import serial
from apscheduler.events import EVENT_JOB_EXECUTED, EVENT_JOB_ERROR
from flask import current_app, jsonify, request, redirect, flash, Markup
from flask_admin import expose, BaseView
from flask_admin.contrib.sqla import ModelView
from flask_admin.form import rules, FormOpts
from flask_admin.helpers import get_redirect_target
from flask_admin.model.base import get_mdict_item_or_list
from flask_admin.model.template import LinkRowAction
from requests.exceptions import MissingSchema
from serial.tools.list_ports import comports
from wtforms.widgets import PasswordInput
from sqlalchemy.exc import IntegrityError

from webapp import utils
from webapp.console import JBODConsoleException
from webapp.jobs import scheduler, query_disk_properties, query_controller_properties, \
    truenas_connection_info, get_console, ping_controllers, console_connection_check, activate_sys_job
from webapp.jobs.events import fan_calibration_job_listener
from webapp.models import db, PhySlot, FanSetpoint, Fan, Controller, SysConfig, Chassis, SysJob, Alert


class JBODBaseView(ModelView):
    form_excluded_columns = ['create_date', 'modify_date']
    column_exclude_list = form_excluded_columns
    column_type_formatters = utils.get_config_formatters()
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
            'chassis': utils.get_model_by_id(Chassis, 1) is not None,
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
    can_create = False
    can_delete = False
    can_edit = True
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
    can_create = True
    can_delete = False
    can_edit = False  # handled by a custom row action
    list_template = 'list.html'
    edit_template = 'fan/edit.html'
    column_list = ['controller', 'port_num', 'description', 'pwm', 'rpm', 'four_pin', 'active', 'logs']
    column_editable_list = ['description', 'pwm']
    form_excluded_columns = JBODBaseView.form_excluded_columns + [
        'setpoints', 'rpm', 'active', 'four_pin', 'port_num', 'controller', 'min_rpm', 'max_rpm',
        'calibration_job_uuid', 'calibration_status', 'pwm', 'logs'
    ]
    form_rules = [rules.Header('Fan Setpoints')]
    column_display_actions = [utils.EditSetpointsRowAction(), utils.FanCalibrationRowAction()]
    column_filters = ['controller.id', 'controller.chassis', 'controller.chassis.id', 'rpm', 'active']
    column_details_list = [
        'pwm', 'rpm', 'active', 'four_pin', 'port_num', 'controller', 'controller.chassis', 'min_rpm', 'max_rpm',
        'calibration_job_uuid', 'calibration_status', 'logs'
    ]
    column_formatters = {'logs': utils.fan_log_formatter}
    column_extra_row_actions = [utils.EditSetpointsRowAction(), utils.FanCalibrationRowAction()]

    def on_model_change(self, form, model, is_created):
        if not is_created and form.__contains__('pwm'):
            tty = get_console()
            if tty:
                try:
                    tty.command_write(tty.cmd.PWM, model.controller_id, model.port_num, model.pwm)
                    current_app.logger.info(f"Manually adjusting PWM value on fan {model.id} to {model.pwm}")
                except JBODConsoleException as err:
                    current_app.logger.error(f"Error while changing pwm value: {err}")
            else:
                current_app.logger.error("Cannot adjust PWM; no controllers are currently connected!")

    @expose('/edit', methods=['GET'])
    def edit_view(self):
        """Override of the builtin edit_view"""
        return_url = get_redirect_target() or self.get_url('.index_view')
        id = get_mdict_item_or_list(request.args, 'id')
        if id is None:
            return redirect(return_url)
        model = self.get_one(id)
        if model is None:
            flash('Record does not exist.', 'error')
            return redirect(return_url)
        form = self.edit_form(obj=model)
        if not hasattr(form, '_validated_ruleset') or not form._validated_ruleset:  # noqa
            self._validate_form_instance(ruleset=self._form_edit_rules, form=form)
        if self.validate_form(form):
            if self.update_model(form, model):
                flash('Record was successfully saved.', 'success')
                if '_add_another' in request.form:
                    return redirect(self.get_url('.create_view', url=return_url))
                elif '_continue_editing' in request.form:
                    return redirect(self.get_url('.edit_view', id=self.get_pk_value(model)))
                else:
                    # save button
                    return redirect(self.get_save_return_url(model, is_created=False))
        if request.method == 'GET' or form.errors:
            self.on_form_prefill(form, id)
        form_opts = FormOpts(widget_args=self.form_widget_args,form_rules=self._form_edit_rules)
        if self.edit_modal and request.args.get('modal'):
            template = self.edit_modal_template
        else:
            template = self.edit_template
        return self.render(template, model=model, form=form, form_opts=form_opts, return_url=return_url)

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
        fan = utils.get_model_by_id(Fan, int(fan_id))
        fan.calibration_job_uuid = str(job_uuid)
        fan.calibration_status = utils.StatusFlag.RUNNING
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
                "status": utils.StatusFlag.ERROR,
                "message": "Fan ID required"
            }), 400
        fan = utils.get_model_by_id(Fan, fan_id)
        if fan.calibration_status == utils.StatusFlag.RUNNING:
            return jsonify({
                "status": utils.StatusFlag.RUNNING,
                "message": "Warning: Calibration is taking longer than expected to complete."
            })
        if fan.calibration_status == utils.StatusFlag.COMPLETE:
            # add default setpoints to PWM Fans
            return jsonify({
                "status": utils.StatusFlag.COMPLETE,
                "message": Markup(f"""Fan calibration complete! 
                    <a href="{self.get_url('.index_view')}" class="alert-link">Reload </a>to see results.
            """)
            })
        if fan.calibration_status == utils.StatusFlag.FAIL:
            return jsonify({
                "status": utils.StatusFlag.FAIL,
                "message": "Fan Calibration failed! Check the logs and try again."
            })
        return jsonify({
            "status": int(utils.StatusFlag.UNKNOWN),
            "message": "Oops! Fan calibration status is unknown."
        })

    @expose('/setpoints', methods=['POST', 'GET'])
    def data(self):
        if request.method == 'POST':
            content = request.get_json(force=True)
            for sp in content:
                try:
                    existing_model = db.session.query(FanSetpoint).where(FanSetpoint.temp == sp['temp']).first()
                    if sp['pwm'] != existing_model.pwm:
                        existing_model.pwm = sp['pwm']
                    db.session.commit()
                except IntegrityError:
                    db.session.rollback()
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
            sp = utils.get_model_by_id(FanSetpoint, int(content['id']))
            db.session.delete(sp)
            db.session.commit()
            return jsonify({'result': 'success'}), 200
        fid = request.args.get('fan_id')
        setpoints = db.session.query(FanSetpoint) \
            .where(FanSetpoint.fan_id == fid) \
            .order_by(FanSetpoint.temp) \
            .all()
        return self.render('fan/delete_setpoint.html', setpoints=setpoints)

    @expose('/copy', methods=['GET', 'POST'])
    def copy_existing(self):
        if request.method == 'POST':
            content = request.get_json(force=True)
            setpoints = db.session.query(FanSetpoint)\
                .where(FanSetpoint.fan_id == int(content['copy_id']))\
                .all()
            for sp in setpoints:
                try:
                    utils.clone_model(sp, fan_id=int(content['id']))
                except IntegrityError:
                    db.session.rollback()
            return jsonify({'result': 'success'}), 200
        fan_id = request.args.get('fan_id')
        if not fan_id:
            return 'fan_id required!', 400
        fans = db.session.query(Fan).where(Fan.setpoints != None, Fan.id != int(fan_id)).all() # noqa
        return self.render('fan/copy_setpoint.html', fans=fans)

    def on_model_change(self, form, model, is_created):
        if 'fan_id' in request.args.keys():
            fid = request.args.get('fan_id')
            fan_model = utils.get_model_by_id(Fan, fid)
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


class FanLogView(JBODBaseView):
    can_create = False
    can_edit = False
    can_delete = True
    can_export = True
    can_view_details = False
    column_filters = ['fan.id', 'last_update']
    column_sortable_list = ['last_update']
    column_list = ['last_update', 'old_pwm']
    column_labels = {'last_update': 'Date', 'old_pwm': 'Change Desc.'}
    column_formatters = {'old_pwm': utils.pwm_change_formatter}


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
    column_formatters = {'size': utils.disk_size_formatter}

    # form_excluded_columns = JBODBaseView.form_excluded_columns + ['disk_temps', ]

    def get_empty_list_message(self):
        return Markup(f"<a href={self.get_url('.refresh')}>Request disk data from Host</a>")

    @expose('/refresh')
    def refresh(self):
        try:
            query_disk_properties()
            # activate supporting jobs
            jobs = [
                activate_sys_job('database_cleanup'),
                activate_sys_job('query_disk_temperatures'),
                activate_sys_job('query_disk_properties')
            ]
            flash('Disk properties successfully refreshed.', 'info')
            if jobs:
                flash('Scheduled jobs where activated.', 'info')
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
        'populated_slots': utils.disk_link_formatter,
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


class PhySlotView(JBODBaseView):
    can_view_details = True
    can_edit = True
    can_create = False
    can_delete = False
    column_list = ['phy_slot', 'chassis', 'disk', 'last_update']

    def get_empty_list_message(self):
        return Markup(f"<a href={self.get_url('chassis.index_view')}>Add a New Chassis</a>")


class ControllerView(JBODBaseView):
    can_create = False
    # can_create = True
    can_edit = False
    refresh_view = '.broadcast'
    list_template = 'refresh_list.html'
    column_list = ['id', 'mcu_device_id', 'firmware_version', 'fan_port_cnt', 'psu_on', 'alive']
    column_formatters = {'id': utils.controller_id_formatter}
    column_extra_row_actions = [utils.ControllerAlarmRowAction(), utils.ControllerLEDRowAction()]

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
            ca = utils.get_model_by_id(Controller, alive_id)
            ca.alive = True
        # update dead controllers
        for old_id in dead_c:
            cd = utils.get_model_by_id(Controller, old_id)
            if request.args.get('action') == 'delete':
                db.session.delete(cd)
            else:
                cd.alive = False
        db.session.commit()
        if request.args.get('action') == 'add':
            new_models = []
            for new_id in new_c:
                c_model = query_controller_properties(Controller(id=int(new_id)))
                if not c_model.alive:
                    flash("Oops! There was a problem communicating with controller. Try again.")
                    return redirect(self.get_url('.index_view')), 500
                db.session.add(c_model)
                # setup job to test connected fans & populate db
                db.session.flush()
                new_models.append(c_model)
            db.session.commit()
            for mdl in new_models:
                job_uuid = uuid.uuid4()
                cascade_fan_job = {
                    "id": str(job_uuid),
                    "name": "cascade_controller_fan",
                    "func": "webapp.jobs:cascade_controller_fan",
                    "replace_existing": False,
                    "args": (mdl.id,),
                    # omit trigger to run immediately
                }
                scheduler.add_job(**cascade_fan_job)
            # Turn on controller data polling job (if not already)
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
        alert = utils.get_model_by_id(Alert, int(alert_id))
        if not alert:
            return jsonify({"result": "error", "msg": f"alert {alert_id} not found."}), 400
        db.session.delete(alert)
        db.session.commit()
        return jsonify({"result": "success"}), 200
