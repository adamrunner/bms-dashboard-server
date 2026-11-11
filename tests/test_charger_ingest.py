import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import bms_mqtt_logger
import database_queries

DEVICE = "gw-e3aba4"
TOPIC = f"charger/telemetry/{DEVICE}"
# Captured from the live broker on 2026-11-11 (truck parked, charger idle).
LIVE_ROW = (
    "gw-e3aba4,1794424191,140,13.4,0.00,0,12.4,0.00,0,100,19,25,0,0,0,0,0,"
    "40,5,0.071,3494,48.295"
)


def charger_row(timestamp=1_794_424_191, elapsed=140, *, device=DEVICE,
                aux_v="14.4", amps="29.50", ign="1", faults_hi="0"):
    return (
        f"{device},{timestamp},{elapsed},{aux_v},{amps},0,13.9,31.20,433,"
        f"100,41,24,8,1,{ign},{faults_hi},0,40,12,0.412,3506,48.600"
    )


class ChargerIngestTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.temp_dir.name) / "charger.db")
        self.original_paths = (
            database_queries.DATABASE_PATH, bms_mqtt_logger.DATABASE_PATH
        )
        database_queries.DATABASE_PATH = self.db_path
        bms_mqtt_logger.DATABASE_PATH = self.db_path
        database_queries.ensure_database_schema()
        self.health = mock.patch.object(bms_mqtt_logger, 'update_health_state')
        self.health_state = self.health.start()

    def tearDown(self):
        self.health.stop()
        database_queries.DATABASE_PATH, bms_mqtt_logger.DATABASE_PATH = (
            self.original_paths
        )
        self.temp_dir.cleanup()

    def rows(self):
        conn = database_queries.create_db_connection()
        try:
            return [dict(r) for r in conn.execute(
                "SELECT * FROM charger_telemetry ORDER BY id"
            )]
        finally:
            conn.close()

    def test_live_row_is_stored_with_types(self):
        result = bms_mqtt_logger.insert_charger_data(LIVE_ROW, TOPIC)
        self.assertEqual(result, {'inserted': 1, 'duplicates': 0, 'rejected': 0})
        [row] = self.rows()
        self.assertEqual(row['device_id'], DEVICE)
        self.assertEqual(row['timestamp'], 1794424191)
        self.assertEqual(row['elapsed_sec'], 140)
        self.assertAlmostEqual(row['aux_battery_v'], 13.4)
        self.assertAlmostEqual(row['alternator_v'], 12.4)
        self.assertEqual(row['controller_temp_c'], 19)
        self.assertEqual(row['ign'], 0)
        self.assertEqual(row['current_limit_a'], 40)
        self.assertEqual(row['total_ah'], 3494)
        self.assertAlmostEqual(row['total_kwh'], 48.295)
        self.assertEqual(row['timestamp_valid'], 1)
        self.assertGreater(row['received_at'], 0)

    def test_charging_row_keeps_raw_register_values(self):
        bms_mqtt_logger.insert_charger_data(charger_row(faults_hi="1024"), TOPIC)
        [row] = self.rows()
        self.assertEqual(row['output_w'], 0)
        self.assertAlmostEqual(row['charge_current_a'], 29.5)
        self.assertEqual(row['charging_state'], 8)
        self.assertEqual(row['monitor_mode'], 1)
        self.assertEqual(row['ign'], 1)
        self.assertEqual(row['faults_hi'], 1024)

    def test_redelivery_creates_one_row(self):
        bms_mqtt_logger.insert_charger_data(charger_row(), TOPIC)
        result = bms_mqtt_logger.insert_charger_data(charger_row(), TOPIC)
        self.assertEqual(result['duplicates'], 1)
        self.assertEqual(len(self.rows()), 1)

    def test_identical_readings_at_a_new_sample_time_are_kept(self):
        bms_mqtt_logger.insert_charger_data(charger_row(1_794_424_191, 140), TOPIC)
        bms_mqtt_logger.insert_charger_data(charger_row(1_794_424_201, 150), TOPIC)
        self.assertEqual(len(self.rows()), 2)

    def test_unsynchronized_clock_is_preserved_as_invalid_time(self):
        bms_mqtt_logger.insert_charger_data(charger_row(timestamp=0, elapsed=12), TOPIC)
        [row] = self.rows()
        self.assertEqual(row['timestamp'], 0)
        self.assertEqual(row['timestamp_valid'], 0)

    def test_invalid_rows_are_rejected(self):
        cases = {
            'too few columns': charger_row().rsplit(',', 1)[0],
            'newer schema': charger_row() + ",7",
            'non-numeric value': charger_row(aux_v="abc"),
            'ign not 0/1': charger_row(ign="2"),
            'bad device id': charger_row(device="gw e3"),
            'negative uptime': charger_row(elapsed=-1),
        }
        for name, payload in cases.items():
            with self.subTest(name):
                result = bms_mqtt_logger.insert_charger_data(payload, TOPIC)
                self.assertEqual(result['rejected'], 1)
        self.assertEqual(self.rows(), [])

    def test_topic_must_match_the_row_device(self):
        result = bms_mqtt_logger.insert_charger_data(
            charger_row(), "charger/telemetry/gw-other"
        )
        self.assertEqual(result['rejected'], 1)
        self.assertEqual(self.rows(), [])

    def test_bad_row_does_not_block_good_rows_in_one_payload(self):
        payload = "\n".join([charger_row(elapsed=140), "junk", charger_row(elapsed=150)])
        result = bms_mqtt_logger.insert_charger_data(payload, TOPIC)
        self.assertEqual(result, {'inserted': 2, 'duplicates': 0, 'rejected': 1})

    def test_counters_reach_the_health_state(self):
        bms_mqtt_logger.insert_charger_data(charger_row(), TOPIC)
        bms_mqtt_logger.insert_charger_data(charger_row(), TOPIC)
        state = self.health_state.call_args.kwargs
        self.assertGreaterEqual(state['charger_rows_inserted'], 1)
        self.assertGreaterEqual(state['charger_duplicates_suppressed'], 1)

    def test_on_message_routes_charger_topic(self):
        message = SimpleNamespace(
            topic=TOPIC, payload=LIVE_ROW.encode('utf-8'), retain=False
        )
        bms_mqtt_logger.on_message(None, None, message)
        self.assertEqual(len(self.rows()), 1)
        conn = database_queries.create_db_connection()
        try:
            bms_rows = conn.execute("SELECT COUNT(*) FROM bms_telemetry").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(bms_rows, 0)


if __name__ == '__main__':
    unittest.main()
