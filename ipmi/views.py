import uuid

from flask import current_app, jsonify, request, redirect, flash, Markup
from flask_admin import expose, BaseView
from flask_admin.form import rules
from flask_admin.contrib.sqla import ModelView
from flask_admin.model.template import LinkRowAction

from apscheduler.events import EVENT_JOB_EXECUTED, EVENT_JOB_ERROR
from wtforms.widgets import PasswordInput
from requests.exceptions import MissingSchema
from serial.tools.list_ports import comports

from ipmi import helpers
from ipmi.models import db, PhySlot, FanSetpoint, Fan, Controller, SysConfig, Chassis, SysJob
from ipmi.jobs.console import JBODConsoleAckException
from ipmi.jobs import scheduler, query_disk_properties, query_controller_properties, ping_controllers, \
    truenas_connection_info, get_console
from ipmi.jobs.events import fan_calibration_job_listener


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
            'controller': get_console() is not None,
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
        if request.method == 'POST':
            content = request.get_json(force=True)
            for k, v in content.items():
                model = db.session.query(SysConfig).where(SysConfig.key == k).first()
                model.value = v if not model.encrypt else current_app.encrypt(v.encode())
            db.session.commit()
            return jsonify({"result": "success"}), 200
        return jsonify({"result": "method not allowed"}), 405

    @expose('/controller', methods=['GET', 'POST'])
    def controller(self):
        if request.method == 'POST':
            content = request.get_json(force=True)
            for k, v in content.items():
                model = db.session.query(SysConfig).where(SysConfig.key == k).first()
                model.value = v if not model.encrypt else current_app.encrypt(v.encode())
            db.session.commit()
            return jsonify({"result": "success"}), 200
        if request.method == 'GET':
            # return a list of usable COM ports
            return jsonify({"avail_ports": [com.device for com in comports()]}), 200

    def is_visible(self):
        return False


# class TestView(BaseView):
#     @expose('/')
#     def index(self):
#         print("writing test started")
#         tty = get_console()
#         tty.transmit(b'\x13\x00test message\x00\r\n')
#         return "Sent..."


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
                    current_app.__setattr__('console', None)
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
    form_excluded_columns = JBODBaseView.form_excluded_columns + [
        'setpoints', 'rpm', 'active', 'four_pin', 'port_num', 'controller', 'min_rpm', 'max_rpm',
        'calibration_job_uuid', 'calibration_status', 'pwm'
    ]
    form_rules = [rules.Header('Fan Setpoints')]
    column_filters = ['controller.id', 'controller.chassis', 'controller.chassis.id', 'rpm', 'active']
    column_extra_row_actions = [helpers.FanCalibrationRowAction()]

    @expose('/calibrate/', methods=['GET'])
    def calibrate(self):
        fan_id = int(request.args.get('id'))
        if not fan_id:
            return jsonify({'result': 'error', 'message': 'fan id required'}), 400
        # fan = fan_calibration(int(request.args.get('id')))
        # db.session.commit()
        # calibration_job = {
        #     "id": "fan_calibration",
        #     "func": "ipmi.jobs:fan_calibration",
        #     "args": (int(request.args.get('id')),),
        #     "trigger": "date",  # triggers once on the given datetime (immediately if no run_date).
        # }
        job_uuid = uuid.uuid4()
        calibration_job = {
            "id": str(job_uuid),
            "name": "fan_calibration",
            "func": "ipmi.jobs:test_fan_job",
            "replace_existing": True,
            "args": (fan_id,),
            "trigger": "date",  # triggers once on the given datetime (immediately if no run_date).
            "run_date": None
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
            return jsonify({
                "status": helpers.StatusFlag.COMPLETE,
                "message": "Fan calibration complete! Reload page to see results."
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
    list_template = 'refresh_list.html'
    # column_editable_list = ['phy_slot']
    form_columns = ['phy_slot']
    column_filters = ['serial', 'bus', 'type', 'size', 'phy_slot.chassis.name', 'zfs_pool']
    column_list = [
        'serial', 'zfs_pool', 'model', 'size', 'type', 'bus', 'phy_slot', 'temperature',
        'last_temp_reading', 'last_update'
    ]
    column_formatters = {'size': helpers.disk_size_formatter}
    # form_excluded_columns = JBODBaseView.form_excluded_columns + ['disk_temps', ]

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
    # can_create = False
    can_create = True
    can_edit = False
    list_template = 'refresh_list.html'

    def get_empty_list_message(self):
        return Markup(f"<a href={self.get_url('.setup')}>Search for connected Controllers</a>")

    def on_model_delete(self, model):
        if model.chassis:
            fans = db.session.query(Fan) \
                .filter(Fan.chassis_id == model.chassis.id) \
                .all()
            for row in fans:
                db.session.delete(row)
            db.session.commit()

    def on_model_change(self, form, model, is_created):
        db_fans = db.session.query(db.func.count(Fan.id)).where(Fan.controller_id == model.id).first()[0]
        if db_fans < model.fan_port_cnt:
            for i in range(db_fans, model.fan_port_cnt):
                f = Fan(controller_id=model.id, port_num=i)
                db.session.add(f)
                db.session.flush()  # flush to populate autoincrement id
                helpers.cascade_add_setpoints(f.id)  # create default setpoints for new fans
        elif db_fans > model.fan_port_cnt:
            del_rows = db.session.query(Fan) \
                .filter(Fan.port_num > model.fan_port_cnt, Fan.controller_id == model.id) \
                .all()
            for row in del_rows:
                db.session.delete(row)
            db.session.commit()

    @expose('/ping')
    def ping(self):
        ack_cnt = ping_controllers()
        if not ack_cnt:
            return jsonify({'result': 'error', 'msg': 'Controller(s) did not respond to ping.'}), 200
        db_count = db.session.query(db.func.count(Controller.id)).first()
        if db_count == ack_cnt:
            return jsonify({'result': 'ready', 'ack_count': ack_cnt, 'db_count': db_count}), 200
        return jsonify({'result': 'error', 'ack_count': ack_cnt, 'db_count': db_count}), 400

    @expose('/refresh')
    def refresh(self):
        return redirect(self.get_url('.setup'))

    @expose('/setup')
    def setup(self):
        ack_cnt = ping_controllers()
        if not ack_cnt:
            flash('No response from controller(s). Check connection settings and try again.', 'error')
            return redirect(self.get_url('.index_view'))
        for i in range(ack_cnt):
            existing = db.session.query(Controller).where(Controller.id == i+1).first()
            c_model = existing if existing else Controller(id=i+1)
            try:
                # Get mcu UUID, firmware version, and supported fan count
                c_model = query_controller_properties(c_model)
            except JBODConsoleAckException as err:
                db.session.rollback()
                flash(err.message, 'error')
                return redirect(self.get_url('.index_view'))
            # add new models
            if not existing:
                db.session.add(c_model)
        db.session.commit()
        flash('Controller(s) properties updated!', 'message')
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