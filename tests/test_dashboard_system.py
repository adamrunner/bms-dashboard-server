import tempfile
import time
import unittest
import json
import sqlite3
from pathlib import Path
from unittest import mock

import bms_mqtt_logger
import dashboard_server
import database_queries


def build_payload(bms_id: str, timestamp: int, pack_voltage: float = 13.1) -> str:
    return (
        f"{bms_id},{timestamp},1,00:00:01,1.0,{pack_voltage},1.2,80,15.7,"
        "100,5,50,4,3.2,1,3.4,4,0.2,3,20,25,true,true,3.3,3.3,3.3,3.3,21,22,23"
    )


def build_gateway_simulator_payload(bms_id: str, timestamp: int) -> str:
    """Match esp32-sim7670g: 23 fixed fields, four cells, two temperatures."""
    return (
        f"{bms_id},{timestamp},1,00:00:01,1.0,13.2,-3.8,87,-50.2,"
        "100,4,53,4,3.295,1,3.305,4,0.010,2,21.9,25.4,1,1,"
        "3.300,3.301,3.299,3.302,22.0,25.3"
    )


def build_status_payload(
    device_id: str,
    *,
    firmware_version: str = "9217453",
    boot_id: str = "0123456789abcdef",
    pending_verify: bool = False,
    schema_version: int = 1,
    status_seq: int = 1,
    reported_at: int | None = None,
    time_source: str | None = None,
    status_reason: str = "boot"
) -> str:
    payload = {
        "schema_version": schema_version,
        "device_id": device_id,
        "online": True,
        "firmware_version": firmware_version,
        "ota_slot": "ota_1",
        "pending_verify": pending_verify,
        "boot_id": boot_id,
        "reset_reason": "software",
        "idf_version": "v5.5",
        "build_date": "Jul 25 2026",
        "build_time": "17:43:00"
    }
    if schema_version == 2:
        payload.update({
            "status_seq": status_seq,
            "reported_at": reported_at,
            "time_source": time_source,
            "status_reason": status_reason,
            "rollback_from_version": None,
            "rollback_target_version": None
        })
    return json.dumps(payload)


def build_availability_payload(
    device_id: str,
    *,
    online: bool = True,
    boot_id: str = "0123456789abcdef"
) -> str:
    return json.dumps({
        "schema_version": 1,
        "device_id": device_id,
        "online": online,
        "boot_id": boot_id
    })


class DashboardSystemTestCase(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.temp_dir.name) / "test_bms_telemetry.db")

        self.original_database_path = database_queries.DATABASE_PATH
        self.original_logger_path = bms_mqtt_logger.DATABASE_PATH

        database_queries.DATABASE_PATH = self.db_path
        bms_mqtt_logger.DATABASE_PATH = self.db_path
        database_queries.ensure_database_schema()

        dashboard_server.connected_clients.clear()
        dashboard_server.client_view_config.clear()
        dashboard_server.monitoring_active = False
        dashboard_server.last_seen_record_id = None
        dashboard_server.last_seen_status_id = None
        dashboard_server.last_seen_availability_id = None

    def tearDown(self):
        database_queries.DATABASE_PATH = self.original_database_path
        bms_mqtt_logger.DATABASE_PATH = self.original_logger_path
        dashboard_server.connected_clients.clear()
        dashboard_server.client_view_config.clear()
        dashboard_server.monitoring_active = False
        dashboard_server.last_seen_record_id = None
        dashboard_server.last_seen_status_id = None
        dashboard_server.last_seen_availability_id = None
        self.temp_dir.cleanup()

    def count_status_rows(self, device_id: str) -> int:
        conn = database_queries.create_db_connection()
        try:
            row = conn.execute(
                "SELECT COUNT(*) AS count FROM device_status_checkins WHERE device_id = ?",
                (device_id,)
            ).fetchone()
            return row["count"]
        finally:
            conn.close()

    def count_availability_rows(self, device_id: str) -> int:
        conn = database_queries.create_db_connection()
        try:
            row = conn.execute(
                """
                SELECT COUNT(*) AS count
                FROM device_availability_events
                WHERE device_id = ?
                """,
                (device_id,)
            ).fetchone()
            return row["count"]
        finally:
            conn.close()

    def test_convert_value_handles_bool_and_numeric_fields(self):
        self.assertTrue(bms_mqtt_logger.convert_value("true", "charging_enabled"))
        self.assertFalse(bms_mqtt_logger.convert_value("0", "discharging_enabled"))
        self.assertEqual(bms_mqtt_logger.convert_value("42.9", "timestamp"), 42)
        self.assertEqual(bms_mqtt_logger.convert_value("3.14", "pack_voltage_v"), 3.14)

    def test_gateway_simulator_row_pads_missing_temperature(self):
        now = int(time.time())
        payload = build_gateway_simulator_payload("gw-simulator", now)

        self.assertEqual(len(payload.split(",")), 29)
        bms_mqtt_logger.insert_telemetry_data(payload)

        latest = database_queries.get_latest_reading("gw-simulator")
        self.assertEqual(latest["cell_count"], 4)
        self.assertEqual(latest["temp_count"], 2)
        self.assertEqual(latest["cells_v_4"], 3.302)
        self.assertEqual(latest["temps_c_1"], 22.0)
        self.assertEqual(latest["temps_c_2"], 25.3)
        self.assertIsNone(latest["temps_c_3"])

    def test_variable_width_row_rejects_count_mismatch(self):
        now = int(time.time())
        payload = build_gateway_simulator_payload("gw-mismatch", now)
        fields = payload.split(",")
        fields[18] = "3"

        bms_mqtt_logger.insert_telemetry_data(",".join(fields))

        self.assertIsNone(database_queries.get_latest_reading("gw-mismatch"))

    def test_variable_width_row_rejects_unsupported_sensor_count(self):
        now = int(time.time())
        payload = build_gateway_simulator_payload("gw-too-many-cells", now)
        fields = payload.split(",")
        fields[12] = "5"

        bms_mqtt_logger.insert_telemetry_data(",".join(fields))

        self.assertIsNone(database_queries.get_latest_reading("gw-too-many-cells"))

    def test_status_schema_is_created_additively(self):
        conn = database_queries.create_db_connection()
        try:
            row = conn.execute(
                """
                SELECT name FROM sqlite_master
                WHERE type = 'table' AND name = 'device_status_checkins'
                """
            ).fetchone()
            columns = {
                column["name"]
                for column in conn.execute("PRAGMA table_info(device_status_checkins)")
            }
            exact_index = conn.execute(
                """
                SELECT name FROM sqlite_master
                WHERE type = 'index' AND name = 'idx_device_status_exact_event'
                """
            ).fetchone()
            availability_table = conn.execute(
                """
                SELECT name FROM sqlite_master
                WHERE type = 'table' AND name = 'device_availability_events'
                """
            ).fetchone()
        finally:
            conn.close()

        self.assertEqual(row["name"], "device_status_checkins")
        self.assertTrue({
            "status_seq",
            "reported_at",
            "time_source",
            "status_reason",
            "rollback_from_version",
            "rollback_target_version"
        }.issubset(columns))
        self.assertEqual(exact_index["name"], "idx_device_status_exact_event")
        self.assertEqual(
            availability_table["name"],
            "device_availability_events"
        )
        database_queries.validate_database_schema()

    def test_status_v2_migration_upgrades_existing_table(self):
        legacy_path = str(Path(self.temp_dir.name) / "legacy_status.db")
        conn = sqlite3.connect(legacy_path)
        conn.executescript(
            """
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
        conn.close()

        database_queries.DATABASE_PATH = legacy_path
        database_queries.ensure_database_schema()
        migrated = database_queries.create_db_connection()
        try:
            columns = {
                column["name"]
                for column in migrated.execute(
                    "PRAGMA table_info(device_status_checkins)"
                )
            }
        finally:
            migrated.close()

        self.assertTrue(set(database_queries.DEVICE_STATUS_V2_COLUMNS).issubset(columns))

    def test_valid_status_is_normalized_and_inserted(self):
        payload = build_status_payload("gw-status")

        row_id = bms_mqtt_logger.insert_status_data(
            payload,
            "bms/status/gw-status",
            mqtt_retained=False
        )
        latest = database_queries.get_latest_device_status("gw-status")

        self.assertIsNotNone(row_id)
        self.assertEqual(latest["firmware_version"], "9217453")
        self.assertEqual(latest["ota_slot"], "ota_1")
        self.assertFalse(latest["pending_verify"])
        self.assertFalse(latest["mqtt_retained"])
        self.assertTrue(latest["reported_online"])

    def test_mqtt_connect_subscribes_to_telemetry_status_and_availability(self):
        client = mock.Mock()
        client.subscribe.return_value = (0, 42)

        bms_mqtt_logger.on_connect(client, None, None, 0)

        client.subscribe.assert_called_once_with([
            (bms_mqtt_logger.MQTT_TOPIC, 0),
            (bms_mqtt_logger.MQTT_STATUS_TOPIC, 1),
            (bms_mqtt_logger.MQTT_AVAILABILITY_TOPIC, 1),
        ])

    def test_status_validation_rejects_bad_payloads(self):
        with self.assertRaisesRegex(ValueError, "invalid JSON"):
            bms_mqtt_logger.normalize_status_payload(
                "{",
                "bms/status/gw-invalid"
            )

        missing_field = json.loads(build_status_payload("gw-missing"))
        del missing_field["firmware_version"]
        with self.assertRaisesRegex(ValueError, "firmware_version"):
            bms_mqtt_logger.normalize_status_payload(
                json.dumps(missing_field),
                "bms/status/gw-missing"
            )

        unsupported = json.loads(build_status_payload("gw-schema"))
        unsupported["schema_version"] = 3
        with self.assertRaisesRegex(ValueError, "unsupported schema_version"):
            bms_mqtt_logger.normalize_status_payload(
                json.dumps(unsupported),
                "bms/status/gw-schema"
            )

        with self.assertRaisesRegex(ValueError, "does not match"):
            bms_mqtt_logger.normalize_status_payload(
                build_status_payload("gw-payload"),
                "bms/status/gw-topic"
            )

    def test_retained_status_replay_is_deduplicated(self):
        payload = build_status_payload("gw-retained")

        first_id = bms_mqtt_logger.insert_status_data(
            payload,
            "bms/status/gw-retained",
            mqtt_retained=True
        )
        second_id = bms_mqtt_logger.insert_status_data(
            payload,
            "bms/status/gw-retained",
            mqtt_retained=True
        )

        self.assertIsNotNone(first_id)
        self.assertIsNone(second_id)
        self.assertEqual(self.count_status_rows("gw-retained"), 1)

    def test_live_status_checkins_from_same_boot_remain_distinct(self):
        payload = build_status_payload("gw-live")

        first_id = bms_mqtt_logger.insert_status_data(
            payload,
            "bms/status/gw-live",
            mqtt_retained=False
        )
        second_id = bms_mqtt_logger.insert_status_data(
            payload,
            "bms/status/gw-live",
            mqtt_retained=False
        )

        self.assertNotEqual(first_id, second_id)
        self.assertEqual(self.count_status_rows("gw-live"), 2)

    def test_schema_v2_status_events_are_exactly_deduplicated(self):
        topic = "bms/status/gw-v2"
        first = build_status_payload(
            "gw-v2",
            schema_version=2,
            status_seq=1
        )
        second = build_status_payload(
            "gw-v2",
            schema_version=2,
            status_seq=2,
            reported_at=1785033600,
            time_source="sntp",
            status_reason="time_synchronized"
        )

        first_id = bms_mqtt_logger.insert_status_data(first, topic, mqtt_retained=False)
        duplicate_id = bms_mqtt_logger.insert_status_data(
            first,
            topic,
            mqtt_retained=False
        )
        second_id = bms_mqtt_logger.insert_status_data(second, topic, mqtt_retained=False)

        self.assertIsNotNone(first_id)
        self.assertIsNone(duplicate_id)
        self.assertIsNotNone(second_id)
        self.assertEqual(self.count_status_rows("gw-v2"), 2)
        latest = database_queries.get_latest_device_status("gw-v2")
        self.assertEqual(latest["status_seq"], 2)
        self.assertEqual(latest["reported_at"], 1785033600)
        self.assertEqual(latest["time_source"], "sntp")
        self.assertEqual(latest["status_reason"], "time_synchronized")

    def test_schema_v2_status_validation_checks_time_pair(self):
        payload = json.loads(build_status_payload("gw-v2-time", schema_version=2))
        payload["time_source"] = "gnss"

        with self.assertRaisesRegex(ValueError, "time_source must be null"):
            bms_mqtt_logger.normalize_status_payload(
                json.dumps(payload),
                "bms/status/gw-v2-time"
            )

    def test_availability_validation_rejects_invalid_documents(self):
        with self.assertRaisesRegex(ValueError, "invalid JSON"):
            bms_mqtt_logger.normalize_availability_payload(
                "{",
                "bms/availability/gw-invalid"
            )

        missing_online = json.loads(build_availability_payload("gw-missing"))
        del missing_online["online"]
        with self.assertRaisesRegex(ValueError, "online must be a boolean"):
            bms_mqtt_logger.normalize_availability_payload(
                json.dumps(missing_online),
                "bms/availability/gw-missing"
            )

        with self.assertRaisesRegex(ValueError, "does not match"):
            bms_mqtt_logger.normalize_availability_payload(
                build_availability_payload("gw-payload"),
                "bms/availability/gw-topic"
            )

    def test_availability_replay_is_suppressed_but_transitions_remain(self):
        topic = "bms/availability/gw-availability"
        online = build_availability_payload("gw-availability", online=True)
        offline = build_availability_payload("gw-availability", online=False)

        first_online_id = bms_mqtt_logger.insert_availability_data(
            online,
            topic,
            mqtt_retained=True
        )
        replay_id = bms_mqtt_logger.insert_availability_data(
            online,
            topic,
            mqtt_retained=True
        )
        offline_id = bms_mqtt_logger.insert_availability_data(
            offline,
            topic,
            mqtt_retained=True
        )
        repeated_offline_id = bms_mqtt_logger.insert_availability_data(
            offline,
            topic,
            mqtt_retained=False
        )
        reconnected_id = bms_mqtt_logger.insert_availability_data(
            online,
            topic,
            mqtt_retained=False
        )

        self.assertIsNotNone(first_online_id)
        self.assertIsNone(replay_id)
        self.assertIsNotNone(offline_id)
        self.assertIsNone(repeated_offline_id)
        self.assertIsNotNone(reconnected_id)
        self.assertEqual(self.count_availability_rows("gw-availability"), 3)
        latest = database_queries.get_latest_device_availability(
            "gw-availability"
        )
        self.assertTrue(latest["online"])
        self.assertFalse(latest["mqtt_retained"])

    def test_new_boot_is_an_availability_event_even_if_state_matches(self):
        topic = "bms/availability/gw-new-boot"
        first = build_availability_payload(
            "gw-new-boot",
            boot_id="boot-one"
        )
        second = build_availability_payload(
            "gw-new-boot",
            boot_id="boot-two"
        )

        first_id = bms_mqtt_logger.insert_availability_data(first, topic, True)
        second_id = bms_mqtt_logger.insert_availability_data(second, topic, True)

        self.assertNotEqual(first_id, second_id)
        self.assertEqual(self.count_availability_rows("gw-new-boot"), 2)

    def test_secret_and_mqtt_config_use_development_fallbacks(self):
        with mock.patch.dict('os.environ', {'APP_ENV': 'development'}, clear=False):
            with mock.patch.dict('os.environ', {'FLASK_SECRET_KEY': ''}, clear=False):
                self.assertEqual(
                    dashboard_server.resolve_flask_secret_key(),
                    'dev-dashboard-secret-key'
                )

            with mock.patch.dict(
                'os.environ',
                {'MQTT_USERNAME': '', 'MQTT_PASSWORD': '', 'APP_ENV': 'development'},
                clear=False
            ):
                self.assertEqual(
                    bms_mqtt_logger.resolve_mqtt_credentials(),
                    ('admin', 'password1234')
                )

    def test_secret_and_mqtt_config_require_env_outside_development(self):
        with mock.patch.dict(
            'os.environ',
            {'APP_ENV': 'production', 'FLASK_SECRET_KEY': ''},
            clear=False
        ):
            with self.assertRaises(RuntimeError):
                dashboard_server.resolve_flask_secret_key()

        with mock.patch.dict(
            'os.environ',
            {'APP_ENV': 'production', 'MQTT_USERNAME': '', 'MQTT_PASSWORD': ''},
            clear=False
        ):
            with self.assertRaises(RuntimeError):
                bms_mqtt_logger.resolve_mqtt_credentials()

    def test_resolve_bucket_seconds_uses_expected_steps(self):
        self.assertEqual(database_queries.resolve_bucket_seconds(0.017), 10)
        self.assertEqual(database_queries.resolve_bucket_seconds(0.5), 30)
        self.assertEqual(database_queries.resolve_bucket_seconds(1), 60)
        self.assertEqual(database_queries.resolve_bucket_seconds(6), 600)
        self.assertEqual(database_queries.resolve_bucket_seconds(24), 1800)
        self.assertEqual(database_queries.resolve_bucket_seconds(24, "10m"), 600)
        self.assertEqual(database_queries.resolve_bucket_seconds(24, "15m"), 900)
        self.assertEqual(database_queries.resolve_bucket_seconds(24, "30m"), 1800)

    def test_view_queries_return_reduced_payload(self):
        now = int(time.time())
        bms_mqtt_logger.insert_telemetry_data(build_payload("bms-a", now))

        records, meta = database_queries.get_telemetry_data_for_view(0.017, "bms-a", "auto", 300)
        latest_point = database_queries.get_latest_point_for_view(0.017, "bms-a", "auto", 300)
        latest_full = database_queries.get_latest_reading("bms-a")

        expected_keys = {
            "timestamp",
            "pack_voltage_v",
            "pack_current_a",
            "state_of_charge_pct",
            "power_w",
            "cell_voltage_delta_v",
            "cells_v_1",
            "cells_v_2",
            "cells_v_3",
            "cells_v_4",
            "temps_c_1",
            "temps_c_2",
            "temps_c_3",
        }

        self.assertEqual(set(records[0].keys()), expected_keys)
        self.assertEqual(set(latest_point.keys()), expected_keys)
        self.assertIn("bms_id", latest_full)
        self.assertIn("created_at", latest_full)
        self.assertEqual(meta["point_count"], 1)

    def test_api_data_returns_reduced_payload(self):
        now = int(time.time())
        bms_mqtt_logger.insert_telemetry_data(build_payload("bms-api", now, pack_voltage=13.4))

        client = dashboard_server.app.test_client()
        response = client.get("/api/data?hours=0.017&bms_id=bms-api&resolution=auto")

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["meta"]["point_count"], 1)
        self.assertNotIn("bms_id", payload["records"][0])
        self.assertNotIn("created_at", payload["records"][0])
        self.assertEqual(payload["records"][0]["pack_voltage_v"], 13.4)

    def test_latest_device_status_api_and_device_id_union(self):
        reported_at = int(time.time()) - 2
        bms_mqtt_logger.insert_status_data(
            build_status_payload(
                "gw-status-only",
                pending_verify=True,
                schema_version=2,
                reported_at=reported_at,
                time_source="gnss",
                status_reason="ota_pending_verify"
            ),
            "bms/status/gw-status-only",
            mqtt_retained=True
        )

        client = dashboard_server.app.test_client()
        response = client.get(
            "/api/device-status/latest?bms_id=gw-status-only"
        )
        missing_id_response = client.get("/api/device-status/latest")
        unknown_response = client.get(
            "/api/device-status/latest?bms_id=gw-unknown"
        )

        self.assertEqual(response.status_code, 200)
        status = response.get_json()
        self.assertEqual(status["device_id"], "gw-status-only")
        self.assertTrue(status["pending_verify"])
        self.assertTrue(status["mqtt_retained"])
        self.assertEqual(status["reported_at"], reported_at)
        self.assertEqual(status["time_source"], "gnss")
        self.assertIn("reported_clock_skew_seconds", status)
        self.assertIn("received_age_seconds", status)
        self.assertEqual(missing_id_response.status_code, 400)
        self.assertEqual(unknown_response.get_json(), {})
        self.assertIn("gw-status-only", database_queries.get_available_bms_ids())

    def test_latest_device_availability_api_and_device_id_union(self):
        bms_mqtt_logger.insert_availability_data(
            build_availability_payload("gw-availability-only", online=False),
            "bms/availability/gw-availability-only",
            mqtt_retained=True
        )

        client = dashboard_server.app.test_client()
        response = client.get(
            "/api/device-availability/latest?bms_id=gw-availability-only"
        )
        missing_id_response = client.get("/api/device-availability/latest")
        unknown_response = client.get(
            "/api/device-availability/latest?bms_id=gw-unknown"
        )

        self.assertEqual(response.status_code, 200)
        availability = response.get_json()
        self.assertFalse(availability["online"])
        self.assertTrue(availability["mqtt_retained"])
        self.assertIn("received_age_seconds", availability)
        self.assertEqual(missing_id_response.status_code, 400)
        self.assertEqual(unknown_response.get_json(), {})
        self.assertIn(
            "gw-availability-only",
            database_queries.get_available_bms_ids()
        )

    def test_dashboard_contains_mqtt_availability_badge(self):
        response = dashboard_server.app.test_client().get("/")

        self.assertEqual(response.status_code, 200)
        self.assertIn(
            'id="deviceAvailabilityBadge"',
            response.get_data(as_text=True)
        )

    def test_auto_view_aggregates_thirty_minute_range(self):
        now = int(time.time())
        bucket_start = (now - 60) - ((now - 60) % 30)
        for offset, voltage in enumerate([13.0, 13.2, 13.4, 13.6]):
            bms_mqtt_logger.insert_telemetry_data(
                build_payload("bms-aggregate", bucket_start + offset * 2, pack_voltage=voltage)
            )

        records, meta = database_queries.get_telemetry_data_for_view(
            0.5,
            "bms-aggregate",
            "auto",
            300
        )

        self.assertTrue(meta["is_aggregated"])
        self.assertEqual(meta["bucket_seconds"], 30)
        self.assertEqual(meta["point_count"], 1)
        self.assertEqual(meta["source_record_count"], 4)
        self.assertEqual(records[0]["sample_count"], 4)
        self.assertAlmostEqual(records[0]["pack_voltage_v"], 13.3)

    def test_explicit_ten_second_resolution_buckets_rows(self):
        now = int(time.time())
        bucket_start = (now - 60) - ((now - 60) % 10)
        bms_mqtt_logger.insert_telemetry_data(
            build_payload("bms-10s", bucket_start, pack_voltage=12.8)
        )
        bms_mqtt_logger.insert_telemetry_data(
            build_payload("bms-10s", bucket_start + 2, pack_voltage=13.2)
        )

        records, meta = database_queries.get_telemetry_data_for_view(
            1,
            "bms-10s",
            "10s",
            300
        )
        latest_point = database_queries.get_latest_point_for_view(
            1,
            "bms-10s",
            "10s",
            300
        )

        self.assertTrue(meta["is_aggregated"])
        self.assertEqual(meta["bucket_seconds"], 10)
        self.assertEqual(meta["point_count"], 1)
        self.assertEqual(meta["source_record_count"], 2)
        self.assertEqual(records[0]["sample_count"], 2)
        self.assertAlmostEqual(records[0]["pack_voltage_v"], 13.0)
        self.assertEqual(latest_point["sample_count"], 2)
        self.assertAlmostEqual(latest_point["pack_voltage_v"], 13.0)

    def test_socketio_set_view_returns_snapshot_data(self):
        now = int(time.time())
        bms_mqtt_logger.insert_telemetry_data(build_payload("bms-socket", now, pack_voltage=13.6))

        with mock.patch.object(dashboard_server.socketio, "start_background_task", return_value=None):
            client = dashboard_server.socketio.test_client(dashboard_server.app)
            client.get_received()
            client.emit(
                "set_view",
                {"hours": 1, "bms_id": "bms-socket", "resolution": "10s", "target_points": 300},
            )
            received = client.get_received()
            client.disconnect()

        view_messages = [message for message in received if message["name"] == "view_data"]
        self.assertEqual(len(view_messages), 1)
        records = view_messages[0]["args"][0]["records"]
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["pack_voltage_v"], 13.6)

    def test_background_monitor_emits_selected_device_status(self):
        now = int(time.time())
        bms_mqtt_logger.insert_telemetry_data(
            build_payload("gw-socket-status", now)
        )

        with mock.patch.object(
            dashboard_server.socketio,
            "start_background_task",
            return_value=None
        ):
            client = dashboard_server.socketio.test_client(dashboard_server.app)
            client.get_received()
            client.emit(
                "set_view",
                {
                    "hours": 1,
                    "bms_id": "gw-socket-status",
                    "resolution": "auto",
                    "target_points": 300
                }
            )
            client.get_received()

        bms_mqtt_logger.insert_status_data(
            build_status_payload("gw-socket-status"),
            "bms/status/gw-socket-status",
            mqtt_retained=False
        )
        dashboard_server.last_seen_record_id = database_queries.get_latest_record_id()
        dashboard_server.last_seen_status_id = None
        dashboard_server.monitoring_active = True

        def stop_monitor(_seconds):
            dashboard_server.monitoring_active = False

        with mock.patch.object(
            dashboard_server.socketio,
            "sleep",
            side_effect=stop_monitor
        ):
            dashboard_server.background_monitor()

        received = client.get_received()
        client.disconnect()
        status_messages = [
            message for message in received
            if message["name"] == "device_status_update"
        ]

        self.assertEqual(len(status_messages), 1)
        status = status_messages[0]["args"][0]
        self.assertEqual(status["device_id"], "gw-socket-status")
        self.assertEqual(status["firmware_version"], "9217453")

    def test_background_monitor_emits_selected_device_availability(self):
        now = int(time.time())
        bms_mqtt_logger.insert_telemetry_data(
            build_payload("gw-socket-availability", now)
        )

        with mock.patch.object(
            dashboard_server.socketio,
            "start_background_task",
            return_value=None
        ):
            client = dashboard_server.socketio.test_client(dashboard_server.app)
            client.get_received()
            client.emit(
                "set_view",
                {
                    "hours": 1,
                    "bms_id": "gw-socket-availability",
                    "resolution": "auto",
                    "target_points": 300
                }
            )
            client.get_received()

        bms_mqtt_logger.insert_availability_data(
            build_availability_payload("gw-socket-availability"),
            "bms/availability/gw-socket-availability",
            mqtt_retained=False
        )
        dashboard_server.last_seen_record_id = database_queries.get_latest_record_id()
        dashboard_server.last_seen_status_id = (
            database_queries.get_latest_status_record_id()
        )
        dashboard_server.last_seen_availability_id = None
        dashboard_server.monitoring_active = True

        def stop_monitor(_seconds):
            dashboard_server.monitoring_active = False

        with mock.patch.object(
            dashboard_server.socketio,
            "sleep",
            side_effect=stop_monitor
        ):
            dashboard_server.background_monitor()

        received = client.get_received()
        client.disconnect()
        availability_messages = [
            message for message in received
            if message["name"] == "device_availability_update"
        ]

        self.assertEqual(len(availability_messages), 1)
        availability = availability_messages[0]["args"][0]
        self.assertEqual(
            availability["device_id"],
            "gw-socket-availability"
        )
        self.assertTrue(availability["online"])


if __name__ == "__main__":
    unittest.main()
