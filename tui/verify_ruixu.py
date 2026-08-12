#!/usr/bin/env python3
"""
verify_ruixu.py — read-only check that an inverter matches the RUiXU
(48V LiFePO4) configuration profile.

Reads each relevant setting over the MK3 and compares against the expected
profile; nothing is ever written. Checks per unit:

    AC1 input current limit        setting 6  == 300   (30.0 A)
    Dynamic current limiter        setting 1  bit 12 set
    Accept wide input freq range   setting 1  bit 11 set
    PowerAssist                    setting 0  bit 5 set
    Lithium (fixed) charge curve   setting 0  bit 11 clear AND setting 10 == 1
    Absorption voltage             setting 2  == 5600  (56.00 V)
    Float voltage                  setting 3  == 5460  (54.60 V)
    DC input low shutdown          setting 11 == 4400  (44.00 V)
    DC input low restart offset    setting 12 == 100   (1.00 V above shutdown)
    Max absorption time param      setting 9  == 1     (LiFePO4 profile, WARN only)
    Live output frequency ~60 Hz   RAM var 7 (proxy for the 60 Hz system setting)
    Runtime shore limit 30 A       'F' 5 config frame (WARN only — a remote
                                   panel may legitimately have moved it)

Two VEConfigure checkboxes have no known setting ID yet and are reported as
MANUAL: "current limit overruled by remote", and (with --charger-disabled)
"charger disabled" for the single 120V unit of a 240V system. Map them with
the sweep-and-diff method in FINDINGS §11 if needed.

    python3 tui/verify_ruixu.py --mock                     # demo, no hardware
    python3 tui/verify_ruixu.py                            # /dev/inverter + /dev/inverter2 if present
    python3 tui/verify_ruixu.py /dev/inverter2             # one specific port
    python3 tui/verify_ruixu.py --charger-disabled /dev/inverter2

Exit code: 0 all required checks pass on every port, 1 any FAIL,
2 could not open/talk to a port.

The production services hold the ports exclusively — stop them first:
    sudo systemctl stop grounded-inverter grounded-inverter2
and start them again afterwards.
"""

from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass
from typing import Optional

from vebus import protocol as p

DEFAULT_CANDIDATES = ["/dev/inverter", "/dev/inverter2"]

PROFILE_NAME = "RUiXU 48V LiFePO4"

# Expected raw setting values (see module docstring for engineering units).
EXPECTED_VALUES = {
    2: 5600,    # absorption 56.00 V
    3: 5460,    # float 54.60 V
    6: 300,     # AC1 input current limit 30.0 A (÷10 scaling)
    10: 1,      # charge characteristic = fixed (LiFePO4)
    11: 4400,   # DC input low shutdown 44.00 V
    12: 100,    # restart offset 1.00 V above shutdown
}
SETTING_IDS = [0, 1, 2, 3, 6, 9, 10, 11, 12]

FREQ_TARGET = 60.0
FREQ_TOLERANCE = 1.0

PORT_BUSY_REMEDY = """\
Could not open {port}: {err}

If another program holds the port (the MK3 is exclusive-open):
  * Stop the production readers first:
        sudo systemctl stop grounded-inverter grounded-inverter2
    …verify, then restore them:
        sudo systemctl start grounded-inverter grounded-inverter2
  * Also check for VEConfigure / VictronConnect / a running tui.py."""


@dataclass
class CheckResult:
    status: str   # PASS | FAIL | WARN | SKIP | MANUAL
    name: str
    detail: str


def _fmt(setting_id: int, raw: int) -> str:
    return p.format_setting_value(setting_id, raw)


def run_checks(backend: p.Backend, charger_disabled: bool = False) -> list[CheckResult]:
    """All profile checks against an open backend. Read-only."""
    results: list[CheckResult] = []
    vals: dict[int, Optional[int]] = {
        sid: backend.read_setting(sid) for sid in SETTING_IDS
    }

    def unreadable(sid: int, name: str) -> bool:
        v = vals[sid]
        if v is None:
            results.append(CheckResult("FAIL", name,
                                       f"setting {sid} did not respond"))
            return True
        if v == p.UNSUPPORTED:
            results.append(CheckResult("FAIL", name,
                                       f"setting {sid} reads unsupported (0xFFFF)"))
            return True
        return False

    def value_check(sid: int, name: str) -> None:
        if unreadable(sid, name):
            return
        v, want = vals[sid], EXPECTED_VALUES[sid]
        if v == want:
            results.append(CheckResult("PASS", name,
                                       f"{_fmt(sid, v)} (setting {sid} = {v})"))
        else:
            results.append(CheckResult("FAIL", name,
                                       f"expected {_fmt(sid, want)}, got {_fmt(sid, v)}"
                                       f" (setting {sid} = {v})"))

    def flag_check(sid: int, bit: int, want_set: bool, name: str) -> None:
        if unreadable(sid, name):
            return
        v = vals[sid]
        is_set = bool(v & (1 << bit))
        state = "set" if is_set else "clear"
        detail = f"setting {sid} bit {bit} {state} (raw 0x{v:04X})"
        status = "PASS" if is_set == want_set else "FAIL"
        results.append(CheckResult(status, name, detail))

    value_check(6, "AC input current limit 30A")
    results.append(CheckResult(
        "MANUAL", "Current limit overruled by remote",
        "no known setting ID — verify the checkbox in VEConfigure"
        " (mappable via FINDINGS §11 sweep-and-diff)"))
    flag_check(1, 12, True, "Dynamic current limiter enabled")
    flag_check(1, 11, True, "Accept wide input frequency range")
    flag_check(0, 5, True, "PowerAssist enabled")
    flag_check(0, 11, False, "Lithium: fixed charge curve (S0 bit 11 clear)")
    value_check(10, "Lithium: charge characteristic fixed")
    value_check(2, "Absorption voltage 56.00V")
    value_check(3, "Float voltage 54.60V")
    value_check(11, "DC input low shutdown 44.00V")
    value_check(12, "DC input low restart +1.00V")

    # Setting 9 = 1 is part of the VEConfigure LiFePO4 profile but was not in
    # the requested list — deviation is worth a look, not a failure.
    v9 = vals[9]
    if v9 is None or v9 == p.UNSUPPORTED:
        results.append(CheckResult("SKIP", "Max absorption time param (S9)",
                                   "setting 9 unreadable"))
    elif v9 == 1:
        results.append(CheckResult("PASS", "Max absorption time param (S9)",
                                   "setting 9 = 1 (LiFePO4 fixed profile)"))
    else:
        results.append(CheckResult("WARN", "Max absorption time param (S9)",
                                   f"setting 9 = {v9}, LiFePO4 profile uses 1"))

    # 60 Hz system frequency: no known setting ID, so check the live output
    # frequency instead (RAM var 7, period → frequency).
    raw7 = backend.read_ramvar(7)
    if raw7 is None:
        results.append(CheckResult("SKIP", "Output frequency ~60Hz (live)",
                                   "RAM var 7 did not respond"))
    else:
        info = backend.read_ramvar_info(7) or p.DEFAULT_RAMVAR_INFO[7]
        freq = p.period_to_frequency(info.parse(raw7))
        if freq <= 0:
            results.append(CheckResult("SKIP", "Output frequency ~60Hz (live)",
                                       "no output right now (period reads 0)"))
        elif abs(freq - FREQ_TARGET) <= FREQ_TOLERANCE:
            results.append(CheckResult("PASS", "Output frequency ~60Hz (live)",
                                       f"{freq:.2f} Hz"))
        else:
            results.append(CheckResult("FAIL", "Output frequency ~60Hz (live)",
                                       f"{freq:.2f} Hz — check the 50/60 Hz"
                                       " setting in VEConfigure"))

    # Runtime shore limit ('F' 5). WARN only: with "overruled by remote" on,
    # a panel/app may have moved it while setting 6 stays the stored value.
    cfg = backend.read_config()
    if cfg is None:
        results.append(CheckResult("SKIP", "Runtime shore limit 30A ('F' 5)",
                                   "config frame did not respond"))
    elif abs(cfg.actual_current - 30.0) < 0.05:
        results.append(CheckResult("PASS", "Runtime shore limit 30A ('F' 5)",
                                   f"{cfg.actual_current:.1f} A"))
    else:
        results.append(CheckResult("WARN", "Runtime shore limit 30A ('F' 5)",
                                   f"live limit is {cfg.actual_current:.1f} A"
                                   " (remote override?) — stored setting 6 is"
                                   " the config check"))

    if charger_disabled:
        results.append(CheckResult(
            "MANUAL", "Charger disabled (240V single-120V unit)",
            "no known setting ID — verify 'Enable charger' is OFF in"
            " VEConfigure for this unit"))

    return results


def all_settings_silent(results: list[CheckResult]) -> bool:
    """True when every setting-backed check failed with 'did not respond'."""
    responded = [r for r in results if "did not respond" not in r.detail
                 and r.status in ("PASS", "FAIL", "WARN")]
    return not responded


def print_report(port_label: str, backend: p.Backend,
                 results: list[CheckResult]) -> None:
    ident = backend.identify()
    fw = f", fw 0x{ident.firmware:08X}" if ident.firmware else ""
    print(f"\n== {port_label} — {ident.model}{fw}")
    for r in results:
        print(f"  {r.status:<6} {r.name:<42} {r.detail}")
    fails = sum(r.status == "FAIL" for r in results)
    warns = sum(r.status == "WARN" for r in results)
    manuals = sum(r.status == "MANUAL" for r in results)
    verdict = "OK" if fails == 0 else f"{fails} FAIL"
    extras = []
    if warns:
        extras.append(f"{warns} warn")
    if manuals:
        extras.append(f"{manuals} manual check{'s' if manuals > 1 else ''} left")
    tail = f" ({', '.join(extras)})" if extras else ""
    print(f"  -> {verdict}{tail}")


def main() -> int:
    ap = argparse.ArgumentParser(
        description=f"Read-only verification of the {PROFILE_NAME} profile.")
    ap.add_argument("ports", nargs="*",
                    help="serial port(s) to check (default: whichever of"
                         f" {', '.join(DEFAULT_CANDIDATES)} exist)")
    ap.add_argument("--mock", action="store_true",
                    help="use the simulated inverter (no hardware)")
    ap.add_argument("--address", type=int, default=0,
                    help="VE.Bus address to read (default 0; only for"
                         " multi-unit-per-MK3 systems)")
    ap.add_argument("--charger-disabled", action="store_true",
                    help="this unit is the single 120V inverter of a 240V"
                         " system and should have its charger disabled")
    args = ap.parse_args()

    if args.mock:
        ports = ["MOCK"]
    elif args.ports:
        ports = args.ports
    else:
        ports = [c for c in DEFAULT_CANDIDATES if os.path.exists(c)]
        if not ports:
            print(f"No port given and none of {', '.join(DEFAULT_CANDIDATES)}"
                  " exist. Pass a port explicitly.", file=sys.stderr)
            return 2

    exit_code = 0
    for port in ports:
        if args.mock:
            backend: p.Backend = p.MockBackend(address=args.address)
        else:
            backend = p.SerialBackend(port=port, address=args.address)
        try:
            backend.open()
        except Exception as err:
            print(PORT_BUSY_REMEDY.format(port=port, err=err), file=sys.stderr)
            exit_code = max(exit_code, 2)
            continue
        try:
            results = run_checks(backend, charger_disabled=args.charger_disabled)
            if all_settings_silent(results):
                print(f"\n== {port} — no response from the unit."
                      " Is it switched ON and is this the right port?")
                exit_code = max(exit_code, 2)
                continue
            print_report(port, backend, results)
            if any(r.status == "FAIL" for r in results):
                exit_code = max(exit_code, 1)
        finally:
            backend.close()
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
