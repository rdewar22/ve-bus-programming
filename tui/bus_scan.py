#!/usr/bin/env python3
"""
bus_scan.py — read-only VE.Bus multi-unit scanner (single MK3-USB).

Enumerates every device on the VE.Bus behind one MK3 adapter (parallel /
split-phase systems), then dumps a side-by-side per-unit status report:
firmware version, front-panel LEDs, all RAM telemetry variables, the DC and
AC info frames ('F' 0/1/2) and the shore config ('F' 5).

Reading the same items under each address IS the scoping experiment: rows
that differ across addresses follow the selected device (per-unit data);
identical rows are system-wide or master-only. Record the results in
FINDINGS §12.

Strictly read-only: 'A' address select, 'V', 'L', 'F' 0/1/2/5, and Winmon
0x30/0x36 reads. No writes, no 'S' state changes. The selected address is
ALWAYS restored to 0 on exit (even on Ctrl-C) so a production reader that
starts afterwards talks to the master again.

    python3 bus_scan.py --mock                    # no hardware needed
    python3 bus_scan.py --port /dev/inverter -v -o scan.csv

On Grounded Pis the production service holds the port exclusively — stop it
first:  sudo systemctl stop grounded-inverter   (start it again afterwards).
Both inverters must be switched ON: a sleeping unit answers nothing.
"""

from __future__ import annotations

import argparse
import csv
import sys
import time
from typing import Optional

from vebus import protocol as p

DEFAULT_CANDIDATES = ["/dev/ttyUSB0", "/dev/inverter"]

PORT_BUSY_REMEDY = """\
Could not open {port}: {err}

If another program holds the port (the MK3 is exclusive-open):
  * On a Grounded Pi, stop the production reader first:
        sudo systemctl stop grounded-inverter
    …run the scan, then restore it:
        sudo systemctl start grounded-inverter
  * Also check for VEConfigure / VictronConnect / another tui.py instance."""


# ─────────────────────────────────────────────────────────────────────────────
# CLI helpers
# ─────────────────────────────────────────────────────────────────────────────

def parse_addresses(spec: str) -> list[int]:
    """Parse '0-31', '0,1,4', '0-3,8' → sorted unique address list."""
    out: set[int] = set()
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            lo_s, hi_s = part.split("-", 1)
            lo, hi = int(lo_s), int(hi_s)
        else:
            lo = hi = int(part)
        if lo > hi:
            lo, hi = hi, lo
        out.update(range(lo, hi + 1))
    bad = [a for a in out if not 0 <= a <= 0x1F]
    if bad:
        raise ValueError(f"VE.Bus addresses must be 0-31, got {sorted(bad)}")
    if not out:
        raise ValueError("empty address spec")
    return sorted(out)


def open_scan_backend(port: Optional[str], mock: bool,
                      verbose: bool) -> p.Backend:
    """Open the backend for scanning. No mock fallback — a real-port failure
    should be loud, not silently produce fake scan results."""
    if mock:
        b = p.MockBackend()
        b.open()
        return b
    candidates = [port] if port else list(DEFAULT_CANDIDATES)
    if "/dev/inverter" not in candidates:
        candidates.append("/dev/inverter")
    last_err: Optional[Exception] = None
    for cand in candidates:
        try:
            b = p.SerialBackend(port=cand)
            if verbose:
                b.trace = lambda d, data: print(f"      [{d}] {data.hex()}")
            b.open()
            return b
        except Exception as e:
            last_err = e
    print(PORT_BUSY_REMEDY.format(port=" / ".join(candidates), err=last_err),
          file=sys.stderr)
    raise SystemExit(1)


# ─────────────────────────────────────────────────────────────────────────────
# Scan phases
# ─────────────────────────────────────────────────────────────────────────────

def presence_scan(backend: p.Backend, addrs: list[int]) -> list[int]:
    """Phase A: which addresses answer a Winmon read of RAM var 4 (UBat)?"""
    print(f"Phase A — presence scan over {len(addrs)} address(es) "
          "(absent addresses time out; a full 0-31 sweep takes a minute or two "
          "at 2400 baud)…")
    found: list[int] = []
    saved_retries = getattr(backend, "READ_RETRIES", None)
    backend.READ_RETRIES = 1  # fast fail on silence; full retries in phase B
    try:
        for addr in addrs:
            backend.select_address(addr)
            present = backend.read_ramvar(4) is not None
            print(f"    addr {addr:2d}: {'FOUND' if present else '—'}")
            if present:
                found.append(addr)
    finally:
        if saved_retries is not None:
            backend.READ_RETRIES = saved_retries
    return found


def snapshot_address(backend: p.Backend, addr: int) -> dict:
    """Phase B: full read-only snapshot of one address."""
    print(f"Phase B — full snapshot of addr {addr} …")
    backend.select_address(addr)
    snap: dict = {}
    snap["version"] = backend.read_version()          # passive 0x56 window
    snap["leds"] = backend.read_leds()                # 'L'
    ram: dict[int, tuple] = {}
    for tv in p.TELEMETRY:
        raw = backend.read_ramvar(tv.var_id)
        info = backend.read_ramvar_info(tv.var_id)
        ram[tv.var_id] = (raw, p.format_telemetry(tv.var_id, raw, info))
    snap["ram"] = ram
    snap["dc"] = backend.read_dc_info()               # 'F' 0
    snap["ac1"] = backend.read_ac_info(1)             # 'F' 1
    snap["ac2"] = backend.read_ac_info(2)             # 'F' 2 — the L2 question
    snap["config"] = backend.read_config()            # 'F' 5
    got = sum(1 for r, _ in ram.values() if r is not None)
    print(f"    version {'✓' if snap['version'] is not None else '✗'}   "
          f"LEDs {'✓' if snap['leds'] else '✗'}   "
          f"RAM {got}/{len(p.TELEMETRY)}   "
          f"F0 {'✓' if snap['dc'] else '✗'}   "
          f"F1 {'✓' if snap['ac1'] else '✗'}   "
          f"F2 {'✓' if snap['ac2'] else '✗'}   "
          f"F5 {'✓' if snap['config'] else '✗'}")
    return snap


# ─────────────────────────────────────────────────────────────────────────────
# Report
# ─────────────────────────────────────────────────────────────────────────────

NO_RESPONSE = "(no response)"


class Row:
    """One report row: a label plus per-address display cell and compare value.

    `shared` marks rows expected identical on one battery bank (battery V,
    SoC) — shown, but excluded from the per-unit verdict."""

    def __init__(self, label: str, shared: bool = False,
                 kind: str = "", ident: str = ""):
        self.label = label
        self.shared = shared
        self.kind = kind          # CSV: version|led|config|dc|ac1|ac2|ram
        self.ident = ident        # CSV: var id / field name
        self.cells: dict[int, str] = {}
        self.values: dict[int, object] = {}   # float|str|None per address
        self.raws: dict[int, str] = {}        # raw hex/source for CSV

    def put(self, addr: int, cell: str, value=None, raw: str = "") -> None:
        self.cells[addr] = cell
        self.values[addr] = value
        self.raws[addr] = raw

    def differs(self, addrs: list[int]) -> Optional[bool]:
        """True/False when comparable across addresses, else None."""
        vals = [self.values[a] for a in addrs
                if a in self.values and self.values[a] is not None]
        if len(vals) < 2:
            return None
        if all(isinstance(v, str) for v in vals):
            return len(set(vals)) > 1
        nums = [float(v) for v in vals if isinstance(v, (int, float))]
        if len(nums) < 2:
            return None
        span = max(nums) - min(nums)
        # Tolerate read-to-read drift: only a clear gap counts as "differs".
        return span > max(0.25, 0.05 * max(abs(n) for n in nums))


def _led_text(leds: Optional[p.LedInfo]) -> tuple[str, str]:
    if leds is None:
        return NO_RESPONSE, ""
    on = ",".join(n for n, s in leds.states() if s == "on") or "—"
    blink = ",".join(n for n, s in leds.states() if s == "blink") or "—"
    return f"{on} / {blink}", f"on=0x{leds.on:02X} blink=0x{leds.blink:02X}"


def build_rows(snapshots: dict[int, dict]) -> list[Row]:
    addrs = sorted(snapshots)
    rows: list[Row] = []

    def row(label: str, shared: bool = False, kind: str = "",
            ident: str = "") -> Row:
        r = Row(label, shared, kind, ident)
        rows.append(r)
        return r

    r = row("Version", kind="version")
    for a in addrs:
        v = snapshots[a]["version"]
        r.put(a, f"0x{v:08X}" if v is not None else NO_RESPONSE,
              value=(f"0x{v:08X}" if v is not None else None))

    r = row("LEDs on / blink", kind="led")
    for a in addrs:
        cell, raw = _led_text(snapshots[a]["leds"])
        r.put(a, cell, value=(cell if snapshots[a]["leds"] else None), raw=raw)

    r = row("F5 shore limit / switch", kind="config")
    for a in addrs:
        cfg = snapshots[a]["config"]
        if cfg is None:
            r.put(a, NO_RESPONSE)
        else:
            r.put(a, f"{cfg.actual_current:.1f} A / {cfg.switch_state_name}",
                  value=f"{cfg.actual_current:.1f}/{cfg.switch_state_name}",
                  raw=f"switchreg=0x{cfg.switch_register:02X}")

    dc_fields = [
        ("F0 DC battery V", "dc_voltage", "V", True),
        ("F0 DC I → inverter", "dc_current_to_inverter", "A", False),
        ("F0 DC I ← charger", "dc_current_from_charger", "A", False),
        ("F0 inverter freq", "inverter_frequency", "Hz", False),
    ]
    for label, attr, unit, shared in dc_fields:
        r = row(label, shared=shared, kind="dc", ident=attr)
        for a in addrs:
            dc = snapshots[a]["dc"]
            if dc is None:
                r.put(a, NO_RESPONSE)
            else:
                val = getattr(dc, attr)
                r.put(a, f"{val:.2f} {unit}", value=val)

    for key in ("ac1", "ac2"):
        fx = key.upper().replace("AC", "F")  # F1 / F2
        r = row(f"{fx} phase / num / state", kind=key, ident="phase_state")
        for a in addrs:
            ac = snapshots[a][key]
            if ac is None:
                r.put(a, NO_RESPONSE)
            else:
                r.put(a, f"{ac.phase} / {ac.num_phases} / {ac.device_state_name}",
                      value=f"{ac.phase}/{ac.num_phases}/{ac.device_state_name}")
        for label, attr, unit in (
                (f"{fx} mains V", "mains_voltage", "V"),
                (f"{fx} mains I", "mains_current", "A"),
                (f"{fx} inverter V", "inverter_voltage", "V"),
                (f"{fx} inverter I", "inverter_current", "A"),
                (f"{fx} mains freq", "mains_frequency", "Hz")):
            r = row(label, kind=key, ident=attr)
            for a in addrs:
                ac = snapshots[a][key]
                if ac is None:
                    r.put(a, NO_RESPONSE)
                else:
                    val = getattr(ac, attr)
                    r.put(a, f"{val:.2f} {unit}", value=val)

    for tv in p.TELEMETRY:
        shared = tv.var_id in (4, 13)   # battery V / SoC: one shared bank
        r = row(f"RAM {tv.var_id:2d} {tv.label}", shared=shared,
                kind="ram", ident=str(tv.var_id))
        for a in addrs:
            raw, shown = snapshots[a]["ram"][tv.var_id]
            if raw is None:
                r.put(a, NO_RESPONSE)
            else:
                num = p.telemetry_value(tv.var_id, raw,
                                        p.DEFAULT_RAMVAR_INFO.get(tv.var_id))
                r.put(a, f"{shown}  (0x{raw:04X})",
                      value=(num if num is not None else shown),
                      raw=f"0x{raw:04X}")
    return rows


def print_report(snapshots: dict[int, dict], port_desc: str,
                 probed: str) -> None:
    addrs = sorted(snapshots)
    rows = build_rows(snapshots)

    print()
    print("═" * 78)
    print(f"VE.Bus address scan — {port_desc}")
    print(f"Probed {probed}.  Found {len(addrs)} device(s): "
          + ", ".join(f"addr {a}" for a in addrs))
    print("═" * 78)

    label_w = max(len(r.label) for r in rows) + 2
    cell_w = max(24, *(len(c) for r in rows for c in r.cells.values())) + 2
    header = "Item".ljust(label_w) + "".join(
        f"addr {a}".ljust(cell_w) for a in addrs)
    print(header)
    print("─" * len(header))
    for r in rows:
        line = r.label.ljust(label_w) + "".join(
            r.cells.get(a, NO_RESPONSE).ljust(cell_w) for a in addrs)
        print(line + ("   [shared bank]" if r.shared and len(addrs) > 1 else ""))

    # ── scope analysis ──────────────────────────────────────────────────────
    print()
    if len(addrs) > 1:
        differing = [r.label for r in rows
                     if not r.shared and r.differs(addrs) is True]
        same = [r.label for r in rows
                if not r.shared and r.differs(addrs) is False]
        print(f"Scope analysis across {len(addrs)} addresses: "
              f"{len(differing)} row(s) clearly differ (follow the selected "
              f"address = per-unit), {len(same)} look identical "
              "(system-wide/master-only or genuinely equal right now).")
        if differing:
            print("  Per-unit rows: " + "; ".join(differing))
        print("  Battery voltage / SoC are excluded from the verdict — one "
              "shared bank looks identical either way. Small drift between "
              "reads is normal; judge by states, LEDs and currents.")
    else:
        only = addrs[0] if addrs else None
        print(f"Only address {only} responded. Likely causes:")
        print("  1. The second unit is switched off or asleep — both front "
              "switches must be ON during the scan.")
        print("  2. The VE.Bus cable between the units is unseated.")
        print("  3. The units hold addresses outside the probed range — rerun "
              "with --addresses 0-31.")
        print("  4. Winmon reads may not follow the address on this firmware — "
              "check the F2 row above: a valid L2 frame under addr 0 still "
              "carries the second unit's AC data.")

    ac1 = snapshots.get(0, {}).get("ac1")
    if ac1 is not None:
        print(f"  L1 frame reports num_phases={ac1.num_phases}. Treat as a "
              "hint only (misreported on some MP-II firmwares); the F2 row is "
              "the ground truth for whether an L2 exists.")
    if snapshots.get(0, {}).get("ac2") is not None:
        print("  ✓ F2 (AC L2) answered under addr 0 — the second unit's AC "
              "data is reachable WITHOUT re-addressing; production can simply "
              "request 'F' 2 each poll cycle.")

    # LED scoping verdict — decides whether per-unit LED reads are trustworthy
    # ('L' could follow the address like Winmon, or ignore it like 'F' frames).
    if len(addrs) > 1:
        led_cells = {a: _led_text(snapshots[a]["leds"])[0]
                     for a in addrs if snapshots[a].get("leds")}
        if len(led_cells) > 1 and len(set(led_cells.values())) > 1:
            print("  ★ LED VERDICT: LEDs DIFFER per address — 'L' follows the "
                  "selected address. Per-unit LED panels are trustworthy; set "
                  "VEBUS_L2_LEDS=1 in the van's server/.env to enable them.")
        elif len(led_cells) > 1:
            print("  LED VERDICT: identical across addresses — INCONCLUSIVE "
                  "unless the units were in visibly different states during "
                  "the scan (e.g. one unit in Overload). Re-run while forcing "
                  "a per-unit condition before trusting addressed LED reads.")


def write_csv(path: str, snapshots: dict[int, dict]) -> None:
    rows = build_rows(snapshots)
    with open(path, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["address", "kind", "id", "label", "raw", "value"])
        for a in sorted(snapshots):
            for r in rows:
                cell = r.cells.get(a, NO_RESPONSE)
                w.writerow([a, r.kind, r.ident, r.label,
                            r.raws.get(a, ""),
                            "no_response" if cell == NO_RESPONSE else cell])
    print(f"\nCSV written to {path}")


# ─────────────────────────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(
        description="Read-only VE.Bus multi-unit scanner (single MK3-USB). "
                    "Enumerates device addresses and prints a side-by-side "
                    "per-unit status report.")
    ap.add_argument("--port", default=None,
                    help="serial port (default: try /dev/ttyUSB0 then "
                         "/dev/inverter)")
    ap.add_argument("--addresses", default="0-31",
                    help="addresses to probe: '0-31' (default), '0-7', "
                         "'0,1,4'. Address 0 is always included.")
    ap.add_argument("--mock", action="store_true",
                    help="scan a simulated two-unit system, no hardware needed")
    ap.add_argument("-o", "--output", metavar="FILE",
                    help="also write the report as CSV")
    ap.add_argument("-v", "--verbose", action="store_true",
                    help="hex-dump all TX/RX traffic (real hardware only; "
                         "capture this for FINDINGS §12)")
    args = ap.parse_args()

    try:
        addrs = parse_addresses(args.addresses)
    except ValueError as e:
        ap.error(str(e))
    if 0 not in addrs:
        addrs = [0] + addrs   # master/standalone is always worth probing

    backend = open_scan_backend(args.port, args.mock, args.verbose)
    print(f"Connected: {backend.description}")

    started = time.monotonic()
    snapshots: dict[int, dict] = {}
    try:
        found = presence_scan(backend, addrs)
        if not found:
            print("\nNo device answered on any probed address. Check the MK3 "
                  "cabling, that the inverters are switched ON, and that no "
                  "other program holds the port.")
            return 1
        for addr in found:
            snapshots[addr] = snapshot_address(backend, addr)
    finally:
        # ALWAYS restore address 0 — an MK3 left pointed at unit 2 would feed
        # the second unit's data to a production reader started afterwards.
        try:
            backend.select_address(0)
            print("Restored VE.Bus address 0.")
        except Exception as e:
            print(f"WARNING: could not restore address 0 ({e}). "
                  "Power-cycle the MK3 (unplug USB) before restarting "
                  "grounded-inverter.", file=sys.stderr)
        finally:
            backend.close()

    probed = args.addresses if args.addresses else "0-31"
    print_report(snapshots, backend.description, probed)
    if args.output:
        write_csv(args.output, snapshots)
    print(f"\nScan finished in {time.monotonic() - started:.0f}s.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
