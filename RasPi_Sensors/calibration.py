"""
================================================================================
SENSOR CALIBRATION MODULE
================================================================================

USAGE AS A MODULE (inside pi_sensor_reader.py):
    from calibration import CalibrationManager
    cal = CalibrationManager("calibration_config.yaml")
    fields = cal.get_fields("A0", raw_voltage=2.5)

COMMAND LINE USAGE:
    python3 calibration.py --apply --sensor A0 --value 2.5
    python3 calibration.py --fit --sensor A0 --refs reference_data.csv
    python3 calibration.py --fit --sensor A0 --refs refs.csv --model polynomial
    python3 calibration.py --validate --sensor A0 --refs reference_data.csv
    python3 calibration.py --generate-refs --sensor A0
    python3 calibration.py --summary

REFERENCE DATA CSV FORMAT:
    raw,reference
    0.50,1.163
    1.00,2.338
    1.50,3.513

================================================================================
"""

import argparse
import csv
import os
import sys
from datetime import date

import numpy as np
import yaml


class CalibrationManager:

    def __init__(self, config_path):
        self.config_path = config_path
        self.config = self._load_config()

    def _load_config(self):
        if not os.path.exists(self.config_path):
            print(f"Warning: calibration config not found at {self.config_path}")
            return {"sensors": {}}
        with open(self.config_path, 'r') as f:
            config = yaml.safe_load(f)
        # Guard against None values
        if config is None:
            config = {"sensors": {}}
        if config.get("sensors") is None:
            config["sensors"] = {}
        return config

    def _save_config(self):
        with open(self.config_path, 'w') as f:
            yaml.dump(self.config, f, default_flow_style=False, sort_keys=False)

    def _get_sensors(self):
        """Safely get sensors dict, never returns None."""
        return self.config.get("sensors") or {}

    def apply(self, sensor_name, raw_value):
        """Apply calibration to a raw reading. Returns calibrated value or None."""
        sensors = self._get_sensors()
        if sensor_name not in sensors:
            return None

        sensor = sensors[sensor_name]
        if not sensor.get("enabled", False):
            return None

        model = sensor.get("model", "linear")

        if model == "linear":
            slope = sensor.get("slope", 1.0)
            offset = sensor.get("offset", 0.0)
            return slope * raw_value + offset

        elif model == "polynomial":
            coeffs = sensor.get("coefficients", [0, 1])
            result = 0.0
            for i, c in enumerate(coeffs):
                result += c * (raw_value ** i)
            return result

        return None

    def validate_range(self, sensor_name, raw_value):
        """Check if a raw reading is within valid range. Returns (is_valid, status)."""
        sensors = self._get_sensors()
        if sensor_name not in sensors:
            return True, "uncalibrated"

        sensor = sensors[sensor_name]
        valid_range = sensor.get("valid_range", None)

        if valid_range is None:
            return True, "ok"

        if valid_range[0] <= raw_value <= valid_range[1]:
            return True, "ok"
        else:
            return False, "out_of_range"

    def get_fields(self, sensor_name, raw_value):
        """
        Get a dict of all fields to write to InfluxDB for this sensor.
        Includes raw value, calibrated value (if available), and status.
        """
        sensors = self._get_sensors()
        fields = {}

        if sensor_name not in sensors or not sensors[sensor_name].get("enabled", False):
            raw_field = (sensors.get(sensor_name) or {}).get("raw_field", "voltage")
            fields[raw_field] = raw_value
            fields["calibration_status"] = "uncalibrated"
            return fields

        sensor = sensors[sensor_name]
        raw_field = sensor.get("raw_field", "voltage")
        cal_field = sensor.get("calibrated_field", "calibrated")

        # Raw value
        fields[raw_field] = raw_value

        # Calibrated value
        calibrated = self.apply(sensor_name, raw_value)
        if calibrated is not None:
            fields[cal_field] = calibrated

        # Range validation
        is_valid, status = self.validate_range(sensor_name, raw_value)
        fields["calibration_status"] = status

        return fields

    def get_sensor_info(self, sensor_name):
        """Get calibration info for a sensor."""
        sensors = self._get_sensors()
        return sensors.get(sensor_name, None)


# ==============================================================================
# FITTING
# ==============================================================================

def load_reference_data(csv_path):
    """Load reference measurements from CSV."""
    raw_vals = []
    ref_vals = []
    with open(csv_path, 'r') as f:
        reader = csv.DictReader(f)
        for row in reader:
            raw_vals.append(float(row["raw"]))
            ref_vals.append(float(row["reference"]))
    return np.array(raw_vals), np.array(ref_vals)


def fit_calibration(raw, reference, model="linear"):
    """Fit calibration parameters from reference data."""
    if model == "linear":
        coeffs = np.polyfit(raw, reference, 1)
        slope, offset = coeffs[0], coeffs[1]
        predicted = slope * raw + offset
        params = {"slope": float(slope), "offset": float(offset)}

    elif model == "polynomial":
        best_r2 = -1
        best_degree = 2
        for degree in [2, 3]:
            coeffs = np.polyfit(raw, reference, degree)
            predicted = np.polyval(coeffs, raw)
            ss_res = np.sum((reference - predicted) ** 2)
            ss_tot = np.sum((reference - np.mean(reference)) ** 2)
            r2 = 1 - (ss_res / ss_tot) if ss_tot > 0 else 0
            if r2 > best_r2:
                best_r2 = r2
                best_degree = degree

        coeffs = np.polyfit(raw, reference, best_degree)
        predicted = np.polyval(coeffs, raw)
        params = {"coefficients": [float(c) for c in reversed(coeffs)]}
    else:
        raise ValueError(f"Unknown model: {model}")

    ss_res = np.sum((reference - predicted) ** 2)
    ss_tot = np.sum((reference - np.mean(reference)) ** 2)
    r_squared = 1 - (ss_res / ss_tot) if ss_tot > 0 else 0
    residuals = reference - predicted

    return params, r_squared, residuals


def generate_synthetic_refs(sensor_config, n_points=10):
    """Generate synthetic reference data for testing."""
    model = sensor_config.get("model", "linear")
    valid_range = sensor_config.get("valid_range", [0, 5])

    raw = np.linspace(valid_range[0], valid_range[1], n_points)

    if model == "linear":
        slope = sensor_config.get("slope", 1.0)
        offset = sensor_config.get("offset", 0.0)
        reference = slope * raw + offset
    elif model == "polynomial":
        coeffs = sensor_config.get("coefficients", [0, 1])
        reference = sum(c * raw**i for i, c in enumerate(coeffs))
    else:
        reference = raw

    reference += np.random.normal(0, 0.01 * np.mean(np.abs(reference)), len(reference))
    return raw, reference


# ==============================================================================
# COMMAND LINE INTERFACE
# ==============================================================================

def main():
    parser = argparse.ArgumentParser(description="Sensor Calibration Tool")
    parser.add_argument("--config", default="calibration_config.yaml")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--fit", action="store_true")
    parser.add_argument("--validate", action="store_true")
    parser.add_argument("--generate-refs", action="store_true")
    parser.add_argument("--summary", action="store_true")
    parser.add_argument("--sensor")
    parser.add_argument("--value", type=float)
    parser.add_argument("--refs")
    parser.add_argument("--model", default="linear", choices=["linear", "polynomial"])

    args = parser.parse_args()
    cal = CalibrationManager(args.config)

    if args.summary:
        print("\nCalibration Summary")
        print("=" * 70)
        sensors = cal._get_sensors()
        for name, s in sensors.items():
            status = "ENABLED" if s.get("enabled") else "DISABLED"
            model = s.get("model", "?")
            if model == "linear":
                params = f"y = {s.get('slope', '?')}x + {s.get('offset', '?')}"
            else:
                params = f"coeffs = {s.get('coefficients', '?')}"
            print(f"  {name:20s}  [{status}]  {model:12s}  {params}")
            print(f"  {'':20s}  {s.get('raw_unit', '?')} -> {s.get('calibrated_unit', '?')}"
                  f"  range: {s.get('valid_range', '?')}"
                  f"  last: {s.get('last_calibrated', '?')}")
            print()
        return

    if args.apply:
        if not args.sensor or args.value is None:
            print("Error: --apply requires --sensor and --value")
            sys.exit(1)
        result = cal.apply(args.sensor, args.value)
        fields = cal.get_fields(args.sensor, args.value)
        print(f"\nSensor: {args.sensor}")
        print(f"Raw:        {args.value}")
        print(f"Calibrated: {result}")
        print(f"Fields:     {fields}")
        return

    if args.generate_refs:
        if not args.sensor:
            print("Error: --generate-refs requires --sensor")
            sys.exit(1)
        sensor_info = cal.get_sensor_info(args.sensor)
        if not sensor_info:
            print(f"Error: sensor '{args.sensor}' not found in config")
            sys.exit(1)
        raw, reference = generate_synthetic_refs(sensor_info)
        out_file = f"reference_data_{args.sensor}.csv"
        with open(out_file, 'w', newline='') as f:
            writer = csv.writer(f)
            writer.writerow(["raw", "reference"])
            for r, ref in zip(raw, reference):
                writer.writerow([round(r, 6), round(ref, 6)])
        print(f"Generated {len(raw)} reference points -> {out_file}")
        return

    if args.fit:
        if not args.sensor or not args.refs:
            print("Error: --fit requires --sensor and --refs")
            sys.exit(1)
        raw, reference = load_reference_data(args.refs)
        params, r_squared, residuals = fit_calibration(raw, reference, args.model)

        print(f"\nFitting '{args.sensor}' with {args.model} model")
        print(f"  Data points: {len(raw)}")
        print(f"  Parameters:  {params}")
        print(f"  R-squared:   {r_squared:.6f}")
        print(f"  Max residual: {np.max(np.abs(residuals)):.6f}")
        print(f"  Mean residual: {np.mean(np.abs(residuals)):.6f}")

        sensor_config = cal._get_sensors().get(args.sensor, {})
        sensor_config["model"] = args.model
        sensor_config.update(params)
        sensor_config["last_calibrated"] = str(date.today())

        if "sensors" not in cal.config:
            cal.config["sensors"] = {}
        cal.config["sensors"][args.sensor] = sensor_config
        cal._save_config()
        print(f"\n  Config updated in {args.config}")
        return

    if args.validate:
        if not args.sensor or not args.refs:
            print("Error: --validate requires --sensor and --refs")
            sys.exit(1)
        raw, reference = load_reference_data(args.refs)

        print(f"\nValidating '{args.sensor}'")
        max_pct_error = 0
        for r, ref in zip(raw, reference):
            calibrated = cal.apply(args.sensor, r)
            if calibrated is None:
                print(f"  Error: no calibration for {args.sensor}")
                sys.exit(1)
            error = abs(calibrated - ref)
            pct_error = (error / abs(ref)) * 100 if abs(ref)>0.01  else 0
            max_pct_error = max(max_pct_error, pct_error)
            print(f"  raw={r:.4f}  ref={ref:.4f}  cal={calibrated:.4f}"
                  f"  err={error:.4f}  ({pct_error:.2f}%)")

        print(f"\n  Max % error: {max_pct_error:.2f}%")
        if max_pct_error < 5:
            print("  PASS (< 5%)")
        else:
            print("  FAIL (>= 5%) — consider recalibrating")
        return

    parser.print_help()


if __name__ == "__main__":
    main()
