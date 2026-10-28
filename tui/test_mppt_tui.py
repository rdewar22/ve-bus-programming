#!/usr/bin/env python3
"""Headless tests for mppt_tui.py against MockVEDirect — no hardware.

    .venv/bin/python tui/test_mppt_tui.py
"""

import unittest

from textual.widgets import Button, DataTable, Input, Select, Static

import mppt_ruixu as m
import vedirect as vd
from mppt_tui import ConfirmSequence, ConfirmWrite, MPPTTUI


async def settle(app, pilot):
    await app.workers.wait_for_complete()
    await pilot.pause()


async def press(app, pilot, selector):
    app.screen.query_one(selector, Button).press()
    await pilot.pause()


async def wait_for_screen(app, pilot, cls, tries=100):
    # Can't settle() here: the worker that opened the modal waits on it.
    for _ in range(tries):
        if isinstance(app.screen, cls):
            return
        await pilot.pause(0.02)
    raise AssertionError(f"{cls.__name__} never opened")


async def confirm(app, pilot):
    await press(app, pilot, "#ok")
    await settle(app, pilot)


def record_notes(app) -> list:
    notes = []
    app.notify = lambda msg, **kw: notes.append(
        (msg, kw.get("severity", "information")))
    return notes


def count_calls(backend, *names) -> dict:
    counts = dict.fromkeys(names, 0)
    for name in names:
        real = getattr(backend, name)

        def wrapped(*a, _real=real, _name=name):
            counts[_name] += 1
            return _real(*a)
        setattr(backend, name, wrapped)
    return counts


class TestMPPTTUI(unittest.IsolatedAsyncioTestCase):
    def make(self, **kw):
        self.backend = vd.MockVEDirect(**kw)
        return MPPTTUI(self.backend, refresh_interval=60)

    async def test_initial_load_fills_everything(self):
        app = self.make()
        async with app.run_test(size=(150, 60)) as pilot:
            await settle(app, pilot)
            self.assertEqual(app.query_one("#raw-table", DataTable).row_count,
                             len(vd.REG_META))
            self.assertGreater(app.query_one("#telemetry", DataTable).row_count, 10)
            bar = str(app.query_one("#status-bar", Static).render())
            self.assertIn("MOCK", bar)
            self.assertIn("SmartSolar MPPT 150/45", bar)
            self.assertIn("v1.65", bar)
            self.assertEqual(app.query_one("#sel-sysv", Select).value, 0)

    async def test_type_user_then_absorption_write(self):
        app = self.make()
        async with app.run_test(size=(150, 60)) as pilot:
            await settle(app, pilot)
            await press(app, pilot, "#type-user")
            self.assertIsInstance(app.screen, ConfirmWrite)
            await confirm(app, pilot)
            self.assertEqual(self.backend.regs[vd.REG_BATTERY_TYPE], 0xFF)

            app.query_one("#in-edf7", Input).value = "57.50"
            await press(app, pilot, "#set-edf7")
            await confirm(app, pilot)
            self.assertEqual(self.backend.regs[vd.REG_ABSORPTION_VOLTAGE], 5750)

    async def test_blocked_write_shows_no_modal(self):
        app = self.make()  # preset battery type: voltages must be refused
        async with app.run_test(size=(150, 60)) as pilot:
            await settle(app, pilot)
            app.query_one("#in-edf7", Input).value = "57.00"
            await press(app, pilot, "#set-edf7")
            await settle(app, pilot)
            self.assertNotIsInstance(app.screen, ConfirmWrite)
            self.assertEqual(self.backend.regs[vd.REG_ABSORPTION_VOLTAGE], 5760)

    async def test_cancel_writes_nothing(self):
        app = self.make()
        async with app.run_test(size=(150, 60)) as pilot:
            await settle(app, pilot)
            app.query_one("#in-edf0", Input).value = "30"
            await press(app, pilot, "#set-edf0")
            await press(app, pilot, "#cancel")
            await settle(app, pilot)
            self.assertEqual(self.backend.regs[vd.REG_MAX_CURRENT], 450)

    async def test_system_voltage_fixed_48(self):
        app = self.make()
        async with app.run_test(size=(150, 60)) as pilot:
            await settle(app, pilot)
            app.query_one("#sel-sysv", Select).value = 48
            await press(app, pilot, "#set-sysv")
            await confirm(app, pilot)
            self.assertEqual(
                self.backend.regs[vd.REG_BATTERY_VOLTAGE_SETTING], 48)

    async def test_ruixu_profile_apply(self):
        app = self.make()
        async with app.run_test(size=(150, 60)) as pilot:
            await settle(app, pilot)
            await press(app, pilot, "#apply-ruixu")
            await wait_for_screen(app, pilot, ConfirmSequence)
            await confirm(app, pilot)
            results = m.run_checks(m.read_current(self.backend))
            self.assertTrue(all(r.status == "PASS" for r in results),
                            [r.detail for r in results])

    async def test_load_tab_disabled_without_load_output(self):
        app = self.make()
        async with app.run_test(size=(150, 60)) as pilot:
            await settle(app, pilot)
            self.assertTrue(app.query_one("#set-load-mode", Button).disabled)

    async def test_load_mode_keeps_reserved_high_nibble(self):
        app = self.make(load_output=True)
        self.backend.regs[vd.REG_LOAD_CONTROL] = 0x81  # timer flag + Auto
        async with app.run_test(size=(150, 60)) as pilot:
            await settle(app, pilot)
            self.assertFalse(app.query_one("#set-load-mode", Button).disabled)
            app.query_one("#sel-load", Select).value = 5
            await press(app, pilot, "#set-load-mode")
            await confirm(app, pilot)
            self.assertEqual(self.backend.regs[vd.REG_LOAD_CONTROL], 0x85)

    async def test_charger_off_enables_remote_bit_first(self):
        app = self.make()
        self.backend.regs[vd.REG_REMOTE_CONTROL] = 0  # firmware < 1.42
        async with app.run_test(size=(150, 60)) as pilot:
            await settle(app, pilot)
            await press(app, pilot, "#mode-off")
            await confirm(app, pilot)
            self.assertEqual(self.backend.regs[vd.REG_REMOTE_CONTROL],
                             vd.REMOTE_ONOFF_BIT)
            self.assertEqual(self.backend.regs[vd.REG_DEVICE_MODE],
                             vd.DEVICE_MODE_OFF)

    async def test_probe_reads_raw_bytes(self):
        app = self.make()
        async with app.run_test(size=(150, 60)) as pilot:
            await settle(app, pilot)
            app.query_one("#in-probe", Input).value = "0xEDF7"
            await press(app, pilot, "#probe")
            await settle(app, pilot)
            out = str(app.query_one("#probe-result", Static).render())
            self.assertIn("80 16", out)        # 5760 little-endian
            self.assertIn("57.60V", out)


    async def test_silent_controller_reads_no_registers(self):
        app = self.make()
        self.backend.silent = True
        calls = count_calls(self.backend, "get", "ping")
        async with app.run_test(size=(150, 60)) as pilot:
            await settle(app, pilot)
            self.assertEqual(calls["get"], 0)   # bailed after the ping
            self.assertEqual(calls["ping"], 1)
            self.assertEqual(app.query_one("#raw-table", DataTable).row_count, 0)

    async def test_dead_link_costs_one_ping_per_telemetry_tick(self):
        app = self.make()
        async with app.run_test(size=(150, 60)) as pilot:
            await settle(app, pilot)
            self.backend.silent = True
            calls = count_calls(self.backend, "get", "ping")
            app.refresh_telemetry()
            await settle(app, pilot)
            self.assertEqual(calls, {"get": 1, "ping": 1})
            app.refresh_telemetry()           # link known dead: ping only
            await settle(app, pilot)
            self.assertEqual(calls, {"get": 1, "ping": 2})

    async def test_one_silent_register_keeps_the_snapshot(self):
        app = self.make()
        last = list(vd.REG_META)[-1]
        real_get = self.backend.get

        def get(reg):
            if reg == last:
                self.backend.healthy = False
                return None, vd.NO_RESPONSE
            return real_get(reg)
        self.backend.get = get
        async with app.run_test(size=(150, 60)) as pilot:
            await settle(app, pilot)
            self.assertEqual(app.query_one("#raw-table", DataTable).row_count,
                             len(vd.REG_META))
            self.assertGreater(app.query_one("#batt-table", DataTable).row_count, 10)
            self.assertTrue(self.backend.healthy)  # the ping restored it

    async def test_charger_on_old_firmware_not_reported_failed(self):
        app = self.make()
        self.backend.fw_word = 0x4116  # v1.16: 0x0200 reads operating state
        real_get = self.backend.get
        self.backend.get = lambda reg: ((0, "") if reg == vd.REG_DEVICE_MODE
                                        else real_get(reg))  # night: not charging
        notes = record_notes(app)
        async with app.run_test(size=(150, 60)) as pilot:
            await settle(app, pilot)
            await press(app, pilot, "#mode-on")
            await confirm(app, pilot)
            self.assertEqual(self.backend.regs[vd.REG_DEVICE_MODE],
                             vd.DEVICE_MODE_ON)
            self.assertFalse([n for n in notes if n[1] == "error"], notes)

    async def test_system_voltage_written_via_edef(self):
        app = self.make()
        self.backend.readonly.add(vd.REG_BATTERY_VOLTAGE_SETTING)  # fw 1.12-1.16
        notes = record_notes(app)
        async with app.run_test(size=(150, 60)) as pilot:
            await settle(app, pilot)
            app.query_one("#sel-sysv", Select).value = 48
            await press(app, pilot, "#set-sysv")
            await confirm(app, pilot)
            self.assertEqual(
                self.backend.regs[vd.REG_BATTERY_VOLTAGE_SETTING], 48)
            self.assertFalse([n for n in notes if n[1] == "error"], notes)

    async def test_auto_detect_on_firmware_without_edea(self):
        app = self.make()
        self.backend.unsupported.add(vd.REG_BATTERY_VOLTAGE_SETTING)  # < 1.12
        self.backend.regs[vd.REG_BATTERY_VOLTAGE] = 24
        notes = record_notes(app)
        async with app.run_test(size=(150, 60)) as pilot:
            await settle(app, pilot)
            app.query_one("#sel-sysv", Select).value = 0
            await press(app, pilot, "#set-sysv")
            await confirm(app, pilot)
            self.assertFalse([n for n in notes if n[1] == "error"], notes)
            self.assertTrue(any("written ✓" in n[0] for n in notes), notes)


if __name__ == "__main__":
    unittest.main(verbosity=2)
