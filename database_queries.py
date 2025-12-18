#!/usr/bin/env python3
"""
Database query functions for BMS telemetry data
"""

import sqlite3
import os
from datetime import datetime, timedelta
from typing import List, Dict, Optional

DATABASE_PATH = os.getenv("DATABASE_PATH", "bms_telemetry.db")


def get_db_connection():
    """Get database connection"""
    try:
        conn = sqlite3.connect(DATABASE_PATH)
        conn.row_factory = sqlite3.Row
        # Test the connection
        cursor = conn.cursor()
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='bms_telemetry'")
        table_exists = cursor.fetchone()
        if not table_exists:
            print(f"ERROR: bms_telemetry table not found in database: {DATABASE_PATH}")
            # List available tables for debugging
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
            tables = cursor.fetchall()
            print(f"Available tables: {[t[0] for t in tables]}")
        return conn
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


def get_telemetry_data(hours: float = 1, bms_id: Optional[str] = None) -> List[Dict]:
    """Get telemetry data for the specified number of hours"""
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
    """Get list of all available BMS IDs"""
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT DISTINCT bms_id 
            FROM bms_telemetry 
            ORDER BY bms_id ASC
        """)
        rows = cursor.fetchall()
        return [row['bms_id'] for row in rows]
    finally:
        conn.close()