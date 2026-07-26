#!/usr/bin/env python3
"""
Database query functions for BMS telemetry data
"""

import sqlite3
import os
import math
from datetime import datetime, timedelta
from typing import List, Dict, Optional, Tuple

DATABASE_PATH = os.getenv("DATABASE_PATH", "bms_telemetry.db")

ALLOWED_BUCKET_SECONDS = [10, 30, 60, 180, 300, 600, 900, 1800]
RESOLUTION_SECONDS_MAP = {
    '10s': 10,
    '30s': 30,
    '1m': 60,
    '3m': 180,
    '5m': 300,
    '10m': 600,
    '15m': 900,
    '30m': 1800
}
AUTO_RAW_MAX_HOURS = 0.167

SCHEMA_PATH = os.path.join(os.path.dirname(__file__), 'bms_schema.sql')
DASHBOARD_VIEW_COLUMNS = [
    'timestamp',
    'pack_voltage_v',
    'pack_current_a',
    'state_of_charge_pct',
    'power_w',
    'cells_v_1',
    'cells_v_2',
    'cells_v_3',
    'cells_v_4',
    'cell_voltage_delta_v',
    'temps_c_1',
    'temps_c_2',
    'temps_c_3'
]
DASHBOARD_VIEW_COLUMN_SQL = ', '.join(DASHBOARD_VIEW_COLUMNS)
DEVICE_STATUS_VIEW_COLUMNS = [
    'id',
    'device_id',
    'schema_version',
    'firmware_version',
    'ota_slot',
    'pending_verify',
    'boot_id',
    'reset_reason',
    'idf_version',
    'build_date',
    'build_time',
    'status_seq',
    'reported_at',
    'time_source',
    'status_reason',
    'rollback_from_version',
    'rollback_target_version',
    'reported_online',
    'mqtt_retained',
    'received_at'
]
DEVICE_STATUS_VIEW_COLUMN_SQL = ', '.join(DEVICE_STATUS_VIEW_COLUMNS)
DEVICE_AVAILABILITY_VIEW_COLUMNS = [
    'id',
    'device_id',
    'boot_id',
    'online',
    'mqtt_retained',
    'received_at',
]
DEVICE_AVAILABILITY_VIEW_COLUMN_SQL = ', '.join(DEVICE_AVAILABILITY_VIEW_COLUMNS)
DEVICE_STATUS_V2_COLUMNS = {
    'status_seq': 'INTEGER',
    'reported_at': 'INTEGER',
    'time_source': 'TEXT',
    'status_reason': 'TEXT',
    'rollback_from_version': 'TEXT',
    'rollback_target_version': 'TEXT',
}


def create_db_connection(*, use_row_factory: bool = True) -> sqlite3.Connection:
    """Create a SQLite connection configured for concurrent read/write access."""
    conn = sqlite3.connect(DATABASE_PATH, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=10000")
    if use_row_factory:
        conn.row_factory = sqlite3.Row
    return conn


def ensure_database_schema() -> None:
    """Create the telemetry schema and indexes if they do not already exist."""
    db_dir = os.path.dirname(DATABASE_PATH)
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)

    conn = create_db_connection(use_row_factory=False)
    try:
        with open(SCHEMA_PATH, 'r', encoding='utf-8') as schema_file:
            conn.executescript(schema_file.read())
        existing_columns = {
            row[1]
            for row in conn.execute("PRAGMA table_info(device_status_checkins)")
        }
        for column, sql_type in DEVICE_STATUS_V2_COLUMNS.items():
            if column not in existing_columns:
                conn.execute(
                    f"ALTER TABLE device_status_checkins ADD COLUMN {column} {sql_type}"
                )
        conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_device_status_exact_event
            ON device_status_checkins(device_id, boot_id, status_seq)
            WHERE status_seq IS NOT NULL
            """
        )
        conn.commit()
    finally:
        conn.close()


def validate_database_schema() -> None:
    """Raise if an expected application table is missing."""
    conn = create_db_connection(use_row_factory=False)
    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT name
            FROM sqlite_master
            WHERE type = 'table'
              AND name IN (
                  'bms_telemetry',
                  'device_status_checkins',
                  'device_availability_events'
              )
            """
        )
        found_tables = {row[0] for row in cursor.fetchall()}
        expected_tables = {
            'bms_telemetry',
            'device_status_checkins',
            'device_availability_events',
        }
        missing_tables = expected_tables - found_tables
        if missing_tables:
            raise RuntimeError(
                f"missing database tables {sorted(missing_tables)} in: {DATABASE_PATH}"
            )
    finally:
        conn.close()


def insert_device_status_checkin(status: Dict) -> Optional[int]:
    """Insert status, exactly deduplicating v2 and heuristically deduplicating v1."""
    conn = create_db_connection(use_row_factory=False)
    try:
        cursor = conn.cursor()
        if status.get('status_seq') is None and status['mqtt_retained']:
            cursor.execute(
                """
                SELECT id
                FROM device_status_checkins
                WHERE device_id = ?
                  AND boot_id = ?
                  AND pending_verify = ?
                  AND firmware_version = ?
                  AND payload_sha256 = ?
                LIMIT 1
                """,
                (
                    status['device_id'],
                    status['boot_id'],
                    status['pending_verify'],
                    status['firmware_version'],
                    status['payload_sha256']
                )
            )
            existing = cursor.fetchone()
            if existing:
                return None

        cursor.execute(
            """
            INSERT INTO device_status_checkins (
                device_id, schema_version, firmware_version, ota_slot,
                pending_verify, boot_id, reset_reason, idf_version,
                build_date, build_time, status_seq, reported_at, time_source,
                status_reason, rollback_from_version, rollback_target_version,
                reported_online, mqtt_retained, payload_sha256, raw_payload,
                received_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT DO NOTHING
            """,
            (
                status['device_id'],
                status['schema_version'],
                status['firmware_version'],
                status['ota_slot'],
                status['pending_verify'],
                status['boot_id'],
                status['reset_reason'],
                status.get('idf_version'),
                status.get('build_date'),
                status.get('build_time'),
                status.get('status_seq'),
                status.get('reported_at'),
                status.get('time_source'),
                status.get('status_reason'),
                status.get('rollback_from_version'),
                status.get('rollback_target_version'),
                status['reported_online'],
                status['mqtt_retained'],
                status['payload_sha256'],
                status['raw_payload'],
                status['received_at']
            )
        )
        if cursor.rowcount == 0:
            return None
        row_id = cursor.lastrowid
        conn.commit()
        return row_id
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _normalize_status_row(row: sqlite3.Row) -> Dict:
    """Convert SQLite integer booleans in a device-status row to JSON booleans."""
    result = dict(row)
    result['pending_verify'] = bool(result['pending_verify'])
    result['reported_online'] = bool(result['reported_online'])
    result['mqtt_retained'] = bool(result['mqtt_retained'])
    return result


def get_latest_device_status(device_id: str) -> Optional[Dict]:
    """Get the most recently received status check-in for one device."""
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            f"""
            SELECT {DEVICE_STATUS_VIEW_COLUMN_SQL}
            FROM device_status_checkins
            WHERE device_id = ?
            ORDER BY received_at DESC, id DESC
            LIMIT 1
            """,
            (device_id,)
        )
        row = cursor.fetchone()
        return _normalize_status_row(row) if row else None
    finally:
        conn.close()


def get_latest_status_record_id() -> Optional[int]:
    """Get the most recent device-status record ID."""
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT MAX(id) AS latest_id FROM device_status_checkins")
        row = cursor.fetchone()
        return row['latest_id'] if row else None
    finally:
        conn.close()


def get_device_status_history(
    device_id: str,
    limit: int = 50,
    before_id: Optional[int] = None
) -> Tuple[List[Dict], Optional[int]]:
    """Return stable, newest-first status history with ID-based pagination."""
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        params = [device_id]
        before_clause = ""
        if before_id is not None:
            before_clause = "AND id < ?"
            params.append(before_id)
        params.append(limit + 1)
        cursor.execute(
            f"""
            SELECT {DEVICE_STATUS_VIEW_COLUMN_SQL}
            FROM device_status_checkins
            WHERE device_id = ?
              {before_clause}
            ORDER BY id DESC
            LIMIT ?
            """,
            params
        )
        rows = cursor.fetchall()
        has_more = len(rows) > limit
        page_rows = rows[:limit]
        records = [_normalize_status_row(row) for row in page_rows]
        next_before_id = records[-1]['id'] if has_more and records else None
        return records, next_before_id
    finally:
        conn.close()


def get_fleet_status() -> List[Dict]:
    """Return one bounded latest-state row per known device in one SQL query."""
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            WITH devices AS (
                SELECT bms_id AS device_id FROM bms_telemetry
                UNION
                SELECT device_id FROM device_status_checkins
                UNION
                SELECT device_id FROM device_availability_events
            ),
            ranked_status AS (
                SELECT
                    id,
                    device_id,
                    schema_version,
                    firmware_version,
                    ota_slot,
                    pending_verify,
                    boot_id,
                    reset_reason,
                    status_seq,
                    reported_at,
                    time_source,
                    status_reason,
                    mqtt_retained,
                    received_at,
                    ROW_NUMBER() OVER (
                        PARTITION BY device_id
                        ORDER BY received_at DESC, id DESC
                    ) AS row_num
                FROM device_status_checkins
            ),
            ranked_availability AS (
                SELECT
                    id,
                    device_id,
                    boot_id,
                    online,
                    mqtt_retained,
                    received_at,
                    ROW_NUMBER() OVER (
                        PARTITION BY device_id
                        ORDER BY received_at DESC, id DESC
                    ) AS row_num
                FROM device_availability_events
            ),
            latest_telemetry AS (
                SELECT
                    bms_id AS device_id,
                    MAX(NULLIF(timestamp, 0)) AS latest_telemetry_at
                FROM bms_telemetry
                GROUP BY bms_id
            )
            SELECT
                devices.device_id,
                status.id AS status_id,
                status.schema_version,
                status.firmware_version,
                status.ota_slot,
                status.pending_verify,
                status.boot_id,
                status.reset_reason,
                status.status_seq,
                status.reported_at,
                status.time_source,
                status.status_reason,
                status.mqtt_retained AS status_mqtt_retained,
                status.received_at AS status_received_at,
                availability.id AS availability_id,
                availability.boot_id AS availability_boot_id,
                availability.online AS mqtt_online,
                availability.mqtt_retained AS availability_mqtt_retained,
                availability.received_at AS availability_received_at,
                telemetry.latest_telemetry_at
            FROM devices
            LEFT JOIN ranked_status AS status
                ON status.device_id = devices.device_id
               AND status.row_num = 1
            LEFT JOIN ranked_availability AS availability
                ON availability.device_id = devices.device_id
               AND availability.row_num = 1
            LEFT JOIN latest_telemetry AS telemetry
                ON telemetry.device_id = devices.device_id
            ORDER BY devices.device_id ASC
            """
        )
        records = []
        for row in cursor.fetchall():
            record = dict(row)
            if record['pending_verify'] is not None:
                record['pending_verify'] = bool(record['pending_verify'])
            if record['status_mqtt_retained'] is not None:
                record['status_mqtt_retained'] = bool(
                    record['status_mqtt_retained']
                )
            if record['mqtt_online'] is not None:
                record['mqtt_online'] = bool(record['mqtt_online'])
            if record['availability_mqtt_retained'] is not None:
                record['availability_mqtt_retained'] = bool(
                    record['availability_mqtt_retained']
                )
            records.append(record)
        return records
    finally:
        conn.close()


def insert_device_availability_event(availability: Dict) -> Optional[int]:
    """Insert a real availability transition, suppressing repeated state."""
    conn = create_db_connection(use_row_factory=False)
    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT boot_id, online
            FROM device_availability_events
            WHERE device_id = ?
            ORDER BY received_at DESC, id DESC
            LIMIT 1
            """,
            (availability['device_id'],)
        )
        latest = cursor.fetchone()
        if (
            latest
            and latest[0] == availability['boot_id']
            and bool(latest[1]) == availability['online']
        ):
            return None

        cursor.execute(
            """
            INSERT INTO device_availability_events (
                device_id, boot_id, online, mqtt_retained,
                payload_sha256, raw_payload, received_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                availability['device_id'],
                availability['boot_id'],
                availability['online'],
                availability['mqtt_retained'],
                availability['payload_sha256'],
                availability['raw_payload'],
                availability['received_at'],
            )
        )
        row_id = cursor.lastrowid
        conn.commit()
        return row_id
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _normalize_availability_row(row: sqlite3.Row) -> Dict:
    """Convert SQLite integer booleans in an availability row."""
    result = dict(row)
    result['online'] = bool(result['online'])
    result['mqtt_retained'] = bool(result['mqtt_retained'])
    return result


def get_latest_device_availability(device_id: str) -> Optional[Dict]:
    """Get the latest broker-session state observed for one device."""
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            f"""
            SELECT {DEVICE_AVAILABILITY_VIEW_COLUMN_SQL}
            FROM device_availability_events
            WHERE device_id = ?
            ORDER BY received_at DESC, id DESC
            LIMIT 1
            """,
            (device_id,)
        )
        row = cursor.fetchone()
        return _normalize_availability_row(row) if row else None
    finally:
        conn.close()


def get_latest_availability_record_id() -> Optional[int]:
    """Get the most recent availability-event row ID."""
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT MAX(id) AS latest_id FROM device_availability_events")
        row = cursor.fetchone()
        return row['latest_id'] if row else None
    finally:
        conn.close()


def resolve_bucket_seconds(hours: float, resolution: str = 'auto', target_points: int = 300) -> int:
    """Resolve requested resolution into a bucket size in seconds."""
    if resolution in RESOLUTION_SECONDS_MAP:
        return RESOLUTION_SECONDS_MAP[resolution]

    if resolution == 'auto':
        # Explicit stepped defaults tuned for dashboard readability.
        if hours <= 0.167:  # 10 minutes
            return 10
        if hours <= 0.5:  # 30 minutes
            return 30
        if hours <= 1:
            return 60
        if hours <= 6:
            return 600
        return 1800

    # Fallback: compute from target points and snap to nearest supported bucket.
    ideal = max(10, int(math.ceil((hours * 3600.0) / max(50, target_points))))
    return min(ALLOWED_BUCKET_SECONDS, key=lambda value: abs(value - ideal))


def should_aggregate_view(hours: float, resolution: str, bucket_seconds: int) -> bool:
    """Return True when a dashboard view should be bucketed before rendering."""
    if resolution in RESOLUTION_SECONDS_MAP:
        return True
    return hours > AUTO_RAW_MAX_HOURS or bucket_seconds > 10


def _fetch_raw_data(hours: float, bms_id: Optional[str] = None) -> List[Dict]:
    """Get raw telemetry data for the specified number of hours."""
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cutoff_time = int((datetime.now() - timedelta(hours=hours)).timestamp())

        if bms_id:
            cursor.execute("""
                SELECT * FROM bms_telemetry
                WHERE timestamp >= ? AND bms_id = ?
                ORDER BY timestamp ASC
            """, (cutoff_time, bms_id))
        else:
            cursor.execute("""
                SELECT * FROM bms_telemetry
                WHERE timestamp >= ?
                ORDER BY timestamp ASC
            """, (cutoff_time,))

        rows = cursor.fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def _fetch_dashboard_raw_data(hours: float, bms_id: Optional[str] = None) -> List[Dict]:
    """Get raw dashboard telemetry data with only chart-required columns."""
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cutoff_time = int((datetime.now() - timedelta(hours=hours)).timestamp())

        if bms_id:
            cursor.execute(f"""
                SELECT {DASHBOARD_VIEW_COLUMN_SQL}
                FROM bms_telemetry
                WHERE timestamp >= ? AND bms_id = ?
                ORDER BY timestamp ASC
            """, (cutoff_time, bms_id))
        else:
            cursor.execute(f"""
                SELECT {DASHBOARD_VIEW_COLUMN_SQL}
                FROM bms_telemetry
                WHERE timestamp >= ?
                ORDER BY timestamp ASC
            """, (cutoff_time,))

        rows = cursor.fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def _fetch_aggregated_data(hours: float, bucket_seconds: int, bms_id: Optional[str] = None) -> List[Dict]:
    """Get bucketed telemetry data where each row is an aggregated time bucket."""
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cutoff_time = int((datetime.now() - timedelta(hours=hours)).timestamp())

        if bms_id:
            cursor.execute("""
                SELECT
                    CAST((timestamp / ?) AS INTEGER) * ? AS timestamp,
                    COUNT(*) AS sample_count,
                    AVG(pack_voltage_v) AS pack_voltage_v,
                    AVG(pack_current_a) AS pack_current_a,
                    AVG(state_of_charge_pct) AS state_of_charge_pct,
                    AVG(power_w) AS power_w,
                    AVG(cells_v_1) AS cells_v_1,
                    AVG(cells_v_2) AS cells_v_2,
                    AVG(cells_v_3) AS cells_v_3,
                    AVG(cells_v_4) AS cells_v_4,
                    AVG(cell_voltage_delta_v) AS cell_voltage_delta_v,
                    AVG(temps_c_1) AS temps_c_1,
                    AVG(temps_c_2) AS temps_c_2,
                    AVG(temps_c_3) AS temps_c_3
                FROM bms_telemetry
                WHERE timestamp >= ? AND bms_id = ?
                GROUP BY CAST((timestamp / ?) AS INTEGER)
                ORDER BY timestamp ASC
            """, (bucket_seconds, bucket_seconds, cutoff_time, bms_id, bucket_seconds))
        else:
            cursor.execute("""
                SELECT
                    CAST((timestamp / ?) AS INTEGER) * ? AS timestamp,
                    COUNT(*) AS sample_count,
                    AVG(pack_voltage_v) AS pack_voltage_v,
                    AVG(pack_current_a) AS pack_current_a,
                    AVG(state_of_charge_pct) AS state_of_charge_pct,
                    AVG(power_w) AS power_w,
                    AVG(cells_v_1) AS cells_v_1,
                    AVG(cells_v_2) AS cells_v_2,
                    AVG(cells_v_3) AS cells_v_3,
                    AVG(cells_v_4) AS cells_v_4,
                    AVG(cell_voltage_delta_v) AS cell_voltage_delta_v,
                    AVG(temps_c_1) AS temps_c_1,
                    AVG(temps_c_2) AS temps_c_2,
                    AVG(temps_c_3) AS temps_c_3
                FROM bms_telemetry
                WHERE timestamp >= ?
                GROUP BY CAST((timestamp / ?) AS INTEGER)
                ORDER BY timestamp ASC
            """, (bucket_seconds, bucket_seconds, cutoff_time, bucket_seconds))

        rows = cursor.fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def get_telemetry_data_for_view(
    hours: float = 1,
    bms_id: Optional[str] = None,
    resolution: str = 'auto',
    target_points: int = 300
) -> Tuple[List[Dict], Dict]:
    """Get telemetry data and metadata for a dashboard view with optional bucketing."""
    bucket_seconds = resolve_bucket_seconds(hours, resolution, target_points)
    is_aggregated = should_aggregate_view(hours, resolution, bucket_seconds)

    if is_aggregated:
        data = _fetch_aggregated_data(hours, bucket_seconds, bms_id)
    else:
        data = _fetch_dashboard_raw_data(hours, bms_id)

    source_record_count = (
        sum(row.get('sample_count') or 0 for row in data)
        if is_aggregated
        else len(data)
    )

    metadata = {
        'resolution': resolution,
        'bucket_seconds': bucket_seconds,
        'is_aggregated': is_aggregated,
        'point_count': len(data),
        'source_record_count': source_record_count,
        'hours': hours,
        'bms_id': bms_id
    }
    return data, metadata


def get_latest_point_for_view(
    hours: float = 1,
    bms_id: Optional[str] = None,
    resolution: str = 'auto',
    target_points: int = 300
) -> Optional[Dict]:
    """Get the latest point for a client view (raw or aggregated)."""
    bucket_seconds = resolve_bucket_seconds(hours, resolution, target_points)
    if not should_aggregate_view(hours, resolution, bucket_seconds):
        return get_latest_dashboard_point(bms_id)
    cutoff_time = int((datetime.now() - timedelta(hours=hours)).timestamp())

    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        if bms_id:
            cursor.execute("""
                SELECT MAX(timestamp) AS latest_timestamp
                FROM bms_telemetry
                WHERE timestamp >= ? AND bms_id = ?
            """, (cutoff_time, bms_id))
        else:
            cursor.execute("""
                SELECT MAX(timestamp) AS latest_timestamp
                FROM bms_telemetry
                WHERE timestamp >= ?
            """, (cutoff_time,))

        latest_row = cursor.fetchone()
        latest_timestamp = latest_row['latest_timestamp'] if latest_row else None
        if latest_timestamp is None:
            return None

        bucket_start = int((latest_timestamp // bucket_seconds) * bucket_seconds)
        bucket_end = bucket_start + bucket_seconds

        if bms_id:
            cursor.execute("""
                SELECT
                    ? AS timestamp,
                    COUNT(*) AS sample_count,
                    AVG(pack_voltage_v) AS pack_voltage_v,
                    AVG(pack_current_a) AS pack_current_a,
                    AVG(state_of_charge_pct) AS state_of_charge_pct,
                    AVG(power_w) AS power_w,
                    AVG(cells_v_1) AS cells_v_1,
                    AVG(cells_v_2) AS cells_v_2,
                    AVG(cells_v_3) AS cells_v_3,
                    AVG(cells_v_4) AS cells_v_4,
                    AVG(temps_c_1) AS temps_c_1,
                    AVG(temps_c_2) AS temps_c_2,
                    AVG(temps_c_3) AS temps_c_3
                FROM bms_telemetry
                WHERE timestamp >= ? AND timestamp < ? AND bms_id = ?
            """, (bucket_start, bucket_start, bucket_end, bms_id))
        else:
            cursor.execute("""
                SELECT
                    ? AS timestamp,
                    COUNT(*) AS sample_count,
                    AVG(pack_voltage_v) AS pack_voltage_v,
                    AVG(pack_current_a) AS pack_current_a,
                    AVG(state_of_charge_pct) AS state_of_charge_pct,
                    AVG(power_w) AS power_w,
                    AVG(cells_v_1) AS cells_v_1,
                    AVG(cells_v_2) AS cells_v_2,
                    AVG(cells_v_3) AS cells_v_3,
                    AVG(cells_v_4) AS cells_v_4,
                    AVG(temps_c_1) AS temps_c_1,
                    AVG(temps_c_2) AS temps_c_2,
                    AVG(temps_c_3) AS temps_c_3
                FROM bms_telemetry
                WHERE timestamp >= ? AND timestamp < ?
            """, (bucket_start, bucket_start, bucket_end))

        aggregated_row = cursor.fetchone()
        if not aggregated_row:
            return None
        row_dict = dict(aggregated_row)
        if row_dict.get('pack_voltage_v') is None:
            return None
        return row_dict
    finally:
        conn.close()


def get_db_connection():
    """Get database connection"""
    try:
        return create_db_connection()
    except Exception as e:
        print(f"Database connection error: {e}")
        print(f"Database path: {DATABASE_PATH}")
        raise


def get_latest_reading(bms_id: Optional[str] = None) -> Optional[Dict]:
    """Get the most recent telemetry reading"""
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        if bms_id:
            cursor.execute("""
                SELECT * FROM bms_telemetry 
                WHERE bms_id = ?
                ORDER BY timestamp DESC 
                LIMIT 1
            """, (bms_id,))
        else:
            cursor.execute("""
                SELECT * FROM bms_telemetry 
                ORDER BY timestamp DESC 
                LIMIT 1
            """)
        row = cursor.fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def get_latest_timestamp(bms_id: Optional[str] = None) -> Optional[int]:
    """Get the most recent telemetry timestamp."""
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        if bms_id:
            cursor.execute("""
                SELECT MAX(timestamp) AS latest_timestamp
                FROM bms_telemetry
                WHERE bms_id = ?
            """, (bms_id,))
        else:
            cursor.execute("""
                SELECT MAX(timestamp) AS latest_timestamp
                FROM bms_telemetry
            """)
        row = cursor.fetchone()
        return row['latest_timestamp'] if row else None
    finally:
        conn.close()


def get_latest_record_id() -> Optional[int]:
    """Get the most recent telemetry record id."""
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT MAX(id) AS latest_id
            FROM bms_telemetry
        """)
        row = cursor.fetchone()
        return row['latest_id'] if row else None
    finally:
        conn.close()


def get_latest_dashboard_point(bms_id: Optional[str] = None) -> Optional[Dict]:
    """Get the latest telemetry row with only dashboard-view columns."""
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        if bms_id:
            cursor.execute(f"""
                SELECT {DASHBOARD_VIEW_COLUMN_SQL}
                FROM bms_telemetry
                WHERE bms_id = ?
                ORDER BY timestamp DESC
                LIMIT 1
            """, (bms_id,))
        else:
            cursor.execute(f"""
                SELECT {DASHBOARD_VIEW_COLUMN_SQL}
                FROM bms_telemetry
                ORDER BY timestamp DESC
                LIMIT 1
            """)
        row = cursor.fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def get_telemetry_data(hours: float = 1, bms_id: Optional[str] = None) -> List[Dict]:
    """Get telemetry data for the specified number of hours"""
    return _fetch_raw_data(hours, bms_id)


def get_pack_voltage_data(hours: float = 1, bms_id: Optional[str] = None) -> List[Dict]:
    """Get pack voltage data over time"""
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cutoff_time = int((datetime.now() - timedelta(hours=hours)).timestamp())
        
        if bms_id:
            cursor.execute("""
                SELECT timestamp, pack_voltage_v, pack_current_a, power_w
                FROM bms_telemetry 
                WHERE timestamp >= ? AND bms_id = ?
                ORDER BY timestamp ASC
            """, (cutoff_time, bms_id))
        else:
            cursor.execute("""
                SELECT timestamp, pack_voltage_v, pack_current_a, power_w
                FROM bms_telemetry 
                WHERE timestamp >= ? 
                ORDER BY timestamp ASC
            """, (cutoff_time,))
        
        rows = cursor.fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def get_cell_voltage_data(hours: float = 1, bms_id: Optional[str] = None) -> List[Dict]:
    """Get individual cell voltage data"""
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cutoff_time = int((datetime.now() - timedelta(hours=hours)).timestamp())
        
        if bms_id:
            cursor.execute("""
                SELECT timestamp, cells_v_1, cells_v_2, cells_v_3, cells_v_4,
                       min_cell_voltage_v, max_cell_voltage_v, cell_voltage_delta_v
                FROM bms_telemetry 
                WHERE timestamp >= ? AND bms_id = ?
                ORDER BY timestamp ASC
            """, (cutoff_time, bms_id))
        else:
            cursor.execute("""
                SELECT timestamp, cells_v_1, cells_v_2, cells_v_3, cells_v_4,
                       min_cell_voltage_v, max_cell_voltage_v, cell_voltage_delta_v
                FROM bms_telemetry 
                WHERE timestamp >= ? 
                ORDER BY timestamp ASC
            """, (cutoff_time,))
        
        rows = cursor.fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def get_temperature_data(hours: float = 1, bms_id: Optional[str] = None) -> List[Dict]:
    """Get temperature data"""
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cutoff_time = int((datetime.now() - timedelta(hours=hours)).timestamp())
        
        if bms_id:
            cursor.execute("""
                SELECT timestamp, temps_c_1, temps_c_2, temps_c_3,
                       min_temp_c, max_temp_c
                FROM bms_telemetry 
                WHERE timestamp >= ? AND bms_id = ?
                ORDER BY timestamp ASC
            """, (cutoff_time, bms_id))
        else:
            cursor.execute("""
                SELECT timestamp, temps_c_1, temps_c_2, temps_c_3,
                       min_temp_c, max_temp_c
                FROM bms_telemetry 
                WHERE timestamp >= ? 
                ORDER BY timestamp ASC
            """, (cutoff_time,))
        
        rows = cursor.fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def get_soc_and_capacity_data(hours: float = 1, bms_id: Optional[str] = None) -> List[Dict]:
    """Get state of charge and capacity data"""
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cutoff_time = int((datetime.now() - timedelta(hours=hours)).timestamp())
        
        if bms_id:
            cursor.execute("""
                SELECT timestamp, state_of_charge_pct, full_capacity_ah, total_energy_wh
                FROM bms_telemetry 
                WHERE timestamp >= ? AND bms_id = ?
                ORDER BY timestamp ASC
            """, (cutoff_time, bms_id))
        else:
            cursor.execute("""
                SELECT timestamp, state_of_charge_pct, full_capacity_ah, total_energy_wh
                FROM bms_telemetry 
                WHERE timestamp >= ? 
                ORDER BY timestamp ASC
            """, (cutoff_time,))
        
        rows = cursor.fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def get_statistics(hours: float = 24, bms_id: Optional[str] = None) -> Dict:
    """Get summary statistics for the specified time period"""
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cutoff_time = int((datetime.now() - timedelta(hours=hours)).timestamp())
        
        if bms_id:
            cursor.execute("""
                SELECT 
                    COUNT(*) as record_count,
                    MIN(pack_voltage_v) as min_pack_voltage,
                    MAX(pack_voltage_v) as max_pack_voltage,
                    AVG(pack_voltage_v) as avg_pack_voltage,
                    MIN(pack_current_a) as min_current,
                    MAX(pack_current_a) as max_current,
                    MIN(state_of_charge_pct) as min_soc,
                    MAX(state_of_charge_pct) as max_soc,
                    MIN(min_temp_c) as min_temperature,
                    MAX(max_temp_c) as max_temperature,
                    MIN(power_w) as min_power,
                    MAX(power_w) as max_power
                FROM bms_telemetry 
                WHERE timestamp >= ? AND bms_id = ?
            """, (cutoff_time, bms_id))
        else:
            cursor.execute("""
                SELECT 
                    COUNT(*) as record_count,
                    MIN(pack_voltage_v) as min_pack_voltage,
                    MAX(pack_voltage_v) as max_pack_voltage,
                    AVG(pack_voltage_v) as avg_pack_voltage,
                    MIN(pack_current_a) as min_current,
                    MAX(pack_current_a) as max_current,
                    MIN(state_of_charge_pct) as min_soc,
                    MAX(state_of_charge_pct) as max_soc,
                    MIN(min_temp_c) as min_temperature,
                    MAX(max_temp_c) as max_temperature,
                    MIN(power_w) as min_power,
                    MAX(power_w) as max_power
                FROM bms_telemetry 
                WHERE timestamp >= ?
            """, (cutoff_time,))
        
        row = cursor.fetchone()
        return dict(row) if row else {}
    finally:
        conn.close()


def get_recent_data_for_websocket(limit: int = 50, bms_id: Optional[str] = None) -> List[Dict]:
    """Get recent data optimized for WebSocket updates"""
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        if bms_id:
            cursor.execute("""
                SELECT timestamp, pack_voltage_v, pack_current_a, state_of_charge_pct,
                       power_w, min_temp_c, max_temp_c, cells_v_1, cells_v_2, 
                       cells_v_3, cells_v_4
                FROM bms_telemetry 
                WHERE bms_id = ?
                ORDER BY timestamp DESC 
                LIMIT ?
            """, (bms_id, limit))
        else:
            cursor.execute("""
                SELECT timestamp, pack_voltage_v, pack_current_a, state_of_charge_pct,
                       power_w, min_temp_c, max_temp_c, cells_v_1, cells_v_2, 
                       cells_v_3, cells_v_4
                FROM bms_telemetry 
                ORDER BY timestamp DESC 
                LIMIT ?
            """, (limit,))
        
        rows = cursor.fetchall()
        return [dict(row) for row in reversed(rows)]
    finally:
        conn.close()


def get_data_count(bms_id: Optional[str] = None) -> int:
    """Get total number of records in database"""
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        if bms_id:
            cursor.execute("SELECT COUNT(*) as count FROM bms_telemetry WHERE bms_id = ?", (bms_id,))
        else:
            cursor.execute("SELECT COUNT(*) as count FROM bms_telemetry")
        row = cursor.fetchone()
        return row['count'] if row else 0
    finally:
        conn.close()


def get_available_bms_ids() -> List[str]:
    """Get IDs observed in either telemetry or device status."""
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT device_id
            FROM (
                SELECT bms_id AS device_id FROM bms_telemetry
                UNION
                SELECT device_id FROM device_status_checkins
                UNION
                SELECT device_id FROM device_availability_events
            )
            ORDER BY device_id ASC
        """)
        rows = cursor.fetchall()
        return [row['device_id'] for row in rows]
    finally:
        conn.close()
