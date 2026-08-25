"""Guards against schema drift between fresh and migrated databases.

SQLite's ALTER TABLE ADD COLUMN can only append. A column declared mid-table in
bms_schema.sql but added via ALTER TABLE in ensure_database_schema() therefore
lands in a different physical position depending on how the database was
created. The two hold identical data in different layouts, and any positional
SQL between them silently writes values into the wrong columns.

These tests fail loudly when that divergence is reintroduced.
"""

import sqlite3
import tempfile
import unittest
from pathlib import Path

import database_queries


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]

# Physical column order of a database that has been through every migration.
# Appending here is fine; reordering means an existing deployment disagrees.
EXPECTED_COLUMN_ORDER = {
    "bms_telemetry": [
        "id", "timestamp", "elapsed_seconds", "elapsed_hms", "total_energy_wh",
        "pack_voltage_v", "pack_current_a", "state_of_charge_pct", "power_w",
        "full_capacity_ah", "peak_current_a", "peak_power_w", "cell_count",
        "min_cell_voltage_v", "min_cell_num", "max_cell_voltage_v",
        "max_cell_num", "cell_voltage_delta_v", "temp_count", "min_temp_c",
        "max_temp_c", "charging_enabled", "discharging_enabled", "cells_v_1",
        "cells_v_2", "cells_v_3", "cells_v_4", "temps_c_1", "temps_c_2",
        "temps_c_3", "created_at", "bms_id", "timestamp_valid",
    ],
    "device_status_checkins": [
        "id", "device_id", "schema_version", "firmware_version", "ota_slot",
        "pending_verify", "boot_id", "reset_reason", "idf_version",
        "build_date", "build_time", "reported_online", "mqtt_retained",
        "payload_sha256", "raw_payload", "received_at", "status_seq",
        "reported_at", "time_source", "status_reason", "rollback_from_version",
        "rollback_target_version",
    ],
    "device_availability_events": [
        "id", "device_id", "boot_id", "online", "mqtt_retained",
        "payload_sha256", "raw_payload", "received_at",
    ],
    "firmware_expectations": [
        "device_id", "expected_version", "grace_until", "updated_at",
    ],
    "device_alerts": [
        "id", "device_id", "alert_type", "severity", "source_status_id",
        "dedup_key", "details_json", "detected_at", "acknowledged_at",
        "resolved_at",
    ],
}


def columns(conn: sqlite3.Connection, table: str) -> list[str]:
    return [row[1] for row in conn.execute(f"PRAGMA table_info({table})")]


def column_details(conn: sqlite3.Connection, table: str) -> list[tuple]:
    """(name, type, notnull, pk) -- the parts that must not silently differ."""
    return [
        (row[1], row[2].upper(), row[3], row[5])
        for row in conn.execute(f"PRAGMA table_info({table})")
    ]


class SchemaParityTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.original_database_path = database_queries.DATABASE_PATH

        def restore():
            database_queries.DATABASE_PATH = self.original_database_path

        self.addCleanup(restore)

    def build_fresh(self) -> sqlite3.Connection:
        """A database created the way a new deployment creates one."""
        path = str(Path(self.temp_dir.name) / "fresh.db")
        database_queries.DATABASE_PATH = path
        database_queries.ensure_database_schema()
        return sqlite3.connect(path)

    def build_legacy_migrated(self) -> sqlite3.Connection:
        """A pre-migration database brought forward by ensure_database_schema().

        Mirrors a long-running deployment: the base tables exist without the
        later columns, which arrive only via ALTER TABLE.
        """
        path = str(Path(self.temp_dir.name) / "legacy.db")
        legacy = sqlite3.connect(path)
        legacy.executescript(
            """
            CREATE TABLE bms_telemetry (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp INTEGER NOT NULL,
                elapsed_seconds REAL,
                elapsed_hms TEXT,
                total_energy_wh REAL,
                pack_voltage_v REAL,
                pack_current_a REAL,
                state_of_charge_pct REAL,
                power_w REAL,
                full_capacity_ah REAL,
                peak_current_a REAL,
                peak_power_w REAL,
                cell_count INTEGER,
                min_cell_voltage_v REAL,
                min_cell_num INTEGER,
                max_cell_voltage_v REAL,
                max_cell_num INTEGER,
                cell_voltage_delta_v REAL,
                temp_count INTEGER,
                min_temp_c REAL,
                max_temp_c REAL,
                charging_enabled BOOLEAN,
                discharging_enabled BOOLEAN,
                cells_v_1 REAL,
                cells_v_2 REAL,
                cells_v_3 REAL,
                cells_v_4 REAL,
                temps_c_1 REAL,
                temps_c_2 REAL,
                temps_c_3 REAL,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            );
            ALTER TABLE bms_telemetry ADD COLUMN bms_id TEXT;

            CREATE TABLE device_status_checkins (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                device_id TEXT NOT NULL,
                schema_version INTEGER NOT NULL,
                firmware_version TEXT NOT NULL,
                ota_slot TEXT NOT NULL,
                pending_verify BOOLEAN NOT NULL,
                boot_id TEXT NOT NULL,
                reset_reason TEXT NOT NULL,
                idf_version TEXT,
                build_date TEXT,
                build_time TEXT,
                reported_online BOOLEAN NOT NULL,
                mqtt_retained BOOLEAN NOT NULL DEFAULT 0,
                payload_sha256 TEXT NOT NULL,
                raw_payload TEXT NOT NULL,
                received_at INTEGER NOT NULL
            );
            """
        )
        legacy.commit()
        legacy.close()

        database_queries.DATABASE_PATH = path
        database_queries.ensure_database_schema()
        return sqlite3.connect(path)

    def test_fresh_database_matches_expected_column_order(self):
        conn = self.build_fresh()
        for table, expected in EXPECTED_COLUMN_ORDER.items():
            with self.subTest(table=table):
                self.assertEqual(
                    columns(conn, table),
                    expected,
                    f"{table} column order in bms_schema.sql has drifted. A new "
                    "column must be appended to the END of the CREATE TABLE, "
                    "because ALTER TABLE can only append it for existing "
                    "deployments.",
                )

    def test_migrated_database_matches_fresh_database(self):
        """The invariant that actually matters: both paths produce one layout."""
        fresh = self.build_fresh()
        migrated = self.build_legacy_migrated()
        for table in EXPECTED_COLUMN_ORDER:
            with self.subTest(table=table):
                self.assertEqual(
                    column_details(migrated, table),
                    column_details(fresh, table),
                    f"{table} differs between a migrated database and a fresh "
                    "one. Positional SQL across the two would silently write "
                    "values into the wrong columns.",
                )

    def test_migrated_and_fresh_agree_on_indexes(self):
        fresh = self.build_fresh()
        migrated = self.build_legacy_migrated()

        def indexes(conn):
            return sorted(
                row[0] for row in conn.execute(
                    "SELECT name FROM sqlite_master "
                    "WHERE type='index' AND sql IS NOT NULL"
                )
            )

        self.assertEqual(indexes(migrated), indexes(fresh))

    def test_schema_file_declares_every_migration_column(self):
        """A column ALTERed in code must also be declared in bms_schema.sql.

        Otherwise a fresh database only acquires it via the migration path,
        which is exactly how the two orders came apart in the first place.
        """
        schema_sql = (REPOSITORY_ROOT / "bms_schema.sql").read_text()
        migration_columns = {
            **database_queries.TELEMETRY_POLICY_COLUMNS,
            **database_queries.DEVICE_STATUS_V2_COLUMNS,
        }
        for column in migration_columns:
            with self.subTest(column=column):
                self.assertIn(
                    column,
                    schema_sql,
                    f"{column} is added by ALTER TABLE but never declared in "
                    "bms_schema.sql; fresh and migrated databases will diverge.",
                )


if __name__ == "__main__":
    unittest.main()
