#!/usr/bin/env python3
"""
mppt_ruixu.py — verify (default) or apply (--apply) the RUiXU solar-charger
battery settings on a Victron MPPT (SmartSolar/BlueSolar) over VE.Direct.

Target profile — everything lives in VictronConnect's Battery section:

    Battery voltage setting    0xEDEA == 48       (never auto-detect; 0xEDEF
                                                   reads the operating voltage,
                                                   so it can't tell auto apart)
    Battery type               0xEDF1 == user     (required so the custom
                                                   voltages below stick)
    Absorption voltage         0xEDF7 == 5700     (57.00 V)
    Float voltage              0xEDF6 == 5500     (55.00 V)

Note these are deliberately ABOVE the inverter/charger profile
(56.00/54.60 V, see verify_ruixu.py): solar is the primary charge source.

Like apply_ruixu: reads first, writes only deviations, every write verified
by readback, full re-verify at the end. Writes persist to the controller's
NVM. The battery-type step is included because MPPTs ignore/override custom
voltages while a fixed preset is selected — same as picking "User defined"
in VictronConnect before editing voltages.

    python3 tui/mppt_ruixu.py --mock               # demo, no hardware
    python3 tui/mppt_ruixu.py                      # verify (read-only)
    python3 tui/mppt_ruixu.py --apply              # prompts, then writes
    python3 tui/mppt_ruixu.py --apply --dry-run    # plan only
    python3 tui/mppt_ruixu.py --apply --yes /dev/ttyUSB1

The port is a VE.Direct-USB cable (auto-detected from
/dev/serial/by-id/*VE_Direct* when not given). If something else holds the
port (a datalogger service), stop it first. Exit codes: 0 compliant,
1 deviations remain / write failed, 2 port or link problem, 3 declined.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from typing import Optional

import vedirect as vd

PROFILE_NAME = "RUiXU solar charger (48V LiFePO4)"

# (register, target raw value) in write order: system voltage and battery
# type first — voltages only stick on a user-defined battery type.
TARGETS = [
    (vd.REG_BATTERY_VOLTAGE_SETTING, 48),
    (vd.REG_BATTERY_TYPE, vd.BATTERY_TYPE_USER),
    (vd.REG_ABSORPTION_VOLTAGE, 5700),
    (vd.REG_FLOAT_VOLTAGE, 5500),
]


@dataclass
class CheckResult:
    status: str   # PASS | FAIL | SKIP
    name: str
    detail: str


@dataclass
class PlanStep:
    reg: int
    value: int
    label: str


def read_current(backend) -> dict[int, tuple[Optional[int], str]]:
    return {reg: backend.get(reg) for reg, _ in TARGETS}


def run_checks(vals) -> list[CheckResult]:
    results = []
    for reg, want in TARGETS:
        meta = vd.REG_META[reg]
        val, err = vals[reg]
        name = f"{meta.name} {vd.format_reg_value(reg, want)}"
        if val is None:
            results.append(CheckResult("FAIL", name,
                                       f"register 0x{reg:04X}: {err}"))
        elif val == want:
            results.append(CheckResult("PASS", name,
                                       f"{vd.format_reg_value(reg, val)}"
                                       f" (0x{reg:04X} = {val})"))
        else:
            results.append(CheckResult(
                "FAIL", name,
                f"expected {vd.format_reg_value(reg, want)},"
                f" got {vd.format_reg_value(reg, val)} (0x{reg:04X} = {val})"))
    return results


def build_plan(vals) -> tuple[list[PlanStep], list[str]]:
    steps, notes = [], []
    for reg, want in TARGETS:
        val, err = vals[reg]
        meta = vd.REG_META[reg]
        if val is None:
            notes.append(f"0x{reg:04X} {meta.name}: {err} — left unchanged")
        elif val != want:
            steps.append(PlanStep(
                reg, want,
                f"{meta.name}: {vd.format_reg_value(reg, val)}"
                f" → {vd.format_reg_value(reg, want)}"))
    return steps, notes


def apply_steps(backend, steps: list[PlanStep], out=print) -> bool:
    for i, s in enumerate(steps, 1):
        ok, err = backend.set(s.reg, s.value)
        if ok:
            readback, rerr = backend.get(s.reg)
            ok = readback == s.value
            err = f"readback got {readback} ({rerr})" if not ok else ""
        msg = "ok      readback confirmed" if ok else f"FAILED  {err}"
        out(f"  [{i}/{len(steps)}] 0x{s.reg:04X} = {s.value:<5} {msg} — {s.label}")
        if not ok:
            out("  Aborting remaining writes — already-written registers are"
                " fine to rewrite on a re-run.")
            return False
    return True


def print_report(port: str, backend, results: list[CheckResult]) -> None:
    version = backend.ping()
    ver = f", app {vd.format_version(version)}" if version else ""
    print(f"\n== {port} — {backend.description}{ver}")
    for r in results:
        print(f"  {r.status:<6} {r.name:<38} {r.detail}")
    fails = sum(r.status == "FAIL" for r in results)
    print(f"  -> {'OK' if fails == 0 else f'{fails} FAIL'}")


def confirm(port: str, n: int) -> bool:
    if not sys.stdin.isatty():
        print("stdin is not a TTY — re-run with --yes to apply"
              " non-interactively.", file=sys.stderr)
        return False
    ans = input(f"Type 'apply' to write {n} register(s) to {port}: ")
    return ans.strip().lower() == "apply"


def process(port: str, backend, args) -> int:
    if backend.ping() is None:
        print(f"\n== {port} — no response to ping. Is this a VE.Direct port"
              " and is the controller powered?")
        return 2

    vals = read_current(backend)
    steps, notes = build_plan(vals)

    if not args.apply:
        print_report(port, backend, run_checks(vals))
        return 1 if steps or notes else 0

    if steps:
        print(f"\n== {port} — {len(steps)} write(s) needed:")
        for s in steps:
            print(f"  0x{s.reg:04X} = {s.value:<5} {s.label}")
    else:
        print(f"\n== {port} — all registers already match the profile.")
    for n in notes:
        print(f"  note: {n}")

    if args.dry_run:
        return 0
    if steps:
        if not args.yes and not confirm(port, len(steps)):
            print("  Declined — nothing written.")
            return 3
        if not apply_steps(backend, steps):
            return 1

    results = run_checks(read_current(backend))
    print_report(port, backend, results)
    return 1 if any(r.status == "FAIL" for r in results) else 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description=f"Verify or apply the {PROFILE_NAME} profile.")
    ap.add_argument("ports", nargs="*",
                    help="VE.Direct serial port(s) (default: auto-detect"
                         " /dev/serial/by-id/*VE_Direct*)")
    ap.add_argument("--mock", action="store_true",
                    help="use the simulated controller (no hardware)")
    ap.add_argument("--apply", action="store_true",
                    help="write deviations (default is read-only verify)")
    ap.add_argument("--dry-run", action="store_true",
                    help="with --apply: show the plan, write nothing")
    ap.add_argument("--yes", action="store_true",
                    help="with --apply: skip the interactive confirmation")
    args = ap.parse_args()

    if args.mock:
        ports = ["MOCK"]
    elif args.ports:
        ports = args.ports
    else:
        found = vd.find_default_port()
        if not found:
            print("No port given and no /dev/serial/by-id/*VE_Direct* found."
                  " Pass the VE.Direct port explicitly.", file=sys.stderr)
            return 2
        ports = [found]

    exit_code = 0
    for port in ports:
        backend = vd.MockVEDirect() if args.mock else vd.SerialVEDirect(port)
        try:
            backend.open()
        except Exception as err:
            print(f"Could not open {port}: {err}\n"
                  "If a datalogger/monitoring service holds the port, stop it"
                  " first and restart it afterwards.", file=sys.stderr)
            exit_code = max(exit_code, 2)
            continue
        try:
            exit_code = max(exit_code, process(port, backend, args))
        finally:
            backend.close()
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
