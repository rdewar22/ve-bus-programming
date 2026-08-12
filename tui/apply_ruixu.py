#!/usr/bin/env python3
"""
apply_ruixu.py — write the RUiXU (48V LiFePO4) configuration profile to an
inverter over the MK3, then re-verify.

Diff-based and minimal-wear: every relevant setting is read first and only
deviations are written (EEPROM has limited write cycles). The charge-profile
cluster is written in the exact order VEConfigure's LiFePO4 wizard uses
(FINDINGS §7.6); flag registers are read-modify-write so no foreign base
value is ever copied in. Every write is verified by readback, and the full
read-only verify_ruixu check suite runs at the end.

Target profile (see verify_ruixu.py for the raw values):

    Setting 0   PowerAssist ON (bit 5), adaptive charge OFF (bit 11)
    Settings 60/65/72/10/2/3/9   LiFePO4 fixed profile: 16 / 190 / 242 / 1 /
                                 56.00V absorption / 54.60V float / 1
    Setting 1   wide input frequency range ON (bit 11),
                dynamic current limiter ON (bit 12)
    Setting 6   AC1 input current limit 30.0 A
    Setting 11  DC input low shutdown 44.00 V
    Setting 12  restart offset +1.00 V
    plus the runtime shore limit ('S' long form) is moved to 30 A if a remote
    panel/app had it elsewhere.

NOT settable here (VEConfigure only — no known setting ID):
    * 50/60 Hz output frequency (verified live only; a 50 Hz unit needs VEConfigure)
    * "current limit overruled by remote" checkbox
    * per-unit charger disable (240V systems)

    python3 tui/apply_ruixu.py --mock              # demo, no hardware
    python3 tui/apply_ruixu.py --dry-run           # show the plan, write nothing
    python3 tui/apply_ruixu.py                     # prompts before writing
    python3 tui/apply_ruixu.py --yes /dev/inverter2

Exit code: 0 compliant (nothing to do, or applied and verified), 1 a write
failed or FAILs remain after applying, 2 could not open/talk to a port,
3 user declined the confirmation.

The production services hold the ports exclusively — stop them first:
    sudo systemctl stop grounded-inverter grounded-inverter2
and start them again afterwards.
"""

from __future__ import annotations

import argparse
import os
import sys
from typing import Callable, Optional

from vebus import protocol as p
import verify_ruixu as v

# Flag-register targets: (setting_id, bits to set, bits to clear, label).
FLAG_TARGETS = [
    (0, 1 << 5, 1 << 11,
     "Setting 0 flags: PowerAssist on, adaptive charge off"),
    (1, (1 << 11) | (1 << 12), 0,
     "Setting 1 flags: wide frequency range on, dynamic current limiter on"),
]

# Value targets in write order: the LiFePO4 cluster first, in VEConfigure's
# sequence (FINDINGS §7.6 — S0 precedes it, S1/6/11/12 follow).
CHARGE_TARGETS = [(60, 16), (65, 190), (72, 242), (10, 1),
                  (2, 5600), (3, 5460), (9, 1)]
OTHER_TARGETS = [(6, 300), (11, 4400), (12, 100)]

PLAN_SIDS = [0, 1] + [sid for sid, _ in CHARGE_TARGETS + OTHER_TARGETS]

SHORE_LIMIT_A = 30.0


def read_current(backend: p.Backend) -> dict[int, Optional[int]]:
    return {sid: backend.read_setting(sid) for sid in PLAN_SIDS}


def build_plan(vals: dict[int, Optional[int]]
               ) -> tuple[list[p.WriteStep], list[str]]:
    """Ordered write steps for every deviation, plus notes for skips."""
    steps: list[p.WriteStep] = []
    notes: list[str] = []

    def skip(sid: int, val: Optional[int]) -> bool:
        if val is None:
            notes.append(f"setting {sid} did not respond — left unchanged")
            return True
        if val == p.UNSUPPORTED:
            notes.append(f"setting {sid} reads unsupported (0xFFFF) — left unchanged")
            return True
        return False

    def rmw(sid: int, set_mask: int, clear_mask: int, label: str) -> None:
        val = vals.get(sid)
        if skip(sid, val):
            return
        new = (val | set_mask) & ~clear_mask
        if new != val:
            steps.append(p.WriteStep(
                sid, new, label,
                note=f"read-modify-write 0x{val:04X} → 0x{new:04X}"))

    def value(sid: int, want: int) -> None:
        val = vals.get(sid)
        if skip(sid, val):
            return
        if val != want:
            steps.append(p.WriteStep(
                sid, want,
                f"{p.setting_meta(sid).name} → {p.format_setting_value(sid, want)}",
                note=f"currently {p.format_setting_value(sid, val)}"))

    rmw(*FLAG_TARGETS[0])
    for sid, want in CHARGE_TARGETS:
        value(sid, want)
    rmw(*FLAG_TARGETS[1])
    for sid, want in OTHER_TARGETS:
        value(sid, want)
    return steps, notes


def validate_plan(steps: list[p.WriteStep]) -> list[str]:
    """Range/ordering sanity on the planned writes. Empty list = all good."""
    problems = [f"setting {s.setting_id}: {r.message}"
                for s in steps
                for r in [p.validate_setting_write(s.setting_id, s.value)]
                if not r.ok]
    order = p.validate_voltage_ordering(56.00, 54.60, 44.00)
    if not order.ok:
        problems.append(order.message)
    return problems


def apply_steps(backend: p.Backend, steps: list[p.WriteStep],
                out: Callable[[str], None] = print) -> bool:
    """Write each step (EEPROM, readback-verified). Stops on first failure."""
    for i, s in enumerate(steps, 1):
        res = backend.write_setting(s.setting_id, s.value)
        status = "ok" if res.ok else "FAILED"
        out(f"  [{i}/{len(steps)}] setting {s.setting_id:<3} = {s.value:<5}"
            f" {status:<7} {s.label} — {res.message}")
        if not res.ok:
            out("  Aborting remaining writes — fix the link and re-run"
                " (already-written settings are fine to rewrite).")
            return False
    return True


def ensure_shore_limit(backend: p.Backend,
                       target: float = SHORE_LIMIT_A) -> tuple[str, str]:
    """Move the runtime shore limit to `target` if a remote left it elsewhere.

    Returns (status, message); status is ok | fixed | fail | skip."""
    cfg = backend.read_config()
    if cfg is None:
        return "skip", "config frame did not respond — runtime limit unchanged"
    if abs(cfg.actual_current - target) < 0.05:
        return "ok", f"runtime shore limit already {cfg.actual_current:.1f} A"
    res = backend.set_current_limit(target)
    if res.ok:
        return "fixed", (f"runtime shore limit {cfg.actual_current:.1f} →"
                         f" {target:.1f} A (a remote panel/app may move it again)")
    return "fail", res.message


def print_plan(port: str, steps: list[p.WriteStep], notes: list[str]) -> None:
    if steps:
        print(f"\n== {port} — {len(steps)} write(s) needed:")
        for s in steps:
            note = f"  ({s.note})" if s.note else ""
            print(f"  setting {s.setting_id:<3} = {s.value:<5} {s.label}{note}")
    else:
        print(f"\n== {port} — all settings already match the profile.")
    for n in notes:
        print(f"  note: {n}")


def confirm(port: str, n: int) -> bool:
    if not sys.stdin.isatty():
        print("stdin is not a TTY — re-run with --yes to apply"
              " non-interactively.", file=sys.stderr)
        return False
    ans = input(f"Type 'apply' to write {n} setting(s) to {port}: ")
    return ans.strip().lower() == "apply"


def process_port(port: str, backend: p.Backend, args) -> int:
    """Plan → confirm → write → runtime limit → re-verify. Returns exit code."""
    vals = read_current(backend)
    if all(val is None for val in vals.values()):
        print(f"\n== {port} — no response from the unit."
              " Is it switched ON and is this the right port?")
        return 2

    steps, notes = build_plan(vals)
    print_plan(port, steps, notes)

    problems = validate_plan(steps)
    if problems:
        for msg in problems:
            print(f"  BLOCKED: {msg}", file=sys.stderr)
        return 1

    if args.dry_run:
        cfg = backend.read_config()
        if cfg is not None and abs(cfg.actual_current - SHORE_LIMIT_A) >= 0.05:
            print(f"  would also move the runtime shore limit"
                  f" {cfg.actual_current:.1f} → {SHORE_LIMIT_A:.1f} A")
        return 0

    if steps:
        if not args.yes and not confirm(port, len(steps)):
            print("  Declined — nothing written.")
            return 3
        if not apply_steps(backend, steps):
            return 1

    status, msg = ensure_shore_limit(backend)
    print(f"  shore limit: {msg}")

    results = v.run_checks(backend, charger_disabled=args.charger_disabled)
    v.print_report(port, backend, results)
    ident = backend.identify()
    if ident.model == "Quattro":
        print("  FYI: Quattro detected — the AC2 input current limit"
              " (setting 49) is not part of this profile and was left unchanged.")
    if any(r.status == "FAIL" for r in results) or status == "fail":
        return 1
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description=f"Write the {v.PROFILE_NAME} profile (diff-based,"
                    " readback-verified), then re-verify.")
    ap.add_argument("ports", nargs="*",
                    help="serial port(s) (default: whichever of"
                         f" {', '.join(v.DEFAULT_CANDIDATES)} exist)")
    ap.add_argument("--mock", action="store_true",
                    help="use the simulated inverter (no hardware)")
    ap.add_argument("--dry-run", action="store_true",
                    help="show what would be written and exit")
    ap.add_argument("--yes", action="store_true",
                    help="write without the interactive confirmation")
    ap.add_argument("--address", type=int, default=0,
                    help="VE.Bus address (default 0). Writes to a non-master"
                         " unit of a configured multi-unit system are"
                         " discouraged — VEConfigure manages those system-wide")
    ap.add_argument("--charger-disabled", action="store_true",
                    help="include the manual charger-disable reminder in the"
                         " final report (240V single-120V unit)")
    args = ap.parse_args()

    if args.mock:
        ports = ["MOCK"]
    elif args.ports:
        ports = args.ports
    else:
        ports = [c for c in v.DEFAULT_CANDIDATES if os.path.exists(c)]
        if not ports:
            print(f"No port given and none of {', '.join(v.DEFAULT_CANDIDATES)}"
                  " exist. Pass a port explicitly.", file=sys.stderr)
            return 2

    if args.address != 0:
        print("Warning: writing per-unit settings on a configured multi-unit"
              " system is discouraged (see --help).", file=sys.stderr)

    exit_code = 0
    for port in ports:
        if args.mock:
            backend: p.Backend = p.MockBackend(address=args.address)
        else:
            backend = p.SerialBackend(port=port, address=args.address)
        try:
            backend.open()
        except Exception as err:
            print(v.PORT_BUSY_REMEDY.format(port=port, err=err), file=sys.stderr)
            exit_code = max(exit_code, 2)
            continue
        try:
            exit_code = max(exit_code, process_port(port, backend, args))
        finally:
            backend.close()
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
