"""
================================================================================
SENSOR MONITOR GUI
================================================================================

Tkinter desktop GUI for the Raspberry Pi sensor data pipeline.

Features:
    - Tabs: Live Readings, Channel Config, Calibration, Settings
    - Multi-board ADS1115 support (direct + TCA9548A multiplexer)
    - Auto-scan: detect new boards on the I2C bus and add them to the config
    - Debug console: environment, I2C bus, ADC board, and input diagnostics
    - Status thresholds: per-sensor raw/calibrated min/max with live alert display
    - ×3 voltage divider correction (matches pi_sensor_reader.py)

NAMING (all three formats accepted in calibration_config.yaml):
    A0..A3                  legacy single-board (treated as ADC0)
    ADCn_Am                 direct multi-board   (n = 0..3)
    MUXn_ADCm_Ak            via TCA9548A mux     (n = 0..7, m = 0..3)

USAGE:
    cd ~/sensor_project
    python3 sensor_gui.py

================================================================================
"""

import tkinter as tk
from tkinter import ttk, filedialog, messagebox, scrolledtext
import threading
import time
import os
import re
import sys
import csv
import math
import platform
import traceback
from datetime import datetime, timezone

import yaml
import numpy as np

from calibration import CalibrationManager, load_reference_data, fit_calibration
from influxdb_client import InfluxDBClient, Point, WritePrecision
from influxdb_client.client.write_api import SYNCHRONOUS
from mariadb_writer import MariaDBWriter

try:
    import board
    import busio
    import adafruit_ads1x15.ads1115 as ADS
    from adafruit_ads1x15.analog_in import AnalogIn
    HAS_ADC = True
    ADC_IMPORT_ERROR = None
except Exception as exc:
    HAS_ADC = False
    ADC_IMPORT_ERROR = str(exc)

try:
    import adafruit_tca9548a
    HAS_MUX = True
    MUX_IMPORT_ERROR = None
except ImportError as exc:
    HAS_MUX = False
    MUX_IMPORT_ERROR = str(exc)

# ==============================================================================
# CONFIG
# ==============================================================================

CONFIG_FILE = os.path.join(os.path.dirname(__file__), "calibration_config.yaml")
SENSOR_CONFIG_FILE = os.path.join(os.path.dirname(__file__), "sensor_config.yaml")

DEFAULT_SETTINGS_Influx = {
    "influxdb_url": "https://us-east-1-1.aws.cloud2.influxdata.com",
    "influxdb_token": "T8tBnSeKupK4akBv-9ec92s5YCuQXx7GEU0ITpRtEygzhx4S4uoJ3DbCjQMrRiMk8IBb70FYkufIqM1q1cikwQ==",
    "influxdb_org": "University of Oxford",
    "influxdb_bucket": "sensor_data",
    "csv_backup_dir": os.path.expanduser("~/sensor_logs"),
    "sample_interval": 2.0,
}

DEFAULT_SETTINGS_MariaDB = {
    "mariadb_host": "127.0.0.1",
    "mariadb_port": 3306,
    "mariadb_user": "",
    "mariadb_password": "",
    "mariadb_database": "sensor_data",
}
DEFAULT_DATABASE_BACKEND = "mariadb"

ADC_ADDRESSES = {0: 0x48, 1: 0x49, 2: 0x4A, 3: 0x4B}
DEFAULT_MUX_ADDRESS = 0x70

# Name parsers
MUX_NAME_RE = re.compile(r"^MUX(\d+)_ADC(\d+)_A([0-3])$")
ADC_NAME_RE = re.compile(r"^ADC(\d+)_A([0-3])$")
LEGACY_NAME_RE = re.compile(r"^A([0-3])$")


def parse_sensor_name(name):
    """Parse a sensor name into a hardware address dict, or None."""
    m = MUX_NAME_RE.match(name)
    if m:
        return {"mux_channel": int(m.group(1)), "adc_index": int(m.group(2)), "channel": int(m.group(3))}
    m = ADC_NAME_RE.match(name)
    if m:
        return {"mux_channel": None, "adc_index": int(m.group(1)), "channel": int(m.group(2))}
    m = LEGACY_NAME_RE.match(name)
    if m:
        return {"mux_channel": None, "adc_index": 0, "channel": int(m.group(1))}
    return None


def hardware_key(sensor_config):
    return (sensor_config.get("mux_channel"), sensor_config["adc_index"])


# ==============================================================================
# STATUS LOGIC
# ==============================================================================

def parse_threshold(value):
    """Convert a YAML threshold value to float or None."""
    if value is None or value == "" or value == "null":
        return None
    try:
        return float(value)
    except (ValueError, TypeError):
        return None


def compute_status(raw_value, cal_value, sensor_config):
    """Determine OK/ALERT based on configured thresholds.

    Returns ("OK", "") or ("ALERT", reason). Bounds left as None mean
    "no constraint". If no bounds at all → always OK.
    """
    raw_min = parse_threshold(sensor_config.get("raw_min"))
    raw_max = parse_threshold(sensor_config.get("raw_max"))
    cal_min = parse_threshold(sensor_config.get("cal_min"))
    cal_max = parse_threshold(sensor_config.get("cal_max"))

    reasons = []
    if raw_value is not None:
        if raw_min is not None and raw_value < raw_min:
            reasons.append(f"raw<{raw_min}")
        if raw_max is not None and raw_value > raw_max:
            reasons.append(f"raw>{raw_max}")
    if cal_value is not None:
        if cal_min is not None and cal_value < cal_min:
            reasons.append(f"cal<{cal_min}")
        if cal_max is not None and cal_value > cal_max:
            reasons.append(f"cal>{cal_max}")

    if reasons:
        return "ALERT", ",".join(reasons)
    return "OK", ""


# ==============================================================================
# SETTINGS
# ==============================================================================

def load_sensor_settings():
    if os.path.exists(SENSOR_CONFIG_FILE):
        with open(SENSOR_CONFIG_FILE, 'r') as f:
            settings = yaml.safe_load(f) or {}
        for key, val in DEFAULT_SETTINGS_Influx.items():
            if key not in settings:
                settings[key] = val
        for key, val in DEFAULT_SETTINGS_MariaDB.items():
            if key not in settings:
                settings[key] = val
        if "database_backend" not in settings:
            settings["database_backend"] = DEFAULT_DATABASE_BACKEND
        return settings
    settings = dict(DEFAULT_SETTINGS_Influx)
    settings.update(DEFAULT_SETTINGS_MariaDB)
    settings["database_backend"] = DEFAULT_DATABASE_BACKEND
    return settings


def save_sensor_settings(settings):
    with open(SENSOR_CONFIG_FILE, 'w') as f:
        yaml.dump(settings, f, default_flow_style=False, sort_keys=False)


def load_channels():
    """Load channel config from calibration_config.yaml.

    Returns (channels_dict, mux_address). Threshold fields included if present.
    """
    if not os.path.exists(CONFIG_FILE):
        return {}, DEFAULT_MUX_ADDRESS

    with open(CONFIG_FILE, 'r') as f:
        config = yaml.safe_load(f) or {}

    mux_address = config.get("multiplexer_address", DEFAULT_MUX_ADDRESS)
    sensors = config.get("sensors") or {}
    channels = {}

    for name, cfg in sensors.items():
        parsed = parse_sensor_name(name)
        if parsed is None:
            continue
        channels[name] = {
            "enabled": cfg.get("enabled", False),
            "channel": parsed["channel"],
            "adc_index": parsed["adc_index"],
            "mux_channel": parsed["mux_channel"],
            "measurement": cfg.get("measurement", "sensor_reading"),
            "field": cfg.get("raw_field", "voltage"),
            "unit": cfg.get("raw_unit", "V"),
            "location": cfg.get("location", "lab"),
            "gain": cfg.get("gain", 1),
            "label": cfg.get("label", name),
            "raw_min": cfg.get("raw_min"),
            "raw_max": cfg.get("raw_max"),
            "cal_min": cfg.get("cal_min"),
            "cal_max": cfg.get("cal_max"),
        }
    return channels, mux_address


# ==============================================================================
# ADC HARDWARE
# ==============================================================================

PIN_MAP = {0: 1, 1: 2, 2: 3, 3: 0}
ADS_PINS = None
if HAS_ADC:
    ADS_PINS = {0: ADS.P0, 1: ADS.P1, 2: ADS.P2, 3: ADS.P3}


def init_adcs(sensor_channels, mux_address=DEFAULT_MUX_ADDRESS):
    """Initialise direct + multiplexed boards needed by enabled sensors.

    Returns (adcs_dict, mux). adcs_dict keyed by (mux_channel, adc_index).
    """
    if not HAS_ADC:
        return {}, None

    enabled = {n: c for n, c in sensor_channels.items() if c.get("enabled", False)}
    if not enabled:
        # Try at least ADC0 so the GUI can confirm hardware is alive
        enabled_indices = [0]
        mux_pairs = []
        needs_mux = False
    else:
        needs_mux = any(c.get("mux_channel") is not None for c in enabled.values())
        enabled_indices = sorted({
            c["adc_index"] for c in enabled.values() if c.get("mux_channel") is None
        })
        mux_pairs = sorted({
            (c["mux_channel"], c["adc_index"]) for c in enabled.values()
            if c.get("mux_channel") is not None
        })

    i2c = busio.I2C(board.SCL, board.SDA)
    adcs = {}
    mux = None

    for adc_index in enabled_indices:
        if adc_index not in ADC_ADDRESSES:
            continue
        try:
            ads = ADS.ADS1115(i2c, address=ADC_ADDRESSES[adc_index])
            chans = {ch: AnalogIn(ads, ADS_PINS[pin]) for ch, pin in PIN_MAP.items()}
            adcs[(None, adc_index)] = (ads, chans)
        except Exception:
            pass

    if needs_mux and HAS_MUX:
        try:
            mux = adafruit_tca9548a.TCA9548A(i2c, address=mux_address)
        except Exception:
            return adcs, None

        for mux_ch, adc_index in mux_pairs:
            if mux_ch < 0 or mux_ch > 7 or adc_index not in ADC_ADDRESSES:
                continue
            try:
                ads = ADS.ADS1115(mux[mux_ch], address=ADC_ADDRESSES[adc_index])
                chans = {ch: AnalogIn(ads, ADS_PINS[pin]) for ch, pin in PIN_MAP.items()}
                adcs[(mux_ch, adc_index)] = (ads, chans)
            except Exception:
                pass

    return adcs, mux


def read_adc(adcs, channel_config):
    """Read voltage from the appropriate ADC, applying ×3 divider scaling."""
    key = hardware_key(channel_config)
    channel = channel_config["channel"]
    if key not in adcs:
        return None
    ads, channels = adcs[key]
    ads.gain = channel_config.get("gain", 1)
    try:
        raw_adc_voltage = channels[channel].voltage
        return raw_adc_voltage
    except Exception:
        return None


def run_hardware_diagnostics(sensor_channels, mux_address, hardware_busy=False):
    """Return a detailed report about the configured ADC hardware."""
    report = [
        "=" * 72,
        "LAB MONITORING HARDWARE DIAGNOSTICS",
        f"Time: {datetime.now().astimezone().isoformat(timespec='seconds')}",
        f"Host: {platform.node() or 'unknown'}",
        f"Platform: {platform.platform()}",
        f"Python: {sys.version.replace(os.linesep, ' ')}",
        f"Application: {os.path.abspath(__file__)}",
        f"Working directory: {os.getcwd()}",
        f"Sensor config: {CONFIG_FILE} ({'found' if os.path.isfile(CONFIG_FILE) else 'missing'})",
        f"Enabled channels: {sum(1 for cfg in sensor_channels.values() if cfg.get('enabled'))}",
        f"Configured channels: {len(sensor_channels)}",
        f"I2C multiplexer address: 0x{mux_address:02X}",
        "",
        "Raspberry Pi I2C pins: SDA1 = BCM GPIO 2 / physical pin 3; "
        "SCL1 = BCM GPIO 3 / physical pin 5.",
        "The bus scan checks which I2C devices acknowledge; it cannot measure "
        "static pin voltage or prove wiring quality.",
        "Diagnostics probe the I2C bus and configure ADC gain for sampling; "
        "they do not set sensor inputs as GPIO outputs.",
        "ADC input labels use the configured board mapping: "
        "A0->ADS P1, A1->P2, A2->P3, A3->P0.",
        "",
        "Optional hardware libraries:",
        f"  ADC/I2C libraries: {'available' if HAS_ADC else 'unavailable'}",
    ]
    if not HAS_ADC:
        report.append(f"    Import error: {ADC_IMPORT_ERROR}")
    report.append(f"  TCA9548A library: {'available' if HAS_MUX else 'unavailable'}")
    if not HAS_MUX:
        report.append(f"    Import error: {MUX_IMPORT_ERROR}")

    enabled_channels = {
        name: cfg for name, cfg in sensor_channels.items() if cfg.get("enabled")
    }
    report.append("")
    report.append("Configured sensor inputs:")
    if not sensor_channels:
        report.append("  No valid channels were loaded from the sensor configuration.")
    else:
        for name, cfg in sorted(sensor_channels.items()):
            state = "enabled" if cfg.get("enabled") else "disabled"
            mux_channel = cfg.get("mux_channel")
            board_name = (
                f"MUX{mux_channel}_ADC{cfg['adc_index']}"
                if mux_channel is not None else f"ADC{cfg['adc_index']}"
            )
            report.append(
                f"  {name} ({cfg.get('label', name)}): {state}; "
                f"{board_name}; input A{cfg['channel']}; gain {cfg.get('gain', 1)}"
            )

    if hardware_busy:
        report.extend([
            "",
            "Hardware tests skipped: data collection is running and owns the ADC bus.",
            "Stop collection and run diagnostics again for live input measurements.",
        ])
        return report

    if not HAS_ADC:
        report.extend([
            "",
            "Hardware tests skipped because the ADC/I2C libraries are unavailable.",
            "Install the project's Adafruit Blinka and ADS1x15 dependencies on the Pi.",
        ])
        return report

    report.extend(["", "I2C bus and ADC input tests:"])
    i2c = None
    mux = None
    try:
        report.append(f"  board.SDA: {board.SDA!r}")
        report.append(f"  board.SCL: {board.SCL!r}")
        i2c = busio.I2C(board.SCL, board.SDA)
        deadline = time.monotonic() + 3.0
        while not i2c.try_lock():
            if time.monotonic() >= deadline:
                raise TimeoutError("I2C bus remained locked for 3 seconds")
            time.sleep(0.01)
        try:
            addresses = i2c.scan()
        finally:
            i2c.unlock()

        report.append(
            "  I2C scan: " + (
                ", ".join(f"0x{address:02X}" for address in addresses)
                if addresses else "no devices acknowledged"
            )
        )
        adc_indices_on_bus = [
            index for index, address in ADC_ADDRESSES.items() if address in addresses
        ]
        report.append(
            "  Direct ADS1115 address candidates: " + (
                ", ".join(f"ADC{index} (0x{ADC_ADDRESSES[index]:02X})"
                          for index in adc_indices_on_bus)
                if adc_indices_on_bus else "none"
            )
        )

        needs_mux = any(cfg.get("mux_channel") is not None
                        for cfg in enabled_channels.values())
        if needs_mux:
            if not HAS_MUX:
                report.append("  Multiplexer test: FAIL — TCA9548A library unavailable")
            else:
                try:
                    mux = adafruit_tca9548a.TCA9548A(i2c, address=mux_address)
                    report.append(
                        f"  Multiplexer at 0x{mux_address:02X}: PASS (responded)"
                    )
                except Exception as exc:
                    report.append(
                        f"  Multiplexer at 0x{mux_address:02X}: FAIL — "
                        f"{type(exc).__name__}: {exc}"
                    )

        keys_to_test = {hardware_key(cfg) for cfg in enabled_channels.values()}
        if not keys_to_test:
            keys_to_test.update((None, index) for index in adc_indices_on_bus)
            if not keys_to_test:
                report.append("  No configured active ADC boards to test.")

        config_by_key_and_channel = {
            (hardware_key(cfg), cfg["channel"]): (name, cfg)
            for name, cfg in enabled_channels.items()
        }
        gain_full_scale = {
            2 / 3: 6.144,
            1: 4.096,
            2: 2.048,
            4: 1.024,
            8: 0.512,
            16: 0.256,
        }

        for mux_channel, adc_index in sorted(
                keys_to_test, key=lambda key: (-1 if key[0] is None else key[0], key[1])):
            if adc_index not in ADC_ADDRESSES:
                report.append(
                    f"  ADC board (mux={mux_channel}, index={adc_index}): "
                    "FAIL — unsupported ADC address index"
                )
                continue
            address = ADC_ADDRESSES[adc_index]
            board_label = (
                f"MUX{mux_channel}_ADC{adc_index}"
                if mux_channel is not None else f"ADC{adc_index}"
            )
            if mux_channel is not None and mux is None:
                report.append(
                    f"  {board_label}: FAIL — multiplexer could not be initialized"
                )
                continue
            if mux_channel is None and address not in addresses:
                report.append(
                    f"  {board_label} (0x{address:02X}): FAIL — "
                    "address did not acknowledge on the I2C bus"
                )
                continue
            if mux_channel is not None and not 0 <= mux_channel <= 7:
                report.append(
                    f"  {board_label}: FAIL — mux channel must be between 0 and 7"
                )
                continue

            try:
                adc_bus = i2c if mux_channel is None else mux[mux_channel]
                ads = ADS.ADS1115(adc_bus, address=address)
                report.append(f"  {board_label} (0x{address:02X}): PASS (initialized)")
            except Exception as exc:
                report.append(
                    f"  {board_label} (0x{address:02X}): FAIL — "
                    f"{type(exc).__name__}: {exc}"
                )
                continue

            for channel in range(4):
                configured = config_by_key_and_channel.get(
                    ((mux_channel, adc_index), channel)
                )
                sensor_name, sensor_cfg = configured if configured else (None, {})
                gain = sensor_cfg.get("gain", 1)
                label = (
                    f"A{channel} ({sensor_name})"
                    if sensor_name else f"A{channel} (not configured)"
                )
                input_pin = PIN_MAP[channel]
                try:
                    ads.gain = gain
                    analog_input = AnalogIn(ads, ADS_PINS[input_pin])
                    samples = []
                    for sample_index in range(3):
                        samples.append(float(analog_input.voltage))
                        if sample_index < 2:
                            time.sleep(0.05)
                    if not all(math.isfinite(value) for value in samples):
                        raise ValueError(f"non-finite voltage samples: {samples}")
                except Exception as exc:
                    report.append(
                        f"    {label}: FAIL — {type(exc).__name__}: {exc}"
                    )
                    continue

                low, high = min(samples), max(samples)
                delta = high - low
                sample_text = ", ".join(f"{value:.6f} V" for value in samples)
                if not sensor_name:
                    report.append(
                        f"    {label}: INFO — readable; samples [{sample_text}], "
                        f"delta {delta:.6f} V; no configured expected range"
                    )
                    continue

                issues = []
                full_scale = gain_full_scale.get(gain)
                if full_scale is None:
                    issues.append(f"gain {gain!r} has no known ADS1115 range")
                elif low < 0 or high > full_scale:
                    issues.append(
                        f"outside the +/-{full_scale:.3f} V PGA range"
                    )

                raw_min = parse_threshold(sensor_cfg.get("raw_min"))
                raw_max = parse_threshold(sensor_cfg.get("raw_max"))
                if raw_min is not None and low < raw_min:
                    issues.append(f"below configured raw_min {raw_min:g} V")
                if raw_max is not None and high > raw_max:
                    issues.append(f"above configured raw_max {raw_max:g} V")

                if issues:
                    status = "WARN — " + "; ".join(issues)
                elif delta == 0:
                    status = (
                        "CAUTION — unchanged across samples; this may be a steady "
                        "signal or a disconnected/stuck input"
                    )
                else:
                    status = "PASS — readable and within known configured ranges"
                report.append(
                    f"    {label}: {status}; samples [{sample_text}], "
                    f"delta {delta:.6f} V"
                )

    except Exception as exc:
        report.append(
            f"  I2C diagnostic failed: {type(exc).__name__}: {exc}"
        )
    finally:
        if i2c is not None:
            try:
                i2c.deinit()
                report.append("  I2C bus released.")
            except Exception as exc:
                report.append(
                    f"  I2C cleanup failed: {type(exc).__name__}: {exc}"
                )

    report.extend([
        "",
        "Interpretation: a readable value only proves the ADC transaction worked. "
        "Confirm sensor wiring, supply voltage, and expected signal range separately.",
        "=" * 72,
    ])
    return report


def save_to_csv(csv_dir, sensor_name, fields, timestamp):
    os.makedirs(csv_dir, exist_ok=True)
    filename = os.path.join(csv_dir, f"{sensor_name}_{timestamp.strftime('%Y-%m')}.csv")
    file_exists = os.path.exists(filename)
    with open(filename, 'a', newline='') as f:
        writer = csv.writer(f)
        if not file_exists:
            writer.writerow(["timestamp"] + list(fields.keys()))
        writer.writerow([timestamp.isoformat()] + list(fields.values()))


# ==============================================================================
# AUTO-SCAN HARDWARE
# ==============================================================================

def _i2c_probe(i2c, address):
    """Fast raw probe — returns True if a device ACKs at the given address.

    Much faster than constructing an ADS1115 object: ~5ms vs ~500ms per address.
    Mirrors what `i2cdetect` does on the command line.
    """
    while not i2c.try_lock():
        pass
    try:
        # Try a 1-byte write of zero. Devices that exist will ACK.
        i2c.writeto(address, b"\x00")
        return True
    except Exception:
        return False
    finally:
        i2c.unlock()


def scan_for_new_boards(existing_channels, mux_address=DEFAULT_MUX_ADDRESS):
    """Scan I2C bus for ADS1115 boards (direct + via mux) not yet in config.

    Uses raw I2C ACK probes for speed — ~10x faster than ADS object construction.
    Returns list of new sensor names that should be added as placeholders.
    """
    if not HAS_ADC:
        return []

    new_names = []
    existing_keys = {(c.get("mux_channel"), c.get("adc_index")) for c in existing_channels.values()}

    try:
        i2c = busio.I2C(board.SCL, board.SDA)
    except Exception:
        return []

    # Direct address probes
    for adc_index, addr in ADC_ADDRESSES.items():
        if (None, adc_index) in existing_keys:
            continue
        if _i2c_probe(i2c, addr):
            for ch in range(4):
                new_names.append(f"ADC{adc_index}_A{ch}")

    # Multiplexer probe — only sweep mux channels if mux itself responds
    if HAS_MUX and _i2c_probe(i2c, mux_address):
        try:
            mux = adafruit_tca9548a.TCA9548A(i2c, address=mux_address)
        except Exception:
            mux = None

        if mux is not None:
            for mux_ch in range(8):
                try:
                    mux_bus = mux[mux_ch]
                except Exception:
                    continue
                for adc_index, addr in ADC_ADDRESSES.items():
                    if (mux_ch, adc_index) in existing_keys:
                        continue
                    if _i2c_probe(mux_bus, addr):
                        for ch in range(4):
                            new_names.append(f"MUX{mux_ch}_ADC{adc_index}_A{ch}")

    return new_names


def add_placeholder_sensors(new_names):
    """Add disabled placeholder entries to calibration_config.yaml."""
    if not new_names:
        return 0

    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, 'r') as f:
            config = yaml.safe_load(f) or {"sensors": {}}
    else:
        config = {"sensors": {}}

    if config.get("sensors") is None:
        config["sensors"] = {}

    today = str(datetime.now().date())
    added = 0
    for name in new_names:
        if name in config["sensors"]:
            continue
        config["sensors"][name] = {
            "enabled": False,
            "label": name,
            "model": "linear",
            "raw_field": "voltage",
            "calibrated_field": "calibrated_voltage",
            "raw_unit": "V",
            "calibrated_unit": "V",
            "slope": 1.0,
            "offset": 0.0,
            "valid_range": [0.0, 10.0],
            "raw_min": None,
            "raw_max": None,
            "cal_min": None,
            "cal_max": None,
            "last_calibrated": today,
            "notes": "Auto-detected — enable and label after wiring up",
        }
        added += 1

    with open(CONFIG_FILE, 'w') as f:
        yaml.dump(config, f, default_flow_style=False, sort_keys=False)

    return added


def run_mariadb_diagnostics(settings):
    """Check MariaDB connectivity and report whether readings are present."""
    report = ["MariaDB diagnostics:"]
    writer = None
    try:
        writer = MariaDBWriter(settings)
        reading_count, latest_reading = writer.get_stream_status()
        report.extend([
            "  Connection and sensor_readings query: PASS",
            f"  Stored readings: {reading_count}",
            f"  Latest recorded_at: {latest_reading or 'no readings yet'}",
        ])
    except Exception as exc:
        report.append(
            f"  Connection/query: FAIL — {type(exc).__name__}: {exc}"
        )
    finally:
        if writer is not None:
            writer.close()
    return report


# ==============================================================================
# MAIN GUI
# ==============================================================================

class SensorMonitorApp:

    def __init__(self, root):
        self.root = root
        self.root.title("Sensor Monitor")
        self.root.geometry("1280x800")
        self.root.configure(bg="#f0f0f0")

        self.running = False
        self.thread = None
        self.adcs = {}
        self.mux = None
        self.cal = None
        self.settings = load_sensor_settings()
        self.channels, self.mux_address = load_channels()
        self.influx_client = None
        self.write_api = None
        self.mariadb_writer = None
        self.debug_window = None
        self.debug_running = False

        self.pending_cal = {}

        if os.path.exists(CONFIG_FILE):
            self.cal = CalibrationManager(CONFIG_FILE)

        self._build_gui()
        self._init_adc_safe()

    def _build_gui(self):
        # --- Top bar ---
        top_frame = ttk.Frame(self.root, padding=10)
        top_frame.pack(fill=tk.X)

        self.status_label = ttk.Label(top_frame, text="● STOPPED", foreground="red",
                                       font=("Helvetica", 14, "bold"))
        self.status_label.pack(side=tk.LEFT)

        self.btn_stop = ttk.Button(top_frame, text="Stop", command=self.stop_collection,
                                    state=tk.DISABLED)
        self.btn_stop.pack(side=tk.RIGHT, padx=5)

        self.btn_start = ttk.Button(top_frame, text="Start Collection",
                                     command=self.start_collection)
        self.btn_start.pack(side=tk.RIGHT, padx=5)

        self.btn_scan = ttk.Button(top_frame, text="Scan Hardware",
                                    command=self._scan_hardware)
        self.btn_scan.pack(side=tk.RIGHT, padx=5)

        self.btn_init_adc = ttk.Button(top_frame, text="Reinit ADC",
                                        command=self._init_adc_safe)
        self.btn_init_adc.pack(side=tk.RIGHT, padx=5)

        self.btn_debug = ttk.Button(top_frame, text="Debug Console",
                                    command=self._open_debug_console)
        self.btn_debug.pack(side=tk.RIGHT, padx=5)

        # --- Notebook ---
        self.notebook = ttk.Notebook(self.root)
        self.notebook.pack(fill=tk.BOTH, expand=True, padx=10, pady=5)

        self.tab_live = ttk.Frame(self.notebook, padding=10)
        self.notebook.add(self.tab_live, text="Live Readings")
        self._build_live_tab()

        self.tab_config = ttk.Frame(self.notebook, padding=10)
        self.notebook.add(self.tab_config, text="Channel Config")
        self._build_config_tab()

        self.tab_cal = ttk.Frame(self.notebook, padding=10)
        self.notebook.add(self.tab_cal, text="Calibration")
        self._build_cal_tab()

        self.tab_settings = ttk.Frame(self.notebook, padding=10)
        self.notebook.add(self.tab_settings, text="Settings")
        self._build_settings_tab()

        # --- Shared log ---
        log_frame = ttk.LabelFrame(self.root, text="Log", padding=5)
        log_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=5)
        self.log_text = scrolledtext.ScrolledText(log_frame, height=10, state=tk.DISABLED,
                                                   font=("Courier", 9))
        self.log_text.pack(fill=tk.BOTH, expand=True)

    # ==========================================================================
    # TAB 1: LIVE READINGS
    # ==========================================================================

    def _build_live_tab(self):
        columns = ("channel", "label", "raw", "calibrated", "status")
        self.tree = ttk.Treeview(self.tab_live, columns=columns, show="headings", height=10)

        self.tree.heading("channel", text="Channel")
        self.tree.heading("label", text="Label")
        self.tree.heading("raw", text="Raw Voltage (V)")
        self.tree.heading("calibrated", text="Calibrated")
        self.tree.heading("status", text="Status")

        self.tree.column("channel", width=140, anchor=tk.CENTER)
        self.tree.column("label", width=180, anchor=tk.CENTER)
        self.tree.column("raw", width=140, anchor=tk.CENTER)
        self.tree.column("calibrated", width=140, anchor=tk.CENTER)
        self.tree.column("status", width=200, anchor=tk.CENTER)

        # Coloured row tags
        self.tree.tag_configure("ok", background="#e8f5e9")
        self.tree.tag_configure("alert", background="#ffebee", foreground="#b71c1c")
        self.tree.tag_configure("disabled", background="#f5f5f5", foreground="#999999")

        self.tree.pack(fill=tk.BOTH, expand=True, pady=5)
        self._refresh_live_tree()

        interval_frame = ttk.Frame(self.tab_live)
        interval_frame.pack(fill=tk.X, pady=5)
        ttk.Label(interval_frame, text="Sample Interval (s):").pack(side=tk.LEFT)
        self.interval_var = tk.DoubleVar(value=self.settings.get("sample_interval", 2.0))
        ttk.Entry(interval_frame, textvariable=self.interval_var, width=8).pack(side=tk.LEFT, padx=5)

    def _refresh_live_tree(self):
        for item in self.tree.get_children():
            self.tree.delete(item)
        for name in sorted(self.channels.keys()):
            ch = self.channels[name]
            enabled = ch.get("enabled", False)
            label = ch.get("label", name)
            status = "Enabled" if enabled else "Disabled"
            tag = "disabled" if not enabled else "ok"
            self.tree.insert("", tk.END, iid=name,
                           values=(name, label, "---", "---", status),
                           tags=(tag,))

    # ==========================================================================
    # TAB 2: CHANNEL CONFIG
    # ==========================================================================

    def _build_config_tab(self):
        # Clear previous widgets in case this is being rebuilt after a scan
        for widget in self.tab_config.winfo_children():
            widget.destroy()

        self.config_widgets = {}

        # Scrollable container with both vertical and horizontal scrollbars
        canvas = tk.Canvas(self.tab_config, highlightthickness=0)
        v_scroll = ttk.Scrollbar(self.tab_config, orient="vertical", command=canvas.yview)
        h_scroll = ttk.Scrollbar(self.tab_config, orient="horizontal", command=canvas.xview)
        scroll_frame = ttk.Frame(canvas)
        scroll_frame.bind("<Configure>",
            lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=scroll_frame, anchor="nw")
        canvas.configure(yscrollcommand=v_scroll.set, xscrollcommand=h_scroll.set)

        # Grid layout: canvas top-left, vertical scroll right, horizontal scroll bottom
        canvas.grid(row=0, column=0, sticky="nsew")
        v_scroll.grid(row=0, column=1, sticky="ns")
        h_scroll.grid(row=1, column=0, sticky="ew")
        self.tab_config.rowconfigure(0, weight=1)
        self.tab_config.columnconfigure(0, weight=1)

        # All header + data rows live in this single grid so columns line up
        grid_frame = ttk.Frame(scroll_frame)
        grid_frame.pack(fill=tk.X, pady=(0, 10))

        # Column specs: (header text, pixel width)
        # Threshold columns deliberately narrow — values are usually single decimals
        col_specs = [
            ("Channel",     110),
            ("On",           36),
            ("Label",       150),
            ("Raw min",      62),
            ("Raw max",      62),
            ("Cal min",      62),
            ("Cal max",      62),
            ("Measurement", 130),
            ("Location",    100),
        ]

        for col, (_, pixwidth) in enumerate(col_specs):
            grid_frame.columnconfigure(col, minsize=pixwidth, weight=0)

        # --- Header row ---
        for col, (text, _) in enumerate(col_specs):
            ttk.Label(grid_frame, text=text,
                      font=("Helvetica", 10, "bold"),
                      anchor="center").grid(row=0, column=col, padx=2, pady=(0, 6), sticky="ew")

        # --- Data rows ---
        for r, ch_name in enumerate(sorted(self.channels.keys()), start=1):
            ch = self.channels[ch_name]

            ttk.Label(grid_frame, text=ch_name, anchor="center").grid(
                row=r, column=0, padx=2, pady=2, sticky="ew")

            enabled_var = tk.BooleanVar(value=ch.get("enabled", False))
            cb = ttk.Checkbutton(grid_frame, variable=enabled_var)
            cb.grid(row=r, column=1, padx=2, pady=2)

            label_var = tk.StringVar(value=ch.get("label", ch_name))
            ttk.Entry(grid_frame, textvariable=label_var).grid(
                row=r, column=2, padx=2, pady=2, sticky="ew")

            raw_min_var = tk.StringVar(value=self._fmt_threshold(ch.get("raw_min")))
            ttk.Entry(grid_frame, textvariable=raw_min_var, width=6).grid(
                row=r, column=3, padx=2, pady=2, sticky="ew")

            raw_max_var = tk.StringVar(value=self._fmt_threshold(ch.get("raw_max")))
            ttk.Entry(grid_frame, textvariable=raw_max_var, width=6).grid(
                row=r, column=4, padx=2, pady=2, sticky="ew")

            cal_min_var = tk.StringVar(value=self._fmt_threshold(ch.get("cal_min")))
            ttk.Entry(grid_frame, textvariable=cal_min_var, width=6).grid(
                row=r, column=5, padx=2, pady=2, sticky="ew")

            cal_max_var = tk.StringVar(value=self._fmt_threshold(ch.get("cal_max")))
            ttk.Entry(grid_frame, textvariable=cal_max_var, width=6).grid(
                row=r, column=6, padx=2, pady=2, sticky="ew")

            meas_var = tk.StringVar(value=ch.get("measurement", "sensor_reading"))
            ttk.Entry(grid_frame, textvariable=meas_var).grid(
                row=r, column=7, padx=2, pady=2, sticky="ew")

            location_var = tk.StringVar(value=ch.get("location", "lab"))
            ttk.Entry(grid_frame, textvariable=location_var).grid(
                row=r, column=8, padx=2, pady=2, sticky="ew")

            self.config_widgets[ch_name] = {
                "enabled": enabled_var,
                "label": label_var,
                "raw_min": raw_min_var,
                "raw_max": raw_max_var,
                "cal_min": cal_min_var,
                "cal_max": cal_max_var,
                "measurement": meas_var,
                "location": location_var,
            }

        btn_frame = ttk.Frame(scroll_frame)
        btn_frame.pack(fill=tk.X, pady=10)
        ttk.Button(btn_frame, text="Save Channel Config",
                   command=self._save_channel_config).pack(side=tk.LEFT, padx=5)
        ttk.Label(btn_frame,
                  text="(Leave threshold fields blank for no constraint. Restart collection after saving.)",
                  foreground="gray").pack(side=tk.LEFT)

    def _fmt_threshold(self, value):
        """Format a threshold value for display in an Entry widget."""
        if value is None:
            return ""
        return str(value)

    def _save_channel_config(self):
        if not os.path.exists(CONFIG_FILE):
            config = {"sensors": {}}
        else:
            with open(CONFIG_FILE, 'r') as f:
                config = yaml.safe_load(f) or {"sensors": {}}
            if config.get("sensors") is None:
                config["sensors"] = {}

        for ch_name, widgets in self.config_widgets.items():
            if ch_name not in config["sensors"]:
                config["sensors"][ch_name] = {
                    "enabled": True,
                    "model": "linear",
                    "raw_field": "voltage",
                    "calibrated_field": "calibrated_voltage",
                    "raw_unit": "V",
                    "calibrated_unit": "V",
                    "slope": 1.0,
                    "offset": 0.0,
                    "valid_range": [0.0, 10.0],
                }

            cfg = config["sensors"][ch_name]
            cfg["enabled"] = widgets["enabled"].get()
            cfg["label"] = widgets["label"].get()
            cfg["raw_min"] = parse_threshold(widgets["raw_min"].get())
            cfg["raw_max"] = parse_threshold(widgets["raw_max"].get())
            cfg["cal_min"] = parse_threshold(widgets["cal_min"].get())
            cfg["cal_max"] = parse_threshold(widgets["cal_max"].get())
            cfg["measurement"] = widgets["measurement"].get()
            cfg["location"] = widgets["location"].get()

            # Mirror into in-memory channels
            ch = self.channels.get(ch_name, {})
            ch["enabled"] = cfg["enabled"]
            ch["label"] = cfg["label"]
            ch["raw_min"] = cfg["raw_min"]
            ch["raw_max"] = cfg["raw_max"]
            ch["cal_min"] = cfg["cal_min"]
            ch["cal_max"] = cfg["cal_max"]
            ch["measurement"] = cfg["measurement"]
            ch["location"] = cfg["location"]
            self.channels[ch_name] = ch

        with open(CONFIG_FILE, 'w') as f:
            yaml.dump(config, f, default_flow_style=False, sort_keys=False)

        self.cal = CalibrationManager(CONFIG_FILE)
        self._refresh_live_tree()
        self._init_adc_safe()
        self.log("Channel config saved.")
        messagebox.showinfo("Saved", "Channel configuration saved successfully.")

    # ==========================================================================
    # TAB 3: CALIBRATION
    # ==========================================================================

    def _build_cal_tab(self):
        sel_frame = ttk.Frame(self.tab_cal)
        sel_frame.pack(fill=tk.X, pady=5)
        ttk.Label(sel_frame, text="Channel:").pack(side=tk.LEFT, padx=5)
        sensor_names = sorted(self.channels.keys())
        default_sensor = sensor_names[0] if sensor_names else ""
        self.cal_sensor_var = tk.StringVar(value=default_sensor)
        self.cal_sensor_combo = ttk.Combobox(sel_frame, textvariable=self.cal_sensor_var,
                     values=sensor_names, width=18, state="readonly")
        self.cal_sensor_combo.pack(side=tk.LEFT, padx=5)

        ttk.Button(sel_frame, text="Show Summary",
                   command=self._do_summary).pack(side=tk.RIGHT, padx=5)

        # Apply
        apply_frame = ttk.LabelFrame(self.tab_cal, text="Apply Calibration", padding=10)
        apply_frame.pack(fill=tk.X, pady=5)

        ttk.Label(apply_frame, text="Raw value:").grid(row=0, column=0, padx=5, sticky=tk.W)
        self.apply_value_var = tk.DoubleVar(value=1.5)
        ttk.Entry(apply_frame, textvariable=self.apply_value_var, width=12).grid(row=0, column=1, padx=5)
        ttk.Button(apply_frame, text="Apply", command=self._do_apply).grid(row=0, column=2, padx=5)
        self.apply_result_var = tk.StringVar(value="---")
        ttk.Label(apply_frame, textvariable=self.apply_result_var,
                  font=("Courier", 11)).grid(row=0, column=3, padx=10)

        # Fit / validate
        fit_frame = ttk.LabelFrame(self.tab_cal, text="Fit & Validate Calibration", padding=10)
        fit_frame.pack(fill=tk.BOTH, expand=True, pady=5)

        csv_btn_frame = ttk.Frame(fit_frame)
        csv_btn_frame.grid(row=0, column=0, columnspan=4, padx=5, sticky=tk.W, pady=(0, 5))
        ttk.Label(csv_btn_frame, text="Reference data:").pack(side=tk.LEFT, padx=(0, 5))
        ttk.Button(csv_btn_frame, text="Load from CSV", command=self._load_refs_csv).pack(side=tk.LEFT, padx=2)
        ttk.Button(csv_btn_frame, text="Save Table as CSV", command=self._save_ref_table_csv).pack(side=tk.LEFT, padx=2)
        ttk.Button(csv_btn_frame, text="Clear Table", command=self._clear_ref_table).pack(side=tk.LEFT, padx=2)

        table_frame = ttk.Frame(fit_frame)
        table_frame.grid(row=1, column=0, columnspan=4, padx=5, pady=5, sticky=tk.NSEW)
        fit_frame.rowconfigure(1, weight=1)

        ttk.Label(table_frame, text="Row", width=5,
                  font=("Helvetica", 9, "bold")).grid(row=0, column=0, padx=2)
        ttk.Label(table_frame, text="Raw (V)", width=15,
                  font=("Helvetica", 9, "bold")).grid(row=0, column=1, padx=2)
        ttk.Label(table_frame, text="Reference", width=15,
                  font=("Helvetica", 9, "bold")).grid(row=0, column=2, padx=2)

        self.ref_canvas = tk.Canvas(table_frame, height=120)
        ref_scrollbar = ttk.Scrollbar(table_frame, orient=tk.VERTICAL, command=self.ref_canvas.yview)
        self.ref_inner_frame = ttk.Frame(self.ref_canvas)

        self.ref_inner_frame.bind("<Configure>",
            lambda e: self.ref_canvas.configure(scrollregion=self.ref_canvas.bbox("all")))

        self.ref_canvas.create_window((0, 0), window=self.ref_inner_frame, anchor=tk.NW)
        self.ref_canvas.configure(yscrollcommand=ref_scrollbar.set)

        self.ref_canvas.grid(row=1, column=0, columnspan=3, sticky=tk.NSEW)
        ref_scrollbar.grid(row=1, column=3, sticky=tk.NS)

        self.ref_rows = []
        for _ in range(5):
            self._add_ref_row()

        ttk.Button(table_frame, text="+ Add Row",
                   command=self._add_ref_row).grid(row=2, column=0, columnspan=3, pady=3)

        action_frame = ttk.Frame(fit_frame)
        action_frame.grid(row=2, column=0, columnspan=4, pady=5, sticky=tk.W)

        ttk.Label(action_frame, text="Model:").pack(side=tk.LEFT, padx=5)
        self.fit_model_var = tk.StringVar(value="linear")
        ttk.Combobox(action_frame, textvariable=self.fit_model_var,
                     values=["linear", "polynomial"], width=12,
                     state="readonly").pack(side=tk.LEFT, padx=5)

        ttk.Button(action_frame, text="Fit", command=self._do_fit).pack(side=tk.LEFT, padx=5)
        ttk.Button(action_frame, text="Validate", command=self._do_validate).pack(side=tk.LEFT, padx=5)

        self.btn_commit = ttk.Button(action_frame, text="Commit Calibration",
                                      command=self._do_commit, state=tk.DISABLED)
        self.btn_commit.pack(side=tk.LEFT, padx=10)

        self.commit_label = ttk.Label(action_frame, text="", foreground="gray")
        self.commit_label.pack(side=tk.LEFT, padx=5)

    def _add_ref_row(self, raw_val="", ref_val=""):
        row_idx = len(self.ref_rows)
        ttk.Label(self.ref_inner_frame, text=str(row_idx + 1), width=5).grid(
            row=row_idx, column=0, padx=2, pady=1)

        raw_var = tk.StringVar(value=str(raw_val) if raw_val != "" else "")
        ttk.Entry(self.ref_inner_frame, textvariable=raw_var, width=15).grid(
            row=row_idx, column=1, padx=2, pady=1)

        ref_var = tk.StringVar(value=str(ref_val) if ref_val != "" else "")
        ttk.Entry(self.ref_inner_frame, textvariable=ref_var, width=15).grid(
            row=row_idx, column=2, padx=2, pady=1)

        self.ref_rows.append({"raw": raw_var, "ref": ref_var})

    def _clear_ref_table(self):
        for widget in self.ref_inner_frame.winfo_children():
            widget.destroy()
        self.ref_rows = []
        for _ in range(5):
            self._add_ref_row()
        self.pending_cal = {}
        self.btn_commit.configure(state=tk.DISABLED)
        self.commit_label.configure(text="")

    def _load_refs_csv(self):
        path = filedialog.askopenfilename(
            title="Select reference CSV",
            initialdir=os.path.dirname(__file__),
            filetypes=[("CSV files", "*.csv"), ("All files", "*.*")]
        )
        if not path:
            return
        try:
            for widget in self.ref_inner_frame.winfo_children():
                widget.destroy()
            self.ref_rows = []

            with open(path, 'r') as f:
                reader = csv.DictReader(f)
                for row in reader:
                    self._add_ref_row(row.get("raw", ""), row.get("reference", ""))

            for _ in range(3):
                self._add_ref_row()

            self.log(f"Loaded reference data from {os.path.basename(path)}")
        except Exception as e:
            messagebox.showerror("Error", f"Failed to load CSV:\n{e}")

    def _save_ref_table_csv(self):
        raw_data, ref_data = self._get_ref_data()
        if len(raw_data) == 0:
            messagebox.showwarning("Warning", "No data in table to save.")
            return

        path = filedialog.asksaveasfilename(
            title="Save reference CSV",
            initialdir=os.path.dirname(__file__),
            defaultextension=".csv",
            filetypes=[("CSV files", "*.csv")]
        )
        if not path:
            return

        with open(path, 'w', newline='') as f:
            writer = csv.writer(f)
            writer.writerow(["raw", "reference"])
            for r, ref in zip(raw_data, ref_data):
                writer.writerow([round(r, 6), round(ref, 6)])

        self.log(f"Saved reference data to {os.path.basename(path)}")

    def _get_ref_data(self):
        raw_vals = []
        ref_vals = []
        for row in self.ref_rows:
            raw_str = row["raw"].get().strip()
            ref_str = row["ref"].get().strip()
            if raw_str and ref_str:
                try:
                    raw_vals.append(float(raw_str))
                    ref_vals.append(float(ref_str))
                except ValueError:
                    pass
        return np.array(raw_vals), np.array(ref_vals)

    def _do_apply(self):
        sensor = self.cal_sensor_var.get()
        value = self.apply_value_var.get()

        if not self.cal:
            self.log("Error: No calibration config loaded.")
            return

        result = self.cal.apply(sensor, value)
        fields = self.cal.get_fields(sensor, value)

        self.log(f"[Calibration] Apply {sensor}: raw={value} -> calibrated={result}")
        self.log(f"  Fields: {fields}")
        self.apply_result_var.set(f"Calibrated: {result}")

    def _do_fit(self):
        sensor = self.cal_sensor_var.get()
        model = self.fit_model_var.get()

        raw, reference = self._get_ref_data()
        if len(raw) < 2:
            self.log("Error: Need at least 2 data points. Fill in the table.")
            return

        try:
            params, r_squared, residuals = fit_calibration(raw, reference, model)

            self.log(f"[Calibration] Fitting '{sensor}' with {model} model")
            self.log(f"  Data points: {len(raw)}")
            self.log(f"  Parameters:  {params}")
            self.log(f"  R-squared:   {r_squared:.6f}")
            self.log(f"  Max residual: {np.max(np.abs(residuals)):.6f}")
            self.log(f"  Mean residual: {np.mean(np.abs(residuals)):.6f}")
            self.log(f"  >> Fit complete. Review results, then click 'Commit Calibration' to save.")

            self.pending_cal[sensor] = {
                "params": params,
                "model": model,
                "r_squared": r_squared,
            }

            self.btn_commit.configure(state=tk.NORMAL)
            self.commit_label.configure(
                text=f"Pending: {sensor} ({model}, R²={r_squared:.4f})",
                foreground="orange"
            )

        except Exception as e:
            self.log(f"Error: {e}")

    def _do_commit(self):
        if not self.pending_cal:
            self.log("Nothing to commit.")
            return

        try:
            with open(CONFIG_FILE, 'r') as f:
                config = yaml.safe_load(f) or {"sensors": {}}
            if config.get("sensors") is None:
                config["sensors"] = {}

            for sensor, pending in self.pending_cal.items():
                sensor_cfg = config["sensors"].get(sensor, {})
                sensor_cfg["model"] = pending["model"]
                sensor_cfg.update(pending["params"])
                sensor_cfg["last_calibrated"] = str(datetime.now().date())
                config["sensors"][sensor] = sensor_cfg

                self.log(f"[Calibration] Committed {sensor}: "
                        f"{pending['model']}, R²={pending['r_squared']:.4f}")

            with open(CONFIG_FILE, 'w') as f:
                yaml.dump(config, f, default_flow_style=False, sort_keys=False)

            self.cal = CalibrationManager(CONFIG_FILE)

            self.pending_cal = {}
            self.btn_commit.configure(state=tk.DISABLED)
            self.commit_label.configure(text="Committed!", foreground="green")
            self.log(f"Calibration config saved to {CONFIG_FILE}")

        except Exception as e:
            self.log(f"Error committing calibration: {e}")

    def _do_validate(self):
        sensor = self.cal_sensor_var.get()

        if not self.cal:
            self.log("Error: No calibration config loaded.")
            return

        raw, reference = self._get_ref_data()
        if len(raw) < 2:
            self.log("Error: Need at least 2 data points. Fill in the table.")
            return

        try:
            self.log(f"[Calibration] Validating '{sensor}'")
            max_pct_error = 0
            for r, ref in zip(raw, reference):
                calibrated = self.cal.apply(sensor, r)
                if calibrated is None:
                    self.log(f"  Error: no calibration for {sensor}")
                    return
                error = abs(calibrated - ref)
                pct_error = (error / abs(ref)) * 100 if abs(ref) > 0.01 else 0
                max_pct_error = max(max_pct_error, pct_error)
                self.log(f"  raw={r:.4f}  ref={ref:.4f}  cal={calibrated:.4f}"
                        f"  err={error:.4f}  ({pct_error:.2f}%)")

            self.log(f"  Max % error: {max_pct_error:.2f}%")
            if max_pct_error < 5:
                self.log("  PASS (< 5%)")
            else:
                self.log("  FAIL (>= 5%) — consider recalibrating")

        except Exception as e:
            self.log(f"Error: {e}")

    def _do_summary(self):
        if not self.cal:
            self.log("No calibration config loaded.")
            return

        sensors = self.cal._get_sensors()
        if not sensors:
            self.log("No sensors in config.")
            return

        self.log("=" * 60)
        self.log("Calibration Summary")
        self.log("=" * 60)
        for name, s in sensors.items():
            status = "ENABLED" if s.get("enabled") else "DISABLED"
            model = s.get("model", "?")
            if model == "linear":
                params = f"y = {s.get('slope', '?')}x + {s.get('offset', '?')}"
            else:
                params = f"coeffs = {s.get('coefficients', '?')}"
            self.log(f"  {name:14s}  [{status}]  {model:10s}  {params}")
            self.log(f"  {'':14s}  {s.get('raw_unit', '?')} -> {s.get('calibrated_unit', '?')}"
                    f"  range: {s.get('valid_range', '?')}"
                    f"  last: {s.get('last_calibrated', '?')}")
            self.log("")

    # ==========================================================================
    # TAB 4: SETTINGS
    # ==========================================================================

    def _build_settings_tab(self):
        self.settings_vars = {}
        ttk.Label(self.tab_settings, text="Database:").grid(
            row=0, column=0, padx=10, pady=5, sticky=tk.W)
        backend = self.settings.get("database_backend", DEFAULT_DATABASE_BACKEND).lower()
        if backend not in ("influx", "mariadb"):
            backend = DEFAULT_DATABASE_BACKEND
        self.database_backend_var = tk.StringVar(
            value="MariaDB" if backend == "mariadb" else "InfluxDB")
        backend_menu = ttk.Combobox(
            self.tab_settings, textvariable=self.database_backend_var,
            values=("MariaDB", "InfluxDB"), state="readonly", width=57)
        backend_menu.grid(row=0, column=1, padx=10, pady=5, sticky=tk.W)
        backend_menu.bind("<<ComboboxSelected>>", self._update_database_settings)

        self.database_settings_frame = ttk.Frame(self.tab_settings)
        self.database_settings_frame.grid(row=1, column=0, columnspan=2, sticky=tk.W)
        self._update_database_settings()

        common_row = 2
        ttk.Label(self.tab_settings, text="CSV Backup Dir:").grid(
            row=common_row, column=0, padx=10, pady=5, sticky=tk.W)
        csv_var = tk.StringVar(value=self.settings.get("csv_backup_dir", ""))
        ttk.Entry(self.tab_settings, textvariable=csv_var, width=60).grid(
            row=common_row, column=1, padx=10, pady=5, sticky=tk.W)
        self.settings_vars["csv_backup_dir"] = csv_var

        btn_frame = ttk.Frame(self.tab_settings)
        btn_frame.grid(row=common_row + 1, column=0, columnspan=2, pady=10)
        ttk.Button(btn_frame, text="Save Settings", command=self._save_settings).pack(side=tk.LEFT, padx=5)
        ttk.Button(btn_frame, text="Test Connection", command=self._test_database).pack(side=tk.LEFT, padx=5)

    def _update_database_settings(self, event=None):
        for child in self.database_settings_frame.winfo_children():
            child.destroy()

        if self.database_backend_var.get().lower() == "mariadb":
            fields = [
                ("MariaDB Host:", "mariadb_host"),
                ("MariaDB Port:", "mariadb_port"),
                ("MariaDB User:", "mariadb_user"),
                ("MariaDB Password:", "mariadb_password"),
                ("MariaDB Database:", "mariadb_database"),
            ]
        else:
            fields = [
                ("InfluxDB URL:", "influxdb_url"),
                ("InfluxDB Token:", "influxdb_token"),
                ("InfluxDB Org:", "influxdb_org"),
                ("InfluxDB Bucket:", "influxdb_bucket"),
            ]

        for row, (label, key) in enumerate(fields):
            ttk.Label(self.database_settings_frame, text=label).grid(
                row=row, column=0, padx=10, pady=5, sticky=tk.W)
            var = tk.StringVar(value=str(self.settings.get(key, "")))
            entry = ttk.Entry(self.database_settings_frame, textvariable=var, width=60)
            if key in ("influxdb_token", "mariadb_password"):
                entry.configure(show="*")
            entry.grid(row=row, column=1, padx=10, pady=5, sticky=tk.W)
            self.settings_vars[key] = var

    def _save_settings(self):
        for key, var in self.settings_vars.items():
            self.settings[key] = var.get()
        self.settings["database_backend"] = self.database_backend_var.get()
        save_sensor_settings(self.settings)
        self.log("Settings saved.")
        messagebox.showinfo("Saved", "Settings saved successfully.")

    def _test_influx(self):
        try:
            url = self.settings_vars["influxdb_url"].get()
            token = self.settings_vars["influxdb_token"].get()
            org = self.settings_vars["influxdb_org"].get()
            bucket = self.settings_vars.get("influxdb_bucket",
                     tk.StringVar(value="sensor_data")).get()

            client = InfluxDBClient(url=url, token=token, org=org)
            write_api = client.write_api(write_options=SYNCHRONOUS)

            test_point = (
                Point("connection_test")
                .tag("source", "gui_test")
                .field("test", 1)
            )
            write_api.write(bucket=bucket, record=test_point)
            client.close()

            self.log("InfluxDB connection OK — test write successful")
            messagebox.showinfo("Success", "Connected to InfluxDB successfully!")

        except Exception as e:
            self.log(f"InfluxDB connection failed: {e}")
            messagebox.showerror("Error", f"Connection failed:\n{e}")

    def _test_database(self):
        if self.database_backend_var.get().lower() == "mariadb":
            self._test_mariadb()
        else:
            self._test_influx()

    def _test_mariadb(self):
        try:
            settings = {key: var.get() for key, var in self.settings_vars.items()}
            writer = MariaDBWriter(settings)
            writer.close()
            self.log("MariaDB connection OK")
            messagebox.showinfo("Success", "Connected to MariaDB successfully!")
        except Exception as e:
            self.log(f"MariaDB connection failed: {e}")
            messagebox.showerror("Error", f"MariaDB connection failed:\n{e}")

    # ==========================================================================
    # AUTO-SCAN HARDWARE
    # ==========================================================================

    def _scan_hardware(self):
        """Scan I2C bus for new boards (runs probe on a background thread)."""
        self.log("Scanning I2C bus for ADS1115 boards...")
        self.btn_scan.configure(state=tk.DISABLED, text="Scanning...")

        def worker():
            try:
                new_names = scan_for_new_boards(self.channels, self.mux_address)
                error = None
            except Exception as e:
                new_names = []
                error = str(e)
            # Hand result back to main thread
            self.root.after(0, self._scan_hardware_done, new_names, error)

        threading.Thread(target=worker, daemon=True).start()

    def _scan_hardware_done(self, new_names, error):
        """Main-thread callback after the background scan finishes."""
        self.btn_scan.configure(state=tk.NORMAL, text="Scan Hardware")

        if error:
            self.log(f"Scan error: {error}")
            messagebox.showerror("Scan Error", f"Hardware scan failed:\n{error}")
            return

        if not new_names:
            self.log("Scan complete — no new boards found.")
            messagebox.showinfo("Scan Complete",
                                "No new ADS1115 boards detected.\n\n"
                                "Either everything is already in the config, or no new\n"
                                "hardware is connected to the I2C bus.")
            return

        # Group by board for the dialog
        boards = {}
        for name in new_names:
            board_id = name.rsplit("_A", 1)[0]
            boards.setdefault(board_id, []).append(name)

        msg = f"Found {len(boards)} new board(s):\n\n"
        for board_id, names in sorted(boards.items()):
            msg += f"  • {board_id} ({len(names)} channels)\n"
        msg += f"\nAdd {len(new_names)} disabled placeholder entries to calibration_config.yaml?"

        if not messagebox.askyesno("New Hardware Detected", msg):
            self.log("Scan: user declined to add placeholders.")
            return

        added = add_placeholder_sensors(new_names)
        self.log(f"Added {added} placeholder entries to calibration_config.yaml")

        # Reload everything
        self.channels, self.mux_address = load_channels()
        self.cal = CalibrationManager(CONFIG_FILE)
        self._refresh_live_tree()
        self._build_config_tab()
        if hasattr(self, "cal_sensor_combo"):
            self.cal_sensor_combo.configure(values=sorted(self.channels.keys()))

        messagebox.showinfo("Scan Complete",
                            f"Added {added} new entries.\n\n"
                            "Go to the 'Channel Config' tab to enable, label,\n"
                            "and set thresholds for the new channels.")

    # ==========================================================================
    # DATA COLLECTION
    # ==========================================================================

    def _open_debug_console(self):
        if self.debug_window is not None and self.debug_window.winfo_exists():
            self.debug_window.lift()
            return

        window = tk.Toplevel(self.root)
        self.debug_window = window
        window.title("Sensor Monitor Debug Console")
        window.geometry("920x620")
        window.transient(self.root)

        toolbar = ttk.Frame(window, padding=8)
        toolbar.pack(fill=tk.X)
        self.debug_run_button = ttk.Button(
            toolbar, text="Run Diagnostics", command=self._run_debug_diagnostics,
            state=tk.DISABLED if self.debug_running else tk.NORMAL)
        self.debug_run_button.pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(toolbar, text="Close", command=window.destroy).pack(side=tk.RIGHT)

        self.debug_text = scrolledtext.ScrolledText(
            window, state=tk.DISABLED, font=("Courier", 9), wrap=tk.WORD)
        self.debug_text.pack(fill=tk.BOTH, expand=True, padx=8, pady=(0, 8))
        self._run_debug_diagnostics()

    def _append_debug_output(self, window, lines):
        if self.debug_window is not window or not window.winfo_exists():
            return
        self.debug_text.configure(state=tk.NORMAL)
        self.debug_text.insert(tk.END, "\n".join(lines) + "\n")
        self.debug_text.see(tk.END)
        self.debug_text.configure(state=tk.DISABLED)

    def _run_debug_diagnostics(self):
        if self.debug_running:
            return
        if self.debug_window is None or not self.debug_window.winfo_exists():
            return

        self.debug_running = True
        window = self.debug_window
        self.debug_text.configure(state=tk.NORMAL)
        self.debug_text.delete("1.0", tk.END)
        self.debug_text.insert(tk.END, "Running diagnostics...\n")
        self.debug_text.configure(state=tk.DISABLED)
        self.debug_run_button.configure(state=tk.DISABLED, text="Running...")
        if not self.running:
            self.btn_start.configure(state=tk.DISABLED)

        channels = {name: dict(cfg) for name, cfg in self.channels.items()}
        mux_address = self.mux_address
        hardware_busy = self.running
        settings = dict(self.settings)

        def worker():
            try:
                report = run_hardware_diagnostics(
                    channels, mux_address, hardware_busy=hardware_busy)
                report.append("")
                if settings.get(
                        "database_backend", DEFAULT_DATABASE_BACKEND).lower() == "mariadb":
                    report.extend(run_mariadb_diagnostics(settings))
                else:
                    report.append(
                        "MariaDB diagnostics skipped: InfluxDB is the selected backend."
                    )
            except Exception as exc:
                report = [
                    f"Diagnostics failed unexpectedly: {type(exc).__name__}: {exc}",
                    traceback.format_exc(),
                ]
            try:
                self.root.after(0, self._debug_diagnostics_done, window, report)
            except tk.TclError:
                pass

        threading.Thread(target=worker, daemon=True).start()

    def _debug_diagnostics_done(self, window, report):
        self.debug_running = False
        if self.debug_window is not None and self.debug_window.winfo_exists():
            self.debug_run_button.configure(state=tk.NORMAL, text="Run Diagnostics")
        if not self.running:
            self.btn_start.configure(state=tk.NORMAL)
        self._append_debug_output(window, report)

    def _init_adc_safe(self):
        try:
            self.adcs, self.mux = init_adcs(self.channels, mux_address=self.mux_address)
            if self.adcs:
                direct = sum(1 for k in self.adcs if k[0] is None)
                muxed = sum(1 for k in self.adcs if k[0] is not None)
                msg = f"ADC initialised: {direct} direct board(s)"
                if muxed:
                    msg += f", {muxed} multiplexed board(s)"
                self.log(msg)
            else:
                self.log("No ADC found — check wiring or run on Raspberry Pi")
        except Exception as e:
            self.log(f"ADC init error: {e}")

    def start_collection(self):
        if self.running or self.debug_running:
            return

        backend = self.settings.get("database_backend", DEFAULT_DATABASE_BACKEND).lower()
        self.influx_client = None
        self.write_api = None
        self.mariadb_writer = None
        if backend == "mariadb":
            try:
                self.mariadb_writer = MariaDBWriter(self.settings)
                self.log("Connected to MariaDB")
            except Exception as e:
                self.log(f"MariaDB connection failed: {e}")
        else:
            try:
                url = self.settings.get("influxdb_url", "")
                token = self.settings.get("influxdb_token", "")
                org = self.settings.get("influxdb_org", "")

                if token and token != "YOUR_TOKEN_HERE":
                    self.influx_client = InfluxDBClient(url=url, token=token, org=org)
                    self.write_api = self.influx_client.write_api(write_options=SYNCHRONOUS)
                    self.log("Connected to InfluxDB Cloud")
                else:
                    self.log("No InfluxDB token — saving to CSV only")
            except Exception as e:
                self.log(f"InfluxDB connection failed: {e} — saving to CSV only")

        if os.path.exists(CONFIG_FILE):
            self.cal = CalibrationManager(CONFIG_FILE)

        self.running = True
        self.btn_start.configure(state=tk.DISABLED)
        self.btn_stop.configure(state=tk.NORMAL)
        self.status_label.configure(text="● RUNNING", foreground="green")
        self.log("Data collection started")

        self.thread = threading.Thread(target=self._collection_loop, daemon=True)
        self.thread.start()

    def stop_collection(self):
        self.running = False
        self.btn_start.configure(state=tk.NORMAL)
        self.btn_stop.configure(state=tk.DISABLED)
        self.status_label.configure(text="● STOPPED", foreground="red")

        if self.influx_client:
            try:
                self.influx_client.close()
            except Exception:
                pass
            self.influx_client = None
            self.write_api = None

        if self.mariadb_writer:
            try:
                self.mariadb_writer.close()
            except Exception:
                pass
            self.mariadb_writer = None

        self.log("Data collection stopped")

    def _collection_loop(self):
        while self.running:
            now = datetime.now(timezone.utc)
            timestamp_ns = int(time.time() * 1e9)
            points = []
            mariadb_readings = []
            enabled_count = sum(
                1 for config in self.channels.values()
                if config.get("enabled", False)
            )
            backend = self.settings.get(
                "database_backend", DEFAULT_DATABASE_BACKEND
            ).lower()
            stream_status = ""
            csv_dir = self.settings.get("csv_backup_dir", os.path.expanduser("~/sensor_logs"))
            bucket = self.settings.get("influxdb_bucket", "sensor_data")

            for sensor_name, sensor_config in self.channels.items():
                if not sensor_config.get("enabled", False):
                    continue

                raw_value = read_adc(self.adcs, sensor_config)
                if raw_value is None:
                    continue

                if self.cal:
                    cal_fields = self.cal.get_fields(sensor_name, raw_value)
                else:
                    cal_fields = {sensor_config.get("field", "voltage"): raw_value}

                # Pull calibrated value out for status check
                cal_val = None
                for key in ("calibrated_voltage", "power", "calibrated",
                           "temperature_calibrated", "humidity_calibrated", "power_calibrated"):
                    if key in cal_fields and isinstance(cal_fields[key], (int, float)):
                        cal_val = cal_fields[key]
                        break

                status, reason = compute_status(raw_value, cal_val, sensor_config)

                point = (
                    Point(sensor_config.get("measurement", "sensor_reading"))
                    .tag("sensor", sensor_name)
                    .tag("display_name", sensor_config.get("label", sensor_name))
                    .tag("location", sensor_config.get("location", "lab"))
                    .tag("unit", sensor_config.get("unit", "V"))
                    .tag("status", status)
                )
                if reason:
                    point = point.tag("alert_reason", reason)
                for fname, fval in cal_fields.items():
                    if isinstance(fval, (int, float)):
                        point = point.field(fname, round(fval, 4))
                    elif fname == "calibration_status":
                        point = point.tag("cal_status", str(fval))
                point = point.time(timestamp_ns, WritePrecision.NS)
                points.append(point)
                mariadb_readings.append(
                    (now, sensor_name, sensor_config, cal_fields, status, reason)
                )

                try:
                    save_to_csv(csv_dir, sensor_name, cal_fields, now)
                except Exception as e:
                    self.root.after(0, self.log, f"CSV error: {e}")

                # Update live display
                raw_display = f"{raw_value:.4f}"
                label = sensor_config.get("label", sensor_name)
                cal_display = f"{cal_val:.4f}" if isinstance(cal_val, (int, float)) else "---"
                status_display = "OK" if status == "OK" else f"ALERT: {reason}"
                tag = "ok" if status == "OK" else "alert"

                self.root.after(0, self._update_tree_row, sensor_name,
                              label, raw_display, cal_display, status_display, tag)

            if points and self.write_api:
                try:
                    self.write_api.write(bucket=bucket, record=points)
                    stream_status = f"InfluxDB wrote {len(points)} points"
                    self.root.after(0, self.log,
                                  f"[{now.strftime('%H:%M:%S')}] Wrote {len(points)} points to cloud + CSV")
                except Exception as e:
                    stream_status = f"InfluxDB write failed: {e}"
                    self.root.after(0, self.log,
                                  f"[{now.strftime('%H:%M:%S')}] Cloud write failed (CSV saved): {e}")
            elif points:
                if backend == "mariadb":
                    stream_status = "MariaDB unavailable; readings saved to CSV only"
                else:
                    stream_status = "InfluxDB unavailable; readings saved to CSV only"
                self.root.after(0, self.log,
                              f"[{now.strftime('%H:%M:%S')}] Wrote {len(points)} points to CSV only")

            if mariadb_readings and self.mariadb_writer:
                try:
                    self.mariadb_writer.write_readings(mariadb_readings)
                    stream_status = f"MariaDB wrote {len(mariadb_readings)} readings"
                    self.root.after(0, self.log,
                                  f"[{now.strftime('%H:%M:%S')}] Wrote {len(mariadb_readings)} readings to MariaDB")
                except Exception as e:
                    stream_status = f"MariaDB write failed: {e}"
                    self.root.after(0, self.log,
                                  f"[{now.strftime('%H:%M:%S')}] MariaDB write failed: {e}")
            elif backend == "mariadb" and self.mariadb_writer:
                stream_status = "MariaDB connected; no sensor readings acquired"
            elif backend == "mariadb" and not self.mariadb_writer and not stream_status:
                stream_status = "MariaDB unavailable; no database write attempted"

            if self.debug_window is not None:
                try:
                    self.root.after(
                        0,
                        self._debug_collection_status,
                        now,
                        len(mariadb_readings),
                        enabled_count,
                        stream_status or "No database write attempted",
                    )
                except tk.TclError:
                    pass

            interval = self.interval_var.get() if self.interval_var.get() > 0 else 2.0
            time.sleep(interval)

    def _debug_collection_status(
            self, timestamp, acquired_count, enabled_count, stream_status):
        window = self.debug_window
        if window is None or not window.winfo_exists():
            return
        self._append_debug_output(window, [
            f"[{timestamp.astimezone().strftime('%H:%M:%S')}] "
            f"Data stream: acquired {acquired_count}/{enabled_count} enabled sensors; "
            f"{stream_status}"
        ])

    def _update_tree_row(self, sensor_name, label, raw, calibrated, status, tag="ok"):
        try:
            self.tree.item(sensor_name,
                           values=(sensor_name, label, raw, calibrated, status),
                           tags=(tag,))
        except Exception:
            pass

    # ==========================================================================
    # LOGGING
    # ==========================================================================

    def log(self, message):
        timestamp = datetime.now().strftime("%H:%M:%S")
        self.log_text.configure(state=tk.NORMAL)
        self.log_text.insert(tk.END, f"[{timestamp}] {message}\n")
        self.log_text.see(tk.END)
        self.log_text.configure(state=tk.DISABLED)


# ==============================================================================
# ENTRY POINT
# ==============================================================================

def main():
    root = tk.Tk()
    style = ttk.Style()
    style.theme_use("clam")
    app = SensorMonitorApp(root)

    def on_close():
        app.running = False
        time.sleep(0.5)
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", on_close)
    root.mainloop()


if __name__ == "__main__":
    main()
