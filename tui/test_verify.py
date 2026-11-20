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


class TestShoreLimitOption(unittest.TestCase):
    def sixteen_amp_mock(self):
        b = compliant_mock()
        b.settings[6] = 160
        b._current_limit = 16.0
        return b

    def test_16a_van_passes_with_option(self):
        results = v.run_checks(self.sixteen_amp_mock(), shore_limit_a=16)
        self.assertEqual(by_name(results, "AC input current limit 16A").status, "PASS")
        self.assertEqual(by_name(results, "Runtime shore limit 16A ('F' 5)").status, "PASS")
        self.assertFalse([r for r in results if r.status == "FAIL"])

    def test_16a_van_fails_default_30a_check(self):
        r = by_name(v.run_checks(self.sixteen_amp_mock()), "AC input current limit 30A")
        self.assertEqual(r.status, "FAIL")
        self.assertIn("16.00A", r.detail)

    def test_arg_accepts_amps_in_tenths(self):
        for text, amps in [("16", 16.0), ("30", 30.0), ("16.5", 16.5)]:
            self.assertEqual(v.shore_limit_arg(text), amps)
        self.assertEqual(v.shore_limit_raw(16.5), 165)

    def test_arg_rejects_bad_values(self):
        import argparse
        for text in ["0", "101", "16.25", "abc", "-16"]:
            with self.assertRaises(argparse.ArgumentTypeError, msg=text):
                v.shore_limit_arg(text)


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
