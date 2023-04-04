import threading
import serial
from typing import Optional, Union
import time
from enum import Enum
import re


class JBODConsoleException(Exception):
    pass


class JBODConsoleTimeoutException(JBODConsoleException):
    pass


class JBODConsoleAckException(JBODConsoleException):
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
        self.message = f"{message} {command_req} {command_args} {response}"
        super().__init__(self.message)


class ResetEvent(Enum):
    UNKNOWN_RESET = 0
    LOW_POWER_RESET = 1
    WINDOW_WATCHDOG_RESET = 2
    INDEPENDENT_WATCHDOG_RESET = 3
    SOFTWARE_RESET = 4
    POWER_ON_POWER_DOWN_RESET = 5
    EXTERNAL_PIN_RESET = 6
    BROWNOUT_RESET = 7


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
    PING = "jbod/? ping"  # responds with ack & 'OK'
    LED_ON = "jbod/? led/? ON"
    LED_OFF = "jbod/? led/? OFF"


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
    DC2 = "\x12"  # Device Control 2
    DC4 = "\x14"  # Device Control 4


class JBODRxData:
    ENCODING = "ASCII"

    def __init__(self, data: bytes):
        self.raw_data = data
        self._data = None
        self.cc_mapper = {
            str(JBODControlCharacter.ACK.value).encode(self.ENCODING): False,
            str(JBODControlCharacter.XON.value).encode(self.ENCODING): False,
            str(JBODControlCharacter.XOFF.value).encode(self.ENCODING): False,
            str(JBODControlCharacter.DC2.value).encode(self.ENCODING): False,
            str(JBODControlCharacter.DC4.value).encode(self.ENCODING): False,
        }
        self._parse_data(data)

    @property
    def ack(self):
        return self.cc_mapper.get(str(JBODControlCharacter.ACK.value).encode(self.ENCODING))

    @property
    def xon(self):
        return self.cc_mapper.get(str(JBODControlCharacter.XON.value).encode(self.ENCODING))

    @property
    def xoff(self):
        return self.cc_mapper.get(str(JBODControlCharacter.XOFF.value).encode(self.ENCODING))

    @property
    def dc2(self):
        return self.cc_mapper.get(str(JBODControlCharacter.DC2.value).encode(self.ENCODING))

    @property
    def dc4(self):
        return self.cc_mapper.get(str(JBODControlCharacter.DC2.value).encode(self.ENCODING))

    def set_flag(self, cc):
        self.cc_mapper[cc] = True

    @property
    def data(self):
        return self._data

    @data.setter
    def data(self, val: Union[bytearray, str]):
        """Automatically decoded to ASCII"""
        if isinstance(val, bytearray):
            self._data = val.decode(self.ENCODING)
        else:
            self._data = val

    def _parse_data(self, data: bytes):
        """First character defines message type; followed by message"""
        for ctrlc in self.cc_mapper.keys():
            if bytearray(data).startswith(ctrlc):
                self.set_flag(ctrlc)
                self.data = bytearray(data).removeprefix(ctrlc).strip(b'\r\n\x00')
                break

    def __repr__(self):
        return f"JBODRxData(ack={self.ack},xon={self.xon},xoff={self.xoff},dc2={self.dc2},dc4={self.dc4}," \
               f"data={self.data},raw_data={self.raw_data})"


class JBODConsole:
    TERMINATOR = b'\r\n'
    ENCODING = 'ASCII'
    NEW_RX_DATA = False
    NEW_TX_DATA = False

    def __init__(self, serial_instance: serial.Serial, callback: Optional[callable] = None, **kwargs):
        self.cmd = JBODCommand
        self.ctrlc = JBODControlCharacter
        self.serial = serial_instance
        self.alive = False
        self.receiver_thread = None
        self.transmitter_thread = None
        self._reader_alive = False
        self._rx_buffer = None
        self._tx_buffer = None
        self._callback = callback
        self._data_received = bytearray()
        self._lock = threading.Lock()
        self._callback_kwargs = kwargs

    def _start_reader(self):
        """Start reader thread"""
        self._reader_alive = True
        # start serial->console thread
        self.receiver_thread = threading.Thread(target=self.reader, name='rx')
        self.receiver_thread.daemon = True
        self.receiver_thread.start()

    def _stop_reader(self):
        """Stop reader thread only, wait for clean exit of thread"""
        self._reader_alive = False
        if hasattr(self.serial, 'cancel_read'):
            self.serial.cancel_read()
        self.receiver_thread.join()

    def start(self):
        """start worker threads"""
        if not self.serial.is_open:
            self.serial.open()
        self.alive = True
        self._start_reader()
        # enter console->serial loop
        self.transmitter_thread = threading.Thread(target=self.writer, name='tx')
        self.transmitter_thread.daemon = True
        self.transmitter_thread.start()

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
                        retries = 0
                        # give time to process rx data (1 sec max)
                        while retries < 10 and self.NEW_RX_DATA:
                            time.sleep(0.1)
                            retries += 1
                        # passing to callback if data has not been processed
                        if self._callback and self.NEW_RX_DATA:
                            # will clear NEW_RX_DATA flag
                            self._callback(self, JBODRxData(bytes(self.rx_buffer)), **self._callback_kwargs)
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
                        self.serial.write(self.tx_buffer)
                    self.tx_buffer = None  # clear buffer once transmitted
                time.sleep(0.01)
        except Exception as err:
            self.alive = False
            raise err

    @staticmethod
    def _command_match(pattern: re.Pattern, comparison):
        return re.match(pattern, comparison)

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
            fmt_command = self._command_format(command, tuple(args) if args else None)
        else:
            fmt_command = command.value
        b = bytearray(str(fmt_command), self.ENCODING)  # convert str to bytearray
        b.extend(self.TERMINATOR)  # add terminator to end of bytearray
        with self._lock:
            self.serial.write(bytes(b))  # convert bytearray to bytes
        resp = JBODRxData(self.receive_now())
        if not resp.ack:
            raise JBODConsoleAckException(
                command_req=fmt_command,
                command_args=[*args],
                response=resp.raw_data
            )
        return resp

    def flush_buffers(self):
        self._data_received = bytearray()
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
        if self.NEW_RX_DATA:
            return self.rx_buffer
        else:
            raise JBODConsoleTimeoutException("JBODConsole receive_now timed out.")

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

    @property
    def callback(self) -> Optional[callable]:
        return self._callback

    @callback.setter
    def callback(self, func: callable):
        self._callback = func

    def transmit(self, data: Union[bytes, JBODControlCharacter]) -> None:
        """
        Non-blocking thread-safe write. Responses should be
        handled by callback.
        """
        # Handle JBODCommands too?
        if isinstance(data, JBODControlCharacter):
            self.tx_buffer = data.value.encode(self.ENCODING)
        else:
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
        if port and port != self.serial.port:
            if self.alive:
                self.stop()
                self.join()
            if self.serial.is_open:
                self.serial.close()
            # save settings
            settings = self.serial.getSettingsDict()
            try:
                new_serial = serial.serial_for_url(port, do_not_open=True)
                # restore settings and open
                new_serial.applySettingsDict(settings)
                self.serial = new_serial
                self.start()  # opens the port
            except serial.SerialException as e:
                self.serial.close()
                raise e
