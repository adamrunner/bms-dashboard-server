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

CREATE TABLE IF NOT EXISTS device_status_checkins (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id TEXT NOT NULL,
    schema_version INTEGER NOT NULL,
    firmware_version TEXT NOT NULL,
    ota_slot TEXT NOT NULL,
    pending_verify BOOLEAN NOT NULL,
    boot_id TEXT NOT NULL,
    reset_reason TEXT NOT NULL,
    idf_version TEXT,
    build_date TEXT,
    build_time TEXT,
    status_seq INTEGER,
    reported_at INTEGER,
    time_source TEXT,
    status_reason TEXT,
    rollback_from_version TEXT,
    rollback_target_version TEXT,
    reported_online BOOLEAN NOT NULL,
    mqtt_retained BOOLEAN NOT NULL DEFAULT 0,
    payload_sha256 TEXT NOT NULL,
    raw_payload TEXT NOT NULL,
    received_at INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_device_status_device_received
    ON device_status_checkins(device_id, received_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_device_status_device_boot
    ON device_status_checkins(device_id, boot_id);
CREATE INDEX IF NOT EXISTS idx_device_status_received
    ON device_status_checkins(received_at);

CREATE TABLE IF NOT EXISTS device_availability_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id TEXT NOT NULL,
    boot_id TEXT NOT NULL,
    online BOOLEAN NOT NULL,
    mqtt_retained BOOLEAN NOT NULL DEFAULT 0,
    payload_sha256 TEXT NOT NULL,
    raw_payload TEXT NOT NULL,
    received_at INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_device_availability_device_received
    ON device_availability_events(device_id, received_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_device_availability_received
    ON device_availability_events(received_at);
