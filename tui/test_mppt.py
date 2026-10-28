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
        self.assertEqual(vd.format_reg_value(vd.REG_BATTERY_VOLTAGE, 0),
                         "auto-detect")
        self.assertEqual(vd.format_reg_value(vd.REG_BATTERY_TYPE, 0xFF),
                         "user defined")


def compliant_mock() -> vd.MockVEDirect:
    b = vd.MockVEDirect()
    b.regs.update({vd.REG_BATTERY_VOLTAGE: 48,
                   vd.REG_BATTERY_TYPE: vd.BATTERY_TYPE_USER,
                   vd.REG_ABSORPTION_VOLTAGE: 5700,
                   vd.REG_FLOAT_VOLTAGE: 5500})
    return b


class TestPlanAndChecks(unittest.TestCase):
    def test_factory_mock_plan_order(self):
        steps, notes = m.build_plan(m.read_current(vd.MockVEDirect()))
        self.assertEqual([(s.reg, s.value) for s in steps],
                         [(vd.REG_BATTERY_VOLTAGE, 48),
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
