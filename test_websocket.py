#!/usr/bin/env python3
"""
Test script to verify WebSocket functionality by adding test data
"""

import sqlite3
import time
from database_queries import DATABASE_PATH

def add_test_record():
    """Add a test telemetry record to trigger WebSocket update"""
    conn = sqlite3.connect(DATABASE_PATH)
    cursor = conn.cursor()
    
    # Get the latest timestamp and increment it
    cursor.execute("SELECT MAX(timestamp) FROM bms_telemetry")
    latest_timestamp = cursor.fetchone()[0] or int(time.time())
    
    test_timestamp = latest_timestamp + 1
    
    # Insert test record with current time
    cursor.execute("""
        INSERT INTO bms_telemetry (
            timestamp, elapsed_seconds, elapsed_hms, total_energy_wh,
            pack_voltage_v, pack_current_a, state_of_charge_pct, power_w,
            full_capacity_ah, peak_current_a, peak_power_w, cell_count,
            min_cell_voltage_v, min_cell_num, max_cell_voltage_v, max_cell_num,
            cell_voltage_delta_v, temp_count, min_temp_c, max_temp_c,
            charging_enabled, discharging_enabled, cells_v_1, cells_v_2,
            cells_v_3, cells_v_4, temps_c_1, temps_c_2, temps_c_3
        ) VALUES (
            ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
            ?, ?, ?, ?, ?, ?, ?, ?, ?
        )
    """, (
        test_timestamp, 999, "TEST", 999.9,
        13.50, 1.23, 99.9, 16.55,
        20.0, 5.0, 50.0, 4,
        3.300, 1, 3.400, 4,
        0.100, 3, 22.0, 25.0,
        1, 1, 3.350, 3.375, 3.400, 3.325,
        22.5, 23.0, 24.5
    ))
    
    conn.commit()
    conn.close()
    
    print(f"Added test record with timestamp {test_timestamp}")

if __name__ == "__main__":
    print("Adding test record to database...")
    add_test_record()
    print("Test record added. Check dashboard for real-time update!")