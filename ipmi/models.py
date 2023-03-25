import datetime
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy.ext.hybrid import hybrid_property


db = SQLAlchemy()


class Celsius(int):
    def __new__(cls, value, *args, **kwargs):
        return super(cls, cls).__new__(cls, value)

    def __str__(self):
        return "%d\N{DEGREE SIGN}" % int(self)

    def __repr__(self):
        return "%d\N{DEGREE SIGN}" % int(self)


class SysConfig(db.Model):
    __tablename__ = "sys_config"
    id = db.Column(db.Integer, primary_key=True)
    key = db.Column(db.String, unique=True)
    value = db.Column(db.String)
    encrypt = db.Column(db.Boolean, default=False)
    create_date = db.Column(db.DateTime, default=datetime.datetime.utcnow)
    modify_date = db.Column(db.DateTime, onupdate=datetime.datetime.utcnow)

    @hybrid_property
    def last_update(self):
        if self.modify_date:
            return self.modify_date
        return self.create_date


class SysJob(db.Model):
    __tablename__ = "sys_job"
    id = db.Column(db.Integer, primary_key=True)
    job_id = db.Column(db.String, unique=True)
    func = db.Column(db.String)
    args = db.Column(db.String)
    trigger = db.Column(db.String, default='interval')
    seconds = db.Column(db.Integer, default=0)
    minutes = db.Column(db.Integer, default=0)
    hours = db.Column(db.Integer, default=0)
    job_name = db.Column(db.String)
    description = db.Column(db.String)
    active = db.Column(db.Boolean, default=False)
    create_date = db.Column(db.DateTime, default=datetime.datetime.utcnow)
    modify_date = db.Column(db.DateTime, onupdate=datetime.datetime.utcnow)

    @hybrid_property
    def last_update(self):
        if self.modify_date:
            return self.modify_date
        return self.create_date

    @hybrid_property
    def job_dict(self):
        return {
            "id": self.job_id,
            "func": self.func,
            "args": self.args,
            "trigger": self.trigger,
            "seconds": self.seconds,
            "minutes": self.minutes,
            "hours": self.hours
        }


class Disk(db.Model):
    __tablename__ = "disk"
    # id = db.Column(db.Integer, primary_key=True)
    # Retrieved from TrueNAS Disk API
    serial = db.Column(db.String, primary_key=True)
    name = db.Column(db.String, unique=True, nullable=False)
    devname = db.Column(db.String, unique=True)
    model = db.Column(db.String)
    subsystem = db.Column(db.String)  #
    size = db.Column(db.Integer)  # in bytes
    rotationrate = db.Column(db.Integer)  # rpm
    type = db.Column(db.String)  # HDD, SDD, etc...
    bus = db.Column(db.String)  # SCSI, SATA, etc...
    zfs_pool = db.Column(db.String)  # zfs pool name
    zfs_topology = db.Column(db.String)  # [data,log,cache,spare,special,dedup]
    zfs_device_path = db.Column(db.String)
    read_errors = db.Column(db.Integer, default=0)
    write_errors = db.Column(db.Integer, default=0)
    checksum_errors = db.Column(db.Integer, default=0)
    # End of columns retrieved from TrueNAS
    phy_slot_id = db.Column(db.Integer, db.ForeignKey("phy_slot.id"))
    create_date = db.Column(db.DateTime, default=datetime.datetime.utcnow)
    modify_date = db.Column(db.DateTime, onupdate=datetime.datetime.utcnow)
    disk_temps = db.relationship('DiskTemp', back_populates='disk')
    phy_slot = db.relationship('PhySlot', back_populates='disk', uselist=False)

    @hybrid_property
    def chassis_id(self):
        if self.phy_slot:
            return self.phy_slot.chassis_id
        return None

    @hybrid_property
    def last_temp_reading(self):
        if self.disk_temps:
            return max([temp.create_date for temp in self.disk_temps])
        return None

    @last_temp_reading.expression
    def last_temp_reading(cls):
        return (db.select(db.func.max(DiskTemp.create_date)).
                where(DiskTemp.disk_serial == cls.serial).
                label('last_temp_reading'))

    @hybrid_property
    def temperature(self):
        if self.disk_temps:
            curr = [t.temp for t in self.disk_temps if self.last_temp_reading == t.create_date]
            try:
                return Celsius(curr[0])
            except IndexError:
                pass
        return Celsius(0)

    @temperature.expression
    def temperature(cls):
        return (db.select(DiskTemp.temp).
                filter(DiskTemp.disk_serial == cls.serial, DiskTemp.create_date == cls.last_temp_reading).
                label('temperature'))

    @hybrid_property
    def last_update(self):
        if self.modify_date:
            return self.modify_date
        return self.create_date


class DiskTemp(db.Model):
    __tablename__ = "disk_temp"
    id = db.Column(db.Integer, primary_key=True)
    temp = db.Column(db.Integer, nullable=False)
    disk_serial = db.Column(db.String, db.ForeignKey("disk.serial"))
    disk = db.relationship('Disk', back_populates='disk_temps')
    create_date = db.Column(db.DateTime, default=datetime.datetime.utcnow)

    def __repr__(self):
        return f"{self.temp}"


class Chassis(db.Model):
    __tablename__ = "chassis"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String, unique=True)
    slot_cnt = db.Column(db.Integer, nullable=False)
    controller_id = db.Column(db.Integer, db.ForeignKey("controller.id"))
    phy_slots = db.relationship('PhySlot', back_populates='chassis', cascade="all, delete-orphan")
    controller = db.relationship('Controller', back_populates='chassis', uselist=False)
    create_date = db.Column(db.DateTime, default=datetime.datetime.utcnow)
    modify_date = db.Column(db.DateTime, onupdate=datetime.datetime.utcnow)

    @hybrid_property
    def psu_on(self):
        if self.controller:
            return getattr(self.controller, 'psu_on')
        return None

    @hybrid_property
    def fans(self):
        if self.controller:
            return getattr(self.controller, 'fans')
        return []

    @hybrid_property
    def disks(self):
        if self.phy_slots:
            return [slot.disk for slot in self.phy_slots if slot.disk is not None]
        return []

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
        if self.fans:
            return sum([fan.active for fan in self.fans])

    @active_fans.expression
    def active_fans(cls):
        return db.select(db.func.count(Fan.id)).\
                where(Fan.controller_id == cls.controller_id, Fan.active == True). \
                label('active_fans')

    @hybrid_property
    def avg_disk_temp(self):
        if self.phy_slots:
            disks = [slot.disk for slot in self.phy_slots if slot.disk is not None]
            if disks:
                return sum([disk.temperature for disk in disks]) / len(disks)
        return 0

    @avg_disk_temp.expression
    def avg_disk_temp(cls):
        return db.select(db.func.avg(Disk.temperature)).\
                where(Disk.chassis_id == cls.id).\
                label('avg_disk_temp')

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
    id = db.Column(db.Integer, primary_key=True)
    phy_slot = db.Column(db.Integer, nullable=False)
    chassis_id = db.Column(db.Integer, db.ForeignKey("chassis.id"))
    chassis = db.relationship('Chassis', back_populates='phy_slots')
    disk = db.relationship('Disk', back_populates='phy_slot', uselist=False)
    create_date = db.Column(db.DateTime, default=datetime.datetime.utcnow)
    modify_date = db.Column(db.DateTime, onupdate=datetime.datetime.utcnow)

    @hybrid_property
    def last_update(self):
        if self.modify_date:
            return self.modify_date
        return self.create_date

    def __repr__(self):
        return f"chassis={self.chassis} | slot={self.phy_slot}"


class Controller(db.Model):
    __tablename__ = "controller"
    id = db.Column(db.Integer, primary_key=True)
    mcu_device_id = db.Column(db.String)
    mcu_lot_id = db.Column(db.String)
    mcu_wafer_id = db.Column(db.String)
    mcu_revision_id = db.Column(db.String)
    firmware_version = db.Column(db.String)
    fan_port_cnt = db.Column(db.Integer)
    psu_on = db.Column(db.Boolean, default=False)
    alive = db.Column(db.Boolean, default=False)
    fans = db.relationship('Fan', back_populates='controller', cascade="all, delete-orphan")
    chassis = db.relationship('Chassis', back_populates='controller', uselist=False)
    create_date = db.Column(db.DateTime, default=datetime.datetime.utcnow)
    modify_date = db.Column(db.DateTime, onupdate=datetime.datetime.utcnow)
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

    @hybrid_property
    def uuid(self):
        return f"{self.mcu_device_id}-{self.mcu_lot_id}-{self.mcu_wafer_id}-{self.mcu_revision_id}"

    def __repr__(self):
        return f"Controller(id={self.id}, " \
               f"mcu={self.mcu_device_id}-{self.mcu_lot_id}-{self.mcu_wafer_id}-{self.mcu_revision_id})"


class Fan(db.Model):
    __tablename__ = "fan"
    id = db.Column(db.Integer, primary_key=True)
    controller_id = db.Column(db.Integer, db.ForeignKey("controller.id"))
    port_num = db.Column(db.Integer)
    pwm = db.Column(db.Integer, default=100)
    rpm = db.Column(db.Integer, default=0)
    max_rpm = db.Column(db.Integer)
    min_rpm = db.Column(db.Integer)
    four_pin = db.Column(db.Boolean, default=False)
    active = db.Column(db.Boolean, default=False)
    calibration_job_uuid = db.Column(db.String)
    calibration_status = db.Column(db.Integer)
    controller = db.relationship('Controller', back_populates='fans', uselist=False)
    setpoints = db.relationship('FanSetpoint', back_populates='fan', cascade="all, delete-orphan")
    create_date = db.Column(db.DateTime, default=datetime.datetime.utcnow)
    modify_date = db.Column(db.DateTime, onupdate=datetime.datetime.utcnow)

    @hybrid_property
    def last_update(self):
        if self.modify_date:
            return self.modify_date
        return self.create_date

    def __repr__(self):
        return f"Fan ID: {self.id}"


class FanSetpoint(db.Model):
    __tablename__ = "fan_setpoint"
    id = db.Column(db.Integer, primary_key=True)
    fan_id = db.Column(db.Integer, db.ForeignKey("fan.id"))
    pwm = db.Column(db.Integer)
    temp = db.Column(db.Integer)
    fan = db.relationship('Fan', back_populates='setpoints')
    create_date = db.Column(db.DateTime, default=datetime.datetime.utcnow)
    modify_date = db.Column(db.DateTime, onupdate=datetime.datetime.utcnow)
    __table_args__ = (
        db.UniqueConstraint('fan_id', 'pwm', name='fan_setpoint_pwm_uc'),
        db.UniqueConstraint('fan_id', 'temp', name='fan_setpoint_temp_uc')
    )

    @hybrid_property
    def last_update(self):
        if self.modify_date:
            return self.modify_date
        return self.create_date

    def __repr__(self):
        return f"(Setpoint: {self.temp} | PWM: {self.pwm})"