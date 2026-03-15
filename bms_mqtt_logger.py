#!/usr/bin/env python3
"""
BMS MQTT to SQLite Logger
Subscribes to MQTT telemetry data and stores it in SQLite database.
"""

import sqlite3
import csv
import io
import logging
import sys
import os
from datetime import datetime
import paho.mqtt.client as mqtt
from database_queries import ensure_database_schema, create_db_connection

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

# Setup logging
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('bms_logger.log'),
        logging.StreamHandler(sys.stdout)
    ]
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
DATABASE_PATH = os.getenv("DATABASE_PATH", "bms_telemetry.db")

INSERT_COLUMNS = ', '.join(EXPECTED_COLUMNS)
INSERT_PLACEHOLDERS = ', '.join(['?' for _ in EXPECTED_COLUMNS])
INSERT_SQL = f"INSERT INTO bms_telemetry ({INSERT_COLUMNS}) VALUES ({INSERT_PLACEHOLDERS})"


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
            if len(row) != len(EXPECTED_COLUMNS):
                logger.warning(f"Row has {len(row)} columns, expected {len(EXPECTED_COLUMNS)}")
                continue

            rows_to_insert.append([
                convert_value(value, EXPECTED_COLUMNS[i])
                for i, value in enumerate(row)
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


def on_connect(client, userdata, flags, rc):
    """Callback for when client connects to MQTT broker"""
    logger.debug(f"Connection callback called with rc={rc}")
    if rc == 0:
        logger.info("Connected to MQTT broker")
        result, mid = client.subscribe(MQTT_TOPIC)
        logger.info(f"Subscribed to topic: {MQTT_TOPIC}, result={result}, mid={mid}")
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
        
        # Insert data into database
        insert_telemetry_data(payload)
        
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
