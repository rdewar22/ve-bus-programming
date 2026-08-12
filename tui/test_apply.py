#!/usr/bin/env python3
"""Unit tests for apply_ruixu.py — no hardware required.

    python3 tui/test_apply.py
"""

import unittest

from vebus import protocol as p
import apply_ruixu as a
import verify_ruixu as v
from test_verify import compliant_mock


def factory_mock() -> p.MockBackend:
    """MockBackend as shipped — deliberately deviates from the profile."""
    b = p.MockBackend()
    b.open()
    return b


class TestPlan(unittest.TestCase):
    def test_factory_mock_plan(self):
        # Mock defaults deviate on: absorption 5680, float 5400, S1 bits 11/12
        # clear, AC limit 99, shutdown 4450 — in exactly this write order.
        steps, notes = a.build_plan(a.read_current(factory_mock()))
        self.assertEqual([(s.setting_id, s.value) for s in steps],
                         [(2, 5600), (3, 5460), (1, 0x5DFE), (6, 300),
                          (11, 4400)])
        self.assertEqual(notes, [])

    def test_compliant_unit_plan_is_empty(self):
        steps, notes = a.build_plan(a.read_current(compliant_mock()))
        self.assertEqual(steps, [])
        self.assertEqual(notes, [])

    def test_setting0_read_modify_write(self):
        b = factory_mock()
        b.settings[0] = (b.settings[0] | (1 << 11)) & ~(1 << 5)  # 0x8914
        steps, _ = a.build_plan(a.read_current(b))
        s0 = [s for s in steps if s.setting_id == 0]
        self.assertEqual(len(s0), 1)
        # Only bits 5/11 change; the rest of the base value is preserved.
        self.assertEqual(s0[0].value, 0x8134)
        self.assertEqual(steps[0].setting_id, 0)  # S0 written first

    def test_charge_cluster_order_matches_veconfigure(self):
        b = p.MockBackend(address=0)
        b.open()
        b.settings.update({60: 0, 65: 0, 72: 0, 10: 0, 2: 5000, 3: 5000, 9: 8})
        steps, _ = a.build_plan(a.read_current(b))
        charge_ids = [s.setting_id for s in steps
                      if s.setting_id in dict(a.CHARGE_TARGETS)]
        self.assertEqual(charge_ids, [60, 65, 72, 10, 2, 3, 9])

    def test_missing_setting_noted_not_written(self):
        b = factory_mock()
        b.settings.pop(6)
        steps, notes = a.build_plan(a.read_current(b))
        self.assertNotIn(6, [s.setting_id for s in steps])
        self.assertTrue(any("6 did not respond" in n for n in notes))

    def test_unsupported_setting_noted_not_written(self):
        b = factory_mock()
        b.settings.pop(11)
        b._unsupported.add(11)
        steps, notes = a.build_plan(a.read_current(b))
        self.assertNotIn(11, [s.setting_id for s in steps])
        self.assertTrue(any("11 reads unsupported" in n for n in notes))

    def test_unreadable_flag_register_never_blind_written(self):
        b = factory_mock()
        b.settings.pop(1)
        steps, notes = a.build_plan(a.read_current(b))
        self.assertNotIn(1, [s.setting_id for s in steps])
        self.assertTrue(any("1 did not respond" in n for n in notes))

    def test_plan_passes_validation(self):
        steps, _ = a.build_plan(a.read_current(factory_mock()))
        self.assertEqual(a.validate_plan(steps), [])


class TestApply(unittest.TestCase):
    def test_apply_then_verify_clean(self):
        b = factory_mock()
        steps, _ = a.build_plan(a.read_current(b))
        self.assertTrue(a.apply_steps(b, steps, out=lambda s: None))
        fails = [r for r in v.run_checks(b) if r.status == "FAIL"]
        self.assertEqual(fails, [])

    def test_apply_is_idempotent(self):
        b = factory_mock()
        steps, _ = a.build_plan(a.read_current(b))
        self.assertTrue(a.apply_steps(b, steps, out=lambda s: None))
        steps2, notes2 = a.build_plan(a.read_current(b))
        self.assertEqual(steps2, [])
        self.assertEqual(notes2, [])

    def test_apply_stops_on_write_failure(self):
        b = factory_mock()
        real_write = b.write_setting
        written = []

        def flaky(sid, value, **kw):
            if sid == 3:
                return p.WriteResult(False, False, None, "simulated failure")
            written.append(sid)
            return real_write(sid, value, **kw)

        b.write_setting = flaky
        steps, _ = a.build_plan(a.read_current(b))
        self.assertFalse(a.apply_steps(b, steps, out=lambda s: None))
        self.assertEqual(written, [2])  # stopped at setting 3, wrote nothing after


class TestShoreLimit(unittest.TestCase):
    def test_already_at_target(self):
        b = factory_mock()  # mock runtime limit defaults to 30.0
        status, _ = a.ensure_shore_limit(b)
        self.assertEqual(status, "ok")

    def test_moves_remote_limit_back(self):
        b = factory_mock()
        b._current_limit = 12.0
        status, msg = a.ensure_shore_limit(b)
        self.assertEqual(status, "fixed")
        self.assertEqual(b.read_config().actual_current, 30.0)
        self.assertIn("12.0", msg)

    def test_dead_config_frame_skips(self):
        b = p.MockBackend(address=5)  # absent address → no config response
        b.open()
        status, _ = a.ensure_shore_limit(b)
        self.assertEqual(status, "skip")


if __name__ == "__main__":
    unittest.main(verbosity=2)
