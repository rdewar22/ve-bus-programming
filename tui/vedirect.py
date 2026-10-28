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
Commands used here: 1 = ping (response command 5), 7 = get register,
8 = set register (response echoes; a non-zero flags byte is an error).
Get/Set payload: register id little-endian (2 bytes), flags (1 byte), then
the value bytes little-endian for Set. Settings persist to NVM on write.

Register ids are from the published BlueSolar/SmartSolar HEX protocol
register list. Everything is readback-verified by callers — a wrong
assumption surfaces as a reported error, never a silent bad write.
"""

from __future__ import annotations

import glob
import string
import time
from dataclasses import dataclass
from typing import Optional

try:
    import serial  # type: ignore
except Exception:  # pragma: no cover - only the real backend needs pyserial
    serial = None

BAUD = 19200

CMD_PING = 0x1
CMD_GET = 0x7
CMD_SET = 0x8
RSP_PING = 0x5
CMD_ASYNC = 0xA          # unsolicited broadcasts — always skipped

# Get/Set response flags byte.
FLAG_ERRORS = {
    0x01: "unknown register id",
    0x02: "register not supported",
    0x04: "parameter error (value rejected)",
}

# Battery-section registers (VictronConnect "Battery" page).
REG_BATTERY_VOLTAGE = 0xEDEF      # un8, V — 0 = auto-detect, else 12/24/36/48
REG_BATTERY_TYPE = 0xEDF1         # un8 — preset index; 0xFF = user defined
REG_ABSORPTION_VOLTAGE = 0xEDF7   # un16, 0.01 V
REG_FLOAT_VOLTAGE = 0xEDF6        # un16, 0.01 V


@dataclass(frozen=True)
class RegMeta:
    name: str
    size: int           # value bytes
    scale: float = 1.0  # raw / scale = engineering value
    unit: str = ""


REG_META: dict[int, RegMeta] = {
    REG_BATTERY_VOLTAGE: RegMeta("Battery (system) voltage", 1, 1.0, "V"),
    REG_BATTERY_TYPE: RegMeta("Battery type", 1),
    REG_ABSORPTION_VOLTAGE: RegMeta("Absorption voltage", 2, 100.0, "V"),
    REG_FLOAT_VOLTAGE: RegMeta("Float voltage", 2, 100.0, "V"),
}

BATTERY_TYPE_USER = 0xFF


def format_reg_value(reg: int, value: Optional[int]) -> str:
    if value is None:
        return "—"
    m = REG_META.get(reg)
    if m is None:
        return str(value)
    if reg == REG_BATTERY_TYPE:
        return "user defined" if value == BATTERY_TYPE_USER else f"preset {value}"
    if reg == REG_BATTERY_VOLTAGE and value == 0:
        return "auto-detect"
    if m.scale != 1.0:
        return f"{value / m.scale:.2f}{m.unit}"
    return f"{value}{m.unit}"


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


# ── backends ─────────────────────────────────────────────────────────────────

class VEDirectError(Exception):
    pass


def find_default_port() -> Optional[str]:
    hits = sorted(glob.glob("/dev/serial/by-id/*VE_Direct*"))
    return hits[0] if hits else None


class SerialVEDirect:
    """Real VE.Direct port. Text-protocol lines stream continuously and are
    skipped; only checksum-valid HEX frames are consumed."""

    RETRIES = 3
    REPLY_TIMEOUT = 2.0

    def __init__(self, port: str):
        if serial is None:
            raise RuntimeError("pyserial is not installed.")
        self.port = port
        self.ser = None
        self.is_mock = False
        self.description = f"{port} @ {BAUD} 8N1"

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
        for _ in range(self.RETRIES):
            self.ser.reset_input_buffer()
            self.ser.write(frame)
            payload = self._await_reply(
                rsp_command, reg, time.monotonic() + self.REPLY_TIMEOUT)
            if payload is not None:
                return payload
        return None

    def ping(self) -> Optional[int]:
        """App version word from a ping reply, or None if the device is silent."""
        payload = self._transact(build_frame(CMD_PING), RSP_PING, None)
        return int.from_bytes(payload, "little") if payload else None

    def get(self, reg: int) -> tuple[Optional[int], str]:
        """(value, error). value None ⇒ error says why (silence or flags)."""
        payload = self._transact(build_get(reg), CMD_GET, reg)
        if payload is None:
            return None, "no response"
        flags, value = decode_reply(payload, reg)
        if flags:
            return None, FLAG_ERRORS.get(flags, f"error flags 0x{flags:02X}")
        return value, ""

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
    """Simulated SmartSolar with factory-ish 48V defaults (deviating from the
    RUiXU solar profile so demos/tests show real work)."""

    def __init__(self):
        self.is_mock = True
        self.description = "MOCK (SmartSolar)"
        self.regs: dict[int, int] = {
            REG_BATTERY_VOLTAGE: 0,          # auto-detect
            REG_BATTERY_TYPE: 3,             # a fixed preset, not user
            REG_ABSORPTION_VOLTAGE: 5760,    # 57.60 V
            REG_FLOAT_VOLTAGE: 5520,         # 55.20 V
        }
        self.unsupported: set[int] = set()

    def open(self) -> None: ...
    def close(self) -> None: ...

    def ping(self) -> Optional[int]:
        return 0x0159  # app version 1.59, arbitrary

    def get(self, reg: int) -> tuple[Optional[int], str]:
        if reg in self.unsupported:
            return None, FLAG_ERRORS[0x02]
        if reg not in self.regs:
            return None, FLAG_ERRORS[0x01]
        return self.regs[reg], ""

    def set(self, reg: int, value: int) -> tuple[bool, str]:
        if reg in self.unsupported:
            return False, FLAG_ERRORS[0x02]
        self.regs[reg] = value
        return True, ""
