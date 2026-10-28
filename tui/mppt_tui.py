#!/usr/bin/env python3
"""
mppt-tui — interactive terminal UI for a Victron MPPT solar charge controller
(SmartSolar / BlueSolar) over VE.Direct. The solar-charger counterpart of
tui.py — different bus: the VE.Direct JST port / VE.Direct-USB cable, not
the VE.Bus RJ45.

Auto-refreshes live telemetry (reads only — safe) and performs guarded
writes: every write is validated, confirmed in a modal (register, old→new,
decoded meaning) and verified by readback. VE.Direct writes always go to
NVM — there is no RAM-only mode.

    python3 mppt_tui.py                       # auto: /dev/serial/by-id/*VE_Direct*
    python3 mppt_tui.py --port /dev/ttyUSB1
    python3 mppt_tui.py --mock                # no hardware needed
    python3 mppt_tui.py --mock --mock-load    # simulate a model with a load output

Run inside the project venv:  .venv/bin/python tui/mppt_tui.py
See README.md for the safety note about NVM writes.
"""

from __future__ import annotations

import argparse
import asyncio
from typing import Optional

from textual import on, work
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import (Button, DataTable, Footer, Header, Input, Label,
                             Select, Static, TabbedContent, TabPane)

import mppt_ruixu as ruixu
import vedirect as vd


# ─────────────────────────────────────────────────────────────────────────────
# Modals
# ─────────────────────────────────────────────────────────────────────────────

class ConfirmWrite(ModalScreen[bool]):
    """Confirm a single register write (always NVM on VE.Direct)."""

    def __init__(self, reg: int, old: Optional[int], new: int,
                 warning: str = "", write_reg: Optional[int] = None):
        super().__init__()
        self.reg = reg
        self.old = old
        self.new = new
        self.warning = warning
        self.write_reg = write_reg

    def compose(self) -> ComposeResult:
        m = vd.REG_META[self.reg]
        old_raw = "—" if self.old is None else f"0x{self.old:X}"
        via = (f" (written via 0x{self.write_reg:04X})"
               if self.write_reg not in (None, self.reg) else "")
        with Vertical(id="dialog"):
            yield Label("⚠  Confirm write", id="dialog-title")
            yield Static(
                f"[b]Register 0x{self.reg:04X}[/b]{via} — {m.name}\n\n"
                f"  raw:      {old_raw} → [b]0x{self.new:X}[/b]\n"
                f"  decoded:  {vd.format_reg_value(self.reg, self.old)} → "
                f"[b]{vd.format_reg_value(self.reg, self.new)}[/b]",
                id="dialog-body")
            if self.warning:
                yield Static(f"[yellow]⚠ {self.warning}[/yellow]", id="dialog-warn")
            yield Static("Stored in the controller's non-volatile memory "
                         "(limited write cycles).", id="dialog-note")
            with Horizontal(id="dialog-buttons"):
                yield Button("Write", variant="error", id="ok")
                yield Button("Cancel", variant="primary", id="cancel")

    @on(Button.Pressed)
    def handle(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "ok")


class ConfirmSequence(ModalScreen[bool]):
    """Preview + confirm the RUiXU profile writes (deviations only)."""

    def __init__(self, steps: list[ruixu.PlanStep], notes: list[str]):
        super().__init__()
        self.steps = steps
        self.notes = notes

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog-wide"):
            yield Label(f"⚠  Apply {ruixu.PROFILE_NAME}", id="dialog-title")
            yield Static("Only the registers that deviate are written, IN ORDER "
                         "(system voltage and battery type first — custom "
                         "voltages only stick on a user-defined type):",
                         id="dialog-body")
            table = DataTable(id="seq-table", zebra_stripes=True)
            table.add_columns("#", "Register", "New raw", "Change")
            for i, s in enumerate(self.steps, 1):
                table.add_row(str(i), f"0x{s.write_reg:04X}", str(s.value), s.label)
            yield table
            for n in self.notes:
                yield Static(f"[yellow]note: {n}[/yellow]")
            yield Static("[yellow]Charger settings affect battery health. "
                         "Confirm values match your battery's datasheet.[/yellow]",
                         id="dialog-warn")
            with Horizontal(id="dialog-buttons"):
                yield Button("Apply", variant="error", id="ok")
                yield Button("Cancel", variant="primary", id="cancel")

    @on(Button.Pressed)
    def handle(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "ok")


class _ConfirmText(ModalScreen[bool]):
    """Generic yes/no confirmation modal."""

    def __init__(self, text: str):
        super().__init__()
        self.text = text

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label("⚠  Confirm", id="dialog-title")
            yield Static(self.text, id="dialog-body")
            with Horizontal(id="dialog-buttons"):
                yield Button("Confirm", variant="error", id="ok")
                yield Button("Cancel", variant="primary", id="cancel")

    @on(Button.Pressed)
    def handle(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "ok")


# ─────────────────────────────────────────────────────────────────────────────
# Main app
# ─────────────────────────────────────────────────────────────────────────────

# Live dashboard rows, in display order (registers a model lacks are dropped).
TELEMETRY = [
    vd.REG_DEVICE_STATE, vd.REG_ERROR, vd.REG_OFF_REASON,
    vd.REG_PANEL_VOLTAGE, vd.REG_PANEL_CURRENT, vd.REG_PANEL_POWER,
    vd.REG_TRACKER_MODE, vd.REG_CHARGER_VOLTAGE, vd.REG_CHARGER_CURRENT,
    vd.REG_LOAD_CURRENT, vd.REG_BATTERY_TEMP, vd.REG_INTERNAL_TEMP,
    vd.REG_YIELD_TODAY, vd.REG_MAX_POWER_TODAY, vd.REG_YIELD_YESTERDAY,
    vd.REG_USER_YIELD,
]

BATTERY_TABLE = [
    vd.REG_BATTERY_VOLTAGE_SETTING, vd.REG_BATTERY_VOLTAGE,
    vd.REG_BATTERY_TYPE, vd.REG_ABSORPTION_VOLTAGE, vd.REG_FLOAT_VOLTAGE,
    vd.REG_EQUALISE_VOLTAGE, vd.REG_REBULK_OFFSET, vd.REG_MAX_CURRENT,
    vd.REG_ABSORPTION_TIME, vd.REG_TAIL_CURRENT, vd.REG_TEMP_COMPENSATION,
    vd.REG_LOW_TEMP_LEVEL, vd.REG_LOW_TEMP_CURRENT, vd.REG_ADAPTIVE_MODE,
    vd.REG_AUTO_EQUALISE, vd.REG_BMS_PRESENT, vd.REG_CHARGER_MAX_CURRENT,
    vd.REG_ADJ_VOLTAGE_MIN, vd.REG_ADJ_VOLTAGE_MAX,
]

# (register, label, placeholder) — one Input + Write button each.
BATTERY_FIELDS = [
    (vd.REG_ABSORPTION_VOLTAGE, "Absorption (V)", "57.00"),
    (vd.REG_FLOAT_VOLTAGE, "Float (V)", "55.00"),
    (vd.REG_EQUALISE_VOLTAGE, "Equalisation (V)", "64.80"),
    (vd.REG_REBULK_OFFSET, "Re-bulk offset (V)", "0.40"),
    (vd.REG_MAX_CURRENT, "Max current (A)", "45.0"),
    (vd.REG_ABSORPTION_TIME, "Absorption time (h)", "6.00"),
    (vd.REG_TAIL_CURRENT, "Tail current (A)", "2.0"),
    (vd.REG_TEMP_COMPENSATION, "Temp comp (mV/K)", "0.00"),
    (vd.REG_LOW_TEMP_LEVEL, "Low-temp cut-off (°C)", "5.00"),
]

LOAD_TABLE = [
    vd.REG_LOAD_STATE, vd.REG_LOAD_CONTROL, vd.REG_LOAD_CURRENT,
    vd.REG_LOAD_HIGH_LEVEL, vd.REG_LOAD_LOW_LEVEL, vd.REG_LOAD_AES_TIMER,
    vd.REG_LOAD_OFF_REASON,
]

LOAD_FIELDS = [
    (vd.REG_LOAD_HIGH_LEVEL, "Switch-on level (V)", "13.10"),
    (vd.REG_LOAD_LOW_LEVEL, "Switch-off level (V)", "11.10"),
    (vd.REG_LOAD_AES_TIMER, "AES timer (min)", "360"),
]

# A type or system-voltage change moves other values — reload everything.
RELOAD_AFTER = {vd.REG_BATTERY_TYPE, vd.REG_BATTERY_VOLTAGE,
                vd.REG_BATTERY_VOLTAGE_SETTING}


class MPPTTUI(App):
    TITLE = "Victron MPPT (VE.Direct)"
    CSS = """
    Screen { layout: vertical; }
    TabPane { overflow-y: auto; }
    #status-bar { height: 1; background: $panel; color: $text; padding: 0 1; }
    #status-bar.mock { background: $warning; color: black; }
    #status-bar.live { background: $success; color: black; }
    .panel-title { text-style: bold; color: $accent; padding: 1 0 0 0; }
    DataTable { height: auto; max-height: 22; }
    .field-row { height: 3; align: left middle; }
    .field-row Label { width: 24; height: 3; content-align: left middle; }
    .field-row Input { width: 14; }
    .field-row Select { width: 40; }
    .field-row Button { margin: 0 1; }
    #dialog, #dialog-wide {
        background: $surface; border: thick $accent; padding: 1 2;
        width: 70; height: auto; align: center middle;
    }
    #dialog-wide { width: 100; }
    #dialog-title { text-style: bold; color: $warning; width: 100%; }
    #dialog-buttons { height: auto; align: center middle; padding-top: 1; }
    #dialog-buttons Button { margin: 0 1; }
    .actions { height: auto; padding: 1 0; }
    .actions Button { margin: 0 1; }
    """

    BINDINGS = [
        ("r", "refresh_all", "Refresh all"),
        ("q", "quit", "Quit"),
    ]

    def __init__(self, backend, refresh_interval: float = 2.0):
        super().__init__()
        self.backend = backend
        self.refresh_interval = refresh_interval
        self.vals: dict[int, Optional[int]] = {}
        self.errs: dict[int, str] = {}
        self.info: dict[str, str] = {}
        self.fw_word: Optional[int] = None
        self._tele_busy = False
        self._tele_started = False

    # ── layout ────────────────────────────────────────────────────────────────

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Static("Connecting…", id="status-bar")
        with TabbedContent(initial="tab-dash"):
            with TabPane("Dashboard", id="tab-dash"):
                yield Static("Live telemetry (auto-refresh — reads only, NVM-safe)",
                             classes="panel-title")
                yield DataTable(id="telemetry", zebra_stripes=True)
                yield Static("Charger on/off (soft switch, register 0x0200)",
                             classes="panel-title")
                yield Static("—", id="mode-info")
                with Horizontal(classes="actions"):
                    yield Button("Charger ON", id="mode-on")
                    yield Button("Charger OFF", id="mode-off")

            with TabPane("Battery", id="tab-batt"):
                yield Static("Battery settings", classes="panel-title")
                yield DataTable(id="batt-table", zebra_stripes=True)
                yield Static("System voltage & battery type", classes="panel-title")
                with Horizontal(classes="field-row"):
                    yield Label("System voltage")
                    yield Select([("Auto-detect", 0), ("12 V", 12), ("24 V", 24),
                                  ("36 V", 36), ("48 V", 48)], id="sel-sysv")
                    yield Button("Write", id="set-sysv", variant="warning")
                with Horizontal(classes="actions"):
                    yield Button("Battery type → User defined", id="type-user",
                                 variant="warning")
                    yield Button("Battery type → preset / rotary switch",
                                 id="type-preset")
                yield Static("Charge voltages and temp compensation only stick "
                             "on a [b]User defined[/b] battery type.",
                             id="type-note")
                yield Static("Edit individual parameters", classes="panel-title")
                for reg, label, ph in BATTERY_FIELDS:
                    with Horizontal(classes="field-row"):
                        yield Label(label)
                        yield Input(id=f"in-{reg:04x}", placeholder=ph)
                        yield Button("Write", id=f"set-{reg:04x}", variant="warning",
                                     classes="reg-write")
                yield Static(f"{ruixu.PROFILE_NAME} profile", classes="panel-title")
                yield Static("48 V system, user-defined type, 57.00 V absorption, "
                             "55.00 V float — writes only what deviates.")
                with Horizontal(classes="actions"):
                    yield Button("Apply RUiXU solar profile", id="apply-ruixu",
                                 variant="error")

            with TabPane("Load output", id="tab-load"):
                yield Static("Load output", classes="panel-title")
                yield Static("—", id="load-note")
                yield DataTable(id="load-table", zebra_stripes=True)
                with Horizontal(classes="field-row"):
                    yield Label("Mode")
                    yield Select([(n, v) for v, n in vd.LOAD_MODES.items()],
                                 id="sel-load")
                    yield Button("Write", id="set-load-mode", variant="warning")
                for reg, label, ph in LOAD_FIELDS:
                    with Horizontal(classes="field-row"):
                        yield Label(label)
                        yield Input(id=f"in-{reg:04x}", placeholder=ph)
                        yield Button("Write", id=f"set-{reg:04x}", variant="warning",
                                     classes="reg-write")
                yield Static("Switch levels apply to the User 1 / User 2 / AES "
                             "modes; AES timer to AES only.")

            with TabPane("Registers", id="tab-raw"):
                yield Static("Every known register (raw + decoded, read-only)",
                             classes="panel-title")
                yield DataTable(id="raw-table", zebra_stripes=True)
                yield Static("Probe any register id (read-only GET)",
                             classes="panel-title")
                with Horizontal(classes="field-row"):
                    yield Label("Register id (hex)")
                    yield Input(id="in-probe", placeholder="EDF7")
                    yield Button("Read", id="probe")
                yield Static("", id="probe-result")
        yield Footer()

    # ── lifecycle ───────────────────────────────────────────────────────────

    def on_mount(self) -> None:
        self.query_one("#telemetry", DataTable).add_columns(
            "Register", "Measurement", "Raw", "Value")
        self.query_one("#batt-table", DataTable).add_columns(
            "Register", "Setting", "Raw", "Value")
        self.query_one("#load-table", DataTable).add_columns(
            "Register", "Setting", "Raw", "Value")
        self.query_one("#raw-table", DataTable).add_columns(
            "Register", "Name", "Raw", "Decoded")
        # Telemetry polling starts after the first full read so the two don't
        # contend for the port during the initial paint.
        self.refresh_all_worker()

    # ── status bar ────────────────────────────────────────────────────────────

    def update_status(self) -> None:
        bar = self.query_one("#status-bar", Static)
        i = self.info
        ident = (f"model: {i.get('model', '…')}  {i.get('fw', '')}  "
                 f"SN {i.get('serial', '—')}")
        if self.backend.is_mock:
            bar.set_classes(["mock"])
            bar.update(f"● MOCK MODE — no device  |  {self.backend.description}"
                       f"  |  {ident}")
        elif not self.backend.healthy:
            bar.set_classes(["mock"])  # reuse warning style
            bar.update("⚠ NO RESPONSE — is the controller powered and nothing "
                       f"else (a datalogger service) holding "
                       f"{self.backend.description}? Press 'r' to retry.")
        else:
            bar.set_classes(["live"])
            bar.update(f"● LIVE — {self.backend.description}  |  {ident}")

    # ── refresh: telemetry (periodic, reads only) ───────────────────────────

    @work(thread=True, group="telemetry")
    def refresh_telemetry(self) -> None:
        if self._tele_busy:  # skip, don't cancel, if a cycle is still running
            return
        self._tele_busy = True
        try:
            if not self.backend.healthy and self.backend.ping() is None:
                self.call_from_thread(self.update_status)
                return  # still dead: one ping per tick, not a timeout per register
            # Skip registers the model rejected on the full read (flag errors).
            regs = [r for r in TELEMETRY + [vd.REG_DEVICE_MODE]
                    if self.errs.get(r, vd.NO_RESPONSE) == vd.NO_RESPONSE]
            fresh, _, _ = self._read_regs(regs)
            self.call_from_thread(self._merge_telemetry, fresh)
        finally:
            self._tele_busy = False

    def _read_regs(self, regs) -> tuple[dict, dict, bool]:
        """Read `regs` (worker thread). On a silent register, ping once to tell
        a dead link (stop; alive=False) from one register that won't answer."""
        vals, errs = {}, {}
        for reg in regs:
            val, err = self.backend.get(reg)
            if val is not None:
                vals[reg] = val
                continue
            errs[reg] = err
            if err == vd.NO_RESPONSE and self.backend.ping() is None:
                return vals, errs, False
        return vals, errs, True

    def _merge_telemetry(self, fresh: dict) -> None:
        self.vals.update(fresh)  # UI thread only — writes validate against vals
        self._fill_telemetry()

    def _row(self, reg: int) -> tuple[str, str, str, str]:
        v = self.vals.get(reg)
        raw = "—" if v is None else f"0x{v:X}"
        return (f"0x{reg:04X}", vd.REG_META[reg].name, raw,
                vd.format_reg_value(reg, v))

    def _fill_telemetry(self) -> None:
        t = self.query_one("#telemetry", DataTable)
        t.clear()
        for reg in TELEMETRY:
            if reg in self.vals:
                t.add_row(*self._row(reg))
        cur = self.vals.get(vd.REG_CHARGER_CURRENT)
        load = self.vals.get(vd.REG_LOAD_CURRENT)
        if cur is not None and load is not None:
            t.add_row("—", "Battery current (charger − load)", "",
                      f"{(cur - load) / 10:.2f}A")
        mode = self.vals.get(vd.REG_DEVICE_MODE)
        self.query_one("#mode-info", Static).update(
            f"soft switch: [b]{vd.format_reg_value(vd.REG_DEVICE_MODE, mode)}[/b]"
            f"   |   state: {vd.format_reg_value(vd.REG_DEVICE_STATE, self.vals.get(vd.REG_DEVICE_STATE))}")
        self.update_status()

    # ── refresh: everything, on startup / 'r' / after type changes ──────────

    def action_refresh_all(self) -> None:
        self.refresh_all_worker()

    @work(thread=True, exclusive=True, group="settings")
    def refresh_all_worker(self) -> None:
        b = self.backend
        ping = b.ping()
        if ping is None:  # silent link: keep the last snapshot on screen
            self.call_from_thread(self.update_status)
            return
        self.fw_word = ping
        if not self.info:
            pid = b.product_id()
            model, _ = b.get_string(vd.REG_MODEL_NAME)
            serial, _ = b.get_string(vd.REG_SERIAL)
            self.info = {
                "fw": f"fw {vd.format_version(ping)}",
                "model": model or (f"product 0x{pid:04X}" if pid else "?"),
                "serial": serial or "—",
            }
        vals, errs, alive = self._read_regs(vd.REG_META)
        if not alive:
            self.call_from_thread(self.update_status)
            return
        self.call_from_thread(self._apply_snapshot, vals, errs)

    def _apply_snapshot(self, vals: dict, errs: dict) -> None:
        self.vals, self.errs = vals, errs
        self._fill_panels()
        self._start_telemetry()

    def _start_telemetry(self) -> None:
        if self._tele_started:
            return
        self._tele_started = True
        self.refresh_telemetry()
        self.set_interval(self.refresh_interval, self.refresh_telemetry)

    def _fill_panels(self) -> None:
        self._fill_telemetry()
        self._fill_table("#batt-table", BATTERY_TABLE)
        self._fill_load()
        self._fill_raw()
        for reg, _, _ in BATTERY_FIELDS + LOAD_FIELDS:
            self._prefill(reg)
        sysv = self.vals.get(vd.REG_BATTERY_VOLTAGE_SETTING,
                             self.vals.get(vd.REG_BATTERY_VOLTAGE))
        if sysv in (0, *vd.SYSTEM_VOLTAGES):
            self.query_one("#sel-sysv", Select).value = sysv
        btype = self.vals.get(vd.REG_BATTERY_TYPE)
        note = self.query_one("#type-note", Static)
        if btype == vd.BATTERY_TYPE_USER:
            note.update("Battery type is [b]User defined[/b] — charge voltages "
                        "are editable.")
        else:
            note.update(f"[yellow]Battery type is "
                        f"{vd.format_reg_value(vd.REG_BATTERY_TYPE, btype)} — set "
                        "User defined before editing charge voltages or temp "
                        "compensation.[/yellow]")

    def _fill_table(self, sel: str, regs: list[int]) -> None:
        t = self.query_one(sel, DataTable)
        t.clear()
        for reg in regs:
            if reg in self.vals:
                t.add_row(*self._row(reg))

    def _has_load(self) -> bool:
        caps = self.vals.get(vd.REG_CAPABILITIES)
        if caps is not None:
            return bool(caps & vd.CAP_LOAD_OUTPUT)
        return vd.REG_LOAD_CONTROL in self.vals

    def _fill_load(self) -> None:
        has = self._has_load()
        self.query_one("#load-note", Static).update(
            "This model has a load output." if has else
            "[yellow]This model has no load output (only the 10/15/20A "
            "models do) — nothing to configure here.[/yellow]")
        for w in self.query("#tab-load Select, #tab-load Input, #tab-load Button"):
            w.disabled = not has
        self._fill_table("#load-table", LOAD_TABLE)
        ctl = self.vals.get(vd.REG_LOAD_CONTROL)
        if ctl is not None and (ctl & vd.LOAD_MODE_MASK) in vd.LOAD_MODES:
            self.query_one("#sel-load", Select).value = ctl & vd.LOAD_MODE_MASK

    def _fill_raw(self) -> None:
        t = self.query_one("#raw-table", DataTable)
        t.clear()
        for reg in sorted(vd.REG_META):
            if reg in self.vals:
                t.add_row(*self._row(reg))
            else:
                t.add_row(f"0x{reg:04X}", vd.REG_META[reg].name, "—",
                          f"({self.errs.get(reg, 'not read')})")

    def _prefill(self, reg: int) -> None:
        inp = self.query_one(f"#in-{reg:04x}", Input)
        raw = self.vals.get(reg)
        if not inp.value and raw is not None:
            m = vd.REG_META[reg]
            inp.value = (f"{vd.to_eng(reg, raw):.2f}" if m.scale != 1.0
                         else str(raw))

    # ── writes ────────────────────────────────────────────────────────────────

    async def _confirm_and_write(self, reg: int, new: int, warning: str = "",
                                 write_reg: Optional[int] = None,
                                 accept=None) -> bool:
        """Validate, confirm, write (to `write_reg` if given), read back `reg`
        and check it (== new, or `accept(readback)`). Returns ok."""
        vr = vd.validate_write(reg, new, self.vals)
        if not vr.ok:
            self.notify(vr.message, severity="error", timeout=8)
            return False
        if not await self.push_screen_wait(ConfirmWrite(
                reg, self.vals.get(reg), new, warning, write_reg)):
            self.notify("Write cancelled.", severity="information")
            return False
        ok, err = await asyncio.to_thread(self.backend.set, write_reg or reg, new)
        readback = None
        if ok:
            readback, rerr = await asyncio.to_thread(self.backend.get, reg)
            ok = readback is not None and (
                accept(readback) if accept else readback == new)
            err = "" if ok else f"readback got {readback} {rerr}".strip()
            if readback is not None:
                self.vals[reg] = readback
        name = vd.REG_META[reg].name
        if ok and accept:
            self.notify(f"{name} written ✓ readback now "
                        f"{vd.format_reg_value(reg, readback)}", timeout=6)
        elif ok:
            self.notify(f"{name} = {vd.format_reg_value(reg, new)} ✓ readback "
                        "confirmed", timeout=6)
        else:
            self.notify(f"{name} FAILED: {err}", severity="error", timeout=10)
        if reg in RELOAD_AFTER:
            self.refresh_all_worker()
        else:
            self._fill_panels()
        return ok

    def _parse_field(self, reg: int) -> Optional[int]:
        try:
            eng = float(self.query_one(f"#in-{reg:04x}", Input).value)
        except ValueError:
            self.notify("Enter a valid number.", severity="error")
            return None
        return vd.to_raw(reg, eng)

    @on(Button.Pressed, ".reg-write")
    @work
    async def write_field(self, event: Button.Pressed) -> None:
        reg = int(event.button.id.removeprefix("set-"), 16)
        raw = self._parse_field(reg)
        if raw is not None:
            await self._confirm_and_write(reg, raw)

    @on(Button.Pressed, "#set-sysv")
    @work
    async def write_sysv(self) -> None:
        value = self.query_one("#sel-sysv", Select).value
        if not isinstance(value, int):
            self.notify("Pick a system voltage.", severity="error")
            return
        # Written via 0xEDEF (every firmware); verified on 0xEDEA, which shows
        # auto vs fixed but is only writable from fw 1.17 (readable from 1.12).
        has_setting = vd.REG_BATTERY_VOLTAGE_SETTING in self.vals
        reg = (vd.REG_BATTERY_VOLTAGE_SETTING if has_setting
               else vd.REG_BATTERY_VOLTAGE)
        accept = None
        if not has_setting and value == 0:
            # 0xEDEF reads the operating voltage, so auto can't be read back.
            accept = lambda rb: rb in vd.SYSTEM_VOLTAGES  # noqa: E731
        await self._confirm_and_write(
            reg, value, warning="Changing the system voltage resets the "
            "voltage-dependent limits — re-check every charge voltage after.",
            write_reg=vd.REG_BATTERY_VOLTAGE, accept=accept)

    @on(Button.Pressed, "#type-user")
    @work
    async def type_user(self) -> None:
        await self._confirm_and_write(
            vd.REG_BATTERY_TYPE, vd.BATTERY_TYPE_USER,
            warning="Charge voltages stay at their current values until you "
            "edit them.")

    @on(Button.Pressed, "#type-preset")
    @work
    async def type_preset(self) -> None:
        await self._confirm_and_write(
            vd.REG_BATTERY_TYPE, 1,
            warning="On rotary-switch chargers (30A+) any non-user value "
            "reverts to the rotary switch position; on 10/15/20A models this "
            "selects the built-in GEL preset. Custom voltages are replaced.")

    @on(Button.Pressed, "#set-load-mode")
    @work
    async def write_load_mode(self) -> None:
        mode = self.query_one("#sel-load", Select).value
        cur = self.vals.get(vd.REG_LOAD_CONTROL)
        if not isinstance(mode, int) or cur is None:
            self.notify("Load mode unavailable — refresh first.", severity="error")
            return
        # Read-modify-write: the high nibble is reserved (timer flag).
        new = (cur & ~vd.LOAD_MODE_MASK & 0xFF) | mode
        warning = ("'Always on' has no low-battery guard." if mode == 4 else "")
        await self._confirm_and_write(vd.REG_LOAD_CONTROL, new, warning)

    @on(Button.Pressed, "#mode-on")
    @work
    async def charger_on(self) -> None:
        await self._set_device_mode(vd.DEVICE_MODE_ON)

    @on(Button.Pressed, "#mode-off")
    @work
    async def charger_off(self) -> None:
        await self._set_device_mode(vd.DEVICE_MODE_OFF)

    async def _set_device_mode(self, mode: int) -> None:
        name = "ON" if mode == vd.DEVICE_MODE_ON else "OFF"
        if not await self.push_screen_wait(_ConfirmText(
                f"Switch the solar charger [b]{name}[/b]?\n\n"
                "This changes charging immediately and persists until switched "
                "back (here, VictronConnect, or the display).")):
            return
        b = self.backend
        # Firmware < 1.42 ignores 0x0200 until remote on/off is enabled
        # (bits only set; cleared on restart).
        mask = self.vals.get(vd.REG_REMOTE_CONTROL)
        if mask is not None and not mask & vd.REMOTE_ONOFF_BIT:
            ok, err = await asyncio.to_thread(
                b.set, vd.REG_REMOTE_CONTROL, mask | vd.REMOTE_ONOFF_BIT)
            if not ok:
                self.notify(f"Enabling remote on/off failed: {err}",
                            severity="error", timeout=10)
                return
        ok, err = await asyncio.to_thread(b.set, vd.REG_DEVICE_MODE, mode)
        readback, _ = await asyncio.to_thread(b.get, vd.REG_DEVICE_MODE)
        on_now = readback == vd.DEVICE_MODE_ON
        if ok and vd.version_number(self.fw_word or 0) < 0x117:
            # fw <= 1.16 reads back whether it is charging, not the switch.
            self.notify(f"Charger {name} accepted ✓ (this firmware can't read "
                        "the switch back — watch the charge state)", timeout=8)
        elif ok and readback is not None and on_now == (mode == vd.DEVICE_MODE_ON):
            self.vals[vd.REG_DEVICE_MODE] = readback
            self.notify(f"Charger switched {name} ✓ readback confirmed", timeout=6)
        else:
            self.notify(f"Charger {name} FAILED: {err or f'readback {readback}'}",
                        severity="error", timeout=10)
        self.refresh_telemetry()

    @on(Button.Pressed, "#apply-ruixu")
    @work
    async def apply_ruixu(self) -> None:
        vals = await asyncio.to_thread(ruixu.read_current, self.backend)
        steps, notes = ruixu.build_plan(vals)
        if not steps:
            msg = "Already matches the RUiXU solar profile."
            if notes:
                msg += " Notes: " + "; ".join(notes)
            self.notify(msg, severity="warning" if notes else "information",
                        timeout=10)
            return
        if not await self.push_screen_wait(ConfirmSequence(steps, notes)):
            self.notify("RUiXU profile cancelled.", severity="information")
            return
        lines: list[str] = []
        ok = await asyncio.to_thread(ruixu.apply_steps, self.backend, steps,
                                     lines.append)
        for line in lines:
            self.notify(line.strip(), severity="information" if ok else "warning",
                        timeout=8)
        self.notify(f"RUiXU profile: {'all writes confirmed' if ok else 'STOPPED'}",
                    severity="information" if ok else "error", timeout=10)
        self.refresh_all_worker()

    # ── register probe (read-only) ──────────────────────────────────────────

    @on(Button.Pressed, "#probe")
    @work
    async def probe(self) -> None:
        text = self.query_one("#in-probe", Input).value.strip().lower()
        try:
            reg = int(text.removeprefix("0x"), 16)
        except ValueError:
            self.notify("Enter a hex register id, e.g. EDF7.", severity="error")
            return
        if not 0 <= reg <= 0xFFFF:
            self.notify("Register ids are 16-bit (0000–FFFF).", severity="error")
            return
        data, err = await asyncio.to_thread(self.backend.get_bytes, reg)
        out = self.query_one("#probe-result", Static)
        if data is None:
            out.update(f"0x{reg:04X}: [red]{err}[/red]")
            return
        le = int.from_bytes(data, "little") if data else 0
        known = (f"  →  {vd.REG_META[reg].name}: "
                 f"{vd.format_reg_value(reg, le)}" if reg in vd.REG_META else "")
        out.update(f"0x{reg:04X}: {len(data)} byte(s)  [b]{data.hex(' ').upper()}"
                   f"[/b]  (LE uint {le}){known}")


# ─────────────────────────────────────────────────────────────────────────────

def open_backend(port: Optional[str], mock: bool, fallback: bool,
                 mock_load: bool = False):
    """Open the requested/auto-detected port, else (optionally) the mock."""
    if mock:
        return vd.MockVEDirect(load_output=mock_load)
    port = port or vd.find_default_port() or "/dev/ttyUSB0"
    try:
        b = vd.SerialVEDirect(port)
        b.open()
        return b
    except Exception as err:
        if not fallback:
            raise SystemExit(f"Could not open {port}: {err}")
        b = vd.MockVEDirect(load_output=mock_load)
        b.description = f"MOCK (fallback — {port} did not open: {err})"
        return b


def main() -> None:
    ap = argparse.ArgumentParser(description="Victron MPPT (VE.Direct) TUI")
    ap.add_argument("--port", default=None,
                    help="VE.Direct serial port (default: auto-detect "
                         "/dev/serial/by-id/*VE_Direct*, else /dev/ttyUSB0; "
                         "falls back to mock)")
    ap.add_argument("--mock", action="store_true",
                    help="run with a simulated controller, no hardware needed")
    ap.add_argument("--mock-load", action="store_true",
                    help="simulate a model with a load output (10/15/20A)")
    ap.add_argument("--no-fallback", action="store_true",
                    help="do not fall back to mock if the port can't be opened")
    ap.add_argument("--interval", type=float, default=2.0,
                    help="telemetry auto-refresh interval, seconds (default 2)")
    args = ap.parse_args()

    backend = open_backend(args.port, args.mock, not args.no_fallback,
                           args.mock_load)
    try:
        MPPTTUI(backend, refresh_interval=args.interval).run()
    finally:
        backend.close()


if __name__ == "__main__":
    main()
