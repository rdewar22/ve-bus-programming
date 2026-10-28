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


if __name__ == "__main__":
    unittest.main(verbosity=2)
