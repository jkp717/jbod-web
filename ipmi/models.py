import datetime
from ipmi import db
import sqlalchemy as sa
from sqlalchemy.ext.hybrid import hybrid_property


class SysConfig(db.Model):
    __tablename__ = "sys_config"
    id = sa.Column(sa.Integer, primary_key=True)
    key = sa.Column(sa.String, unique=True)
    value = sa.Column(sa.String)
    encrypt = sa.Column(sa.Boolean, default=False)
    create_date = sa.Column(sa.DateTime, default=datetime.datetime.utcnow)
    modify_date = sa.Column(sa.DateTime, onupdate=datetime.datetime.utcnow)

    @hybrid_property
    def last_update(self):
        if self.modify_date:
            return self.modify_date
        return self.create_date


class Disk(db.Model):
    __tablename__ = "disk"
    # id = sa.Column(sa.Integer, primary_key=True)
    # Retrieved from TrueNAS Disk API
    serial = sa.Column(sa.String, primary_key=True)
    name = sa.Column(sa.String, unique=True, nullable=False)
    devname = sa.Column(sa.String, unique=True)
    model = sa.Column(sa.String)
    subsystem = sa.Column(sa.String)  #
    size = sa.Column(sa.Integer)  # in bytes
    rotationrate = sa.Column(sa.Integer)  # rpm
    type = sa.Column(sa.String)  # HDD, SDD, etc...
    bus = sa.Column(sa.String)  # SCSI, SATA, etc...
    # End of columns retrieved from TrueNAS
    phy_slot_id = sa.Column(sa.Integer, sa.ForeignKey("phy_slot.id"))
    create_date = sa.Column(sa.DateTime, default=datetime.datetime.utcnow)
    modify_date = sa.Column(sa.DateTime, onupdate=datetime.datetime.utcnow)
    disk_temps = db.relationship('DiskTemp', back_populates='disk')
    phy_slot = db.relationship('PhySlot', back_populates='disk', uselist=False)

    @hybrid_property
    def chassis_id(self):
        if self.phy_slot:
            return self.phy_slot.chassis_id
        return None

    @hybrid_property
    def last_temp_reading(self):
        return max([temp.create_date is not None for temp in self.disk_temps])

    @last_temp_reading.expression
    def last_temp_reading(cls):
        return db.select(db.func.max(DiskTemp.create_date)).\
                where(DiskTemp.disk_id == cls.id).\
                label('last_temp_reading')

    @hybrid_property
    def last_update(self):
        if self.modify_date:
            return self.modify_date
        return self.create_date


class Chassis(db.Model):
    __tablename__ = "chassis"
    id = sa.Column(sa.Integer, primary_key=True)
    name = sa.Column(sa.String, unique=True)
    slot_cnt = sa.Column(sa.Integer, nullable=False)
    psu_on = sa.Column(sa.Boolean, default=False)
    controller_id = sa.Column(sa.Integer, sa.ForeignKey("controller.id"))
    phy_slots = db.relationship('PhySlot', back_populates='chassis', cascade="all, delete-orphan")
    fans = db.relationship('Fan', back_populates='chassis', cascade="all, delete-orphan")
    controller = db.relationship('Controller', back_populates='chassis', uselist=False)
    create_date = sa.Column(sa.DateTime, default=datetime.datetime.utcnow)
    modify_date = sa.Column(sa.DateTime, onupdate=datetime.datetime.utcnow)

    @hybrid_property
    def fan_port_cnt(self):
        if self.controller:
            return self.controller.fan_port_cnt
        return 0

    @hybrid_property
    def populated_slots(self):
        return sum([slot.disk is not None for slot in self.phy_slots if slot.chassis_id == self.id])

    @populated_slots.expression
    def populated_slots(cls):
        return db.select(db.func.count(PhySlot.disk_id)).\
                where(PhySlot.chassis_id == cls.id).\
                label('populated_slots')

    @hybrid_property
    def active_fans(self):
        return sum([fan.active for fan in self.fans if fan.chassis_id == self.id])

    @active_fans.expression
    def active_fans(cls):
        return db.select(db.func.count(Fan.id)).\
                where(PhySlot.chassis_id == cls.id).\
                label('active_fans')

    @hybrid_property
    def last_update(self):
        if self.modify_date:
            return self.modify_date
        return self.create_date

    def __repr__(self):
        if not self.name:
            return f"chassis/{self.id}"
        return f"{self.name}"


class PhySlot(db.Model):
    __tablename__ = "phy_slot"
    id = sa.Column(sa.Integer, primary_key=True)
    phy_slot = sa.Column(sa.Integer, nullable=False)
    chassis_id = sa.Column(sa.Integer, sa.ForeignKey("chassis.id"))
    chassis = db.relationship('Chassis', back_populates='phy_slots')
    disk = db.relationship('Disk', back_populates='phy_slot', uselist=False)
    create_date = sa.Column(sa.DateTime, default=datetime.datetime.utcnow)
    modify_date = sa.Column(sa.DateTime, onupdate=datetime.datetime.utcnow)

    @hybrid_property
    def last_update(self):
        if self.modify_date:
            return self.modify_date
        return self.create_date

    def __repr__(self):
        return f"chassis={self.chassis} | slot={self.phy_slot}"


class Controller(db.Model):
    __tablename__ = "controller"
    id = sa.Column(sa.Integer, primary_key=True)
    mcu_device_id = sa.Column(sa.String)
    mcu_lot_id = sa.Column(sa.String)
    mcu_wafer_id = sa.Column(sa.String)
    mcu_revision_id = sa.Column(sa.String)
    firmware_version = sa.Column(sa.String)
    fan_port_cnt = sa.Column(sa.Integer, nullable=False)
    chassis = db.relationship('Chassis', back_populates='controller', uselist=False)
    __table_args__ = (
        db.UniqueConstraint(
            'mcu_device_id',
            'mcu_lot_id',
            'mcu_wafer_id',
            'mcu_revision_id',
            name='controller_device_id_uc'),
    )

    @hybrid_property
    def last_update(self):
        if self.modify_date:
            return self.modify_date
        return self.create_date

    def __repr__(self):
        return f"Controller(id={self.id}, " \
               f"mcu={self.mcu_device_id}-{self.mcu_lot_id}-{self.mcu_wafer_id}-{self.mcu_revision_id})"


class DiskTemp(db.Model):
    __tablename__ = "disk_temp"
    id = sa.Column(sa.Integer, primary_key=True)
    temp = sa.Column(sa.Integer, nullable=False)
    disk_id = sa.Column(sa.String, sa.ForeignKey("disk.serial"))
    disk = db.relationship('Disk', back_populates='disk_temps')
    create_date = sa.Column(sa.DateTime, default=datetime.datetime.utcnow)


class Fan(db.Model):
    __tablename__ = "fan"
    id = sa.Column(sa.Integer, primary_key=True)
    chassis_id = sa.Column(sa.Integer, sa.ForeignKey("chassis.id"))
    port_num = sa.Column(sa.Integer)
    pwm = sa.Column(sa.Integer, default=100)
    rpm = sa.Column(sa.Integer, default=0)
    max_rpm = sa.Column(sa.Integer)
    min_rpm = sa.Column(sa.Integer)
    four_pin = sa.Column(sa.Boolean, default=False)
    active = sa.Column(sa.Boolean, default=False)
    chassis = db.relationship('Chassis', back_populates='fans')
    setpoints = db.relationship('FanSetpoint', back_populates='fan', cascade="all, delete-orphan")
    create_date = sa.Column(sa.DateTime, default=datetime.datetime.utcnow)
    modify_date = sa.Column(sa.DateTime, onupdate=datetime.datetime.utcnow)

    @hybrid_property
    def v(self):
        if self.chassis:
            return self.chassis.controller
        return None

    @hybrid_property
    def last_update(self):
        if self.modify_date:
            return self.modify_date
        return self.create_date

    def __repr__(self):
        if self.chassis:
            return f"{self.chassis} / Fan: {self.port_num}"
        return f"Fan ID: {self.id}"


class FanSetpoint(db.Model):
    __tablename__ = "fan_setpoint"
    id = sa.Column(sa.Integer, primary_key=True)
    fan_id = sa.Column(sa.Integer, sa.ForeignKey("fan.id"))
    pwm = sa.Column(sa.Integer)
    temp = sa.Column(sa.Integer)
    fan = db.relationship('Fan', back_populates='setpoints')
    create_date = sa.Column(sa.DateTime, default=datetime.datetime.utcnow)
    modify_date = sa.Column(sa.DateTime, onupdate=datetime.datetime.utcnow)
    __table_args__ = (
        sa.UniqueConstraint('fan_id', 'pwm', name='fan_setpoint_pwm_uc'),
        sa.UniqueConstraint('fan_id', 'temp', name='fan_setpoint_temp_uc')
    )

    @hybrid_property
    def last_update(self):
        if self.modify_date:
            return self.modify_date
        return self.create_date

    def __repr__(self):
        return f"(Setpoint: {self.temp} | PWM: {self.pwm})"