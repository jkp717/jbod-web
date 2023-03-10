import re
import logging
import serial
from serial import Serial
from typing import Optional


_logger = logging.getLogger(__name__)


class JBODConsoleAckException(Exception):
    """
    NAK acknowledgement recieved exception
    """

    def __init__(self, message: str = None, command_req: str = None, command_args: Optional[list] = None):
        self.cmd_str, self.arg_str = "", ""
        if command_req:
            self.cmd_str = f"; Command sent [{command_req}]"
        if command_args:
            self.arg_str = f"; Args {command_args}"
        if not message:
            message = "Command not acknowledged by JBOD Controller"
        self.message = f"{message}{command_req}{command_args}"
        super().__init__(self.message)


class JBODCommand:
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


class JBODControlCharacter:
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


class JBODConsole(Serial):
    """
    Serial Wrapper class providing additional JBOD Functionallity
    """

    def __init__(self, baudrate, port, timeout):
        super().__init__()
        # define command list
        self.cmd = JBODCommand
        # define ctrl chars
        self.ctrlc = JBODControlCharacter
        # configure port
        self.baudrate = baudrate
        self.port = port
        self.timeout = timeout
        _logger.debug("Serial settings: %s", self.get_settings())

        self.open()

    # def __call__(self):
    #     self.open()
    #     return self

    @staticmethod
    def _command_format(command, cmd_vars=None):
        """
        Method for replacing command wildcards ('?') with variables provided
        Just return unchanged command if no vars provided
        """
        cmd = command
        if cmd_vars:
            for i in cmd_vars:
                cmd = cmd.replace("?", str(i), 1)
        return cmd

    @staticmethod
    def _command_match(command, comparison):
        regex = re.compile(command)
        return re.match(regex, comparison)

    def send_command(self, command, *args):
        """
        Takes a jbod command and positional args, sends request to controller
        Returns acknowledged response
        """
        # add variables (if any) to command string
        fmt_cmd = self._command_format(command, tuple(args) if args else None)
        # send command and then return response
        self._jbod_tx(fmt_cmd)
        return self.read_ack()

    def send_ctrlc(self, ctrlc):
        """
        Sends a single control character to controller
        Returns acknowledged response
        """
        # send command and then return response
        self._jbod_tx(ctrlc, eol=False)
        return self.read_ack()

    def read_ack(self):
        """
        Read input for acknowledgement
        Returns a tuple of str repr of ACK bit and message
        """
        _rx = self._jbod_rx()
        # prevent IndexError by returning an empty tuple
        if _rx is None or _rx == b'\x00':
            return (None, None)  # NOQA
        # convert bytes to ascii string; remove null terminator; filter null items
        return tuple(filter(None, _rx.decode('ASCII').split('\x00')))

    def _jbod_rx(self):
        rx_buffer = self.read_until(expected=serial.LF)
        _logger.debug("Raw received msg: %s", rx_buffer)
        return rx_buffer

    def _jbod_tx(self, cmd, eol=True):
        # if msg is bytes convert to str
        msg = cmd.decode('ASCII') if isinstance(cmd, bytes) else cmd

        if eol:
            msg = f"{msg}\r\n".encode('ASCII')
        else:
            msg = f"{msg}".encode('ASCII')
        self.write(msg)
        _logger.debug("Raw transmitted Msg: %s", msg)
