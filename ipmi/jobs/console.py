import threading
import serial
from typing import Optional, Union
import time
from enum import Enum


class JBODConsoleAckException(Exception):
    """
    NAK acknowledgement received exception
    """

    def __init__(
            self,
            message: str = None,
            command_req: str = None,
            command_args: Optional[list] = None,
            response: Optional[Union[bytes, str]] = None,
    ):
        self.cmd_str, self.arg_str = "", ""
        if command_req:
            self.cmd_str = f"; Command sent [{command_req}]"
        if command_args:
            self.arg_str = f"; Args {command_args}"
        if response:
            self.response = f"; Response {response}"
        if not message:
            message = "Command not acknowledged by JBOD Controller"
        self.message = f"{message}{command_req}{command_args}{response}"
        super().__init__(self.message)


class JBODCommand(Enum):
    """
    Defined jbod controller commands
    """
    SHUTDOWN = "jbod/? psu shutdown"  # shutdown power supply
    STARTUP = "jbod/? psu startup"  # turn-on power supply
    CANCEL_SHUTDOWN = "jbod/? psu cancel"  # cancel pending PSU shutdown
    STATUS = "jbod/? psu status"  # PSU power state
    RESET = "jbod/? reset"  # hard reset of MCU
    PWM = "jbod/? pwm fan/? ?"  # Set new PWM Value 1-99
    RPM = "jbod/? rpm fan/?"  # Get current RPM
    ALARM = "jbod/? alarm ?"  # Turn on buzzer alarm
    FAN_CNT = "jbod/? fans"  # Get count of fans supported
    DEVICE_ID = "jbod/? id"
    FIRMWARE_VERSION = "jbod/? version"


class JBODControlCharacter(Enum):
    """
    Defined ASCII Control Characters
    """
    # ASCII Control Characters
    ENQ = "\x05"   # Enquiry
    ACK = "\x06"   # Acknowledge
    LF = "\x0A"    # Linefeed (Newline)
    CR = "\x0D"    # Carriage Return
    NAK = "\x15"   # Negative Ack
    XOFF = "\x13"  # Device Control (XOFF)
    XON = "\x11"  # Device Control (XON)


class JBODRxData:
    ENCODING = "ASCII"

    def __init__(self, data: bytes):
        self._raw_data = data
        self._ack = False
        self._xon = False
        self._xoff = False
        self._data = None
        # ACK & NAK ascii character as bytes
        self._ackc = str(JBODControlCharacter.ACK.value).encode(self.ENCODING)
        self._nakc = str(JBODControlCharacter.NAK.value).encode(self.ENCODING)
        # XON & XOFF ascii character as bytes
        self._xonc = str(JBODControlCharacter.XON.value).encode(self.ENCODING)
        self._xoffc = str(JBODControlCharacter.XOFF.value).encode(self.ENCODING)

        self._parse_data(data)

    @property
    def raw_data(self):
        return self._raw_data

    @property
    def ack(self):
        return self._ack

    @ack.setter
    def ack(self, val: bytes):
        self._ack = (self._ackc == val)

    @property
    def xon(self):
        return self._xon

    @xon.setter
    def xon(self, val: bytes):
        self._xon = (self._xonc == val)

    @property
    def xoff(self):
        return self._xoff

    @xoff.setter
    def xoff(self, val: bytes):
        self._xoff = (self._xoffc == val)

    @property
    def data(self):
        return self._data

    def _parse_data(self, data: bytes):
        d = tuple(filter(None, data.decode(self.ENCODING).strip('\r\n').split('\x00')))
        for prop in ['ack', 'xon', 'xoff']:
            self.__setattr__(prop, d[0].encode(self.ENCODING))
        try:
            self._data = d[1].encode(self.ENCODING)
        except IndexError:
            self._data = None

    def __repr__(self):
        return f"JBODRxData(ack={self.ack},xon={self.xon},xoff={self.xoff},data={self.data},raw_data={self.raw_data})"


class JBODConsole:
    TERMINATOR = b'\r\n'
    ENCODING = 'ASCII'
    NEW_RX_DATA = False
    NEW_TX_DATA = False

    def __init__(self, serial_instance, rx_callback: Optional[callable] = None):
        self.cmd = JBODCommand
        self.ctrlc = JBODControlCharacter
        self.serial = serial_instance
        self.alive = False
        self.receiver_thread = None
        self.transmitter_thread = None
        self._reader_alive = False
        self._rx_buffer = None
        self._tx_buffer = None
        self._rx_callback = rx_callback
        self._data_received = bytearray()
        self._lock = threading.Lock()
        self.serial.open()

    def _start_reader(self):
        """Start reader thread"""
        self._reader_alive = True
        # start serial->console thread
        self.receiver_thread = threading.Thread(target=self.reader, name='rx')
        self.receiver_thread.daemon = True
        self.receiver_thread.start()
        print(f"Hello from receiver thread {self.receiver_thread}")

    def _stop_reader(self):
        """Stop reader thread only, wait for clean exit of thread"""
        self._reader_alive = False
        if hasattr(self.serial, 'cancel_read'):
            self.serial.cancel_read()
        self.receiver_thread.join()

    def start(self):
        """start worker threads"""
        self.alive = True
        self._start_reader()
        # enter console->serial loop
        self.transmitter_thread = threading.Thread(target=self.writer, name='tx')
        self.transmitter_thread.daemon = True
        self.transmitter_thread.start()
        print(f"Hello from transmitter thread {self.transmitter_thread}")

    def stop(self):
        """set flag to stop worker threads"""
        self.alive = False

    def join(self, transmit_only=False):
        """wait for worker threads to terminate"""
        self.transmitter_thread.join()
        if not transmit_only:
            if hasattr(self.serial, 'cancel_read'):
                self.serial.cancel_read()
            self.receiver_thread.join()

    def close(self):
        self.alive = False
        self.serial.close()

    def reader(self):
        """loop and process received data"""
        try:
            while self.alive and self._reader_alive:
                # read all that is there or wait for one byte
                data = self.serial.read(self.serial.in_waiting or 1)
                if data:
                    self._data_received.extend(data)
                    if self.TERMINATOR in self._data_received:
                        # sets NEW_RX_DATA flag
                        self.rx_buffer = bytes(self._data_received)
                        # reset bytearray
                        self._data_received = bytearray()
                        print("Data received and ready to read!")
                        retries = 0
                        # give time to process rx data (1 sec max)
                        while retries < 10 and self.NEW_RX_DATA:
                            time.sleep(0.1)
                            retries += 1
                        # passing to callback if data has not been processed
                        if self._rx_callback and self.NEW_RX_DATA:
                            # will clear NEW_RX_DATA flag
                            self._rx_callback(JBODRxData(bytes(self.rx_buffer)))
                time.sleep(0.01)
        except serial.SerialException as err:
            self.alive = False
            raise err

    def writer(self):
        """loop write (thread safe)"""
        try:
            while self.alive:
                if self.NEW_TX_DATA:
                    with self._lock:
                        print(f"writing from writer thread {self.tx_buffer}")
                        self.serial.write(self.tx_buffer)
                        self.tx_buffer = None  # clear buffer once transmitted
                time.sleep(0.01)
        except Exception as err:
            self.alive = False
            raise err

    @staticmethod
    def _command_format(command: JBODCommand, cmd_vars=None):
        """
        Method for replacing command wildcards ('?') with variables provided
        Just return unchanged command if no vars provided
        """
        cmd = command.value
        if cmd_vars:
            for i in cmd_vars:
                cmd = cmd.replace("?", str(i), 1)
        return cmd

    def command_write(self, command: Union[JBODCommand, JBODControlCharacter], *args) -> JBODRxData:
        """Blocking write command and return JBODRxData"""
        self.flush_buffers()
        if isinstance(command, JBODCommand):
            command = self._command_format(command, tuple(args) if args else None)
        else:
            command = command.value
        b = bytearray(str(command), self.ENCODING)  # convert str to bytearray
        b.extend(self.TERMINATOR)  # add terminator to end of bytearray
        self.serial.write(bytes(b))  # convert bytearray to bytes
        resp = JBODRxData(self.receive_now())
        if not resp.ack:
            raise JBODConsoleAckException(
                command_req=command,
                command_args=[*args],
                response=resp.raw_data
            )
        return resp

    def flush_buffers(self):
        self.rx_buffer = None
        self.tx_buffer = None

    @property
    def rx_buffer(self) -> Optional[bytes]:
        """
        Getter clears buffer and sets flag on read.
        @return: buffer
        """
        if self._rx_buffer:
            self.NEW_RX_DATA = False
            return self._rx_buffer
        return None

    @rx_buffer.setter
    def rx_buffer(self, data: Optional[bytearray]):
        # set to flag to false if None
        if not data:
            self.NEW_RX_DATA = False
        else:
            self.NEW_RX_DATA = True
        self._rx_buffer = data

    def receive_now(self):
        """Blocking wait for receive"""
        retries = 0
        # wait for new data (1 sec max)
        while retries < 100 and not self.NEW_RX_DATA:
            time.sleep(0.01)
            retries += 1
        return self.rx_buffer

    @property
    def tx_buffer(self):
        if self._tx_buffer:
            self.NEW_TX_DATA = False
            return self._tx_buffer
        return None

    @tx_buffer.setter
    def tx_buffer(self, data: Optional[bytes]):
        if not data:
            self.NEW_TX_DATA = False
        else:
            self.NEW_TX_DATA = True
        self._tx_buffer = data

    def transmit(self, data: bytes):
        # sets NEW_TX_DATA flag when set
        self.tx_buffer = data

    def change_baudrate(self, baudrate: int):
        """Change baudrate after initialized"""
        backup = self.serial.baudrate
        try:
            self.serial.baudrate = baudrate
        except ValueError as e:
            self.serial.baudrate = backup

    def change_port(self, port: str):
        """Change port after initialized"""
        if port != self.serial.port:
            # reader thread needs to be shut down
            self._stop_reader()
            # save settings
            settings = self.serial.getSettingsDict()
            new_serial = serial.serial_for_url(port, do_not_open=True)
            # restore settings and open
            new_serial.applySettingsDict(settings)
            self.serial.close()
            self.serial = new_serial
            try:
                new_serial.open()
            except serial.SerialException as e:
                self.close()
                raise e
            # and restart the reader thread
            self._start_reader()