import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import bms_mqtt_logger
import charger_registers
import dashboard_server
import database_queries

DEVICE = "gw-e3aba4"
TOPIC = f"charger/telemetry/{DEVICE}"


def charger_row(timestamp, elapsed, *, device=DEVICE, aux_v="14.4",
                amps="29.50", state="4", faults_hi="0", faults_lo="0"):
    # output_w (column 6) is 0, as the RBC40D1U reports it while charging.
    return (
        f"{device},{timestamp},{elapsed},{aux_v},{amps},0,13.9,31.20,433,"
        f"100,41,24,{state},1,0,{faults_hi},{faults_lo},40,12,0.412,3506,48.600"
    )


def insert(row, device=DEVICE):
    with mock.patch.object(bms_mqtt_logger, 'update_health_state'):
        result = bms_mqtt_logger.insert_charger_data(row, f"charger/telemetry/{device}")
    assert result['inserted'] == 1, result


class ChargerRegisterDecodeTest(unittest.TestCase):
    def test_known_codes_and_fault_bits_decode(self):
        reading = charger_registers.decode_charger_reading({
            'charging_state': 6, 'monitor_mode': 1,
            'faults_hi': (1 << 8) | (1 << 3), 'faults_lo': 1 << 2,
        })
        self.assertEqual(reading['charging_state_label'], 'Current limiting')
        self.assertEqual(reading['monitor_mode_label'], 'Alternator charging house battery')
        self.assertEqual(reading['faults'], [
            'Unknown fault (0x0121 bit 3)',
            'Alternator overvoltage',
            'House battery undervoltage warning',
        ])

    def test_unknown_codes_are_not_guessed(self):
        reading = charger_registers.decode_charger_reading({
            'charging_state': 7, 'monitor_mode': 9, 'faults_hi': 0, 'faults_lo': 0,
        })
        self.assertEqual(reading['charging_state_label'], 'Unknown (7)')
        self.assertEqual(reading['monitor_mode_label'], 'Unknown (9)')
        self.assertEqual(reading['faults'], [])


class ChargerDashboardTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.temp_dir.name) / "charger_dash.db")
        self.original_paths = (
            database_queries.DATABASE_PATH, bms_mqtt_logger.DATABASE_PATH
        )
        database_queries.DATABASE_PATH = self.db_path
        bms_mqtt_logger.DATABASE_PATH = self.db_path
        database_queries.ensure_database_schema()
        dashboard_server.connected_clients.clear()
        dashboard_server.client_view_config.clear()
        dashboard_server.monitoring_active = False
        self.now = int(time.time())

    def tearDown(self):
        database_queries.DATABASE_PATH, bms_mqtt_logger.DATABASE_PATH = (
            self.original_paths
        )
        dashboard_server.connected_clients.clear()
        dashboard_server.client_view_config.clear()
        dashboard_server.monitoring_active = False
        dashboard_server.last_seen_charger_id = None
        self.temp_dir.cleanup()

    def test_raw_view_derives_output_power(self):
        insert(charger_row(self.now - 20, 100))
        insert(charger_row(self.now - 20, 100, device="gw-other"), device="gw-other")

        records, meta = database_queries.get_charger_data_for_view(0.017, DEVICE)

        self.assertFalse(meta['is_aggregated'])
        [record] = records
        self.assertEqual(set(record), {
            'timestamp', 'aux_battery_v', 'charge_current_a', 'output_power_w',
            'alternator_v', 'alternator_current_a', 'alternator_w',
        })
        self.assertAlmostEqual(record['output_power_w'], 14.4 * 29.5)

    def test_aggregated_view_uses_bms_bucket_boundaries(self):
        bucket_start = (self.now - 120) - ((self.now - 120) % 30)
        insert(charger_row(bucket_start + 2, 10, amps="10"))
        insert(charger_row(bucket_start + 12, 20, amps="30"))

        records, meta = database_queries.get_charger_data_for_view(0.5, DEVICE)
        latest = database_queries.get_latest_charger_point_for_view(0.5, DEVICE)

        self.assertTrue(meta['is_aggregated'])
        self.assertEqual(meta['bucket_seconds'], 30)
        [record] = records
        self.assertEqual(record['timestamp'], bucket_start)
        self.assertEqual(record['sample_count'], 2)
        self.assertAlmostEqual(record['charge_current_a'], 20.0)
        self.assertAlmostEqual(record['output_power_w'], 14.4 * 20.0)
        self.assertEqual(latest['timestamp'], bucket_start)
        self.assertAlmostEqual(latest['charge_current_a'], 20.0)

    def test_absolute_window_excludes_rows_outside_it(self):
        insert(charger_row(self.now - 3000, 10))
        insert(charger_row(self.now - 1000, 20))

        records, _ = database_queries.get_charger_data_for_view(
            1, DEVICE, '10s', 300,
            start_ts=self.now - 3600, end_ts=self.now - 2000
        )

        self.assertEqual([r['timestamp'] for r in records], [self.now - 3000 - (self.now - 3000) % 10])

    def test_api_data_carries_charger_series_and_decoded_reading(self):
        insert(charger_row(self.now - 20, 100, state="6", faults_lo=str(1 << 2)))

        client = dashboard_server.app.test_client()
        payload = client.get(f"/api/data?hours=0.017&bms_id={DEVICE}").get_json()

        charger = payload['charger']
        self.assertEqual(len(charger['records']), 1)
        reading = charger['latest_reading']
        self.assertEqual(reading['charging_state_label'], 'Current limiting')
        self.assertEqual(reading['faults'], ['House battery undervoltage warning'])
        self.assertIs(reading['ign'], False)
        self.assertAlmostEqual(reading['output_power_w'], 14.4 * 29.5)
        self.assertGreaterEqual(reading['received_age_seconds'], 0)

    def test_api_data_survives_a_charger_query_failure(self):
        client = dashboard_server.app.test_client()
        with mock.patch.object(
            dashboard_server, 'get_charger_data_for_view',
            side_effect=RuntimeError("boom")
        ):
            response = client.get(f"/api/data?hours=0.017&bms_id={DEVICE}")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()['charger']['records'], [])

    def test_latest_charger_api(self):
        client = dashboard_server.app.test_client()
        self.assertEqual(client.get(f"/api/charger/latest?bms_id={DEVICE}").get_json(), {})
        self.assertEqual(client.get("/api/charger/latest?bms_id=bad%20id").status_code, 400)

        insert(charger_row(self.now - 20, 100))
        reading = client.get(f"/api/charger/latest?bms_id={DEVICE}").get_json()
        self.assertEqual(reading['device_id'], DEVICE)
        self.assertEqual(reading['charging_state_label'], 'Boost')

    def test_background_monitor_pushes_new_charger_rows(self):
        insert(charger_row(self.now - 30, 90))
        with mock.patch.object(
            dashboard_server.socketio, "start_background_task", return_value=None
        ):
            client = dashboard_server.socketio.test_client(dashboard_server.app)
            client.emit("set_view", {"hours": 0.017, "bms_id": DEVICE})
            [view] = [m for m in client.get_received() if m["name"] == "view_data"]
        self.assertEqual(len(view["args"][0]["charger"]["records"]), 1)

        insert(charger_row(self.now - 10, 110, amps="40.00"))
        dashboard_server.last_seen_record_id = database_queries.get_latest_record_id()
        dashboard_server.last_seen_status_id = database_queries.get_latest_status_record_id()
        dashboard_server.last_seen_availability_id = (
            database_queries.get_latest_availability_record_id()
        )
        dashboard_server.last_seen_charger_id = None
        dashboard_server.monitoring_active = True

        def stop_monitor(_seconds):
            dashboard_server.monitoring_active = False

        with mock.patch.object(dashboard_server.socketio, "sleep", side_effect=stop_monitor):
            dashboard_server.background_monitor()

        received = client.get_received()
        client.disconnect()
        [update] = [m["args"][0] for m in received if m["name"] == "charger_update"]
        self.assertEqual(update["point"]["timestamp"], self.now - 10)
        self.assertAlmostEqual(update["point"]["charge_current_a"], 40.0)
        self.assertEqual(update["latest_reading"]["charge_current_a"], 40.0)
        self.assertFalse(update["meta"]["is_aggregated"])

    def test_dashboard_template_has_charger_card_and_charts(self):
        html = dashboard_server.app.test_client().get("/").get_data(as_text=True)
        for element_id in ('chargerCard', 'chargerStateBadge', 'chargerPowerChart',
                           'chargerVoltageChart'):
            self.assertIn(f'id="{element_id}"', html)

    def test_dashboard_image_copies_charger_registers(self):
        dockerfile = Path("Dockerfile.dashboard").read_text(encoding="utf-8")
        self.assertIn("COPY charger_registers.py .", dockerfile)


if __name__ == '__main__':
    unittest.main()
