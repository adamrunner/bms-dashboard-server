# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Development Commands

### Docker-based Development (Recommended)
```bash
# Start all services (MQTT broker, logger, dashboard)
docker-compose up -d

# View logs for all services
docker-compose logs -f

# View specific service logs
docker-compose logs bms-dashboard
docker-compose logs bms-logger
docker-compose logs mosquitto

# Stop services
docker-compose down

# Rebuild and restart services
docker-compose build && docker-compose up -d

# Reset database (removes all data)
docker-compose down -v && docker-compose up -d
```

### Local Development
```bash
# Install Python dependencies
pip install -r requirements.txt

# Run dashboard server locally
python dashboard_server.py

# Run MQTT logger locally (requires MQTT broker)
python bms_mqtt_logger.py

# Test WebSocket connection
python test_websocket.py
```

### Database Operations
```bash
# Create or upgrade the schema (preferred -- runs migrations too)
python -c "import database_queries; database_queries.ensure_database_schema()"

# Backup database from Docker
docker cp bms-dashboard:/app/data/bms_telemetry.db ./backup.db

# Access database directly
sqlite3 bms_telemetry.db

# Consistent snapshot of a live database (safe under WAL; plain `cp` is not)
sqlite3 bms_telemetry.db ".backup 'backup.db'"
```

Do **not** create databases with `sqlite3 bms_telemetry.db < bms_schema.sql`.
That applies the schema file but skips the migration pass in
`ensure_database_schema()`, so indexes over migration-added columns (such as
`idx_device_status_exact_event`) are never created.

## Architecture

This is a **Battery Management System (BMS) telemetry monitoring system** with three core components:

### 1. MQTT Data Flow
- **BMS devices** → publish CSV telemetry data → **MQTT broker** (Eclipse Mosquitto)
- **MQTT Logger** (`bms_mqtt_logger.py`) → subscribes to `bms/telemetry/+` topic → parses CSV → stores in SQLite
- Data format: CSV without headers, with 23 fixed columns followed by the
  declared number of cell-voltage and temperature values.

### 2. Web Dashboard 
- **Flask application** (`dashboard_server.py`) with Flask-SocketIO for real-time updates
- **Real-time monitoring**: Background thread detects new database entries and pushes via WebSocket
- **REST API endpoints**: `/api/data`, `/api/latest`, `/api/statistics`, `/api/health`
- **Frontend**: Single-page dashboard at `templates/dashboard.html` with Chart.js visualizations

### 3. Database Layer
- **SQLite database** with schema defined in `bms_schema.sql`
- **Query functions** in `database_queries.py` for data retrieval and statistics
- **29 telemetry fields**: timestamps, voltages, currents, temperatures, state of charge, power metrics
- **Schema changes follow the rules below** -- they are not optional, and
  violating them corrupts data silently rather than raising

## Schema Changes

SQLite's `ALTER TABLE ADD COLUMN` can only **append**. A database created fresh
from `bms_schema.sql` and one upgraded by `ensure_database_schema()` will
therefore hold identical data in **different physical column orders** if a new
column is declared mid-table in the schema file but added by `ALTER TABLE` in
code. This has happened before: `bms_id` and `timestamp_valid` sat at positions
31/32 in production and 1/3 in `bms_schema.sql`.

The failure mode is silent. `INSERT INTO t VALUES (...)`, `.dump`/restore
between the two layouts, `.mode insert`, and positional indexing of a
`SELECT *` row all write values into the wrong columns without erroring --
SQLite's type affinity accepts a REAL into a TEXT column without complaint.

### Adding a column

1. **Append it to the END** of the `CREATE TABLE` in `bms_schema.sql`. Never
   insert it mid-table, however much tidier that reads.
2. Add it to the matching dict in `database_queries.py`
   (`TELEMETRY_POLICY_COLUMNS`, `DEVICE_STATUS_V2_COLUMNS`) so
   `ensure_database_schema()` upgrades existing deployments.
3. Extend `EXPECTED_COLUMN_ORDER` in `tests/test_schema_parity.py`.
4. Run `python -m pytest tests/test_schema_parity.py`.

`NOT NULL` without a default cannot be added by `ALTER TABLE`. Declare such
columns nullable and enforce the invariant in the writer, or plan a deliberate
table rebuild. `bms_id` is nullable in the database for exactly this reason.

Indexes covering a migration-added column must be created in
`ensure_database_schema()` **after** the ALTER pass, not in `bms_schema.sql` --
the script runs before the column exists on older databases.

### Always write column-explicit SQL

```sql
INSERT INTO bms_telemetry (bms_id, timestamp) VALUES (?, ?);  -- safe
INSERT INTO bms_telemetry VALUES (?, ?);                      -- silently wrong
```

This applies to ad-hoc exports too. `sqlite3 .mode insert` emits positional
`VALUES` and is unsafe for anything intended to be restored elsewhere.

`tests/test_schema_parity.py` enforces all of this by building a fresh database
and a legacy-then-migrated database and asserting the two are identical.

## Key Configuration

### Environment Variables (.env)
- `MQTT_BROKER`: Hostname of MQTT broker (default: `mosquitto` for Docker; try `anton` over Tailscale MagicDNS first for external access, then `anton.local` when connected directly to the LAN)
- `MQTT_USERNAME`/`MQTT_PASSWORD`: MQTT authentication
- `MQTT_TOPIC`: Topic to subscribe to (default: `bms/telemetry/+`)
- `DATABASE_PATH`: SQLite database location
- `DASHBOARD_PORT`: Web dashboard port (default: 5000)

### MQTT Message Format
The system accepts variable-width gateway CSV data and normalizes it into this
four-cell/three-temperature commissioning schema:
1. `timestamp` (Unix timestamp)
2. `elapsed_seconds`, `elapsed_hms`, `total_energy_wh`
3. `pack_voltage_v`, `pack_current_a`, `state_of_charge_pct`, `power_w`
4. `full_capacity_ah`, `peak_current_a`, `peak_power_w`, `cell_count`
5. Cell voltage data: `min_cell_voltage_v`, `min_cell_num`, `max_cell_voltage_v`, `max_cell_num`, `cell_voltage_delta_v`
6. Temperature data: `temp_count`, `min_temp_c`, `max_temp_c`
7. Status: `charging_enabled`, `discharging_enabled`
8. Individual measurements: `cells_v_1` through `cells_v_4`, `temps_c_1` through `temps_c_3`

## Development Notes

- **Real-time updates**: Dashboard uses WebSocket broadcasting when new MQTT data arrives
- **Background monitoring**: Flask-SocketIO background task monitors database changes every 2 seconds
- **Data persistence**: SQLite database with indexed timestamps for performance
- **Health monitoring**: All Docker services include health checks
- **Security**: MQTT requires authentication, services run as non-root in containers
