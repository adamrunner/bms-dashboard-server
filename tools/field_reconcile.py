#!/usr/bin/env python3
"""Reconcile field SD telemetry with an exported production data set.

The tool deliberately uses only Python's standard library. It accepts legacy
CSV payloads and schema-v2 MQTT envelopes, preserves delivery identities when
present, and emits a machine-readable report. Raw field data is input-only and
must not be committed.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import statistics
import sys
from collections import Counter
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Iterable


FIXED_COLUMN_COUNT = 23
STRING_COLUMNS = {0, 3}
BOOLEAN_COLUMNS = {21, 22}


@dataclass(frozen=True)
class TelemetryRecord:
    device_id: str
    captured_at: int
    elapsed_seconds: int
    row_identity: str
    delivery_identity: str | None = None
    boot_id: str | None = None
    sequence: int | None = None
    received_at: int | None = None


def _canonical_number(value: str) -> str:
    try:
        number = Decimal(value.strip())
    except InvalidOperation as exc:
        raise ValueError(f"invalid numeric value {value!r}") from exc
    if not number.is_finite():
        raise ValueError(f"non-finite numeric value {value!r}")
    if number == 0:
        return "0"
    return format(number.normalize(), "f")


def normalize_legacy_csv(csv_line: str) -> tuple[list[str], str]:
    """Validate one gateway row and return canonical fields plus its identity."""
    rows = list(csv.reader(io.StringIO(csv_line.strip())))
    if len(rows) != 1:
        raise ValueError("expected exactly one CSV row")
    row = rows[0]
    if len(row) < FIXED_COLUMN_COUNT:
        raise ValueError(
            f"row has {len(row)} columns; expected at least {FIXED_COLUMN_COUNT}"
        )
    try:
        cell_count = int(row[12])
        temp_count = int(row[18])
    except ValueError as exc:
        raise ValueError("cell_count and temp_count must be integers") from exc
    expected_width = FIXED_COLUMN_COUNT + cell_count + temp_count
    if cell_count < 0 or temp_count < 0 or len(row) != expected_width:
        raise ValueError(
            f"row has {len(row)} columns; counts require exactly {expected_width}"
        )

    canonical: list[str] = []
    for index, raw_value in enumerate(row):
        value = raw_value.strip()
        if index in STRING_COLUMNS:
            canonical.append(value)
        elif index in BOOLEAN_COLUMNS:
            lowered = value.lower()
            if lowered not in {"0", "1", "true", "false"}:
                raise ValueError(f"invalid boolean value {value!r}")
            canonical.append("1" if lowered in {"1", "true"} else "0")
        else:
            canonical.append(_canonical_number(value))

    digest_input = json.dumps(
        canonical, ensure_ascii=True, separators=(",", ":")
    ).encode("utf-8")
    return canonical, hashlib.sha256(digest_input).hexdigest()


def parse_payload(
    payload: str,
    *,
    received_at: int | None = None,
) -> list[TelemetryRecord]:
    """Parse legacy CSV rows or one schema-v2 telemetry envelope."""
    stripped = payload.strip()
    if not stripped:
        return []

    if stripped.startswith("{"):
        try:
            envelope = json.loads(stripped)
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid envelope JSON: {exc.msg}") from exc
        if not isinstance(envelope, dict) or envelope.get("schema_version") != 2:
            raise ValueError("expected a schema-v2 telemetry envelope")
        device_id = envelope.get("device_id")
        boot_id = envelope.get("boot_id")
        sequence = envelope.get("sequence")
        csv_line = envelope.get("csv")
        if not isinstance(device_id, str) or not device_id:
            raise ValueError("envelope device_id must be a non-empty string")
        if not isinstance(boot_id, str) or not boot_id:
            raise ValueError("envelope boot_id must be a non-empty string")
        if type(sequence) is not int or sequence < 0:
            raise ValueError("envelope sequence must be a non-negative integer")
        if not isinstance(csv_line, str):
            raise ValueError("envelope csv must be a string")
        canonical, row_identity = normalize_legacy_csv(csv_line)
        if canonical[0] != device_id:
            raise ValueError("envelope and CSV device IDs do not match")
        captured_at = int(canonical[1])
        declared_capture = envelope.get("captured_at")
        if type(declared_capture) is not int or declared_capture != captured_at:
            raise ValueError("envelope captured_at does not match the CSV timestamp")
        timestamp_valid = envelope.get("timestamp_valid")
        if type(timestamp_valid) is not bool:
            raise ValueError("envelope timestamp_valid must be boolean")
        if timestamp_valid != (captured_at > 0):
            raise ValueError("envelope timestamp_valid contradicts captured_at")
        return [
            TelemetryRecord(
                device_id=device_id,
                captured_at=captured_at,
                elapsed_seconds=int(canonical[2]),
                row_identity=row_identity,
                delivery_identity=f"{device_id}:{boot_id}:{sequence}",
                boot_id=boot_id,
                sequence=sequence,
                received_at=received_at,
            )
        ]

    records = []
    for line in stripped.splitlines():
        if not line.strip():
            continue
        canonical, row_identity = normalize_legacy_csv(line)
        records.append(
            TelemetryRecord(
                device_id=canonical[0],
                captured_at=int(canonical[1]),
                elapsed_seconds=int(canonical[2]),
                row_identity=row_identity,
                received_at=received_at,
            )
        )
    return records


def load_card_directory(path: Path) -> list[TelemetryRecord]:
    records: list[TelemetryRecord] = []
    for csv_path in sorted(path.glob("*.csv")):
        with csv_path.open("r", encoding="utf-8", newline="") as handle:
            next(handle, None)  # daily files have one human-readable header
            for line_number, line in enumerate(handle, 2):
                try:
                    records.extend(parse_payload(line))
                except ValueError as exc:
                    raise ValueError(
                        f"{csv_path}:{line_number}: {exc}"
                    ) from exc
    return records


def load_production_jsonl(path: Path) -> list[TelemetryRecord]:
    """Load JSONL records shaped as {"payload": ..., "received_at": ...}."""
    records: list[TelemetryRecord] = []
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                item = json.loads(line)
                payload = item["payload"]
                received_at = item.get("received_at")
            except (json.JSONDecodeError, KeyError, TypeError) as exc:
                raise ValueError(f"{path}:{line_number}: invalid export row") from exc
            if not isinstance(payload, str):
                raise ValueError(f"{path}:{line_number}: payload must be a string")
            records.extend(parse_payload(payload, received_at=received_at))
    return records


def _summarize_segments(records: Iterable[TelemetryRecord]) -> dict:
    segment_count = 0
    gaps: list[int] = []
    previous: TelemetryRecord | None = None
    for record in records:
        if (
            previous is None
            or previous.device_id != record.device_id
            or record.elapsed_seconds < previous.elapsed_seconds
        ):
            segment_count += 1
        elif record.captured_at > 0 and previous.captured_at > 0:
            gap = record.captured_at - previous.captured_at
            if gap > 15:
                gaps.append(gap)
        previous = record
    return {
        "boot_segments": segment_count,
        "gap_count_over_15s": len(gaps),
        "largest_gap_seconds": max(gaps, default=0),
    }


def reconcile(
    card_records: list[TelemetryRecord],
    production_records: list[TelemetryRecord],
) -> dict:
    card_counts = Counter(record.row_identity for record in card_records)
    production_counts = Counter(record.row_identity for record in production_records)
    card_ids = set(card_counts)
    production_ids = set(production_counts)

    replay_delays = [
        record.received_at - record.captured_at
        for record in production_records
        if record.received_at is not None and record.captured_at > 0
    ]
    delivery_counts = Counter(
        record.delivery_identity
        for record in production_records
        if record.delivery_identity is not None
    )

    summary = {
        "card_rows": len(card_records),
        "card_unique_rows": len(card_ids),
        "production_rows": len(production_records),
        "production_unique_rows": len(production_ids),
        "matched_unique_rows": len(card_ids & production_ids),
        "card_only_rows": sum(card_counts[key] for key in card_ids - production_ids),
        "production_only_unique_rows": len(production_ids - card_ids),
        "production_duplicate_excess": sum(
            count - 1 for count in production_counts.values() if count > 1
        ),
        "identified_delivery_duplicate_excess": sum(
            count - 1 for count in delivery_counts.values() if count > 1
        ),
        "timestamp_zero_rows": sum(
            record.captured_at <= 0 for record in card_records
        ),
        "replay_delay_seconds": {
            "samples": len(replay_delays),
            "maximum": max(replay_delays, default=None),
            "median": statistics.median(replay_delays) if replay_delays else None,
        },
    }
    summary.update(_summarize_segments(card_records))
    return summary


def verify_baseline(path: Path) -> dict:
    fixture = json.loads(path.read_text(encoding="utf-8"))
    counts = fixture["counts"]
    derived = {
        "production_unique_rows": (
            counts["card_rows"]
            - counts["card_only_rows"]
            + counts["production_only_unique_rows"]
        ),
        "production_rows": (
            counts["card_rows"]
            - counts["card_only_rows"]
            + counts["production_only_unique_rows"]
            + counts["production_duplicate_excess"]
        ),
    }
    expected = fixture["derived_counts"]
    if derived != expected:
        raise ValueError(f"baseline derived counts differ: {derived} != {expected}")
    return {
        "baseline_date": fixture["evidence_date"],
        "verified": True,
        "counts": counts,
        "derived_counts": derived,
        "source_hashes": fixture["source_hashes"],
        "versions": fixture["versions"],
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--card-dir", type=Path)
    parser.add_argument("--production-jsonl", type=Path)
    parser.add_argument("--verify-baseline", type=Path)
    parser.add_argument("--output", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.verify_baseline:
            if args.card_dir or args.production_jsonl:
                raise ValueError("--verify-baseline cannot be combined with raw inputs")
            report = verify_baseline(args.verify_baseline)
        else:
            if not args.card_dir or not args.production_jsonl:
                raise ValueError(
                    "--card-dir and --production-jsonl are required together"
                )
            report = {
                "card_source": str(args.card_dir),
                "production_source": str(args.production_jsonl),
                "summary": reconcile(
                    load_card_directory(args.card_dir),
                    load_production_jsonl(args.production_jsonl),
                ),
            }
    except (OSError, ValueError) as exc:
        print(f"field_reconcile: {exc}", file=sys.stderr)
        return 2

    serialized = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.write_text(serialized, encoding="utf-8")
    else:
        sys.stdout.write(serialized)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
