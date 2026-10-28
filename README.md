# ve-bus-programming

Programming a Victron Inverter with VE.Bus without Venus or GX device. Linux based, only using the MK3 adapter.

Also check out [victron-mk3](https://github.com/j9brown/victron-mk3) — a working library that implements many inverter functions. This repo focuses on the parts that library doesn't cover: direct setting reads/writes, charge profile configuration, and protocol details that Victron's documentation gets wrong.

## Documentation

**[FINDINGS.md](FINDINGS.md)** — Comprehensive protocol reference covering the full VE.Bus MK2/MK3 wire protocol, reverse-engineered from USB packet captures and iterative bench testing. Includes:

- Complete frame format, checksum calculation, and command/response reference
- Setting 0 and Setting 1 flag register bit maps (UPS, PowerAssist, Weak AC, adaptive charge, dynamic current limiter, wide frequency range)
- Charge profile settings and the exact 8-write sequence VEConfigure uses for LiFePO4
- Grid code / LOM setting behavior and the residual cleanup problem when reverting to "None"
- Timing, ACK quirks, cross-model differences (MultiPlus vs. Quattro), and practical gotchas
- Methodology for mapping unknown settings using sweep-and-diff

Find Victron's official (incomplete) protocol documentation here:
https://www.victronenergy.com/upload/documents/Technical-Information-Interfacing-with-VE-Bus-products-MK2-Protocol-3-14.pdf

## Tools

| Script | Purpose |
|--------|---------|
| `tui/tui.py` | **Interactive terminal dashboard** — live telemetry, decoded flags, charge-profile editing, guarded writes (see below) |
| `tui/bus_scan.py` | **Read-only VE.Bus address scanner** — enumerates every unit behind one MK3 (parallel / split-phase systems), per-unit telemetry + AC/DC info frames, side-by-side report (see FINDINGS §12) |
| `tui/verify_ruixu.py` | **Read-only profile check** — compares every setting against the RUiXU 48V LiFePO4 profile, PASS/FAIL per setting (see below) |
| `tui/apply_ruixu.py` | **Profile writer** — diff-based, writes only deviations in VEConfigure order, readback-verified, then re-runs the verify suite (see below) |
| `tui/mppt_tui.py` | **Solar charger (MPPT) terminal dashboard** — the `tui.py` counterpart over VE.Direct: live telemetry, battery + load-output settings, register browser, guarded writes (see below) |
| `tui/mppt_ruixu.py` | **Solar charger (MPPT) profile tool** — verify/apply the battery-section settings over VE.Direct HEX protocol (different bus than everything above; see below) |
| `tui/vebus/protocol.py` | Reusable backend: serial/checksum/frame protocol, `SerialBackend`, `MockBackend`, and all decoders. Importable by scripts and tests |
| `tui/vedirect.py` | VE.Direct HEX backend for MPPT controllers: frame build/parse, register map + decoders from Victron's HEX doc, write validation, `SerialVEDirect` / `MockVEDirect` |
| `setv.py` | Set absorption and float voltage (WriteViaID example) |
| `settings_sweep.py` | Sweep all 256 setting IDs — dump supported settings to CSV for diffing |
| `ram_sweep.py` | Sweep all 256 RAM variable IDs — identify live telemetry values |

The sweeper scripts are the primary tool for mapping unknown settings. Run a sweep, change one parameter in VEConfigure, sweep again, and diff the CSV outputs. See [FINDINGS.md § Methodology for Mapping Unknown Settings](FINDINGS.md#11-methodology-for-mapping-unknown-settings) for details.

## Interactive TUI (`tui/tui.py`)

> ⚠️ **This TUI is vibe++ coded (I mostly paid attention) and should not be used in production without manual
> verification.** Power systems can be dangerous. It sits on top of a partially
> reverse-engineered protocol — verify every value (multimeter / VEConfigure)
> before trusting it, and remember that wrong charge parameters can damage batteries.

A reactive terminal dashboard ([Textual](https://textual.textualize.io/)) that exposes the
reverse-engineered features in one place:

- **Dashboard** — live telemetry (battery V/I, AC, inverter) on an auto-refresh, plus
  explicit operating-state controls (Off / On / Charger-only / Normal).
- **Flags** — Setting 0 and Setting 1 decoded bit-by-bit (UPS, PowerAssist, Weak AC,
  adaptive vs. fixed charge, Wide Frequency Range, Dynamic Current Limiter) with the raw
  word shown; model-dependent/unknown bits are displayed but not interpreted. One-click
  read-modify-write toggles for the confirmed flags.
- **Charge** — absorption / float / charge-current / low-battery-cutoff values, individual
  editing, and a one-button **LiFePO4 fixed-profile** wizard that previews and applies the
  exact 8-write VEConfigure sequence in order.
- **Grid / LOM** — settings 81/128/190/191 with the grid-code residual situation explained
  and a guarded residual-cleanup action.
- **All settings** — every supported setting ID, raw + decoded.

### Running

```bash
# One-time setup (keeps system Python clean):
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# Real device (auto-detects /dev/ttyUSB0, then /dev/inverter):
.venv/bin/python tui/tui.py

# Specific port:
.venv/bin/python tui/tui.py --port /dev/inverter

# Mock mode — no hardware needed (also the automatic fallback if no port opens):
.venv/bin/python tui/tui.py --mock

# Telemetry refresh interval (seconds, default 2):
.venv/bin/python tui/tui.py --interval 3

# Watch another unit of a multi-unit (parallel/split-phase) system —
# discover addresses with tui/bus_scan.py first:
.venv/bin/python tui/tui.py --port /dev/inverter --address 1
```

Keys: `r` refresh all settings · `q` quit. The status bar shows LIVE vs MOCK, the detected
model, and the port (plus an `[ADDR n]` chip when watching a non-master unit).

> With `--address` ≠ 0 the confirm dialogs carry an extra warning: per-unit
> settings writes on a configured multi-unit system are discouraged
> (VEConfigure manages those system-wide) — reads are safe.

### Safety

- **Every write is confirmed in a modal** showing the setting ID, old→new value, and decoded
  meaning before anything is sent.
- **Writes are verified by readback** (high IDs often skip the `0x88` ACK even on success, so
  the readback is authoritative).
- **Flag changes use read-modify-write** — a single bit is flipped on the live value; whole
  flag words are never copied between units/models (base values differ — see FINDINGS §9).
- **Telemetry reads are EEPROM-safe** and auto-refresh freely. **Writes hit EEPROM**, which has
  limited write cycles — they are never auto-looped, and every confirm dialog offers a
  **RAM-only** option (flag `0x03`) to test without wear (value is lost on power cycle).
- Inputs are range-checked (e.g. absorption/float within a plausible 48V band, float < absorption)
  and out-of-range values raise a warning in the confirm dialog before you commit.

### Tests

```bash
python3 tui/test_protocol.py    # unit tests, no hardware required
python3 tui/test_verify.py      # verify_ruixu checks
python3 tui/test_apply.py       # apply_ruixu plan/write/idempotency
python3 tui/test_mppt.py        # VE.Direct frames, decoders, validation, mppt_ruixu
.venv/bin/python tui/test_mppt_tui.py   # mppt_tui end-to-end against the mock (needs textual)
```

## Profile Verify & Apply (`tui/verify_ruixu.py`, `tui/apply_ruixu.py`)

Provisioning tools for the RUiXU 48V LiFePO4 profile (30A AC limit, 56.00/54.60V
charge voltages, 44.00V shutdown +1.00V restart, PowerAssist, dynamic current
limiter, wide frequency range, fixed charge curve):

```bash
# Check compliance — strictly read-only, both default ports:
python3 tui/verify_ruixu.py                  # /dev/inverter + /dev/inverter2
python3 tui/verify_ruixu.py --mock           # demo without hardware

# Bring a unit into compliance — writes ONLY the settings that deviate
# (minimal EEPROM wear), flag registers via read-modify-write, every write
# readback-verified, full verify report afterwards:
python3 tui/apply_ruixu.py --dry-run         # show the plan, write nothing
python3 tui/apply_ruixu.py                   # interactive confirm per port
python3 tui/apply_ruixu.py --yes /dev/inverter2
```

Three things stay VEConfigure-only (no known setting ID): the 50/60 Hz output
frequency (verified live only — a 50 Hz unit can't be fixed here), the
"current limit overruled by remote" checkbox, and per-unit charger disable for
240V systems. Stop whatever holds the port first (`sudo systemctl stop
grounded-inverter grounded-inverter2`), restart it after. Exit codes are
scriptable: 0 compliant, 1 deviations/failed writes, 2 port/link problem.

### Solar charge controller (`tui/mppt_ruixu.py`)

The MPPT counterpart, over **VE.Direct** (the small JST port / VE.Direct-USB
cable, 19200 8N1 — not the VE.Bus RJ45). Targets the Battery-section values:
48V system voltage, user-defined battery type, 57.00V absorption, 55.00V
float (deliberately above the inverter profile — solar is the primary
charge source). The battery-type write is included because custom voltages
don't stick while a fixed preset is selected:

```bash
python3 tui/mppt_ruixu.py --mock               # demo without hardware
python3 tui/mppt_ruixu.py                      # verify only (read-only)
python3 tui/mppt_ruixu.py --apply --dry-run    # show the write plan
python3 tui/mppt_ruixu.py --apply              # interactive confirm, readback-verified
```

Auto-detects the port from `/dev/serial/by-id/*VE_Direct*`. Register ids are
from Victron's [VE.Direct HEX protocol doc for BlueSolar/SmartSolar MPPTs](https://www.victronenergy.com/upload/documents/BlueSolar-HEX-protocol.pdf)
(Rev 18); every write is readback-verified, so a model that disagrees fails
loudly instead of writing silently wrong values. The system-voltage check reads
`0xEDEA` (the *setting*, 0 = auto-detect), not `0xEDEF` — `0xEDEF` always
reports the operating voltage, so an auto-detecting unit that found 48V would
otherwise pass.

## Solar Charger TUI (`tui/mppt_tui.py`)

The MPPT counterpart of `tui/tui.py` — same layout and safety model, but over
VE.Direct (VE.Direct-USB cable, not the MK3). Tabs:

- **Dashboard** — live telemetry on an auto-refresh (charge state, error, off
  reason, PV V/A/W, tracker mode, battery V, charger and battery current,
  temperatures, yield today/yesterday/total) plus a guarded Charger ON/OFF
  soft switch (`0x0200`; enables the remote on/off bit first on firmware
  < 1.42).
- **Battery** — every battery-section setting, system voltage (auto / 12 /
  24 / 36 / 48), battery type → User defined / back to preset, individual
  edits (absorption, float, equalisation, re-bulk offset, max current,
  absorption time, tail current, temp compensation, low-temp cut-off), and a
  one-button **RUiXU solar profile** (same diff-based plan as `mppt_ruixu.py`).
- **Load output** — mode + switch levels + AES timer, on the 10/15/20A models
  that have a load output (disabled otherwise, from the capabilities register).
  Mode writes are read-modify-write: the reserved high nibble is preserved.
- **Registers** — every known register raw + decoded, plus a read-only probe
  for any register id (useful on a new model or firmware).

```bash
.venv/bin/python tui/mppt_tui.py                    # auto-detect /dev/serial/by-id/*VE_Direct*
.venv/bin/python tui/mppt_tui.py --port /dev/ttyUSB1
.venv/bin/python tui/mppt_tui.py --mock              # no hardware (also the fallback if no port opens)
.venv/bin/python tui/mppt_tui.py --mock --mock-load  # simulate a model with a load output
```

Safety differences from the VE.Bus TUI:

- **No RAM-only mode** — every VE.Direct write goes straight to the
  controller's NVM. Writes are never looped; telemetry is reads only.
- **Writes are validated before the confirm dialog, and blocked if they
  fail** — charge voltages within the device's own adjustable range
  (`0x2211`/`0x2212`, else the doc's 12V window scaled to the system), float ≤
  absorption, temp compensation within the scaled ±21 mV/K, max current ≤ the
  charger rating, and charge voltages refused unless the battery type is User
  defined (they don't stick otherwise; an out-of-range set raises error 119,
  which needs a factory reset + power cycle to clear).
- **Readback-verified**, and a battery-type or system-voltage change reloads
  everything (both move other values).

Not exposed on purpose: restore factory defaults (`0x0004`), history clear,
relay settings, and the lighting-controller timer.

## Multi-Unit Scanning (`tui/bus_scan.py`)

Multiple VE.Bus units configured as one system (parallel or split-phase
120/240V) share the bus, and one MK3 can read all of them — via the 'A'
address-select command and the per-phase `'F' 2` AC frame. `bus_scan.py`
discovers which addresses answer and dumps every unit's status side-by-side,
strictly read-only (it always restores address 0 on exit):

```bash
# Demo against a simulated two-unit system, no hardware:
python3 tui/bus_scan.py --mock

# Real bus — hex trace + CSV, worth capturing for FINDINGS §12:
python3 tui/bus_scan.py --port /dev/inverter -v -o scan.csv
```

Needs only pyserial (no textual). Both inverters must be switched ON — a
sleeping unit answers nothing. If another program holds the port, stop it
first (on Grounded Pis: `sudo systemctl stop grounded-inverter`, and start it
again afterwards). Full background + results matrix:
[FINDINGS.md §12](FINDINGS.md#12-multi-unit--split-phase-systems-single-mk3).

## Key Protocol Corrections

Victron's documentation has several errors. The most critical:

**Use CommandWriteViaID (0x37), not CommandWriteSetting (0x33) + CommandWriteData (0x34).** The documented two-step write process does not work. The correct write frame is:

```
07 FF 58 37 01 [setting_id] [value_lo] [value_hi] [checksum]
```

- `07` — length byte
- `FF` — protocol marker
- `58` — Winmon slot ('X'; any of 0x57–0x5A works)
- `37` — CommandWriteViaID
- `01` — flags: RAM + EEPROM (use `0x03` for RAM only)
- Setting value is little-endian, voltage settings scaled ×100 (e.g., 5600 = 56.00V)

See [FINDINGS.md § Command Reference](FINDINGS.md#4-command-reference) for the full command set including ReadSetting, GetSettingInfo, ReadRAMVar, and State commands.

## Required Hardware

- Victron MultiPlus, Quattro, or compatible VE.Bus inverter/charger
- MK3-USB interface
- USB connection to host computer

## Software Requirements

- Python 3.x
- pyserial

```bash
pip install pyserial
```

## Quick Start

```python
# Set absorption and float voltage
from setv import VoltageSettings

setter = VoltageSettings()
setter.set_voltage(56.0, 2)  # Absorption = 56.00V
setter.set_voltage(54.0, 3)  # Float = 54.00V
```

```bash
# Discover all settings on your inverter
python settings_sweep.py /dev/ttyUSB0 -o baseline.csv

# Discover live RAM variables
python ram_sweep.py /dev/ttyUSB0 -o ramvars.csv
```

**Note:** Writes go to EEPROM which has limited write cycles. These tools are for configuration and discovery, not automation loops.

## Safety

- Verify voltage settings with a multimeter after writing
- Incorrect charge parameters can damage batteries
- Never blindly copy flag register values between different models — base values differ between MultiPlus and Quattro
- Always use read-modify-write when changing individual bits in flag registers (Settings 0 and 1)

## Contributing

This project was developed through reverse engineering and practical testing. If you discover additional protocol details, setting ID mappings, or corrections, please contribute them back.

## License

[MIT](LICENSE) — permissive. You're free to use, modify, and distribute this, including commercially; just keep the copyright and permission notice. The software is provided "as is", with **no warranty and no liability for misuse** — and note this drives real power hardware, so see the safety notes above.
