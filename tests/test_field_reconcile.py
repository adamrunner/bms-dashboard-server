import json
import sys
import unittest
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "tools"))

import field_reconcile


def telemetry_row(
    device_id: str,
    timestamp: int,
    elapsed: int,
    voltage: str = "13.20",
) -> str:
    return (
        f"{device_id},{timestamp},{elapsed},00:00:01,1.0,{voltage},-3.8,87,-50.2,"
        "100,4,53,4,3.295,1,3.305,4,0.010,2,21.9,25.4,1,1,"
        "3.300,3.301,3.299,3.302,22.0,25.3"
    )


class FieldReconcileTests(unittest.TestCase):
    def test_legacy_rows_use_numeric_canonical_identity(self):
        first = field_reconcile.parse_payload(
            telemetry_row("gw-test", 100, 1, "13.20")
        )[0]
        second = field_reconcile.parse_payload(
            telemetry_row("gw-test", 100, 1, "13.2")
        )[0]
        self.assertEqual(first.row_identity, second.row_identity)

    def test_schema_v2_envelope_preserves_delivery_identity(self):
        envelope = {
            "schema_version": 2,
            "device_id": "gw-test",
            "boot_id": "boot-a",
            "sequence": 7,
            "captured_at": 0,
            "timestamp_valid": False,
            "csv": telemetry_row("gw-test", 0, 3),
        }
        record = field_reconcile.parse_payload(json.dumps(envelope))[0]
        self.assertEqual(record.delivery_identity, "gw-test:boot-a:7")
        self.assertEqual(record.captured_at, 0)

    def test_reconcile_summarizes_duplicates_segments_and_replay_delay(self):
        card = []
        for timestamp, elapsed, voltage in [
            (0, 1, "13.1"),
            (100, 2, "13.2"),
            (140, 3, "13.3"),
            (200, 1, "13.4"),
        ]:
            card.extend(
                field_reconcile.parse_payload(
                    telemetry_row("gw-test", timestamp, elapsed, voltage)
                )
            )
        production = [
            field_reconcile.TelemetryRecord(
                **{
                    **record.__dict__,
                    "received_at": record.captured_at + 5
                    if record.captured_at > 0
                    else None,
                }
            )
            for record in card[1:]
        ]
        production.append(production[-1])
        production.extend(
            field_reconcile.parse_payload(
                telemetry_row("gw-test", 300, 2, "13.5"),
                received_at=310,
            )
        )

        report = field_reconcile.reconcile(card, production)

        self.assertEqual(report["card_rows"], 4)
        self.assertEqual(report["card_only_rows"], 1)
        self.assertEqual(report["production_only_unique_rows"], 1)
        self.assertEqual(report["production_duplicate_excess"], 1)
        self.assertEqual(report["timestamp_zero_rows"], 1)
        self.assertEqual(report["boot_segments"], 2)
        self.assertEqual(report["largest_gap_seconds"], 40)
        self.assertEqual(report["replay_delay_seconds"]["samples"], 5)

    def test_documented_baseline_reproduces_derived_counts(self):
        fixture = (
            REPOSITORY_ROOT
            / "test_fixtures"
            / "field_reliability"
            / "2026-07-26-baseline.json"
        )
        report = field_reconcile.verify_baseline(fixture)
        self.assertTrue(report["verified"])
        self.assertEqual(report["counts"]["card_rows"], 27345)
        self.assertEqual(report["derived_counts"]["production_rows"], 27429)
        self.assertEqual(
            report["derived_counts"]["production_unique_rows"], 26680
        )

    def test_duplicate_report_separates_copies_from_same_second_samples(self):
        import tempfile

        import database_queries

        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "report.db"
            original = database_queries.DATABASE_PATH
            database_queries.DATABASE_PATH = str(db_path)
            try:
                database_queries.ensure_database_schema()
                conn = database_queries.create_db_connection()
                insert = (
                    "INSERT INTO bms_telemetry (bms_id, timestamp, elapsed_seconds, "
                    "pack_voltage_v, delivery_boot_id, delivery_sequence) "
                    "VALUES (?, ?, ?, 13.2, ?, ?)"
                )
                rows = [
                    ("gw-a", 1000, 10, None, None),
                    ("gw-a", 1000, 10, None, None),  # redelivered copy
                    ("gw-a", 1000, 3, None, None),   # distinct sample, same second
                    ("gw-a", 1010, 20, "b1", 1),
                    ("gw-b", 1010, 20, None, None),  # other device, ignored
                ]
                conn.executemany(insert, rows)
                conn.commit()
                conn.close()
            finally:
                database_queries.DATABASE_PATH = original

            report = field_reconcile.duplicate_report(db_path, "gw-a")

        self.assertEqual(report["totals"], {
            "db_rows": 4,
            "timestamp_duplicate_excess": 2,
            "content_duplicate_excess": 1,
        })
        self.assertEqual(report["identified_deliveries"], [{
            "delivery_boot_id": "b1",
            "rows": 1,
            "distinct_sequences": 1,
            "duplicate_excess": 0,
        }])


if __name__ == "__main__":
    unittest.main()
