#!/usr/bin/env python3
"""Unit tests for verify_ruixu.py — no hardware required.

    python3 tui/test_verify.py
"""

import unittest

from vebus import protocol as p
import verify_ruixu as v


def compliant_mock() -> p.MockBackend:
    """MockBackend configured to fully match the RUiXU profile."""
    b = p.MockBackend()
    b.open()
    b.settings.update({2: 5600, 3: 5460, 6: 300, 9: 1, 10: 1,
                       11: 4400, 12: 100})
    # Setting 0 base 0x8134 already has PowerAssist (bit 5) set and the
    # adaptive-charge bit (11) clear; force both anyway for clarity.
    b.settings[0] = (b.settings[0] | (1 << 5)) & ~(1 << 11)
    b.settings[1] = b.settings[1] | (1 << 11) | (1 << 12)
    return b


def by_name(results, name):
    matches = [r for r in results if r.name == name]
    assert len(matches) == 1, f"expected exactly one result named {name!r}"
    return matches[0]


class TestCompliantUnit(unittest.TestCase):
    def setUp(self):
        self.results = v.run_checks(compliant_mock())

    def test_no_failures(self):
        fails = [r for r in self.results if r.status == "FAIL"]
        self.assertEqual(fails, [])

    def test_required_checks_pass(self):
        for name in [
            "AC input current limit 30A",
            "Dynamic current limiter enabled",
            "Accept wide input frequency range",
            "PowerAssist enabled",
            "Lithium: fixed charge curve (S0 bit 11 clear)",
            "Lithium: charge characteristic fixed",
            "Absorption voltage 56.00V",
            "Float voltage 54.60V",
            "DC input low shutdown 44.00V",
            "DC input low restart +1.00V",
        ]:
            self.assertEqual(by_name(self.results, name).status, "PASS", name)

    def test_live_frequency_passes_on_mock(self):
        r = by_name(self.results, "Output frequency ~60Hz (live)")
        self.assertEqual(r.status, "PASS")

    def test_remote_overrule_is_manual(self):
        r = by_name(self.results, "Current limit overruled by remote")
        self.assertEqual(r.status, "MANUAL")

    def test_no_charger_row_without_flag(self):
        names = [r.name for r in self.results]
        self.assertNotIn("Charger disabled (240V single-120V unit)", names)


class TestDeviations(unittest.TestCase):
    def check(self, mutate, name, expect_status="FAIL"):
        b = compliant_mock()
        mutate(b)
        r = by_name(v.run_checks(b), name)
        self.assertEqual(r.status, expect_status, r)
        return r

    def test_wrong_absorption(self):
        r = self.check(lambda b: b.settings.update({2: 5680}),
                       "Absorption voltage 56.00V")
        self.assertIn("56.80", r.detail)

    def test_wrong_float(self):
        self.check(lambda b: b.settings.update({3: 5400}),
                   "Float voltage 54.60V")

    def test_wrong_current_limit(self):
        r = self.check(lambda b: b.settings.update({6: 99}),
                       "AC input current limit 30A")
        self.assertIn("9.90", r.detail)

    def test_dynamic_limiter_off(self):
        self.check(lambda b: b.settings.update({1: b.settings[1] & ~(1 << 12)}),
                   "Dynamic current limiter enabled")

    def test_wide_freq_off(self):
        self.check(lambda b: b.settings.update({1: b.settings[1] & ~(1 << 11)}),
                   "Accept wide input frequency range")

    def test_powerassist_off(self):
        self.check(lambda b: b.settings.update({0: b.settings[0] & ~(1 << 5)}),
                   "PowerAssist enabled")

    def test_adaptive_charge_fails_lithium(self):
        self.check(lambda b: b.settings.update({0: b.settings[0] | (1 << 11)}),
                   "Lithium: fixed charge curve (S0 bit 11 clear)")

    def test_lead_acid_characteristic(self):
        self.check(lambda b: b.settings.update({10: 0}),
                   "Lithium: charge characteristic fixed")

    def test_wrong_shutdown_voltage(self):
        self.check(lambda b: b.settings.update({11: 4450}),
                   "DC input low shutdown 44.00V")

    def test_wrong_restart_offset(self):
        self.check(lambda b: b.settings.update({12: 120}),
                   "DC input low restart +1.00V")

    def test_missing_setting_reports_no_response(self):
        r = self.check(lambda b: b.settings.pop(6),
                       "AC input current limit 30A")
        self.assertIn("did not respond", r.detail)

    def test_nonstandard_absorption_param_warns(self):
        self.check(lambda b: b.settings.update({9: 6}),
                   "Max absorption time param (S9)", expect_status="WARN")

    def test_charger_flag_adds_manual_row(self):
        b = compliant_mock()
        r = by_name(v.run_checks(b, charger_disabled=True),
                    "Charger disabled (240V single-120V unit)")
        self.assertEqual(r.status, "MANUAL")


class TestSilentUnit(unittest.TestCase):
    def test_dead_bus_detected(self):
        b = p.MockBackend(address=5)  # absent address → every read is None
        b.open()
        results = v.run_checks(b)
        self.assertTrue(v.all_settings_silent(results))

    def test_live_unit_not_silent(self):
        self.assertFalse(v.all_settings_silent(v.run_checks(compliant_mock())))


if __name__ == "__main__":
    unittest.main(verbosity=2)
