import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import bms_mqtt_logger
import database_queries

DEVICE = "gw-e3aba4"
TOPIC = f"bms/telemetry/{DEVICE}"


def csv_row(timestamp: int, elapsed: int = 100, current: float = -0.5,
            device: str = DEVICE) -> str:
    return (
        f"{device},{timestamp},{elapsed},00:01:40,1.0,13.2,{current},87,-6.6,"
        "100,4,53,4,3.295,1,3.305,4,0.010,2,21.9,25.4,1,1,"
        "3.300,3.301,3.299,3.302,22.0,25.3"
    )


def envelope(sequence: int, timestamp: int = 1_790_000_000, *,
             boot_id: str = "a9635d23c0ffee01", **overrides) -> str:
    body = {
        "schema_version": 2,
        "device_id": DEVICE,
        "boot_id": boot_id,
        "sequence": sequence,
        "captured_at": timestamp,
        "timestamp_valid": timestamp > 0,
        "csv": csv_row(timestamp, elapsed=100 + sequence),
    }
    body.update(overrides)
    return json.dumps(body)


class TelemetryDedupTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.temp_dir.name) / "dedup.db")
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

    def rows(self, where: str = "1 = 1", params: tuple = ()):
        conn = database_queries.create_db_connection()
        try:
            return [dict(row) for row in conn.execute(
                "SELECT id, bms_id, timestamp, elapsed_seconds, pack_current_a, "
                "timestamp_valid, delivery_boot_id, delivery_sequence "
                f"FROM bms_telemetry WHERE {where} ORDER BY id",
                params,
            )]
        finally:
            conn.close()

    # --- Identified (schema-v2 envelope) deliveries -----------------------

    def test_envelope_stores_its_delivery_identity(self):
        result = bms_mqtt_logger.insert_telemetry_data(envelope(7), TOPIC)
        self.assertEqual(result['inserted'], 1)
        [row] = self.rows()
        self.assertEqual(row['bms_id'], DEVICE)
        self.assertEqual(row['timestamp'], 1_790_000_000)
        self.assertEqual(row['delivery_boot_id'], "a9635d23c0ffee01")
        self.assertEqual(row['delivery_sequence'], 7)
        self.assertEqual(row['timestamp_valid'], 1)

    def test_repeated_envelope_creates_one_row(self):
        payload = envelope(7)
        results = [
            bms_mqtt_logger.insert_telemetry_data(payload, TOPIC)
            for _ in range(3)
        ]
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual([r['inserted'] for r in results], [1, 0, 0])
        self.assertEqual(sum(r['identified_duplicates'] for r in results), 2)

    def test_spool_replay_after_later_rows_is_suppressed(self):
        for sequence in range(1, 4):
            bms_mqtt_logger.insert_telemetry_data(
                envelope(sequence, 1_790_000_000 + sequence * 10), TOPIC
            )
        replay = bms_mqtt_logger.insert_telemetry_data(
            envelope(2, 1_790_000_020), TOPIC
        )
        self.assertEqual(replay['identified_duplicates'], 1)
        self.assertEqual(
            [r['delivery_sequence'] for r in self.rows()], [1, 2, 3]
        )

    def test_same_sequence_in_a_new_boot_is_a_new_row(self):
        bms_mqtt_logger.insert_telemetry_data(envelope(1), TOPIC)
        bms_mqtt_logger.insert_telemetry_data(
            envelope(1, 1_790_000_600, boot_id="b0075eed00000002"), TOPIC
        )
        self.assertEqual(len(self.rows()), 2)

    def test_unsynchronized_envelope_is_preserved_as_invalid_time(self):
        bms_mqtt_logger.insert_telemetry_data(envelope(1, 0), TOPIC)
        [row] = self.rows()
        self.assertEqual(row['timestamp_valid'], 0)
        self.assertEqual(row['delivery_sequence'], 1)

    def test_invalid_envelopes_are_rejected(self):
        cases = {
            'topic mismatch': (envelope(1), "bms/telemetry/gw-other"),
            'captured_at mismatch': (envelope(1, captured_at=5), TOPIC),
            'timestamp_valid contradiction': (
                envelope(1, timestamp_valid=False), TOPIC
            ),
            'negative sequence': (envelope(-1), TOPIC),
            'sequence over 32 bits': (envelope(2**32), TOPIC),
            'boolean sequence': (envelope(True), TOPIC),
            'bad boot id': (envelope(1, boot_id="has space"), TOPIC),
            'csv device mismatch': (
                envelope(1, csv=csv_row(1_790_000_000, device="gw-other")), TOPIC
            ),
            'two csv rows': (
                envelope(1, csv=csv_row(1_790_000_000) + "\n"
                         + csv_row(1_790_000_010)), TOPIC
            ),
            'schema version 3': (envelope(1, schema_version=3), TOPIC),
            'not json': ("{not json", TOPIC),
            'oversize': (envelope(1, padding="x" * 9000), TOPIC),
        }
        for name, (payload, topic) in cases.items():
            with self.subTest(name):
                result = bms_mqtt_logger.insert_telemetry_data(payload, topic)
                self.assertEqual(result['inserted'], 0)
                self.assertEqual(result['rejected'], 1)
        self.assertEqual(self.rows(), [])

    # --- Legacy CSV deliveries ----------------------------------------------

    def test_identical_legacy_row_is_suppressed(self):
        payload = csv_row(1_790_000_000)
        first = bms_mqtt_logger.insert_telemetry_data(payload, TOPIC)
        second = bms_mqtt_logger.insert_telemetry_data(payload, TOPIC)
        self.assertEqual(first['inserted'], 1)
        self.assertEqual(second['content_duplicates'], 1)
        self.assertEqual(len(self.rows()), 1)

    def test_legacy_row_that_differs_in_any_field_is_kept(self):
        bms_mqtt_logger.insert_telemetry_data(csv_row(1_790_000_000), TOPIC)
        # Same capture second, different reading: not a duplicate delivery.
        bms_mqtt_logger.insert_telemetry_data(
            csv_row(1_790_000_000, current=-3.8), TOPIC
        )
        # Same values, new capture time: a pack at rest, not a duplicate.
        bms_mqtt_logger.insert_telemetry_data(
            csv_row(1_790_000_010, elapsed=110), TOPIC
        )
        self.assertEqual(len(self.rows()), 3)

    def test_duplicate_inside_one_multi_row_payload(self):
        row = csv_row(1_790_000_000)
        result = bms_mqtt_logger.insert_telemetry_data(
            "\n".join([row, row, csv_row(1_790_000_010, elapsed=110)]), TOPIC
        )
        self.assertEqual(result['inserted'], 2)
        self.assertEqual(result['content_duplicates'], 1)

    def test_content_guard_can_be_disabled(self):
        payload = csv_row(1_790_000_000)
        with mock.patch.dict(os.environ, {'TELEMETRY_CONTENT_DEDUP': '0'}):
            bms_mqtt_logger.insert_telemetry_data(payload, TOPIC)
            bms_mqtt_logger.insert_telemetry_data(payload, TOPIC)
        self.assertEqual(len(self.rows()), 2)

    def test_legacy_duplicate_check_uses_the_device_timestamp_index(self):
        conn = database_queries.create_db_connection(use_row_factory=False)
        try:
            plan = " ".join(
                row[-1] for row in conn.execute(
                    "EXPLAIN QUERY PLAN " + bms_mqtt_logger.LEGACY_DUPLICATE_SQL,
                    [None] * len(bms_mqtt_logger.EXPECTED_COLUMNS),
                )
            )
        finally:
            conn.close()
        self.assertIn("USING INDEX", plan)
        self.assertNotIn("SCAN", plan)

    # --- Mixed fleet and history ----------------------------------------------

    def test_legacy_and_identified_deliveries_coexist(self):
        bms_mqtt_logger.insert_telemetry_data(csv_row(1_789_999_990, elapsed=90), TOPIC)
        bms_mqtt_logger.insert_telemetry_data(envelope(10), TOPIC)
        bms_mqtt_logger.insert_telemetry_data(envelope(10), TOPIC)
        rows = self.rows()
        self.assertEqual(len(rows), 2)
        self.assertIsNone(rows[0]['delivery_sequence'])
        self.assertEqual(rows[1]['delivery_sequence'], 10)

    def test_migration_leaves_historical_duplicates_intact(self):
        conn = database_queries.create_db_connection()
        try:
            for _ in range(3):
                conn.execute(
                    "INSERT INTO bms_telemetry (bms_id, timestamp, pack_voltage_v) "
                    "VALUES (?, ?, ?)",
                    (DEVICE, 1_780_000_000, 13.2),
                )
            conn.commit()
        finally:
            conn.close()
        database_queries.ensure_database_schema()
        self.assertEqual(len(self.rows("timestamp = ?", (1_780_000_000,))), 3)

    def test_counters_reach_the_health_state(self):
        bms_mqtt_logger.insert_telemetry_data(envelope(1), TOPIC)
        bms_mqtt_logger.insert_telemetry_data(envelope(1), TOPIC)
        state = self.health_state.call_args.kwargs
        self.assertGreaterEqual(state['telemetry_identified_duplicates_suppressed'], 1)
        self.assertGreaterEqual(state['telemetry_rows_inserted'], 1)

    def test_on_message_passes_the_topic_through(self):
        message = SimpleNamespace(
            topic="bms/telemetry/gw-other",
            payload=envelope(1).encode('utf-8'),
            retain=False,
        )
        bms_mqtt_logger.on_message(None, None, message)
        self.assertEqual(self.rows(), [])
        message.topic = TOPIC
        bms_mqtt_logger.on_message(None, None, message)
        self.assertEqual(len(self.rows()), 1)


if __name__ == '__main__':
    unittest.main()
