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
# Create database schema
sqlite3 bms_telemetry.db < bms_schema.sql

# Backup database from Docker
docker cp bms-dashboard:/app/data/bms_telemetry.db ./backup.db

# Access database directly
sqlite3 bms_telemetry.db
```

## Architecture

This is a **Battery Management System (BMS) telemetry monitoring system** with three core components:

### 1. MQTT Data Flow
- **BMS devices** → publish CSV telemetry data → **MQTT broker** (Eclipse Mosquitto)
- **MQTT Logger** (`bms_mqtt_logger.py`) → subscribes to `bms/telemetry` topic → parses CSV → stores in SQLite
- Data format: CSV without headers, 29 columns of battery telemetry (voltage, current, temperature, SoC, etc.)

### 2. Web Dashboard 
- **Flask application** (`dashboard_server.py`) with Flask-SocketIO for real-time updates
- **Real-time monitoring**: Background thread detects new database entries and pushes via WebSocket
- **REST API endpoints**: `/api/data`, `/api/latest`, `/api/statistics`, `/api/health`
- **Frontend**: Single-page dashboard at `templates/dashboard.html` with Chart.js visualizations

### 3. Database Layer
- **SQLite database** with schema defined in `bms_schema.sql`
- **Query functions** in `database_queries.py` for data retrieval and statistics
- **29 telemetry fields**: timestamps, voltages, currents, temperatures, state of charge, power metrics

## Key Configuration

### Environment Variables (.env)
- `MQTT_BROKER`: Hostname of MQTT broker (default: `mosquitto` for Docker, `anton.local` for external)
- `MQTT_USERNAME`/`MQTT_PASSWORD`: MQTT authentication
- `MQTT_TOPIC`: Topic to subscribe to (default: `bms/telemetry`)
- `DATABASE_PATH`: SQLite database location
- `DASHBOARD_PORT`: Web dashboard port (default: 5000)

### MQTT Message Format
The system expects CSV data with exactly 29 columns in this order:
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