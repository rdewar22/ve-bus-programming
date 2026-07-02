#!/usr/bin/env python3
"""
Unit tests for vebus.protocol. No hardware required — exercises the wire-format
helpers, decoders, validation, the LiFePO4 sequence builder, and MockBackend.

Run:  python3 -m pytest test_protocol.py      (if pytest installed)
      python3 test_protocol.py                (stdlib unittest fallback)
"""

import unittest

from vebus import protocol as p


class TestChecksumAndFraming(unittest.TestCase):
    def test_checksum_address_frame(self):
        # 04 FF 41 01 00 -> 0xBB (documented address-set frame).
        self.assertEqual(p.calculate_checksum(bytes([0x04, 0xFF, 0x41, 0x01, 0x00])), 0xBB)

    def test_frame_sums_to_zero_mod_256(self):
        frame = p.build_frame(bytes([0xFF, 0x58, 0x31, 0x00]))
        self.assertEqual(sum(frame) % 256, 0)

    def test_read_frame_matches_known_good(self):
        # ReadSetting id 0 must be 04 FF 58 31 00 <chk>; length excludes checksum.
        frame = p.build_frame(bytes([0xFF, p.WINMON_SLOT, p.CMD_READ_SETTING, 0]))
        self.assertEqual(frame[0], 0x04)
        self.assertEqual(frame[:5], bytes([0x04, 0xFF, 0x58, 0x31, 0x00]))

    def test_write_frame_matches_known_good(self):
        # WriteViaID setting 2 = 5680 (0x1630): 07 FF 58 37 01 02 30 16 <chk>.
        frame = p.build_frame(bytes([0xFF, p.WINMON_SLOT, p.CMD_WRITE_VIA_ID,
                                     p.FLAG_RAM_EEPROM, 2, 0x30, 0x16]))
        self.assertEqual(frame[0], 0x07)
        self.assertEqual(frame[:8], bytes([0x07, 0xFF, 0x58, 0x37, 0x01, 0x02, 0x30, 0x16]))
        self.assertEqual(sum(frame) % 256, 0)

    def test_find_response_skips_version_frame(self):
        # A Version frame followed by a ReadSetting (0x86) response.
        version = bytes([0x07, 0xFF, 0x56, 0x28, 0xDB, 0x11, 0x00, 0x00, 0x90])
        resp = bytes([0x05, 0xFF, 0x58, 0x86, 0x34, 0x81, 0x00])
        f = p.find_response(version + resp, p.RSP_READ_SETTING)
        self.assertIsNotNone(f)
        self.assertEqual(f[4] | (f[5] << 8), 0x8134)

    def test_find_response_absent(self):
        self.assertIsNone(p.find_response(bytes([0x07, 0xFF, 0x56, 0x28]), 0x86))

    def test_scan_value_clean_frame(self):
        resp = bytes([0x05, 0xFF, 0x58, 0x86, 0x34, 0x81, 0xD2])
        self.assertEqual(p.scan_value_response(resp, 0x86), 0x8134)

    def test_scan_value_corrupted_length_byte(self):
        # Desynced MK3 returns a bogus length byte; value must still be recovered.
        corrupt = bytes([0x87, 0xFF, 0x58, 0x86, 0x34, 0x81, 0x1F, 0x1F, 0xA9])
        self.assertEqual(p.scan_value_response(corrupt, 0x86), 0x8134)

    def test_scan_value_skips_version_frame(self):
        version = bytes([0x07, 0xFF, 0x56, 0x28, 0xDB, 0x11, 0x00, 0x00, 0x90])
        resp = bytes([0x05, 0xFF, 0x59, 0x85, 0xB6, 0x14, 0x00])
        self.assertEqual(p.scan_value_response(version + resp, 0x85), 0x14B6)

    def test_scan_value_no_false_match_on_version(self):
        version = bytes([0x07, 0xFF, 0x56, 0x28, 0xDB, 0x11, 0x00, 0x00, 0x90])
        self.assertIsNone(p.scan_value_response(version, 0x86))


class TestDecoders(unittest.TestCase):
    def test_setting0_lifepo4_fixed_charge(self):
        # 0x8134: UPS on (bit3 clear), PA on (bit5 set), fixed charge (bit11 clear).
        flags = {f.name: f for f in p.decode_setting0(0x8134)}
        self.assertFalse(flags["UPS function"].is_set)         # clear => enabled
        self.assertIn("enabled", flags["UPS function"].meaning)
        self.assertTrue(flags["PowerAssist"].is_set)
        self.assertFalse(flags["Charge mode"].is_set)          # clear => fixed
        self.assertIn("Fixed", flags["Charge mode"].meaning)
        self.assertFalse(flags["Weak AC input"].is_set)

    def test_setting0_unknown_bits_not_interpreted(self):
        for f in p.decode_setting0(0x8134):
            if f.bit in (7, 15):
                self.assertFalse(f.interpret)
                self.assertFalse(f.confirmed)

    def test_setting1_bits(self):
        # bit 11 set, bit 12 clear.
        flags = {f.name: f for f in p.decode_setting1(0x0800)}
        self.assertTrue(flags["Accept Wide Frequency Range"].is_set)
        self.assertFalse(flags["Dynamic Current Limiter"].is_set)

    def test_format_setting_value_scaling(self):
        self.assertEqual(p.format_setting_value(2, 5680), "56.80V")
        self.assertEqual(p.format_setting_value(4, 35), "35 A")
        self.assertEqual(p.format_setting_value(10, 1), "1 (Fixed (LiFePO4))")
        self.assertEqual(p.format_setting_value(0, 0x8134), "0x8134")
        self.assertEqual(p.format_setting_value(2, p.UNSUPPORTED), "unsupported (0xFFFF)")

    def test_signed16(self):
        self.assertEqual(p.signed16(0xFFEA), -22)
        self.assertEqual(p.signed16(0x0023), 35)

    def test_interpret_ramvar_battery_fallback(self):
        # No info → fallback divisors (still correct for these vars).
        self.assertEqual(p.interpret_ramvar(4, 5302), "53.02 V")
        self.assertEqual(p.interpret_ramvar(5, 0xFFEA), "-2.20 A")


class TestRamVarInfo(unittest.TestCase):
    def test_parse_scale_offset_response(self):
        # FF 58 8E <scale=0x000A> 8F <offset=0> ... -> scale 10? no: 0x000A=10
        frame = bytes([0x08, 0xFF, 0x58, 0x8E, 0x64, 0x00, 0x8F, 0x00, 0x00, 0x00])
        info = p.parse_ramvar_info(frame)
        self.assertIsNotNone(info)
        self.assertFalse(info.signed)
        self.assertEqual(info.scale, 100)  # 0x0064

    def test_parse_signed_fractional_scale(self):
        # scale 0xFFF6 -> negative => signed; 0x10000-0xFFF6 = 10; <0x4000 so scale=10
        frame = bytes([0x08, 0xFF, 0x58, 0x8E, 0xF6, 0xFF, 0x8F, 0x00, 0x00, 0x00])
        info = p.parse_ramvar_info(frame)
        self.assertTrue(info.signed)
        self.assertEqual(info.scale, 10)

    def test_fractional_scale_battery_voltage(self):
        # 0.01 scale is encoded as 0x8000 - 100 = 0x7F9C
        enc = 0x8000 - 100
        frame = bytes([0x08, 0xFF, 0x58, 0x8E, enc & 0xFF, enc >> 8, 0x8F, 0, 0, 0])
        info = p.parse_ramvar_info(frame)
        self.assertAlmostEqual(info.scale, 0.01)
        self.assertAlmostEqual(info.parse(5302), 53.02)

    def test_signed_parse(self):
        info = p.RamVarInfo(signed=True, scale=0.1, offset=0)
        self.assertAlmostEqual(info.parse(0xFFEA), -2.2)

    def test_unsupported_variable_info(self):
        # scale field 0 ⇒ unsupported.
        frame = bytes([0x08, 0xFF, 0x58, 0x8E, 0x00, 0x00, 0x8F, 0x00, 0x00, 0x00])
        self.assertIsNone(p.parse_ramvar_info(frame))

    def test_bit_variable_info(self):
        # offset field 0x8000 ⇒ boolean bit; scale 0x0005 ⇒ bit index 4 (ignore AC input).
        frame = bytes([0x08, 0xFF, 0x58, 0x8E, 0x05, 0x00, 0x8F, 0x00, 0x80, 0x00])
        info = p.parse_ramvar_info(frame)
        self.assertEqual(info.bit, 4)
        self.assertEqual(info.parse(0x8002), 0.0)   # bit 4 clear → off
        self.assertEqual(info.parse(0x8010), 1.0)   # bit 4 set → on

    def test_bit_variable_info_relay(self):
        # scale 0x0006 ⇒ bit index 5 (multi-functional relay).
        frame = bytes([0x08, 0xFF, 0x58, 0x8E, 0x06, 0x00, 0x8F, 0x00, 0x80, 0x00])
        info = p.parse_ramvar_info(frame)
        self.assertEqual(info.bit, 5)
        self.assertEqual(info.parse(0x8C70), 1.0)   # bit 5 set → on

    def test_format_telemetry_bit_as_on_off(self):
        on_info = p.RamVarInfo(bit=4)
        self.assertEqual(p.format_telemetry(11, 0x8010, on_info), "on")
        self.assertEqual(p.format_telemetry(11, 0x8002, on_info), "off")

    def test_format_telemetry_ripple_and_load(self):
        self.assertEqual(p.format_telemetry(6, 21, p.RamVarInfo(False, 0.01, 0)), "0.21 V")
        self.assertEqual(p.format_telemetry(9, 79, p.RamVarInfo(True, 0.01, 0)), "0.79 A")

    def test_battery_soc_normalized_to_percent(self):
        # var 13 SoC: fractional scale 0.005; raw 200 → 1.0 → 100%.
        info = p.RamVarInfo(False, 0.005, 0)
        self.assertEqual(p.format_telemetry(13, 200, info), "100.00 %")
        self.assertEqual(p.format_telemetry(13, 170, info), "85.00 %")

    def test_mock_battery_soc(self):
        b = p.MockBackend(); b.open()
        soc = p.format_telemetry(13, b.read_ramvar(13), b.read_ramvar_info(13))
        self.assertTrue(soc.endswith("%"))

    def test_setting_names_from_reference(self):
        self.assertEqual(p.setting_meta(65).name, "Battery SoC when bulk finished")
        self.assertEqual(p.setting_meta(64).name, "Battery capacity")
        self.assertEqual(p.setting_meta(6).name, "AC1 input current limit")
        # setting 65 raw 190 → 95.0% (scale 0.5 → divisor 2).
        self.assertEqual(p.format_setting_value(65, 190), "95.00%")

    def test_period_to_frequency(self):
        self.assertEqual(p.period_to_frequency(0), 0.0)
        self.assertAlmostEqual(p.period_to_frequency(10 / 60), 60.0, places=1)

    def test_telemetry_value_uses_info(self):
        # Mains current: fallback would give /100; info gives 0.01 signed.
        info = p.RamVarInfo(True, 0.01, 0)
        self.assertAlmostEqual(p.telemetry_value(1, 100, info), 1.0)

    def test_telemetry_period_var_to_hz(self):
        info = p.RamVarInfo(False, 0.000512, 256)
        hz = p.telemetry_value(7, 70, info)  # period 0.167 -> ~59.9 Hz
        self.assertTrue(59 < hz < 61)

    def test_mock_provides_ramvar_info(self):
        b = p.MockBackend(); b.open()
        self.assertEqual(b.read_ramvar_info(3).signed, True)  # MP-II quirk
        self.assertAlmostEqual(b.read_ramvar_info(4).scale, 0.01)


class TestConfigAndLeds(unittest.TestCase):
    def test_parse_config_current_limits(self):
        # Real captured payload: shore 9.9A, range 9.4-50.0A, switchreg 0x77.
        pl = bytes.fromhex("4110050000945e00f401630077")
        cfg = p.parse_config(pl)
        self.assertAlmostEqual(cfg.min_current, 9.4)
        self.assertAlmostEqual(cfg.max_current, 50.0)
        self.assertAlmostEqual(cfg.actual_current, 9.9)
        self.assertEqual(cfg.switch_register, 0x77)

    def test_config_switch_state_decode(self):
        self.assertEqual(p.ConfigInfo(0, 50, 30, p.SW_CHARGE | p.SW_INVERT).switch_state_name, "On")
        self.assertEqual(p.ConfigInfo(0, 50, 30, p.SW_CHARGE).switch_state_name, "Charger only")
        self.assertEqual(p.ConfigInfo(0, 50, 30, p.SW_INVERT).switch_state_name, "Inverter only")
        self.assertEqual(p.ConfigInfo(0, 50, 30, 0).switch_state_name, "Off")

    def test_config_switch_state_code_preserves_operation(self):
        self.assertEqual(p.ConfigInfo(0, 50, 30, p.SW_CHARGE | p.SW_INVERT).switch_state_code, p.SWITCH_ON)
        self.assertTrue(p.ConfigInfo(0, 50, 30, p.SW_FRONT_UP).front_switch_up)

    def test_parse_config_rejects_short_or_wrong(self):
        self.assertIsNone(p.parse_config(bytes([0x41, 0x10])))
        self.assertIsNone(p.parse_config(bytes([0x20] + [0] * 13)))

    def test_led_states_decode(self):
        led = p.LedInfo(on=0x05, blink=0x00)  # Mains + Bulk
        states = dict(led.states())
        self.assertEqual(states["Mains"], "on")
        self.assertEqual(states["Bulk"], "on")
        self.assertEqual(states["Float"], "off")

    def test_led_blink_takes_precedence(self):
        led = p.LedInfo(on=0x00, blink=0x40)  # Low battery blinking
        self.assertEqual(dict(led.states())["Low battery"], "blink")

    def test_mock_config_and_set_current_limit(self):
        b = p.MockBackend(); b.open()
        self.assertEqual(b.read_config().actual_current, 30.0)
        r = b.set_current_limit(45)
        self.assertTrue(r.ok)
        self.assertEqual(b.read_config().actual_current, 45.0)

    def test_mock_current_limit_clamped(self):
        b = p.MockBackend(); b.open()
        b.set_current_limit(999)
        self.assertEqual(b.read_config().actual_current, 50.0)  # clamped to max
        b.set_current_limit(0)
        self.assertEqual(b.read_config().actual_current, 9.4)   # clamped to min

    def test_mock_leds(self):
        b = p.MockBackend(); b.open()
        self.assertIsNotNone(b.read_leds())


class TestModelDetection(unittest.TestCase):
    def test_quattro_when_quattro_only_supported(self):
        self.assertEqual(p.detect_model(0x8134, True).model, "Quattro")

    def test_multiplus_family_when_absent(self):
        # MultiPlus II reads 0x8134 with bit 7 CLEAR; must NOT be called Quattro.
        info = p.detect_model(0x8134, False)
        self.assertEqual(info.model, "MultiPlus / MultiPlus II")

    def test_bit7_not_used_as_discriminator(self):
        # Same flags, only the 49/88 support differs => different model.
        self.assertNotEqual(p.detect_model(0x8134, True).model,
                            p.detect_model(0x8134, False).model)


class TestValidation(unittest.TestCase):
    def test_absorption_in_band(self):
        self.assertTrue(p.validate_setting_write(2, 5680).ok)

    def test_absorption_out_of_band(self):
        self.assertFalse(p.validate_setting_write(2, 9900).ok)

    def test_charge_characteristic_enum(self):
        self.assertTrue(p.validate_setting_write(10, 1).ok)
        self.assertFalse(p.validate_setting_write(10, 5).ok)

    def test_value_range(self):
        self.assertFalse(p.validate_setting_write(60, 70000).ok)

    def test_voltage_ordering(self):
        self.assertTrue(p.validate_voltage_ordering(56.8, 54.0, 44.5).ok)
        self.assertFalse(p.validate_voltage_ordering(54.0, 56.0).ok)  # float > abs
        self.assertFalse(p.validate_voltage_ordering(56.8, 54.0, 55.0).ok)  # cutoff>float


class TestLiFePO4Sequence(unittest.TestCase):
    def test_order_and_values(self):
        seq = p.build_lifepo4_sequence(0x8134, 56.8, 54.0, 35)
        ids = [s.setting_id for s in seq]
        # Setting 0 first (clear adaptive), then 60,65,72,10,2,3,9, then current.
        self.assertEqual(ids, [0, 60, 65, 72, 10, 2, 3, 9, 4])

    def test_setting0_read_modify_write_clears_bit11_only(self):
        seq = p.build_lifepo4_sequence(0x8134, 56.8, 54.0, None)
        step0 = seq[0]
        self.assertEqual(step0.setting_id, 0)
        # 0x8134 has bit 11 clear already, so value unchanged here...
        self.assertEqual(step0.value, 0x8134)
        # ...but if adaptive was on (bit 11 set), only bit 11 is cleared.
        seq2 = p.build_lifepo4_sequence(0x8934, 56.8, 54.0, None)
        self.assertEqual(seq2[0].value, 0x8134)  # 0x8934 & ~0x0800

    def test_voltage_scaling_in_sequence(self):
        seq = p.build_lifepo4_sequence(0x8134, 56.8, 54.0, 35)
        by_id = {s.setting_id: s.value for s in seq}
        self.assertEqual(by_id[2], 5680)
        self.assertEqual(by_id[3], 5400)
        self.assertEqual(by_id[4], 35)
        self.assertEqual(by_id[10], 1)

    def test_no_current_when_none(self):
        seq = p.build_lifepo4_sequence(0x8134, 56.8, 54.0, None)
        self.assertNotIn(4, [s.setting_id for s in seq])


class TestMockBackend(unittest.TestCase):
    def setUp(self):
        self.b = p.MockBackend()
        self.b.open()

    def test_reads_plausible_values(self):
        self.assertEqual(self.b.read_setting(0), 0x8134)
        self.assertEqual(self.b.read_setting(2), 5680)

    def test_unsupported_vs_absent(self):
        self.assertEqual(self.b.read_setting(128), p.UNSUPPORTED)  # present, n/a
        self.assertIsNone(self.b.read_setting(88))                 # Quattro-only, absent
        self.assertIsNone(self.b.read_setting(200))                # no such setting

    def test_identifies_as_multiplus_family(self):
        self.assertEqual(self.b.identify().model, "MultiPlus / MultiPlus II")

    def test_write_roundtrip(self):
        r = self.b.write_setting(2, 5700)
        self.assertTrue(r.ok)
        self.assertEqual(r.readback, 5700)
        self.assertEqual(self.b.read_setting(2), 5700)

    def test_high_id_missing_ack_but_ok(self):
        r = self.b.write_setting(191, 0xFFFF)
        self.assertFalse(r.acked)   # high IDs don't ACK
        self.assertTrue(r.ok)       # but readback confirms

    def test_telemetry_changes_over_reads(self):
        reads = [self.b.read_ramvar(5) for _ in range(12)]
        self.assertGreater(len(set(reads)), 1)


class TestAddressFrame(unittest.TestCase):
    def test_address0_is_exact_legacy_frame(self):
        # Must be byte-identical to the init frame every tool has always sent.
        self.assertEqual(p.build_address_frame(0),
                         bytes([0x04, 0xFF, 0x41, 0x01, 0x00, 0xBB]))

    def test_address1_frame(self):
        self.assertEqual(p.build_address_frame(1),
                         bytes([0x04, 0xFF, 0x41, 0x01, 0x01, 0xBA]))

    def test_all_addresses_sum_to_zero_mod_256(self):
        for addr in range(32):
            self.assertEqual(sum(p.build_address_frame(addr)) % 256, 0)

    def test_out_of_range_raises(self):
        with self.assertRaises(ValueError):
            p.build_address_frame(32)
        with self.assertRaises(ValueError):
            p.build_address_frame(-1)

    def test_version_request_frame(self):
        # Explicit 'V' request — same frame gvos inverter.py validates with.
        self.assertEqual(p.build_frame(bytes([0xFF, p.RSP_VERSION])),
                         bytes([0x02, 0xFF, 0x56, 0xA9]))


class TestParseBytes(unittest.TestCase):
    def test_24bit_negative_current(self):
        # 0xFFFFF6 = -10 raw; battery-current scale 0.1 → -1.0 A.
        info = p.RamVarInfo(signed=True, scale=0.1, offset=0)
        self.assertAlmostEqual(info.parse_bytes(b"\xf6\xff\xff"), -1.0)

    def test_24bit_positive_current(self):
        info = p.RamVarInfo(signed=True, scale=0.1, offset=0)
        self.assertAlmostEqual(info.parse_bytes(b"\x0f\x00\x00"), 1.5)

    def test_16bit_matches_parse(self):
        info = p.RamVarInfo(signed=True, scale=0.1, offset=0)
        self.assertAlmostEqual(info.parse_bytes(b"\xea\xff"), info.parse(0xFFEA))

    def test_16bit_unsigned_voltage(self):
        info = p.RamVarInfo(signed=False, scale=0.01, offset=0)
        self.assertAlmostEqual(info.parse_bytes(b"\xb6\x14"), 53.02)

    def test_1byte_signed(self):
        info = p.RamVarInfo(signed=True, scale=1.0, offset=0)
        self.assertEqual(info.parse_bytes(b"\x80"), -128.0)
        self.assertEqual(info.parse_bytes(b"\x7f"), 127.0)

    def test_1byte_period_with_offset(self):
        # Inverter period: scale 0.000512, offset 256; raw 70 → ~0.167 s → ~60 Hz.
        info = p.RamVarInfo(signed=False, scale=0.000512, offset=256)
        period = info.parse_bytes(b"\x46")
        self.assertTrue(59 < p.period_to_frequency(period) < 61)

    def test_wrong_width_raises(self):
        info = p.RamVarInfo(signed=False, scale=1.0, offset=0)
        with self.assertRaises(ValueError):
            info.parse_bytes(b"")
        with self.assertRaises(ValueError):
            info.parse_bytes(b"\x00\x00\x00\x00")


class TestDcAcInfoParsing(unittest.TestCase):
    """Synthetic info frames built with the exact victron_mk3 byte offsets."""

    DC_INFOS = {4: p.RamVarInfo(False, 0.01, 0),
                5: p.RamVarInfo(True, 0.1, 0),
                7: p.RamVarInfo(False, 0.000512, 256)}
    AC_INFOS = {0: p.RamVarInfo(False, 0.01, 0),
                1: p.RamVarInfo(True, 0.01, 0),
                2: p.RamVarInfo(False, 0.01, 0),
                3: p.RamVarInfo(True, 0.01, 0),
                8: p.RamVarInfo(False, 0.001024, 0)}

    @staticmethod
    def _u16(v):
        return bytes([v & 0xFF, (v >> 8) & 0xFF])

    @staticmethod
    def _u24(v):
        return bytes([v & 0xFF, (v >> 8) & 0xFF, (v >> 16) & 0xFF])

    def _dc_payload(self, volts_raw=5302, to_inv=15, from_chg=0, period=70):
        return (bytes([0x20, 0, 0, 0, 0, p.DC_FRAME_TYPE])
                + self._u16(volts_raw) + self._u24(to_inv & 0xFFFFFF)
                + self._u24(from_chg & 0xFFFFFF) + bytes([period]))

    def _ac_payload(self, phase_byte, state=8, mains_mult=1, inv_mult=1,
                    mains_v=12013, mains_i=820, inv_v=12000, inv_i=790,
                    period=162):
        return (bytes([0x20, mains_mult, inv_mult, 0, state, phase_byte])
                + self._u16(mains_v) + self._u16(mains_i)
                + self._u16(inv_v) + self._u16(inv_i) + bytes([period]))

    def test_payloads_are_wire_valid(self):
        # build_frame wraps a payload with length + checksum summing to 0.
        for payload in (self._dc_payload(), self._ac_payload(0x08)):
            self.assertEqual(sum(p.build_frame(payload)) % 256, 0)

    def test_dc_frame_decodes(self):
        dc = p.parse_dc_info(self._dc_payload(), self.DC_INFOS)
        self.assertIsNotNone(dc)
        self.assertAlmostEqual(dc.dc_voltage, 53.02)
        self.assertAlmostEqual(dc.dc_current_to_inverter, 1.5)
        self.assertAlmostEqual(dc.dc_current_from_charger, 0.0)
        self.assertAlmostEqual(dc.dc_current, -1.5)  # net: discharging
        self.assertTrue(59 < dc.inverter_frequency < 61)

    def test_dc_24bit_negative_current(self):
        dc = p.parse_dc_info(self._dc_payload(to_inv=-10), self.DC_INFOS)
        self.assertAlmostEqual(dc.dc_current_to_inverter, -1.0)

    def test_ac_l1_frame(self):
        ac = p.parse_ac_info(self._ac_payload(0x08, state=8), self.AC_INFOS)
        self.assertEqual(ac.phase, 1)
        self.assertEqual(ac.num_phases, 1)
        self.assertEqual(ac.device_state_name, "Bypass")
        self.assertAlmostEqual(ac.mains_voltage, 120.13)
        self.assertAlmostEqual(ac.mains_current, 8.2)
        self.assertAlmostEqual(ac.inverter_voltage, 120.0)
        self.assertAlmostEqual(ac.inverter_current, 7.9)
        self.assertTrue(59 < ac.mains_frequency < 61)

    def test_ac_l2_frame_slave_state(self):
        ac = p.parse_ac_info(self._ac_payload(0x07, state=3), self.AC_INFOS)
        self.assertEqual(ac.phase, 2)
        self.assertEqual(ac.num_phases, 0)   # only the L1 frame reports it
        self.assertEqual(ac.device_state_name, "Slave")

    def test_ac_current_multipliers(self):
        ac = p.parse_ac_info(self._ac_payload(0x08, mains_mult=2, inv_mult=3),
                             self.AC_INFOS)
        self.assertAlmostEqual(ac.mains_current, 16.4)   # 8.2 × 2
        self.assertAlmostEqual(ac.inverter_current, 23.7)  # 7.9 × 3

    def test_parsers_reject_wrong_frames(self):
        self.assertIsNone(p.parse_dc_info(self._ac_payload(0x08), self.DC_INFOS))
        self.assertIsNone(p.parse_ac_info(self._dc_payload(), self.AC_INFOS))
        self.assertIsNone(p.parse_dc_info(self._dc_payload()[:10], self.DC_INFOS))
        bad_marker = b"\x21" + self._dc_payload()[1:]
        self.assertIsNone(p.parse_dc_info(bad_marker, self.DC_INFOS))

    def test_parsers_fall_back_to_default_scaling(self):
        # No infos passed → DEFAULT_RAMVAR_INFO scales apply.
        dc = p.parse_dc_info(self._dc_payload())
        self.assertAlmostEqual(dc.dc_voltage, 53.02)


class TestMockMultiUnit(unittest.TestCase):
    def setUp(self):
        self.b = p.MockBackend()
        self.b.open()

    def test_two_units_present(self):
        self.assertIsNotNone(self.b.read_ramvar(4))
        self.b.select_address(1)
        self.assertIsNotNone(self.b.read_ramvar(4))

    def test_absent_address_is_silent(self):
        self.b.select_address(5)
        self.assertIsNone(self.b.read_ramvar(4))
        self.assertIsNone(self.b.read_version())
        self.assertIsNone(self.b.read_leds())
        self.assertIsNone(self.b.read_config())
        self.assertIsNone(self.b.read_dc_info())
        self.assertIsNone(self.b.read_ac_info(1))
        self.assertIsNone(self.b.read_setting(0))
        self.b.select_address(0)  # back to master: alive again
        self.assertIsNotNone(self.b.read_ramvar(4))

    def test_per_address_values_differ_but_battery_shared(self):
        mains0 = self.b.read_ramvar(0)
        batt0 = self.b.read_ramvar(4)
        self.b.select_address(1)
        mains1 = self.b.read_ramvar(0)
        batt1 = self.b.read_ramvar(4)
        self.assertGreater(abs(mains0 - mains1), 100)  # distinct AC side
        self.assertLess(abs(batt0 - batt1), 10)        # shared bank (drift only)

    def test_version_and_leds_differ_per_address(self):
        v0, l0 = self.b.read_version(), self.b.read_leds()
        self.b.select_address(1)
        v1, l1 = self.b.read_version(), self.b.read_leds()
        self.assertNotEqual(v0, v1)
        self.assertNotEqual((l0.on, l0.blink), (l1.on, l1.blink))

    def test_ac_frames_phase_scoped(self):
        ac1 = self.b.read_ac_info(1)
        ac2 = self.b.read_ac_info(2)
        self.assertEqual(ac1.phase, 1)
        self.assertEqual(ac1.num_phases, 1)  # MP-II 2x120V quirk baked in
        self.assertEqual(ac2.phase, 2)
        self.assertEqual(ac2.device_state_name, "Slave")
        self.assertIsNone(self.b.read_ac_info(3))  # split-phase pair: no L3
        with self.assertRaises(ValueError):
            self.b.read_ac_info(0)

    def test_select_address_validates(self):
        with self.assertRaises(ValueError):
            self.b.select_address(32)

    def test_description_reflects_address(self):
        self.assertNotIn("addr", self.b.description)
        self.b.select_address(1)
        self.assertIn("addr 1", self.b.description)

    def test_open_backend_threads_address(self):
        b = p.open_backend(mock=True, address=1)
        self.assertEqual(b.address, 1)
        self.assertIsNotNone(b.read_ramvar(4))


if __name__ == "__main__":
    unittest.main(verbosity=2)
