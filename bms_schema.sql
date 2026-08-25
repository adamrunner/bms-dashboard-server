-- BMS telemetry schema.
--
-- ---------------------------------------------------------------------------
-- INVARIANT: declared column order must match post-migration physical order.
-- ---------------------------------------------------------------------------
-- SQLite's ALTER TABLE ADD COLUMN can only APPEND a column to the end of a
-- table. A database that has been migrated therefore carries added columns in
-- the order they were added, regardless of where this file declares them.
--
-- If a new column is declared in the middle of a CREATE TABLE here but added
-- via ALTER TABLE in ensure_database_schema(), the two paths diverge
-- permanently:
--
--     fresh install  (this file)          -> column sits mid-table
--     migrated install (ALTER TABLE)      -> column sits at the end
--
-- Both databases then hold identical data in DIFFERENT physical layouts. Any
-- positional SQL -- `INSERT INTO t VALUES (...)`, `.dump`/restore between the
-- two, `.mode insert`, indexing a `SELECT *` row by position -- silently writes
-- values into the wrong columns. It does not raise: SQLite's type affinity is
-- permissive enough that a REAL lands in a TEXT column without complaint.
--
-- RULE FOR ADDING A COLUMN:
--   1. Append it to the END of the CREATE TABLE below -- never mid-table.
--   2. Add it to the matching *_COLUMNS dict in database_queries.py so
--      ensure_database_schema() ALTERs existing databases.
--   3. Run tests/test_schema_parity.py, which fails if the two disagree.
--
-- Always write column-explicit SQL. `INSERT INTO t (a, b) VALUES (?, ?)` is
-- safe under any physical order; `INSERT INTO t VALUES (?, ?)` is not.

CREATE TABLE IF NOT EXISTS bms_telemetry (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
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
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    -- Added by migration; nullable because ALTER TABLE ADD COLUMN cannot add a
    -- NOT NULL column without a default. The logger always supplies a value --
    -- tightening this to NOT NULL requires a deliberate table rebuild.
    bms_id TEXT,
    -- A zero/negative gateway timestamp means capture occurred before wall
    -- clock synchronization. Such samples are preserved but never charted.
    timestamp_valid BOOLEAN NOT NULL DEFAULT 1
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
    reported_online BOOLEAN NOT NULL,
    mqtt_retained BOOLEAN NOT NULL DEFAULT 0,
    payload_sha256 TEXT NOT NULL,
    raw_payload TEXT NOT NULL,
    received_at INTEGER NOT NULL,
    -- v2 status fields, appended by migration (DEVICE_STATUS_V2_COLUMNS).
    status_seq INTEGER,
    reported_at INTEGER,
    time_source TEXT,
    status_reason TEXT,
    rollback_from_version TEXT,
    rollback_target_version TEXT
);

CREATE INDEX IF NOT EXISTS idx_device_status_device_received
    ON device_status_checkins(device_id, received_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_device_status_device_boot
    ON device_status_checkins(device_id, boot_id);
CREATE INDEX IF NOT EXISTS idx_device_status_received
    ON device_status_checkins(received_at);

-- NOTE: idx_device_status_exact_event covers status_seq, a migration-added
-- column, so it is created in ensure_database_schema() AFTER the ALTER TABLE
-- pass. Declaring it here would break upgrades from pre-v2 databases, where
-- the column does not yet exist when this script runs.

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

CREATE TABLE IF NOT EXISTS firmware_expectations (
    device_id TEXT PRIMARY KEY,
    expected_version TEXT NOT NULL,
    grace_until INTEGER,
    updated_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS device_alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id TEXT NOT NULL,
    alert_type TEXT NOT NULL,
    severity TEXT NOT NULL,
    -- NULL for alerts that are not derived from a status check-in, such as
    -- telemetry_stale: a silent device publishes nothing to point at.
    source_status_id INTEGER,
    dedup_key TEXT NOT NULL UNIQUE,
    details_json TEXT NOT NULL,
    detected_at INTEGER NOT NULL,
    acknowledged_at INTEGER,
    resolved_at INTEGER,
    FOREIGN KEY(source_status_id) REFERENCES device_status_checkins(id)
);

CREATE INDEX IF NOT EXISTS idx_device_alerts_device_detected
    ON device_alerts(device_id, detected_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_device_alerts_active
    ON device_alerts(device_id, resolved_at, detected_at DESC);
