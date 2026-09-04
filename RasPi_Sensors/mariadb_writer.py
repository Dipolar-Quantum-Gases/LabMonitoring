"""Optional MariaDB storage for sensor readings."""

import json

try:
    import mariadb
except ImportError:
    mariadb = None


CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS sensor_readings (
    reading_id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    recorded_at DATETIME(6) NOT NULL,
    sensor VARCHAR(128) NOT NULL,
    display_name VARCHAR(255) NOT NULL,
    location VARCHAR(255) NOT NULL,
    unit VARCHAR(32) NOT NULL,
    status VARCHAR(16) NOT NULL,
    alert_reason VARCHAR(255) NULL,
    calibration_status VARCHAR(64) NULL,
    fields_json JSON NOT NULL,
    PRIMARY KEY (reading_id),
    INDEX idx_sensor_recorded_at (sensor, recorded_at),
    INDEX idx_recorded_at (recorded_at)
)
"""


class MariaDBWriter:
    """Create and write sensor readings to a MariaDB database."""

    def __init__(self, settings):
        if mariadb is None:
            raise RuntimeError(
                "MariaDB support is not installed. "
                "Install it with: pip install mariadb"
            )

        self.connection = mariadb.connect(
            host=settings.get("mariadb_host", "127.0.0.1"),
            port=int(settings.get("mariadb_port", 3306)),
            user=settings.get("mariadb_user", ""),
            password=settings.get("mariadb_password", ""),
            database=settings.get("mariadb_database", "sensor_data"),
        )
        self.connection.autocommit = False
        self._create_table()

    def _create_table(self):
        cursor = self.connection.cursor()
        try:
            cursor.execute(CREATE_TABLE_SQL)
            self.connection.commit()
        finally:
            cursor.close()

    def write_readings(self, readings):
        """Write an iterable of (timestamp, sensor, config, fields, status, reason)."""
        rows = []
        for timestamp, sensor_name, sensor_config, fields, status, reason in readings:
            numeric_fields = {
                key: round(value, 4)
                for key, value in fields.items()
                if isinstance(value, (int, float)) and not isinstance(value, bool)
            }
            calibration_status = fields.get("calibration_status")
            rows.append((
                timestamp.replace(tzinfo=None),
                sensor_name,
                sensor_config.get("label", sensor_name),
                sensor_config.get("location", "lab"),
                sensor_config.get("unit", "V"),
                status,
                reason or None,
                str(calibration_status) if calibration_status is not None else None,
                json.dumps(numeric_fields),
            ))

        if not rows:
            return

        cursor = self.connection.cursor()
        try:
            cursor.executemany(
                """
                INSERT INTO sensor_readings
                    (recorded_at, sensor, display_name, location, unit, status,
                     alert_reason, calibration_status, fields_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise
        finally:
            cursor.close()

    def close(self):
        self.connection.close()
