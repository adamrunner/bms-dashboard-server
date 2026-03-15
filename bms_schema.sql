CREATE TABLE IF NOT EXISTS bms_telemetry (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    bms_id TEXT NOT NULL,
    timestamp INTEGER NOT NULL,
    elapsed_seconds REAL,
    elapsed_hms TEXT,
    total_energy_wh REAL,
    pack_voltage_v REAL,
    pack_current_a REAL,
    state_of_charge_pct REAL,
    power_w REAL,
    full_capacity_ah REAL,
    peak_current_a REAL,
    peak_power_w REAL,
    cell_count INTEGER,
    min_cell_voltage_v REAL,
    min_cell_num INTEGER,
    max_cell_voltage_v REAL,
    max_cell_num INTEGER,
    cell_voltage_delta_v REAL,
    temp_count INTEGER,
    min_temp_c REAL,
    max_temp_c REAL,
    charging_enabled BOOLEAN,
    discharging_enabled BOOLEAN,
    cells_v_1 REAL,
    cells_v_2 REAL,
    cells_v_3 REAL,
    cells_v_4 REAL,
    temps_c_1 REAL,
    temps_c_2 REAL,
    temps_c_3 REAL,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_bms_id ON bms_telemetry(bms_id);
CREATE INDEX IF NOT EXISTS idx_timestamp ON bms_telemetry(timestamp);
CREATE INDEX IF NOT EXISTS idx_bms_id_timestamp ON bms_telemetry(bms_id, timestamp);
CREATE INDEX IF NOT EXISTS idx_created_at ON bms_telemetry(created_at);
