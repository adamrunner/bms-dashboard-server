#!/usr/bin/env python3
"""
BMS MQTT to SQLite Logger
Subscribes to MQTT telemetry data and stores it in SQLite database.
"""

import sqlite3
import csv
import io
import hashlib
import json
import logging
import sys
import os
import re
import time
from datetime import datetime
import paho.mqtt.client as mqtt
from database_queries import (
    ensure_database_schema,
    create_db_connection,
    insert_device_status_checkin,
    insert_device_availability_event,
)

DEVELOPMENT_ENV_NAMES = {'development', 'dev', 'local'}

# Expected CSV columns
EXPECTED_COLUMNS = [
    'bms_id', 'timestamp', 'elapsed_seconds', 'elapsed_hms', 'total_energy_wh',
    'pack_voltage_v', 'pack_current_a', 'state_of_charge_pct', 'power_w',
    'full_capacity_ah', 'peak_current_a', 'peak_power_w', 'cell_count',
    'min_cell_voltage_v', 'min_cell_num', 'max_cell_voltage_v', 'max_cell_num',
    'cell_voltage_delta_v', 'temp_count', 'min_temp_c', 'max_temp_c',
    'charging_enabled', 'discharging_enabled', 'cells_v_1', 'cells_v_2',
    'cells_v_3', 'cells_v_4', 'temps_c_1', 'temps_c_2', 'temps_c_3'
]

FIXED_COLUMN_COUNT = 23
MAX_CELL_COUNT = 4
MAX_TEMP_COUNT = 3

# Setup logging
logging.basicConfig(
    level=getattr(logging, os.getenv('LOG_LEVEL', 'INFO').upper(), logging.INFO),
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger(__name__)


def get_app_env() -> str:
    """Return the current application environment name."""
    return os.getenv('APP_ENV', 'development').strip().lower()


def is_development_env() -> bool:
    """Return True when running in development mode."""
    return get_app_env() in DEVELOPMENT_ENV_NAMES


def resolve_mqtt_credentials() -> tuple[str, str]:
    """Resolve MQTT credentials with explicit development behavior."""
    username = os.getenv('MQTT_USERNAME')
    password = os.getenv('MQTT_PASSWORD')

    if username and password:
        return username, password

    if username or password:
        raise RuntimeError("MQTT_USERNAME and MQTT_PASSWORD must both be set")

    if is_development_env():
        logger.warning(
            "MQTT credentials not set; using development-only fallback credentials"
        )
        return 'admin', 'password1234'

    raise RuntimeError(
        "MQTT_USERNAME and MQTT_PASSWORD must be set when APP_ENV is not development"
    )


# Configuration
MQTT_BROKER = os.getenv("MQTT_BROKER", "mosquitto")
MQTT_PORT = int(os.getenv("MQTT_PORT", "1883"))
MQTT_USERNAME, MQTT_PASSWORD = resolve_mqtt_credentials()
MQTT_TOPIC = os.getenv("MQTT_TOPIC", "bms/telemetry/+")
MQTT_STATUS_TOPIC = os.getenv("MQTT_STATUS_TOPIC", "bms/status/+")
MQTT_AVAILABILITY_TOPIC = os.getenv(
    "MQTT_AVAILABILITY_TOPIC",
    "bms/availability/+"
)
DATABASE_PATH = os.getenv("DATABASE_PATH", "bms_telemetry.db")

INSERT_COLUMNS = ', '.join(EXPECTED_COLUMNS)
INSERT_PLACEHOLDERS = ', '.join(['?' for _ in EXPECTED_COLUMNS])
INSERT_SQL = f"INSERT INTO bms_telemetry ({INSERT_COLUMNS}) VALUES ({INSERT_PLACEHOLDERS})"

STATUS_REQUIRED_STRING_FIELDS = {
    'device_id': 64,
    'firmware_version': 32,
    'ota_slot': 32,
    'boot_id': 64,
    'reset_reason': 64,
    'idf_version': 64,
    'build_date': 32,
    'build_time': 32
}
STATUS_V2_REASONS = {
    'boot',
    'time_synchronized',
    'mqtt_reconnected',
    'ota_pending_verify',
    'ota_verified',
    'rollback_detected',
}
STATUS_TIME_SOURCES = {'sntp', 'gnss'}
DEVICE_ID_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_-]*$')


def init_database():
    """Initialize SQLite database with schema"""
    try:
        # Ensure database directory exists
        db_dir = os.path.dirname(DATABASE_PATH)
        if db_dir and not os.path.exists(db_dir):
            os.makedirs(db_dir, exist_ok=True)
            logger.info(f"Created database directory: {db_dir}")
            
        ensure_database_schema()

        conn = create_db_connection(use_row_factory=False)
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM bms_telemetry")
        record_count = cursor.fetchone()[0]
        logger.info(f"Database schema ready with {record_count} records at: {DATABASE_PATH}")
        conn.close()
    except Exception as e:
        logger.error(f"Failed to initialize database: {e}")
        sys.exit(1)


def convert_value(value, column_name):
    """Convert string values to appropriate types"""
    if not value or value.strip() == '':
        return None
    
    value = value.strip()
    
    # Boolean columns
    if column_name in ['charging_enabled', 'discharging_enabled']:
        return value.lower() in ['true', '1', 'yes', 'on']
    
    # Integer columns
    if column_name in ['timestamp', 'cell_count', 'min_cell_num', 'max_cell_num', 'temp_count']:
        try:
            return int(float(value))
        except ValueError:
            return None
    
    # String columns
    if column_name in ['bms_id', 'elapsed_hms']:
        return value
    
    # Float columns (everything else)
    try:
        return float(value)
    except ValueError:
        return None


def normalize_telemetry_row(row):
    """Pad a variable-width gateway row to the commissioning DB schema."""
    if len(row) < FIXED_COLUMN_COUNT:
        raise ValueError(
            f"row has {len(row)} columns; expected at least {FIXED_COLUMN_COUNT}"
        )

    try:
        cell_count = int(row[12])
        temp_count = int(row[18])
    except (TypeError, ValueError) as exc:
        raise ValueError("cell_count and temp_count must be integers") from exc

    if not 0 <= cell_count <= MAX_CELL_COUNT:
        raise ValueError(
            f"cell_count {cell_count} exceeds supported range 0..{MAX_CELL_COUNT}"
        )
    if not 0 <= temp_count <= MAX_TEMP_COUNT:
        raise ValueError(
            f"temp_count {temp_count} exceeds supported range 0..{MAX_TEMP_COUNT}"
        )

    expected_width = FIXED_COLUMN_COUNT + cell_count + temp_count
    if len(row) != expected_width:
        raise ValueError(
            f"row has {len(row)} columns; counts require exactly {expected_width}"
        )

    cell_start = FIXED_COLUMN_COUNT
    temp_start = cell_start + cell_count
    cells = row[cell_start:temp_start]
    temps = row[temp_start:]

    return (
        row[:FIXED_COLUMN_COUNT]
        + cells
        + [''] * (MAX_CELL_COUNT - cell_count)
        + temps
        + [''] * (MAX_TEMP_COUNT - temp_count)
    )


def insert_telemetry_data(csv_data):
    """Parse CSV data and insert into database"""
    conn = None
    try:
        # Parse CSV data (no headers in MQTT data)
        csv_reader = csv.reader(io.StringIO(csv_data.strip()))

        conn = create_db_connection(use_row_factory=False)
        cursor = conn.cursor()
        rows_to_insert = []

        for row in csv_reader:
            try:
                normalized_row = normalize_telemetry_row(row)
            except ValueError as exc:
                logger.warning(f"Rejected telemetry row: {exc}")
                continue

            rows_to_insert.append([
                convert_value(value, EXPECTED_COLUMNS[i])
                for i, value in enumerate(normalized_row)
            ])

        if not rows_to_insert:
            logger.warning("No valid telemetry rows found in MQTT payload")
            return

        cursor.executemany(INSERT_SQL, rows_to_insert)
        rows_inserted = len(rows_to_insert)
        
        conn.commit()
        logger.info(f"Successfully inserted {rows_inserted} row(s) of telemetry data")
        
    except Exception as e:
        logger.error(f"Failed to insert telemetry data: {e}")
        if conn:
            conn.rollback()
    finally:
        if conn:
            conn.close()


def normalize_status_payload(payload: str, topic: str, mqtt_retained: bool = False) -> dict:
    """Validate and normalize one schema-v1 or schema-v2 status document."""
    try:
        parsed = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSON: {exc.msg}") from exc

    if not isinstance(parsed, dict):
        raise ValueError("status payload must be a JSON object")
    if type(parsed.get('schema_version')) is not int:
        raise ValueError("schema_version must be an integer")
    if parsed['schema_version'] not in (1, 2):
        raise ValueError(f"unsupported schema_version {parsed['schema_version']}")

    for field, max_length in STATUS_REQUIRED_STRING_FIELDS.items():
        value = parsed.get(field)
        if not isinstance(value, str) or not value:
            raise ValueError(f"{field} must be a non-empty string")
        if len(value) > max_length:
            raise ValueError(f"{field} exceeds {max_length} characters")

    for field in ('online', 'pending_verify'):
        if type(parsed.get(field)) is not bool:
            raise ValueError(f"{field} must be a boolean")

    device_id = parsed['device_id']
    if not DEVICE_ID_RE.fullmatch(device_id):
        raise ValueError("device_id contains unsupported characters")

    topic_device_id = topic.rsplit('/', 1)[-1]
    if topic_device_id != device_id:
        raise ValueError(
            f"topic device ID {topic_device_id!r} does not match payload {device_id!r}"
        )

    status_seq = None
    reported_at = None
    time_source = None
    status_reason = None
    rollback_from_version = None
    rollback_target_version = None
    if parsed['schema_version'] == 2:
        status_seq = parsed.get('status_seq')
        if type(status_seq) is not int or not 1 <= status_seq <= 0xffffffff:
            raise ValueError("status_seq must be an unsigned 32-bit integer greater than zero")

        reported_at = parsed.get('reported_at')
        time_source = parsed.get('time_source')
        if reported_at is None:
            if time_source is not None:
                raise ValueError("time_source must be null when reported_at is null")
        else:
            if type(reported_at) is not int or reported_at <= 0:
                raise ValueError("reported_at must be a positive Unix timestamp or null")
            if time_source not in STATUS_TIME_SOURCES:
                raise ValueError("time_source must be sntp or gnss when reported_at is set")

        status_reason = parsed.get('status_reason')
        if status_reason not in STATUS_V2_REASONS:
            raise ValueError("status_reason is unsupported")

        rollback_from_version = parsed.get('rollback_from_version')
        rollback_target_version = parsed.get('rollback_target_version')
        for field, value in (
            ('rollback_from_version', rollback_from_version),
            ('rollback_target_version', rollback_target_version),
        ):
            if value is not None and (
                not isinstance(value, str) or not value or len(value) > 32
            ):
                raise ValueError(f"{field} must be a non-empty string up to 32 characters or null")
        if status_reason == 'rollback_detected' and (
            rollback_from_version is None or rollback_target_version is None
        ):
            raise ValueError("rollback_detected requires both rollback version fields")

    canonical_payload = json.dumps(parsed, sort_keys=True, separators=(',', ':'))
    return {
        'device_id': device_id,
        'schema_version': parsed['schema_version'],
        'firmware_version': parsed['firmware_version'],
        'ota_slot': parsed['ota_slot'],
        'pending_verify': parsed['pending_verify'],
        'boot_id': parsed['boot_id'],
        'reset_reason': parsed['reset_reason'],
        'idf_version': parsed['idf_version'],
        'build_date': parsed['build_date'],
        'build_time': parsed['build_time'],
        'status_seq': status_seq,
        'reported_at': reported_at,
        'time_source': time_source,
        'status_reason': status_reason,
        'rollback_from_version': rollback_from_version,
        'rollback_target_version': rollback_target_version,
        'reported_online': parsed['online'],
        'mqtt_retained': bool(mqtt_retained),
        'payload_sha256': hashlib.sha256(canonical_payload.encode('utf-8')).hexdigest(),
        'raw_payload': canonical_payload,
        'received_at': int(time.time())
    }


def insert_status_data(payload: str, topic: str, mqtt_retained: bool = False):
    """Validate and persist one device status, returning its row ID if inserted."""
    status = normalize_status_payload(payload, topic, mqtt_retained)
    row_id = insert_device_status_checkin(status)
    if row_id is None:
        logger.info(
            "Ignored duplicate status for %s boot %s sequence %s",
            status['device_id'],
            status['boot_id'],
            status.get('status_seq')
        )
    else:
        logger.info(
            "Inserted device status row %s for %s firmware %s",
            row_id,
            status['device_id'],
            status['firmware_version']
        )
    return row_id


def normalize_availability_payload(
    payload: str,
    topic: str,
    mqtt_retained: bool = False
) -> dict:
    """Validate and normalize one schema-v1 MQTT availability document."""
    try:
        parsed = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSON: {exc.msg}") from exc

    if not isinstance(parsed, dict):
        raise ValueError("availability payload must be a JSON object")
    if type(parsed.get('schema_version')) is not int:
        raise ValueError("schema_version must be an integer")
    if parsed['schema_version'] != 1:
        raise ValueError(f"unsupported schema_version {parsed['schema_version']}")

    device_id = parsed.get('device_id')
    if not isinstance(device_id, str) or not device_id or len(device_id) > 64:
        raise ValueError("device_id must be a non-empty string up to 64 characters")
    if not DEVICE_ID_RE.fullmatch(device_id):
        raise ValueError("device_id contains unsupported characters")

    boot_id = parsed.get('boot_id')
    if not isinstance(boot_id, str) or not boot_id or len(boot_id) > 64:
        raise ValueError("boot_id must be a non-empty string up to 64 characters")
    if type(parsed.get('online')) is not bool:
        raise ValueError("online must be a boolean")

    topic_device_id = topic.rsplit('/', 1)[-1]
    if topic_device_id != device_id:
        raise ValueError(
            f"topic device ID {topic_device_id!r} does not match payload {device_id!r}"
        )

    canonical_payload = json.dumps(parsed, sort_keys=True, separators=(',', ':'))
    return {
        'device_id': device_id,
        'boot_id': boot_id,
        'online': parsed['online'],
        'mqtt_retained': bool(mqtt_retained),
        'payload_sha256': hashlib.sha256(canonical_payload.encode('utf-8')).hexdigest(),
        'raw_payload': canonical_payload,
        'received_at': int(time.time()),
    }


def insert_availability_data(
    payload: str,
    topic: str,
    mqtt_retained: bool = False
):
    """Validate and persist one MQTT availability transition."""
    availability = normalize_availability_payload(payload, topic, mqtt_retained)
    row_id = insert_device_availability_event(availability)
    if row_id is None:
        logger.info(
            "Ignored repeated %s availability for %s boot %s",
            "online" if availability['online'] else "offline",
            availability['device_id'],
            availability['boot_id']
        )
    else:
        logger.info(
            "Inserted %s availability row %s for %s",
            "online" if availability['online'] else "offline",
            row_id,
            availability['device_id']
        )
    return row_id


def on_connect(client, userdata, flags, rc):
    """Callback for when client connects to MQTT broker"""
    logger.debug(f"Connection callback called with rc={rc}")
    if rc == 0:
        logger.info("Connected to MQTT broker")
        result, mid = client.subscribe([
            (MQTT_TOPIC, 0),
            (MQTT_STATUS_TOPIC, 1),
            (MQTT_AVAILABILITY_TOPIC, 1),
        ])
        logger.info(
            "Subscribed to topics: %s, %s, and %s, result=%s, mid=%s",
            MQTT_TOPIC,
            MQTT_STATUS_TOPIC,
            MQTT_AVAILABILITY_TOPIC,
            result,
            mid
        )
    else:
        logger.error(f"Failed to connect to MQTT broker, return code {rc}")
        
        
def on_subscribe(client, userdata, mid, granted_qos):
    """Callback for when subscription is confirmed"""
    logger.info(f"Subscription confirmed: mid={mid}, granted_qos={granted_qos}")


def on_message(client, userdata, msg):
    """Callback for when a message is received"""
    try:
        payload = msg.payload.decode('utf-8')
        logger.info(f"Received message on topic {msg.topic}")
        logger.debug(f"Payload: {payload}")

        if mqtt.topic_matches_sub(MQTT_AVAILABILITY_TOPIC, msg.topic):
            insert_availability_data(payload, msg.topic, msg.retain)
        elif mqtt.topic_matches_sub(MQTT_STATUS_TOPIC, msg.topic):
            insert_status_data(payload, msg.topic, msg.retain)
        elif mqtt.topic_matches_sub(MQTT_TOPIC, msg.topic):
            insert_telemetry_data(payload)
        else:
            logger.warning("Ignoring message on unexpected topic: %s", msg.topic)
        
    except Exception as e:
        logger.error(f"Error processing message: {e}")


def on_disconnect(client, userdata, rc):
    """Callback for when client disconnects from MQTT broker"""
    logger.info("Disconnected from MQTT broker")


def main():
    """Main function"""
    logger.info("Starting BMS MQTT Logger")
    
    # Initialize database
    init_database()
    
    # Setup MQTT client
    client = mqtt.Client()
    client.username_pw_set(MQTT_USERNAME, MQTT_PASSWORD)
    client.on_connect = on_connect
    client.on_subscribe = on_subscribe
    client.on_message = on_message
    client.on_disconnect = on_disconnect
    
    # Enable detailed logging for MQTT client
    client.enable_logger(logger)
    
    try:
        # Connect to MQTT broker
        logger.info(f"Connecting to MQTT broker at {MQTT_BROKER}:{MQTT_PORT}")
        client.connect(MQTT_BROKER, MQTT_PORT, 60)
        
        # Start the loop
        client.loop_forever()
        
    except KeyboardInterrupt:
        logger.info("Shutting down...")
        client.disconnect()
    except Exception as e:
        logger.error(f"Unexpected error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
