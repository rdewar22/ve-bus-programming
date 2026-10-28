#!/usr/bin/env python3
"""Unit tests for vedirect.py + mppt_ruixu.py — no hardware required.

    python3 tui/test_mppt.py
"""

import unittest

import vedirect as vd
import mppt_ruixu as m


class TestFrames(unittest.TestCase):
    def test_checksum_identity(self):
        # Sum of command + payload + checksum ≡ 0x55 for every built frame.
        frame = vd.build_get(vd.REG_ABSORPTION_VOLTAGE)
        body = frame.decode().strip()[1:]
        cmd, data = int(body[0], 16), bytes.fromhex(body[1:])
        self.assertEqual((cmd + sum(data)) & 0xFF, 0x55)

    def test_get_frame_layout(self):
        # 0xEDF7 little-endian with zero flags.
        self.assertEqual(vd.build_get(0xEDF7), b":7F7ED006A\n")

    def test_set_frame_value_le(self):
        frame = vd.build_set(0xEDF7, 5700, 2).decode().strip()
        self.assertTrue(frame.startswith(":8F7ED00"))
        self.assertEqual(frame[8:12], "4416")  # 5700 = 0x1644, LE

    def test_roundtrip_parse(self):
        frame = vd.build_set(vd.REG_FLOAT_VOLTAGE, 5500, 2)
        cmd, payload = vd.parse_frame(frame.decode().strip())
        self.assertEqual(cmd, vd.CMD_SET)
        self.assertEqual(vd.decode_reply(payload, vd.REG_FLOAT_VOLTAGE),
                         (0, 5500))

    def test_parse_rejects_text_protocol_noise(self):
        for line in ["PID\t0xA060", "V\t53210", "Checksum\t:", "",
                     ":7F7ED0072", ":XYZ", ":7F7ED007"]:
            self.assertIsNone(vd.parse_frame(line), line)

    def test_parse_rejects_bad_checksum(self):
        good = vd.build_get(0xEDF7).decode().strip()
        bad = good[:-2] + ("00" if good[-2:] != "00" else "01")
        self.assertIsNone(vd.parse_frame(bad))

    def test_decode_reply_wrong_register(self):
        _, payload = vd.parse_frame(vd.build_get(0xEDF7).decode().strip())
        self.assertIsNone(vd.decode_reply(payload, 0xEDF6))

    def test_format_values(self):
        self.assertEqual(vd.format_reg_value(vd.REG_ABSORPTION_VOLTAGE, 5700),
                         "57.00V")
        self.assertEqual(
            vd.format_reg_value(vd.REG_BATTERY_VOLTAGE_SETTING, 0),
            "auto-detect")
        self.assertEqual(vd.format_reg_value(vd.REG_BATTERY_TYPE, 0xFF),
                         "User defined")
        self.assertEqual(vd.format_reg_value(vd.REG_DEVICE_STATE, 5), "Float")
        self.assertEqual(vd.format_reg_value(vd.REG_BATTERY_TEMP, 0xFFFF), "n/a")
        self.assertEqual(vd.format_reg_value(vd.REG_LOAD_CONTROL, 0x81),
                         "Auto (BatteryLife)")  # high nibble masked
        self.assertEqual(vd.format_reg_value(vd.REG_OFF_REASON, 0b101),
                         "no PV input, soft switch (device mode)")

    def test_signed_roundtrip(self):
        raw = vd.to_raw(vd.REG_TEMP_COMPENSATION, -64.8)
        self.assertEqual(raw, 0x10000 - 6480)
        self.assertAlmostEqual(vd.to_eng(vd.REG_TEMP_COMPENSATION, raw), -64.8)
        self.assertEqual(vd.format_reg_value(vd.REG_TEMP_COMPENSATION, raw),
                         "-64.80mV/K")


class TestDocExamples(unittest.TestCase):
    """Frames copied from the HEX protocol doc's 'Message examples'."""

    def test_get_max_current(self):
        self.assertEqual(vd.build_get(vd.REG_MAX_CURRENT), b":7F0ED0071\n")
        cmd, payload = vd.parse_frame(":7F0ED009600DB")
        self.assertEqual(vd.decode_reply(payload, vd.REG_MAX_CURRENT), (0, 150))

    def test_set_max_current(self):
        self.assertEqual(vd.build_set(vd.REG_MAX_CURRENT, 100, 2),
                         b":8F0ED0064000C\n")

    def test_ping_reply_and_version(self):
        cmd, payload = vd.parse_frame(":51641F9")
        self.assertEqual(cmd, vd.RSP_PING)
        word = int.from_bytes(payload, "little")
        self.assertEqual(word, 0x4116)
        self.assertEqual(vd.format_version(word), "v1.16")  # not v65.16

    def test_product_id_reply(self):
        self.assertEqual(vd.build_frame(vd.CMD_PRODUCT_ID), b":451\n")
        cmd, payload = vd.parse_frame(":1000351")
        self.assertEqual((cmd, int.from_bytes(payload[:2], "little")),
                         (vd.RSP_DONE, 0x0300))

    def test_async_frame_parses_but_is_not_a_get(self):
        cmd, payload = vd.parse_frame(":A0102000543")
        self.assertEqual(cmd, vd.CMD_ASYNC)
        self.assertEqual(vd.decode_reply(payload, vd.REG_DEVICE_STATE), (0, 5))


def vals_of(b) -> dict:
    return {reg: b.get(reg)[0] for reg in vd.REG_META}


class TestValidation(unittest.TestCase):
    def setUp(self):
        self.b = vd.MockVEDirect()
        self.b.regs[vd.REG_BATTERY_TYPE] = vd.BATTERY_TYPE_USER

    def check(self, reg, raw):
        return vd.validate_write(reg, raw, vals_of(self.b))

    def test_voltage_needs_user_type(self):
        self.b.regs[vd.REG_BATTERY_TYPE] = 3
        self.assertFalse(self.check(vd.REG_ABSORPTION_VOLTAGE, 5700).ok)
        ok, err = self.b.set(vd.REG_ABSORPTION_VOLTAGE, 5700)
        self.assertFalse(ok)  # mock mirrors the charger: parameter error
        self.assertIn("parameter", err)

    def test_voltage_range_from_device(self):
        self.assertTrue(self.check(vd.REG_ABSORPTION_VOLTAGE, 5700).ok)
        self.assertFalse(self.check(vd.REG_ABSORPTION_VOLTAGE, 7000).ok)

    def test_voltage_range_fallback_scales_12v_window(self):
        self.b.unsupported |= {vd.REG_ADJ_VOLTAGE_MIN, vd.REG_ADJ_VOLTAGE_MAX}
        self.assertEqual(vd.voltage_limits(vals_of(self.b)), (32.0, 69.6))

    def test_float_not_above_absorption(self):
        self.assertFalse(self.check(vd.REG_FLOAT_VOLTAGE, 5800).ok)
        self.assertFalse(self.check(vd.REG_ABSORPTION_VOLTAGE, 5500).ok)

    def test_temp_comp_limit_scales(self):
        tc = vd.REG_TEMP_COMPENSATION
        self.assertTrue(self.check(tc, vd.to_raw(tc, -64.8)).ok)
        self.assertFalse(self.check(tc, vd.to_raw(tc, -90.0)).ok)

    def test_max_current_vs_rating(self):
        self.assertTrue(self.check(vd.REG_MAX_CURRENT, 450).ok)
        self.assertFalse(self.check(vd.REG_MAX_CURRENT, 451).ok)

    def test_read_only_and_out_of_range(self):
        self.assertFalse(self.check(vd.REG_PANEL_POWER, 1).ok)
        self.assertFalse(self.check(vd.REG_MAX_CURRENT, -1).ok)
        self.assertFalse(self.check(vd.REG_BATTERY_VOLTAGE_SETTING, 20).ok)

    def test_load_levels_ordered(self):
        b = vd.MockVEDirect(load_output=True)
        v = vals_of(b)
        self.assertFalse(vd.validate_write(vd.REG_LOAD_HIGH_LEVEL, 4500, v).ok)
        self.assertFalse(vd.validate_write(vd.REG_LOAD_LOW_LEVEL, 5300, v).ok)
        self.assertTrue(vd.validate_write(vd.REG_LOAD_LOW_LEVEL, 4700, v).ok)


def compliant_mock() -> vd.MockVEDirect:
    b = vd.MockVEDirect()
    b.regs.update({vd.REG_BATTERY_VOLTAGE_SETTING: 48,
                   vd.REG_BATTERY_TYPE: vd.BATTERY_TYPE_USER,
                   vd.REG_ABSORPTION_VOLTAGE: 5700,
                   vd.REG_FLOAT_VOLTAGE: 5500})
    return b


class TestPlanAndChecks(unittest.TestCase):
    def test_factory_mock_plan_order(self):
        steps, notes = m.build_plan(m.read_current(vd.MockVEDirect()))
        self.assertEqual([(s.reg, s.value) for s in steps],
                         [(vd.REG_BATTERY_VOLTAGE_SETTING, 48),
                          (vd.REG_BATTERY_TYPE, 0xFF),
                          (vd.REG_ABSORPTION_VOLTAGE, 5700),
                          (vd.REG_FLOAT_VOLTAGE, 5500)])
        self.assertEqual(notes, [])

    def test_compliant_plan_empty_and_checks_pass(self):
        vals = m.read_current(compliant_mock())
        steps, notes = m.build_plan(vals)
        self.assertEqual((steps, notes), ([], []))
        self.assertTrue(all(r.status == "PASS" for r in m.run_checks(vals)))

    def test_factory_mock_checks_fail(self):
        results = m.run_checks(m.read_current(vd.MockVEDirect()))
        self.assertEqual(sum(r.status == "FAIL" for r in results), 4)

    def test_unsupported_register_noted_not_written(self):
        b = vd.MockVEDirect()
        b.unsupported.add(vd.REG_BATTERY_TYPE)
        steps, notes = m.build_plan(m.read_current(b))
        self.assertNotIn(vd.REG_BATTERY_TYPE, [s.reg for s in steps])
        self.assertTrue(any("not supported" in n for n in notes))


class TestApply(unittest.TestCase):
    def test_apply_then_verify_clean(self):
        b = vd.MockVEDirect()
        steps, _ = m.build_plan(m.read_current(b))
        self.assertTrue(m.apply_steps(b, steps, out=lambda s: None))
        results = m.run_checks(m.read_current(b))
        self.assertTrue(all(r.status == "PASS" for r in results))

    def test_apply_is_idempotent(self):
        b = vd.MockVEDirect()
        steps, _ = m.build_plan(m.read_current(b))
        self.assertTrue(m.apply_steps(b, steps, out=lambda s: None))
        self.assertEqual(m.build_plan(m.read_current(b)), ([], []))

    def test_apply_stops_on_failed_readback(self):
        b = vd.MockVEDirect()
        real_set = b.set
        written = []

        def flaky(reg, value):
            if reg == vd.REG_BATTERY_TYPE:
                return False, "simulated failure"
            written.append(reg)
            return real_set(reg, value)

        b.set = flaky
        steps, _ = m.build_plan(m.read_current(b))
        self.assertFalse(m.apply_steps(b, steps, out=lambda s: None))
        self.assertEqual(written, [vd.REG_BATTERY_VOLTAGE])

    def test_system_voltage_written_via_edef_on_pre_117_firmware(self):
        # fw 1.12–1.16: 0xEDEA is readable but answers 'not supported' to set.
        b = vd.MockVEDirect()
        b.readonly.add(vd.REG_BATTERY_VOLTAGE_SETTING)
        steps, _ = m.build_plan(m.read_current(b))
        self.assertEqual(steps[0].write_reg, vd.REG_BATTERY_VOLTAGE)
        self.assertTrue(m.apply_steps(b, steps, out=lambda s: None))
        self.assertTrue(all(r.status == "PASS"
                            for r in m.run_checks(m.read_current(b))))

    def test_auto_detect_that_found_48v_still_fails(self):
        # 0xEDEF reads 48 on an auto-detecting unit; only 0xEDEA shows auto.
        b = compliant_mock()
        b.regs[vd.REG_BATTERY_VOLTAGE_SETTING] = 0
        b.regs[vd.REG_BATTERY_VOLTAGE] = 48
        results = m.run_checks(m.read_current(b))
        self.assertEqual([r.status for r in results].count("FAIL"), 1)
        self.assertIn("auto-detect", results[0].detail)


if __name__ == "__main__":
    unittest.main(verbosity=2)
