from flask import current_app, jsonify, request, redirect, flash, Markup
from flask_admin import expose
from flask_admin.contrib.sqla import ModelView
from flask_admin.model.template import LinkRowAction
from wtforms.widgets import PasswordInput
from serial.serialutil import SerialException

from ipmi import db
from ipmi.tasks.console import JBODConsoleAckException
from ipmi.tasks import query_disk_properties, query_controller_properties, ping_controllers
from ipmi.models import PhySlot, FanSetpoint, Fan, Chassis, Controller
from ipmi.helpers import CONFIG_FORMATTERS, disk_size_formatter, disk_link_formatter, cascade_add_setpoints, \
    psu_toggle_formatter, get_model_by_id


class JBODBaseView(ModelView):
    form_excluded_columns = ['create_date', 'modify_date']
    column_exclude_list = form_excluded_columns
    column_type_formatters = CONFIG_FORMATTERS
    named_filter_urls = True
    can_view_details = True

    def get_list_columns(self):
        return self.get_column_names(
            only_columns=self.column_list or self.scaffold_list_columns() + ['last_update'],
            excluded_columns=self.column_exclude_list,
        )


class SysConfigView(JBODBaseView):
    column_exclude_list = JBODBaseView.column_exclude_list + ['encrypt']

    def on_model_change(self, form, model, is_created):
        # encrypt API key
        if model.encrypt:
            model.value = current_app.encrypt(model.value.encode())

    def on_form_prefill(self, form, id):
        if form.encrypt.data:
            form.value.widget = PasswordInput(hide_value=True)


class FanView(JBODBaseView):
    can_create = False
    can_delete = False
    edit_template = 'fan/edit.html'
    form_excluded_columns = JBODBaseView.form_excluded_columns + [
        'setpoints', 'rpm', 'active', 'four_pin', 'port_num', 'controller'
    ]
    column_filters = ['chassis.id', 'chassis.name', 'rpm', 'active']

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
        if request.method == 'GET':
            content = request.get_json(force=True)
            sp = get_model_by_id(FanSetpoint, int(content['id']))
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
            fan_model = get_model_by_id(Fan, fid)
            fan_setpoints = db.session.query(FanSetpoint) \
                .where(FanSetpoint.fan_id == fid) \
                .order_by(FanSetpoint.temp) \
                .all()
            if not fan_model or not fan_setpoints:
                raise ValueError(f"Unable to update setpoint model for fan_id = {fid}")
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
    column_filters = ['serial', 'bus', 'type', 'size', 'phy_slot.chassis.name']
    column_list = ['serial', 'model', 'size', 'type', 'bus', 'phy_slot', 'last_update']
    column_formatters = {'size': disk_size_formatter}
    # form_excluded_columns = JBODBaseView.form_excluded_columns + ['disk_temps', ]

    @expose('/refresh')
    def refresh(self):
        try:
            query_disk_properties()
            flash('Disk properties successfully refreshed.', 'info')
        except Exception:
            flash('Failed to refresh disk properties.', 'warning')
        return redirect(self.get_url('.index_view'))


class ChassisView(JBODBaseView):
    can_view_details = True
    list_template = 'chassis/chassis_list.html'
    form_excluded_columns = JBODBaseView.form_excluded_columns + [
        'fans', 'disks', 'phy_slots', 'psu_on'
    ]
    column_list = [
        'name', 'slot_cnt', 'populated_slots', 'active_fans', 'psu_on', 'last_update'
    ]
    column_formatters = {
        'populated_slots': disk_link_formatter,
        'psu_on': psu_toggle_formatter
    }
    column_extra_row_actions = [
        LinkRowAction('mdl-fan', '/admin/fan/?flt1_chassis_chassis_id_equals={row_id}'),
    ]
    column_labels = {
        'slot_cnt': 'Disk Slots',
        'Chassis.populated_slots': 'Disks',
        'populated_slots': 'Slots In-Use',
        'psu_on': 'PSU'
    }

    @expose('/psu')
    def psu_toggle(self):
        id = request.args.get('id')
        state = request.args.get('state')
        # TODO: add func to turn off psu
        flash(f"request to turn chassis: {id} to {state}")
        return redirect(self.get_url('.index_view'))

    def after_model_change(self, form, model, is_created):
        if is_created:
            for i in range(model.slot_cnt):
                db.session.add(PhySlot(chassis_id=model.id, phy_slot=i + 1))
            for i in range(model.fan_port_cnt):
                fan = Fan(chassis_id=model.id, port_num=i + 1)
                db.session.add(fan)
                db.session.flush()  # flush to populate autoincrement id
                cascade_add_setpoints(fan.id)
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
            db_fans = db.session.query(db.func.count(Fan.id)).where(Fan.chassis_id == model.id).first()[0]
            if not getattr(model, 'controller', None):
                del_rows += db.session.query(Fan).where(Fan.chassis_id == model.id).all()
            elif db_fans < model.fan_port_cnt:
                for i in range(db_fans, model.fan_port_cnt):
                    f = Fan(chassis_id=model.id, port_num=i)
                    db.session.add(f)
                    db.session.flush()  # flush to populate autoincrement id
                    cascade_add_setpoints(f.id)
            elif db_fans > model.fan_port_cnt:
                del_rows += db.session.query(Fan) \
                    .filter(Fan.port_num > model.fan_port_cnt, Fan.chassis_id == model.id) \
                    .all()
            for row in del_rows:
                db.session.delete(row)
            db.session.commit()


class ControllerView(JBODBaseView):
    can_create = False
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

    @expose('/ping')
    def ping(self):
        ack_cnt = ping_controllers()
        db_count = db.session.query(db.func.count(Controller.id)).first()
        if db_count == ack_cnt:
            return jsonify({'result': 'ready', 'ack_count': ack_cnt, 'db_count': db_count}), 200
        return jsonify({'result': 'error', 'ack_count': ack_cnt, 'db_count': db_count}), 400

    @expose('/refresh')
    def refresh(self):
        return redirect(self.get_url('.setup'))

    @expose('/setup')
    def setup(self):
        try:
            # returns a count of acknowledgements received
            ack_cnt = ping_controllers()
        except SerialException as err:
            flash(str(err), 'error')
            return redirect(self.get_url('.index_view'))
        if not ack_cnt:
            flash('No response from controller. Check connection settings and try again.', 'error')
            return redirect(self.get_url('.index_view'))
        for i in range(ack_cnt):
            existing = db.session.query(Controller).where(Controller.id == i).first()
            c_model = existing if existing else Controller(id=i)
            try:
                c_model = query_controller_properties(c_model)
            except JBODConsoleAckException as err:
                flash(err.message, 'error')
                return redirect(self.get_url('.index_view'))
            # add new models
            if not existing:
                db.session.add(c_model)
        db.session.commit()
        flash('Controller(s) properties updated!', 'message')
        return redirect(self.get_url('.index_view'))

