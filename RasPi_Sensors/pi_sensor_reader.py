"""
================================================================================
RASPBERRY PI SENSOR DATA READER (multi-board + mux + status thresholds)
================================================================================

Reads analog sensors via one or more ADS1115 ADCs over I2C, optionally routed
through a TCA9548A I2C multiplexer. Applies calibration and writes to
InfluxDB Cloud + local CSV backup.

NAMING (all three formats accepted in calibration_config.yaml):
    A0..A3                 legacy single-board (treated as ADC0)
    ADCn_Am                direct multi-board   (n = 0..3)
    MUXn_ADCm_Ak           via TCA9548A mux     (n = 0..7)

STATUS THRESHOLDS (per sensor in YAML, all optional):
    raw_min, raw_max       bounds on raw scaled voltage (V, after ×3)
    cal_min, cal_max       bounds on calibrated value
    Sensor is OK if all defined bounds are satisfied. Otherwise ALERT.

HOW TO RUN:
    cd ~/sensor_project
    python3 pi_sensor_reader.py

DEPENDENCIES:
    pip install adafruit-circuitpython-ads1x15==2.* adafruit-circuitpython-tca9548a influxdb-client mariadb pyyaml --break-system-packages

MariaDB is optional. Enable it for the headless reader with MARIADB_ENABLED=true
and set MARIADB_HOST, MARIADB_PORT, MARIADB_USER, MARIADB_PASSWORD, and
MARIADB_DATABASE in the service environment. The target database must exist.

================================================================================
"""

import os
import sys
import time
import csv
import re
import signal
import yaml
from datetime import datetime, timezone

from influxdb_client import InfluxDBClient, Point, WritePrecision
from influxdb_client.client.write_api import SYNCHRONOUS
from RasPi_Sensors.mariadb_writer import MariaDBWriter

try:
    import board
    import busio
    import adafruit_ads1x15.ads1115 as ADS
    from adafruit_ads1x15.analog_in import AnalogIn
    HAS_ADC = True
except ImportError:
    HAS_ADC = False
    print("Warning: ADC libraries not found.")

try:
    import adafruit_tca9548a
    HAS_MUX = True
except ImportError:
    HAS_MUX = False

from RasPi_Sensors.calibration import CalibrationManager


# ==============================================================================
# CONFIGURATION
# ==============================================================================

INFLUXDB_URL    = "https://us-east-1-1.aws.cloud2.influxdata.com"
INFLUXDB_TOKEN  = "YOUR_TOKEN_HERE"
INFLUXDB_ORG    = "University of Oxford"
INFLUXDB_BUCKET = "sensor_data"

CALIBRATION_CONFIG = os.path.join(os.path.dirname(__file__), "calibration_config.yaml")
CSV_BACKUP_DIR     = os.path.expanduser("~/sensor_logs")
SAMPLE_INTERVAL    = 2.0
MARIADB_ENABLED    = os.getenv("MARIADB_ENABLED", "false").lower() in ("1", "true", "yes")
MARIADB_SETTINGS   = {
    "mariadb_host": os.getenv("MARIADB_HOST", "127.0.0.1"),
    "mariadb_port": os.getenv("MARIADB_PORT", "3306"),
    "mariadb_user": os.getenv("MARIADB_USER", ""),
    "mariadb_password": os.getenv("MARIADB_PASSWORD", ""),
    "mariadb_database": os.getenv("MARIADB_DATABASE", "sensor_data"),
}

ADC_ADDRESSES = {0: 0x48, 1: 0x49, 2: 0x4A, 3: 0x4B}
DEFAULT_MUX_ADDRESS = 0x70


# ==============================================================================
# SENSOR NAME PARSING
# ==============================================================================

MUX_NAME_RE = re.compile(r"^MUX(\d+)_ADC(\d+)_A([0-3])$")
ADC_NAME_RE = re.compile(r"^ADC(\d+)_A([0-3])$")
LEGACY_NAME_RE = re.compile(r"^A([0-3])$")


def parse_sensor_name(name):
    """Parse a sensor name into a hardware address dict, or None if invalid."""
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
    """Hashable key identifying which physical ADC to use."""
    return (sensor_config.get("mux_channel"), sensor_config["adc_index"])


# ==============================================================================
# STATUS LOGIC
# ==============================================================================

def compute_status(raw_value, cal_value, sensor_config):
    """Determine OK/ALERT based on configured thresholds.

    Returns ("OK", "") if all defined bounds satisfied (or none defined).
    Returns ("ALERT", reason) if any defined bound is violated.
    """
    raw_min = sensor_config.get("raw_min")
    raw_max = sensor_config.get("raw_max")
    cal_min = sensor_config.get("cal_min")
    cal_max = sensor_config.get("cal_max")

    reasons = []
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
# SENSOR CHANNELS
# ==============================================================================

def load_channels_from_config():
    """Load enabled sensor channels from calibration_config.yaml.

    Returns (channels_dict, mux_address).
    """
    if not os.path.exists(CALIBRATION_CONFIG):
        print(f"Warning: {CALIBRATION_CONFIG} not found")
        return {}, DEFAULT_MUX_ADDRESS

    with open(CALIBRATION_CONFIG, 'r') as f:
        config = yaml.safe_load(f) or {}

    mux_address = config.get("multiplexer_address", DEFAULT_MUX_ADDRESS)
    sensors = config.get("sensors") or {}
    channels = {}

    for name, cfg in sensors.items():
        parsed = parse_sensor_name(name)
        if parsed is None:
            print(f"  Warning: skipping '{name}' — must be A0-A3, ADCn_Am, or MUXn_ADCm_Ak")
            continue

        if not cfg.get("enabled", False):
            continue

        channels[name] = {
            "enabled": True,
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

# AZDelivery off-by-one mapping (label A0 → ADS.P1, etc.)
PIN_MAP = {0: 1, 1: 2, 2: 3, 3: 0}

ADS_PINS = None
if HAS_ADC:
    ADS_PINS = {0: ADS.P0, 1: ADS.P1, 2: ADS.P2, 3: ADS.P3}


def init_adcs(sensor_channels, mux_address=DEFAULT_MUX_ADDRESS):
    """Initialise direct + multiplexed ADC boards needed by enabled sensors.

    Returns (adcs_dict, mux_object). adcs_dict keyed by (mux_channel, adc_index).
    """
    if not HAS_ADC:
        return {}, None

    i2c = busio.I2C(board.SCL, board.SDA)
    adcs = {}
    mux = None

    needs_mux = any(c.get("mux_channel") is not None for c in sensor_channels.values())
    direct_indices = sorted({
        c["adc_index"] for c in sensor_channels.values() if c.get("mux_channel") is None
    })
    mux_pairs = sorted({
        (c["mux_channel"], c["adc_index"]) for c in sensor_channels.values()
        if c.get("mux_channel") is not None
    })

    # Direct boards
    for adc_index in direct_indices:
        if adc_index not in ADC_ADDRESSES:
            continue
        addr = ADC_ADDRESSES[adc_index]
        try:
            ads = ADS.ADS1115(i2c, address=addr)
            chans = {ch: AnalogIn(ads, ADS_PINS[pin]) for ch, pin in PIN_MAP.items()}
            adcs[(None, adc_index)] = (ads, chans)
            print(f"  Direct ADC{adc_index} (0x{addr:02X}): OK")
        except Exception as e:
            print(f"  Direct ADC{adc_index} (0x{addr:02X}): NOT FOUND ({e})")

    # Multiplexed boards
    if needs_mux:
        if not HAS_MUX:
            print("  ERROR: TCA9548A library not installed.")
            print("  pip install adafruit-circuitpython-tca9548a --break-system-packages")
            return adcs, None

        try:
            mux = adafruit_tca9548a.TCA9548A(i2c, address=mux_address)
            print(f"  Multiplexer (0x{mux_address:02X}): OK")
        except Exception as e:
            print(f"  Multiplexer (0x{mux_address:02X}): NOT FOUND ({e})")
            return adcs, None

        for mux_ch, adc_index in mux_pairs:
            if mux_ch < 0 or mux_ch > 7 or adc_index not in ADC_ADDRESSES:
                continue
            addr = ADC_ADDRESSES[adc_index]
            try:
                ads = ADS.ADS1115(mux[mux_ch], address=addr)
                chans = {ch: AnalogIn(ads, ADS_PINS[pin]) for ch, pin in PIN_MAP.items()}
                adcs[(mux_ch, adc_index)] = (ads, chans)
                print(f"  MUX{mux_ch}_ADC{adc_index} (0x{addr:02X}): OK")
            except Exception as e:
                print(f"  MUX{mux_ch}_ADC{adc_index} (0x{addr:02X}): NOT FOUND ({e})")

    return adcs, mux


def read_adc(adcs, sensor_name, sensor_config):
    """Read voltage from a specific ADC channel, applying ×3 divider scaling."""
    key = hardware_key(sensor_config)
    channel = sensor_config["channel"]

    if key not in adcs:
        return None

    ads, channels = adcs[key]
    ads.gain = sensor_config.get("gain", 1)

    try:
        raw_adc_voltage = channels[channel].voltage
        return raw_adc_voltage
    except Exception as e:
        print(f"  Warning: error reading {sensor_name}: {e}")
        return None


# ==============================================================================
# DATA OUTPUT
# ==============================================================================

def extract_calibrated_value(cal_fields):
    """Pull the calibrated numeric value from a CalibrationManager fields dict."""
    for key in ("calibrated_voltage", "power", "calibrated",
                "temperature_calibrated", "humidity_calibrated", "power_calibrated"):
        if key in cal_fields and isinstance(cal_fields[key], (int, float)):
            return cal_fields[key]
    return None


def build_point(sensor_name, sensor_config, cal_fields, raw_value, timestamp_ns):
    """Build an InfluxDB Point with raw + calibrated values + status tag."""
    cal_value = extract_calibrated_value(cal_fields)
    status, reason = compute_status(raw_value, cal_value, sensor_config)

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
    return point


def save_to_csv(sensor_name, cal_fields, now):
    """Append to monthly CSV file (e.g. ADC0_A0_2026-04.csv)."""
    os.makedirs(CSV_BACKUP_DIR, exist_ok=True)
    filename = os.path.join(CSV_BACKUP_DIR, f"{sensor_name}_{now.strftime('%Y-%m')}.csv")
    file_exists = os.path.exists(filename)

    with open(filename, 'a', newline='') as f:
        writer = csv.writer(f)
        if not file_exists:
            writer.writerow(["timestamp"] + list(cal_fields.keys()))
        writer.writerow([now.isoformat()] + list(cal_fields.values()))


# ==============================================================================
# MAIN
# ==============================================================================

def main():
    print("=" * 60)
    print("SENSOR READER")
    print("=" * 60)
    print(f"InfluxDB Cloud:  {INFLUXDB_URL}")
    print(f"Bucket:          {INFLUXDB_BUCKET}")
    print(f"CSV backup:      {CSV_BACKUP_DIR}")
    print(f"MariaDB:         {'enabled' if MARIADB_ENABLED else 'disabled'}")
    print(f"Sample interval: {SAMPLE_INTERVAL}s")

    sensor_channels, mux_address = load_channels_from_config()
    if not sensor_channels:
        print("\nNo enabled sensors found in calibration_config.yaml")
        sys.exit(1)

    print(f"Sensors:         {len(sensor_channels)}")
    for name, cfg in sensor_channels.items():
        print(f"  {name}: {cfg['label']}")
    print("=" * 60)

    cal = None
    if os.path.exists(CALIBRATION_CONFIG):
        cal = CalibrationManager(CALIBRATION_CONFIG)
        print(f"Calibration loaded from {CALIBRATION_CONFIG}")

    print("\nInitialising hardware...")
    adcs, mux = init_adcs(sensor_channels, mux_address=mux_address)
    if not adcs:
        print("ERROR: No ADC boards initialised. Check wiring and I2C is enabled.")
        sys.exit(1)

    missing = [name for name, cfg in sensor_channels.items() if hardware_key(cfg) not in adcs]
    if missing:
        print(f"\n  WARNING: hardware missing for: {', '.join(missing)}")

    print("\nConnecting to InfluxDB Cloud...")
    client = InfluxDBClient(url=INFLUXDB_URL, token=INFLUXDB_TOKEN, org=INFLUXDB_ORG)
    write_api = client.write_api(write_options=SYNCHRONOUS)
    print("Connected.\n")

    mariadb_writer = None
    if MARIADB_ENABLED:
        try:
            mariadb_writer = MariaDBWriter(MARIADB_SETTINGS)
            print("Connected to MariaDB.\n")
        except Exception as e:
            print(f"MariaDB connection failed: {e}")

    running = True
    def handle_signal(sig, frame):
        nonlocal running
        print("\nShutting down...")
        running = False
    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    print("Starting data collection. Press Ctrl+C to stop.\n")

    while running:
        now = datetime.now(timezone.utc)
        timestamp_ns = time.time_ns()
        points = []
        mariadb_readings = []
        line_parts = []

        for sensor_name, sensor_config in sensor_channels.items():
            voltage = read_adc(adcs, sensor_name, sensor_config)
            if voltage is None:
                continue

            if cal:
                cal_fields = cal.get_fields(sensor_name, voltage)
            else:
                cal_fields = {sensor_config["field"]: voltage}

            point = build_point(sensor_name, sensor_config, cal_fields, voltage, timestamp_ns)
            points.append(point)

            cal_value = extract_calibrated_value(cal_fields)
            status, reason = compute_status(voltage, cal_value, sensor_config)
            mariadb_readings.append(
                (now, sensor_name, sensor_config, cal_fields, status, reason)
            )

            save_to_csv(sensor_name, cal_fields, now)

            label = sensor_config.get("label", sensor_name)
            tag = "" if status == "OK" else " [ALERT]"
            line_parts.append(f"{sensor_name}({label}): {voltage:.4f}V{tag}")

        if points:
            try:
                write_api.write(bucket=INFLUXDB_BUCKET, record=points)
            except Exception as e:
                print(f"  InfluxDB write error: {e}")

        if mariadb_writer and mariadb_readings:
            try:
                mariadb_writer.write_readings(mariadb_readings)
            except Exception as e:
                print(f"  MariaDB write error: {e}")

        if line_parts:
            print(f"  [{now.strftime('%H:%M:%S')}] {' | '.join(line_parts)}")

        time.sleep(SAMPLE_INTERVAL)

    client.close()
    if mariadb_writer:
        mariadb_writer.close()
    print("Stopped.")


if __name__ == "__main__":
    main()
