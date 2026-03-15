import tempfile
import time
import unittest
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

    def tearDown(self):
        database_queries.DATABASE_PATH = self.original_database_path
        bms_mqtt_logger.DATABASE_PATH = self.original_logger_path
        dashboard_server.connected_clients.clear()
        dashboard_server.client_view_config.clear()
        dashboard_server.monitoring_active = False
        dashboard_server.last_seen_record_id = None
        self.temp_dir.cleanup()

    def test_convert_value_handles_bool_and_numeric_fields(self):
        self.assertTrue(bms_mqtt_logger.convert_value("true", "charging_enabled"))
        self.assertFalse(bms_mqtt_logger.convert_value("0", "discharging_enabled"))
        self.assertEqual(bms_mqtt_logger.convert_value("42.9", "timestamp"), 42)
        self.assertEqual(bms_mqtt_logger.convert_value("3.14", "pack_voltage_v"), 3.14)

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
        self.assertEqual(database_queries.resolve_bucket_seconds(6), 180)
        self.assertEqual(database_queries.resolve_bucket_seconds(24), 300)

    def test_view_queries_return_reduced_payload(self):
        now = int(time.time())
        bms_mqtt_logger.insert_telemetry_data(build_payload("bms-a", now))

        records, meta = database_queries.get_telemetry_data_for_view(1, "bms-a", "10s", 300)
        latest_point = database_queries.get_latest_point_for_view(1, "bms-a", "10s", 300)
        latest_full = database_queries.get_latest_reading("bms-a")

        expected_keys = {
            "timestamp",
            "pack_voltage_v",
            "pack_current_a",
            "state_of_charge_pct",
            "power_w",
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
        response = client.get("/api/data?hours=1&bms_id=bms-api&resolution=10s")

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["meta"]["point_count"], 1)
        self.assertNotIn("bms_id", payload["records"][0])
        self.assertNotIn("created_at", payload["records"][0])
        self.assertEqual(payload["records"][0]["pack_voltage_v"], 13.4)

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


if __name__ == "__main__":
    unittest.main()
