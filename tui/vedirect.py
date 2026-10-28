#!/usr/bin/env python3
"""
vedirect — minimal VE.Direct HEX-protocol backend for Victron MPPT solar
charge controllers (SmartSolar / BlueSolar).

This is a DIFFERENT bus from the rest of this repo: VE.Direct is the small
JST connector (or VE.Direct-USB cable), 19200 8N1, not the VE.Bus/MK3 RJ45.
The device continuously streams the human-readable text protocol; HEX frames
can be interleaved at any time and the device answers them individually.

HEX frame format (VE.Direct HEX protocol doc):

    ':' <command nibble, 1 hex char> <payload bytes, hex> <checksum byte, hex> '\\n'

The checksum makes command + all payload bytes + checksum ≡ 0x55 (mod 256).
Commands used here: 1 = ping (response command 5), 4 = product id (response
command 1), 7 = get register, 8 = set register (response echoes; a non-zero
flags byte is an error). Get/Set payload: register id little-endian
(2 bytes), flags (1 byte), then the value bytes little-endian for Set.
Settings persist to NVM on write — never write from a loop.

Register ids are from Victron's "VE.Direct Protocol — BlueSolar and
SmartSolar MPPT chargers" (HEX protocol doc, Rev 18). Everything is
readback-verified by callers — a wrong assumption surfaces as a reported
error, never a silent bad write.
"""

from __future__ import annotations

import glob
import string
import threading
import time
from dataclasses import dataclass, field
from typing import Optional

try:
    import serial  # type: ignore
except Exception:  # pragma: no cover - only the real backend needs pyserial
    serial = None

BAUD = 19200

CMD_PING = 0x1
CMD_PRODUCT_ID = 0x4
CMD_GET = 0x7
CMD_SET = 0x8
RSP_DONE = 0x1
RSP_PING = 0x5
CMD_ASYNC = 0xA          # unsolicited broadcasts — always skipped

# Get/Set response flags byte.
FLAG_ERRORS = {
    0x01: "unknown register id",
    0x02: "register not supported",
    0x04: "parameter error (value rejected)",
}

# ── register ids ─────────────────────────────────────────────────────────────

# Product info / generic device control.
REG_SERIAL = 0x010A               # string
REG_MODEL_NAME = 0x010B           # string
REG_CAPABILITIES = 0x0140         # un32 bit-mask
REG_DEVICE_MODE = 0x0200          # un8 — 1 on, 4 (or 0) off
REG_DEVICE_STATE = 0x0201         # un8 enum
REG_REMOTE_CONTROL = 0x0202       # un32 — bit 1 enables 0x0200 on/off
REG_OFF_REASON = 0x0207           # un32 bit-mask (fw 1.44+)

# Battery section (VictronConnect "Battery" page) — all NVM.
REG_ADAPTIVE_MODE = 0xEDFE        # un8 0/1
REG_AUTO_EQUALISE = 0xEDFD        # un8 — 0 off, N = every N days
REG_ABSORPTION_TIME = 0xEDFB      # un16, 0.01 h
REG_ABSORPTION_VOLTAGE = 0xEDF7   # un16, 0.01 V
REG_FLOAT_VOLTAGE = 0xEDF6        # un16, 0.01 V
REG_EQUALISE_VOLTAGE = 0xEDF4     # un16, 0.01 V
REG_TEMP_COMPENSATION = 0xEDF2    # sn16, 0.01 mV/K
REG_BATTERY_TYPE = 0xEDF1         # un8 — preset index; 0xFF = user defined
REG_MAX_CURRENT = 0xEDF0          # un16, 0.1 A
REG_BATTERY_VOLTAGE = 0xEDEF      # un8, V — READS the operating voltage
REG_BATTERY_VOLTAGE_SETTING = 0xEDEA  # un8, V — 0 = auto-detect (fw 1.12+)
REG_BMS_PRESENT = 0xEDE8          # un8 0/1
REG_TAIL_CURRENT = 0xEDE7         # un16, 0.1 A
REG_LOW_TEMP_CURRENT = 0xEDE6     # un16, 0.1 A — 0xFFFF = use max
REG_LOW_TEMP_LEVEL = 0xEDE0       # sn16, 0.01 °C
REG_REBULK_OFFSET = 0xED2E        # un16, 0.01 V (fw 1.42+)

# Charger / solar data (read-only).
REG_BATTERY_TEMP = 0xEDEC         # un16, 0.01 K — 0xFFFF = N/A
REG_CHARGER_MAX_CURRENT = 0xEDDF  # un16, 0.1 A — hardware rating
REG_USER_YIELD = 0xEDDC           # un32, 0.01 kWh (resettable)
REG_INTERNAL_TEMP = 0xEDDB        # sn16, 0.01 °C
REG_ERROR = 0xEDDA                # un8 enum
REG_CHARGER_CURRENT = 0xEDD7      # un16, 0.1 A — battery + load
REG_CHARGER_VOLTAGE = 0xEDD5      # un16, 0.01 V
REG_YIELD_TODAY = 0xEDD3          # un16, 0.01 kWh
REG_MAX_POWER_TODAY = 0xEDD2      # un16, W
REG_YIELD_YESTERDAY = 0xEDD1      # un16, 0.01 kWh
REG_ADJ_VOLTAGE_MIN = 0x2211      # un16, 0.01 V (fw 1.30+)
REG_ADJ_VOLTAGE_MAX = 0x2212      # un16, 0.01 V (fw 1.30+)
REG_PANEL_POWER = 0xEDBC          # un32, 0.01 W
REG_PANEL_VOLTAGE = 0xEDBB        # un16, 0.01 V
REG_PANEL_CURRENT = 0xEDBD        # un16, 0.1 A (not on 10/15/20A models)
REG_TRACKER_MODE = 0xEDB3         # un8 enum (fw 1.42+)

# Load output (10/15/20A models only).
REG_LOAD_CURRENT = 0xEDAD         # un16, 0.1 A
REG_LOAD_CONTROL = 0xEDAB         # un8 — low nibble = mode, high nibble reserved
REG_LOAD_STATE = 0xEDA8           # un8 0/1
REG_LOAD_HIGH_LEVEL = 0xED9D      # un16, 0.01 V — switch-on level
REG_LOAD_LOW_LEVEL = 0xED9C       # un16, 0.01 V — switch-off level
REG_LOAD_OFF_REASON = 0xED91      # un8 bit-mask
REG_LOAD_AES_TIMER = 0xED90       # un16, minutes

BATTERY_TYPE_USER = 0xFF
DEVICE_MODE_ON = 1
DEVICE_MODE_OFF = 4
REMOTE_ONOFF_BIT = 0x00000002
LOAD_MODE_MASK = 0x0F
CAP_LOAD_OUTPUT = 1 << 0

DEVICE_STATES = {
    0: "Not charging", 2: "Fault", 3: "Bulk", 4: "Absorption", 5: "Float",
    6: "Storage", 7: "Manual equalise", 245: "Wake-up", 247: "Auto equalise",
    250: "Blocked (updating)", 252: "External control", 255: "Unavailable",
}

ERROR_CODES = {
    0: "No error", 2: "Battery voltage too high",
    3: "Battery temp sensor issue", 4: "Battery temp sensor issue",
    5: "Battery temp sensor issue", 6: "Battery voltage sensor issue",
    7: "Battery voltage sensor issue", 8: "Battery voltage sensor issue",
    14: "Battery too cold to charge", 17: "Charger too hot",
    18: "Excessive output current", 19: "Current polarity reversed",
    20: "Bulk time expired", 21: "Current sensor issue",
    22: "Internal temp sensor issue", 23: "Internal temp sensor issue",
    26: "Terminals overheated", 27: "Short circuit", 28: "Converter issue",
    29: "Battery over-charge protection", 33: "PV input voltage too high",
    34: "PV input excessive current", 38: "Input shutdown (battery V high)",
    39: "Input shutdown (current while off)", 66: "Incompatible network device",
    67: "BMS connection lost", 68: "Network misconfigured",
    116: "Calibration data lost", 117: "Incompatible firmware",
    119: "Settings invalid/corrupted (restore defaults + power cycle)",
}

TRACKER_MODES = {0: "Off", 1: "Voltage/current limited", 2: "MPP tracking"}

# Rotary-switch chargers (30A+). Writing anything but 0xFF reverts to the
# rotary setting there; on 10/15/20A only 1 and 0xFF exist.
BATTERY_TYPES = {
    1: "GEL Long Life", 2: "GEL Deep discharge", 3: "AGM Deep discharge",
    5: "Tubular plate 1", 6: "Tubular plate 2", 7: "Tubular plate 3",
    8: "LiFePO4", BATTERY_TYPE_USER: "User defined",
}

LOAD_MODES = {
    0: "Off", 1: "Auto (BatteryLife)", 2: "Alt 1 (off<11.1V, on>13.1V)",
    3: "Alt 2 (off<11.8V, on>14.0V)", 4: "Always on (no discharge guard!)",
    5: "User 1 (off<low, on>high)", 6: "User 2 (off<low<on<high<off)",
    7: "Automatic Energy Selector",
}

OFF_REASON_BITS = {
    0: "no PV input", 1: "power switch", 2: "soft switch (device mode)",
    3: "remote input", 4: "internal", 5: "pay-as-you-go", 6: "BMS shutdown",
    9: "battery too cold",
}

LOAD_OFF_REASON_BITS = {
    0: "battery low", 1: "short circuit", 2: "timer program",
    3: "remote input", 4: "pay-as-you-go", 7: "starting up",
}


@dataclass(frozen=True)
class RegMeta:
    name: str
    size: int           # value bytes
    scale: float = 1.0  # raw / scale = engineering value
    unit: str = ""
    signed: bool = False
    writable: bool = False
    enum: dict = field(default_factory=dict)
    na: Optional[int] = None  # raw sentinel meaning "not available"


def _v(name, writable=True):
    return RegMeta(name, 2, 100.0, "V", writable=writable)


REG_META: dict[int, RegMeta] = {
    REG_CAPABILITIES: RegMeta("Capabilities", 4),
    REG_DEVICE_MODE: RegMeta("Device mode", 1, writable=True,
                             enum={0: "off", 1: "on", 4: "off"}),
    REG_DEVICE_STATE: RegMeta("Charge state", 1, enum=DEVICE_STATES),
    REG_REMOTE_CONTROL: RegMeta("Remote control mask", 4, writable=True),
    REG_OFF_REASON: RegMeta("Off reason", 4),

    REG_BATTERY_VOLTAGE_SETTING: RegMeta("Battery voltage setting", 1, 1.0,
                                         "V", writable=True,
                                         enum={0: "auto-detect"}),
    REG_BATTERY_VOLTAGE: RegMeta("Battery voltage (operating)", 1, 1.0, "V",
                                 writable=True),
    REG_BATTERY_TYPE: RegMeta("Battery type", 1, writable=True,
                              enum=BATTERY_TYPES),
    REG_ABSORPTION_VOLTAGE: _v("Absorption voltage"),
    REG_FLOAT_VOLTAGE: _v("Float voltage"),
    REG_EQUALISE_VOLTAGE: _v("Equalisation voltage"),
    REG_REBULK_OFFSET: _v("Re-bulk voltage offset"),
    REG_MAX_CURRENT: RegMeta("Max charge current", 2, 10.0, "A",
                             writable=True),
    REG_ABSORPTION_TIME: RegMeta("Max absorption time", 2, 100.0, "h",
                                 writable=True),
    REG_TAIL_CURRENT: RegMeta("Tail current", 2, 10.0, "A", writable=True),
    REG_TEMP_COMPENSATION: RegMeta("Temp compensation", 2, 100.0, "mV/K",
                                   signed=True, writable=True),
    REG_LOW_TEMP_LEVEL: RegMeta("Low-temp cut-off", 2, 100.0, "°C",
                                signed=True, writable=True),
    REG_LOW_TEMP_CURRENT: RegMeta("Low-temp charge current", 2, 10.0, "A",
                                  writable=True, na=0xFFFF),
    REG_ADAPTIVE_MODE: RegMeta("Adaptive absorption", 1, writable=True,
                               enum={0: "off", 1: "on"}),
    REG_AUTO_EQUALISE: RegMeta("Auto equalisation (days)", 1, writable=True,
                               enum={0: "off"}),
    REG_BMS_PRESENT: RegMeta("BMS present", 1, writable=True,
                             enum={0: "no", 1: "yes"}),

    REG_PANEL_VOLTAGE: _v("PV voltage", writable=False),
    REG_PANEL_POWER: RegMeta("PV power", 4, 100.0, "W"),
    REG_PANEL_CURRENT: RegMeta("PV current", 2, 10.0, "A"),
    REG_CHARGER_VOLTAGE: _v("Battery voltage", writable=False),
    REG_CHARGER_CURRENT: RegMeta("Charger current (batt + load)", 2, 10.0,
                                 "A"),
    REG_BATTERY_TEMP: RegMeta("Battery temperature", 2, 100.0, "K",
                              na=0xFFFF),
    REG_INTERNAL_TEMP: RegMeta("Charger temperature", 2, 100.0, "°C",
                               signed=True),
    REG_ERROR: RegMeta("Error", 1, enum=ERROR_CODES),
    REG_TRACKER_MODE: RegMeta("Tracker mode", 1, enum=TRACKER_MODES),
    REG_YIELD_TODAY: RegMeta("Yield today", 2, 100.0, "kWh"),
    REG_MAX_POWER_TODAY: RegMeta("Max power today", 2, 1.0, "W"),
    REG_YIELD_YESTERDAY: RegMeta("Yield yesterday", 2, 100.0, "kWh"),
    REG_USER_YIELD: RegMeta("Total yield (user)", 4, 100.0, "kWh"),
    REG_CHARGER_MAX_CURRENT: RegMeta("Charger rated current", 2, 10.0, "A"),
    REG_ADJ_VOLTAGE_MIN: _v("Adjustable voltage min", writable=False),
    REG_ADJ_VOLTAGE_MAX: _v("Adjustable voltage max", writable=False),

    REG_LOAD_CURRENT: RegMeta("Load current", 2, 10.0, "A"),
    REG_LOAD_STATE: RegMeta("Load output state", 1, enum={0: "off", 1: "on"}),
    REG_LOAD_CONTROL: RegMeta("Load output mode", 1, writable=True),
    REG_LOAD_HIGH_LEVEL: _v("Load switch-on level"),
    REG_LOAD_LOW_LEVEL: _v("Load switch-off level"),
    REG_LOAD_OFF_REASON: RegMeta("Load off reason", 1),
    REG_LOAD_AES_TIMER: RegMeta("AES timer", 2, 1.0, "min", writable=True),
}

# Registers whose value only sticks with a user-defined battery type.
NEEDS_USER_TYPE = {REG_ABSORPTION_VOLTAGE, REG_FLOAT_VOLTAGE,
                   REG_EQUALISE_VOLTAGE, REG_TEMP_COMPENSATION}

SYSTEM_VOLTAGES = (12, 24, 36, 48)


# ── value conversion / formatting ────────────────────────────────────────────

def to_eng(reg: int, raw: int) -> float:
    """Raw register value → engineering units (sign-extended where signed)."""
    m = REG_META[reg]
    if m.signed and raw >= 1 << (8 * m.size - 1):
        raw -= 1 << (8 * m.size)
    return raw / m.scale


def to_raw(reg: int, eng: float) -> int:
    """Engineering value → unsigned raw register value (two's complement)."""
    m = REG_META[reg]
    raw = round(eng * m.scale)
    return raw & ((1 << (8 * m.size)) - 1) if m.signed else raw


def _bits(value: int, names: dict[int, str]) -> str:
    hit = [names.get(b, f"bit {b}") for b in range(32) if value >> b & 1]
    return ", ".join(hit) if hit else "none"


def format_reg_value(reg: int, value: Optional[int]) -> str:
    if value is None:
        return "—"
    m = REG_META.get(reg)
    if m is None:
        return str(value)
    if m.na is not None and value == m.na:
        return "n/a"
    if reg == REG_BATTERY_TYPE:
        return BATTERY_TYPES.get(value, f"preset {value}")
    if reg == REG_LOAD_CONTROL:
        mode = value & LOAD_MODE_MASK
        return LOAD_MODES.get(mode, f"mode {mode}")
    if reg == REG_OFF_REASON:
        return _bits(value, OFF_REASON_BITS)
    if reg == REG_LOAD_OFF_REASON:
        return _bits(value, LOAD_OFF_REASON_BITS)
    if value in m.enum:
        return m.enum[value]
    if reg == REG_AUTO_EQUALISE:
        return f"every {value} day(s)"
    if m.enum and not m.unit:
        return f"unknown ({value})"
    if reg == REG_BATTERY_TEMP:
        return f"{value / 100.0 - 273.15:.1f}°C"
    if m.scale != 1.0:
        return f"{to_eng(reg, value):.2f}{m.unit}"
    return f"{value}{m.unit}"


def format_version(word: Optional[int]) -> str:
    """Ping word → 'v1.16'. The top two bits are the firmware type, not part
    of the version (0x4116 = application v1.16)."""
    if word is None:
        return "—"
    return f"v{(word >> 8) & 0x0F:X}.{word & 0xFF:02X}"


# ── write validation ─────────────────────────────────────────────────────────

@dataclass
class Validation:
    ok: bool
    message: str = ""


def voltage_limits(vals: dict[int, Optional[int]]) -> Optional[tuple[float, float]]:
    """Allowed charge-voltage range: the device's own 0x2211/0x2212 when it
    reports them, else the doc's 12V window (8–17.4V) scaled to the system."""
    lo, hi = vals.get(REG_ADJ_VOLTAGE_MIN), vals.get(REG_ADJ_VOLTAGE_MAX)
    if lo is not None and hi is not None and hi > lo:
        return lo / 100.0, hi / 100.0
    sysv = vals.get(REG_BATTERY_VOLTAGE)
    if sysv not in SYSTEM_VOLTAGES:
        return None
    k = sysv / 12
    return 8.0 * k, 17.4 * k


def validate_write(reg: int, raw: int,
                   vals: dict[int, Optional[int]]) -> Validation:
    """Sanity checks before a write. Hard failures block the write; the
    device's own range check (parameter error) and readback still apply."""
    m = REG_META.get(reg)
    if m is None or not m.writable:
        return Validation(False, f"0x{reg:04X} is not a writable register.")
    if raw < 0 or raw >= 1 << (8 * m.size):
        return Validation(False, f"{m.name}: value out of register range.")
    if reg in NEEDS_USER_TYPE and vals.get(REG_BATTERY_TYPE) != BATTERY_TYPE_USER:
        return Validation(False, f"{m.name} only sticks with battery type "
                          "'User defined' — set that first.")
    if reg in (REG_ABSORPTION_VOLTAGE, REG_FLOAT_VOLTAGE, REG_EQUALISE_VOLTAGE):
        volts = raw / 100.0
        lim = voltage_limits(vals)
        if lim and not lim[0] <= volts <= lim[1]:
            return Validation(False, f"{m.name} {volts:.2f}V is outside the "
                              f"allowed {lim[0]:.2f}–{lim[1]:.2f}V (out-of-"
                              "range settings raise error 119).")
        absv = vals.get(REG_ABSORPTION_VOLTAGE)
        flt = vals.get(REG_FLOAT_VOLTAGE)
        if reg == REG_FLOAT_VOLTAGE and absv is not None and raw > absv:
            return Validation(False, f"Float {volts:.2f}V is above absorption "
                              f"{absv / 100:.2f}V.")
        if reg == REG_ABSORPTION_VOLTAGE and flt is not None and raw < flt:
            return Validation(False, f"Absorption {volts:.2f}V is below float "
                              f"{flt / 100:.2f}V.")
    if reg == REG_TEMP_COMPENSATION:
        sysv = vals.get(REG_BATTERY_VOLTAGE)
        if sysv in SYSTEM_VOLTAGES:
            limit = 21.0 * sysv / 12
            if abs(to_eng(reg, raw)) > limit:
                return Validation(False, f"Temp compensation must be within "
                                  f"±{limit:.1f} mV/K for {sysv}V.")
    if reg == REG_MAX_CURRENT:
        rated = vals.get(REG_CHARGER_MAX_CURRENT)
        if rated and raw > rated:
            return Validation(False, f"Max current {raw / 10:.1f}A exceeds the "
                              f"charger rating {rated / 10:.1f}A.")
    if reg in (REG_BATTERY_VOLTAGE, REG_BATTERY_VOLTAGE_SETTING) \
            and raw not in (0, *SYSTEM_VOLTAGES):
        return Validation(False, "System voltage must be 0 (auto), 12, 24, 36 "
                          "or 48.")
    if reg == REG_LOAD_HIGH_LEVEL:
        low = vals.get(REG_LOAD_LOW_LEVEL)
        if low is not None and raw <= low:
            return Validation(False, "Switch-on level must be above the "
                              "switch-off level.")
    if reg == REG_LOAD_LOW_LEVEL:
        high = vals.get(REG_LOAD_HIGH_LEVEL)
        if high is not None and raw >= high:
            return Validation(False, "Switch-off level must be below the "
                              "switch-on level.")
    return Validation(True)


# ── frame build / parse ──────────────────────────────────────────────────────

def checksum(command: int, payload: bytes) -> int:
    return (0x55 - command - sum(payload)) & 0xFF


def build_frame(command: int, payload: bytes = b"") -> bytes:
    body = payload.hex().upper() + f"{checksum(command, payload):02X}"
    return f":{command:X}{body}\n".encode("ascii")


def parse_frame(line: str) -> Optional[tuple[int, bytes]]:
    """
    Decode one ':'-prefixed HEX line (no trailing newline) into
    (command, payload) with the checksum validated and stripped.
    Returns None for anything malformed — text-protocol noise, a corrupted
    line, or a bad checksum.
    """
    if not line.startswith(":"):
        return None
    body = line[1:].strip()
    # 1 command char + even payload chars + 2 checksum chars ⇒ odd length ≥ 3.
    if len(body) < 3 or len(body) % 2 == 0:
        return None
    if any(c not in string.hexdigits for c in body):
        return None
    command = int(body[0], 16)
    data = bytes.fromhex(body[1:])
    if (command + sum(data)) & 0xFF != 0x55:
        return None
    return command, data[:-1]


def build_get(reg: int) -> bytes:
    return build_frame(CMD_GET, bytes([reg & 0xFF, reg >> 8, 0x00]))


def build_set(reg: int, value: int, size: int) -> bytes:
    return build_frame(CMD_SET, bytes([reg & 0xFF, reg >> 8, 0x00])
                       + value.to_bytes(size, "little"))


def decode_reply(payload: bytes, reg: int) -> Optional[tuple[int, int]]:
    """(flags, value) from a Get/Set response payload for `reg`, else None."""
    if len(payload) < 3 or (payload[0] | payload[1] << 8) != reg:
        return None
    return payload[2], int.from_bytes(payload[3:], "little")


def decode_string(payload: bytes) -> str:
    """Value bytes of a string register (payload after id + flags)."""
    return payload[3:].split(b"\x00", 1)[0].decode("ascii", "replace")


# ── backends ─────────────────────────────────────────────────────────────────

class VEDirectError(Exception):
    pass


def find_default_port() -> Optional[str]:
    hits = sorted(glob.glob("/dev/serial/by-id/*VE_Direct*"))
    return hits[0] if hits else None


class SerialVEDirect:
    """Real VE.Direct port. Text-protocol lines stream continuously and are
    skipped; only checksum-valid HEX frames are consumed. A lock serialises
    transactions so a UI's telemetry thread can't interleave with a write."""

    RETRIES = 3
    REPLY_TIMEOUT = 2.0

    def __init__(self, port: str):
        if serial is None:
            raise RuntimeError("pyserial is not installed.")
        self.port = port
        self.ser = None
        self.is_mock = False
        self.healthy = True
        self.description = f"{port} @ {BAUD} 8N1"
        self._lock = threading.Lock()

    def open(self) -> None:
        try:
            self.ser = serial.Serial(self.port, BAUD, timeout=0.2,
                                     exclusive=True)
        except TypeError:  # older pyserial without `exclusive`
            self.ser = serial.Serial(self.port, BAUD, timeout=0.2)

    def close(self) -> None:
        if self.ser is not None:
            try:
                self.ser.close()
            finally:
                self.ser = None

    def _await_reply(self, command: int, reg: Optional[int],
                     deadline: float) -> Optional[bytes]:
        buf = b""
        while time.monotonic() < deadline:
            buf += self.ser.read(self.ser.in_waiting or 1)
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                parsed = parse_frame(line.decode("ascii", "replace"))
                if parsed is None or parsed[0] != command:
                    continue  # text protocol, async frame, or other reply
                if reg is None:
                    return parsed[1]
                if decode_reply(parsed[1], reg) is not None:
                    return parsed[1]
        return None

    def _transact(self, frame: bytes, rsp_command: int,
                  reg: Optional[int]) -> Optional[bytes]:
        with self._lock:
            for _ in range(self.RETRIES):
                self.ser.reset_input_buffer()
                self.ser.write(frame)
                payload = self._await_reply(
                    rsp_command, reg, time.monotonic() + self.REPLY_TIMEOUT)
                if payload is not None:
                    self.healthy = True
                    return payload
            self.healthy = False
            return None

    def ping(self) -> Optional[int]:
        """Ping word (firmware type + version), or None if the device is silent."""
        payload = self._transact(build_frame(CMD_PING), RSP_PING, None)
        return int.from_bytes(payload, "little") if payload else None

    def product_id(self) -> Optional[int]:
        """Product id via the ':4' command (works on every firmware)."""
        payload = self._transact(build_frame(CMD_PRODUCT_ID), RSP_DONE, None)
        return int.from_bytes(payload[:2], "little") if payload else None

    def get(self, reg: int) -> tuple[Optional[int], str]:
        """(value, error). value None ⇒ error says why (silence or flags)."""
        payload = self._transact(build_get(reg), CMD_GET, reg)
        if payload is None:
            return None, "no response"
        flags, value = decode_reply(payload, reg)
        if flags:
            return None, FLAG_ERRORS.get(flags, f"error flags 0x{flags:02X}")
        return value, ""

    def get_string(self, reg: int) -> tuple[Optional[str], str]:
        payload = self._transact(build_get(reg), CMD_GET, reg)
        if payload is None:
            return None, "no response"
        if payload[2]:
            return None, FLAG_ERRORS.get(payload[2], f"error flags 0x{payload[2]:02X}")
        return decode_string(payload), ""

    def get_bytes(self, reg: int) -> tuple[Optional[bytes], str]:
        """Raw value bytes of any register — for probing unknown ids."""
        payload = self._transact(build_get(reg), CMD_GET, reg)
        if payload is None:
            return None, "no response"
        if payload[2]:
            return None, FLAG_ERRORS.get(payload[2], f"error flags 0x{payload[2]:02X}")
        return payload[3:], ""

    def set(self, reg: int, value: int) -> tuple[bool, str]:
        """Write + device-echo check. Callers should still readback-verify."""
        size = REG_META[reg].size
        payload = self._transact(build_set(reg, value, size), CMD_SET, reg)
        if payload is None:
            return False, "no response to set"
        flags, echoed = decode_reply(payload, reg)
        if flags:
            return False, FLAG_ERRORS.get(flags, f"error flags 0x{flags:02X}")
        if echoed != value:
            return False, f"device echoed {echoed}, not {value}"
        return True, ""


class MockVEDirect:
    """Simulated SmartSolar 150/45 with load-less, factory-ish 48V defaults
    (deviating from the RUiXU solar profile so demos/tests show real work).
    Like the real charger, it rejects charge-voltage writes unless the
    battery type is user defined."""

    PRODUCT_ID = 0xA061

    def __init__(self, load_output: bool = False):
        self.is_mock = True
        self.healthy = True
        self.description = "MOCK (SmartSolar)"
        self.regs: dict[int, int] = {
            # history, adaptive, equalise, remote, panel I, BMS, ext ctl, sync
            REG_CAPABILITIES: 0x0001E174 | (CAP_LOAD_OUTPUT if load_output else 0),
            REG_DEVICE_MODE: DEVICE_MODE_ON,
            REG_DEVICE_STATE: 3,             # bulk
            REG_REMOTE_CONTROL: REMOTE_ONOFF_BIT,
            REG_OFF_REASON: 0,
            REG_BATTERY_VOLTAGE_SETTING: 0,  # auto-detect …
            REG_BATTERY_VOLTAGE: 48,         # … which detected 48V
            REG_BATTERY_TYPE: 3,             # a fixed preset, not user
            REG_ABSORPTION_VOLTAGE: 5760,    # 57.60 V
            REG_FLOAT_VOLTAGE: 5520,         # 55.20 V
            REG_EQUALISE_VOLTAGE: 6480,      # 64.80 V
            REG_REBULK_OFFSET: 40,           # 0.40 V
            REG_MAX_CURRENT: 450,            # 45.0 A
            REG_ABSORPTION_TIME: 600,        # 6.00 h
            REG_TAIL_CURRENT: 20,            # 2.0 A
            REG_TEMP_COMPENSATION: to_raw(REG_TEMP_COMPENSATION, -64.8),
            REG_LOW_TEMP_LEVEL: to_raw(REG_LOW_TEMP_LEVEL, 5.0),
            REG_LOW_TEMP_CURRENT: 0xFFFF,
            REG_ADAPTIVE_MODE: 1,
            REG_AUTO_EQUALISE: 0,
            REG_BMS_PRESENT: 0,
            REG_PANEL_VOLTAGE: 9840,         # 98.40 V
            REG_PANEL_POWER: 61200,          # 612.00 W
            REG_PANEL_CURRENT: 62,           # 6.2 A
            REG_CHARGER_VOLTAGE: 5312,       # 53.12 V
            REG_CHARGER_CURRENT: 114,        # 11.4 A
            REG_BATTERY_TEMP: 0xFFFF,
            REG_INTERNAL_TEMP: to_raw(REG_INTERNAL_TEMP, 31.5),
            REG_ERROR: 0,
            REG_TRACKER_MODE: 2,
            REG_YIELD_TODAY: 184,            # 1.84 kWh
            REG_MAX_POWER_TODAY: 655,
            REG_YIELD_YESTERDAY: 312,
            REG_USER_YIELD: 41237,           # 412.37 kWh
            REG_CHARGER_MAX_CURRENT: 450,
            REG_ADJ_VOLTAGE_MIN: 3200,       # 32.00 V
            REG_ADJ_VOLTAGE_MAX: 6800,       # 68.00 V
        }
        if load_output:
            self.regs.update({
                REG_LOAD_CURRENT: 8, REG_LOAD_CONTROL: 1, REG_LOAD_STATE: 1,
                REG_LOAD_HIGH_LEVEL: 5200, REG_LOAD_LOW_LEVEL: 4600,
                REG_LOAD_OFF_REASON: 0, REG_LOAD_AES_TIMER: 360,
            })
        self.strings = {REG_SERIAL: "HQ2236MOCK1",
                        REG_MODEL_NAME: "SmartSolar MPPT 150/45"}
        self.unsupported: set[int] = set()
        self._tick = 0

    def open(self) -> None: ...
    def close(self) -> None: ...

    def ping(self) -> Optional[int]:
        return 0x4165  # application firmware v1.65

    def product_id(self) -> Optional[int]:
        return self.PRODUCT_ID

    def get(self, reg: int) -> tuple[Optional[int], str]:
        if reg in self.unsupported:
            return None, FLAG_ERRORS[0x02]
        if reg not in self.regs:
            return None, FLAG_ERRORS[0x01]
        if reg == REG_PANEL_POWER:  # a little life for the dashboard
            self._tick += 1
            return self.regs[reg] + (self._tick % 7) * 137, ""
        return self.regs[reg], ""

    def get_string(self, reg: int) -> tuple[Optional[str], str]:
        if reg in self.strings:
            return self.strings[reg], ""
        return None, FLAG_ERRORS[0x01]

    def get_bytes(self, reg: int) -> tuple[Optional[bytes], str]:
        if reg in self.strings:
            return self.strings[reg].encode(), ""
        val, err = self.get(reg)
        if val is None:
            return None, err
        size = REG_META[reg].size if reg in REG_META else 2
        return val.to_bytes(size, "little"), ""

    def set(self, reg: int, value: int) -> tuple[bool, str]:
        if reg in self.unsupported:
            return False, FLAG_ERRORS[0x02]
        meta = REG_META.get(reg)
        if meta is None:
            return False, FLAG_ERRORS[0x01]
        if not meta.writable:
            return False, FLAG_ERRORS[0x02]
        if reg in NEEDS_USER_TYPE \
                and self.regs.get(REG_BATTERY_TYPE) != BATTERY_TYPE_USER:
            return False, FLAG_ERRORS[0x04]
        if reg == REG_REMOTE_CONTROL:
            value |= self.regs.get(reg, 0)  # bits can only be set
        self.regs[reg] = value
        if reg in (REG_BATTERY_VOLTAGE, REG_BATTERY_VOLTAGE_SETTING):
            self.regs[REG_BATTERY_VOLTAGE_SETTING] = value
            if value:
                self.regs[REG_BATTERY_VOLTAGE] = value
        if reg == REG_DEVICE_MODE:
            self.regs[REG_DEVICE_STATE] = 3 if value == DEVICE_MODE_ON else 0
            self.regs[REG_OFF_REASON] = 0 if value == DEVICE_MODE_ON else 1 << 2
        return True, ""
