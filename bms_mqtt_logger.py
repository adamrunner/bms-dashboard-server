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

# Configuration
MQTT_BROKER = os.getenv("MQTT_BROKER", "anton.local")
MQTT_PORT = int(os.getenv("MQTT_PORT", "1883"))
MQTT_USERNAME = os.getenv("MQTT_USERNAME", "admin")
MQTT_PASSWORD = os.getenv("MQTT_PASSWORD", "password1234")
MQTT_TOPIC = os.getenv("MQTT_TOPIC", "bms/telemetry")
DATABASE_PATH = os.getenv("DATABASE_PATH", "bms_telemetry.db")

# Expected CSV columns
EXPECTED_COLUMNS = [
    'timestamp', 'elapsed_seconds', 'elapsed_hms', 'total_energy_wh',
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


def init_database():
    """Initialize SQLite database with schema"""
    try:
        # Ensure database directory exists
        db_dir = os.path.dirname(DATABASE_PATH)
        if db_dir and not os.path.exists(db_dir):
            os.makedirs(db_dir, exist_ok=True)
            logger.info(f"Created database directory: {db_dir}")
            
        conn = sqlite3.connect(DATABASE_PATH)
        cursor = conn.cursor()
        
        # Check if table already exists and has data
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='bms_telemetry'")
        table_exists = cursor.fetchone()
        
        if table_exists:
            cursor.execute("SELECT COUNT(*) FROM bms_telemetry")
            record_count = cursor.fetchone()[0]
            logger.info(f"Database already exists with {record_count} records at: {DATABASE_PATH}")
        else:
            logger.info("Creating new database schema")
            with open('bms_schema.sql', 'r') as f:
                schema = f.read()
            conn.executescript(schema)
            conn.commit()
            logger.info(f"Database initialized successfully at: {DATABASE_PATH}")
            
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
    if column_name in ['elapsed_hms']:
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
        
        conn = sqlite3.connect(DATABASE_PATH)
        cursor = conn.cursor()
        
        rows_inserted = 0
        for row in csv_reader:
            if len(row) != len(EXPECTED_COLUMNS):
                logger.warning(f"Row has {len(row)} columns, expected {len(EXPECTED_COLUMNS)}")
                continue
                
            # Convert values to appropriate types
            values = []
            for i, value in enumerate(row):
                column_name = EXPECTED_COLUMNS[i]
                converted_value = convert_value(value, column_name)
                values.append(converted_value)
            
            # Insert into database
            placeholders = ', '.join(['?' for _ in EXPECTED_COLUMNS])
            columns = ', '.join(EXPECTED_COLUMNS)
            
            cursor.execute(
                f"INSERT INTO bms_telemetry ({columns}) VALUES ({placeholders})",
                values
            )
            rows_inserted += 1
        
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