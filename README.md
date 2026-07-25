# BMS Dashboard Server

A real-time telemetry monitoring system for Battery Management Systems (BMS) that collects MQTT data and provides a web-based dashboard for visualization.

## Features

- **Real-time MQTT data collection** from BMS devices
- **Live web dashboard** with WebSocket updates
- **Historical data visualization** with interactive charts
- **SQLite database storage** with efficient querying
- **Containerized deployment** with Docker Compose
- **RESTful API** for data access
- **Health monitoring** and logging

## Quick Start

### Prerequisites

- Docker Engine 20.10+
- Docker Compose v2.0+

### Setup

1. **Clone the repository**
   ```bash
   git clone <repository-url>
   cd bms-dashboard-server
   ```

2. **Configure environment**
   ```bash
   cp .env.example .env
   # Edit .env with your MQTT broker settings
   ```

3. **Start the services**
   ```bash
   docker-compose up -d
   ```

4. **Access the dashboard**
   - Open http://localhost:5000 in your browser
   - Real-time telemetry data will appear as MQTT messages arrive

## Architecture

The system consists of three containerized services:

- **Mosquitto MQTT Broker**: Handles MQTT message routing
- **BMS MQTT Logger**: Subscribes to telemetry data and stores in SQLite
- **BMS Dashboard**: Flask web application with real-time visualization

## Configuration

### Environment Variables

Edit `.env` file to configure:

```bash
# Runtime mode
APP_ENV=development           # development uses explicit fallback secrets; production requires env vars

# MQTT Configuration
MQTT_BROKER=mosquitto          # Use internal broker or external hostname
MQTT_PORT=1883
MQTT_USERNAME=admin            # required outside development
MQTT_PASSWORD=password1234     # required outside development
MQTT_TOPIC=bms/telemetry/+     # subscribe to bms/telemetry/<bms-id>

# Dashboard secret
FLASK_SECRET_KEY=change-me     # required outside development

# Service Ports
MQTT_EXTERNAL_PORT=1883        # MQTT port
MQTT_WEBSOCKET_PORT=9001       # WebSocket port
DASHBOARD_PORT=5000            # Web dashboard port
FLASK_DEBUG=false
```

In `APP_ENV=development`, the dashboard and logger will log a warning and use development-only fallback values if `FLASK_SECRET_KEY` or MQTT credentials are unset. In any other environment, startup fails unless those variables are set explicitly.

### Data Format

The system expects the gateway's variable-width CSV telemetry format. Every
row starts with 23 fixed columns (including the BMS identifier), followed by
`cell_count` cell voltages and `temp_count` temperatures. The commissioning
SQLite schema stores up to four cells and three temperatures; missing values
are padded with `NULL`, while larger or inconsistent rows are rejected.

The normalized database column order is:

1. `bms_id`
2. `timestamp`
3. `elapsed_seconds`
4. `elapsed_hms`
5. `total_energy_wh`
6. `pack_voltage_v`
7. `pack_current_a`
8. `state_of_charge_pct`
9. `power_w`
10. `full_capacity_ah`
11. `peak_current_a`
12. `peak_power_w`
13. `cell_count`
14. `min_cell_voltage_v`
15. `min_cell_num`
16. `max_cell_voltage_v`
17. `max_cell_num`
18. `cell_voltage_delta_v`
19. `temp_count`
20. `min_temp_c`
21. `max_temp_c`
22. `charging_enabled`
23. `discharging_enabled`
24. `cells_v_1`
25. `cells_v_2`
26. `cells_v_3`
27. `cells_v_4`
28. `temps_c_1`
29. `temps_c_2`
30. `temps_c_3`

## API Endpoints

- `GET /` - Web dashboard
- `GET /api/data?hours=1` - Get telemetry data for specified hours
- `GET /api/latest` - Get most recent reading
- `GET /api/statistics?hours=24` - Get summary statistics
- `GET /api/health` - Health check endpoint

## Development

### Local Development

```bash
# Install dependencies
pip install -r requirements.txt

# Run services locally
python dashboard_server.py
python bms_mqtt_logger.py
```

### Docker Commands

```bash
# View logs
docker-compose logs -f

# Restart specific service
docker-compose restart bms-dashboard

# Rebuild services
docker-compose build

# Reset database
docker-compose down -v && docker-compose up -d
```

### Development Mode

For local development with auto-reloading of templates and code changes:

```bash
# Start dashboard in debug mode (auto-reload enabled)
./start_dashboard_debug.sh

# Or manually pass the debug flag
python start_dashboard.py --debug
```

When running in debug mode:
- Templates in `templates/` will auto-reload when changed
- Code changes will trigger server restart
- Debug logging is enabled

### Database Operations

```bash
# Backup database
docker cp bms-dashboard:/app/data/bms_telemetry.db ./backup.db

# Access database
sqlite3 bms_telemetry.db
```

## Monitoring

All services include health checks and logging:

- **Dashboard**: Available at http://localhost:5000
- **MQTT Broker**: Ports 1883 (MQTT) and 9001 (WebSocket)
- **Database**: SQLite with automatic schema creation
- **Logs**: Available via `docker-compose logs`

## Troubleshooting

### Common Issues

1. **MQTT Connection Failed**
   - Check broker hostname in `.env`
   - Verify credentials
   - Check network connectivity

2. **Dashboard Not Loading**
   - Ensure port 5000 is available
   - Check service status: `docker-compose ps`
   - View logs: `docker-compose logs bms-dashboard`

3. **No Data Appearing**
   - Verify MQTT messages are being published to correct topic
   - Check logger logs: `docker-compose logs bms-logger`
   - Ensure CSV data format matches expected schema

### Health Checks

```bash
# Check all service status
docker-compose ps

# Test API health
curl http://localhost:5000/api/health

# Test MQTT connection
docker exec mosquitto-broker mosquitto_pub -h localhost -t test -m "hello" -u admin -P password1234
```

## Contributing

1. Follow existing code style and conventions
2. Add tests for new functionality
3. Update documentation as needed
4. Ensure Docker builds succeed

## License

[Add your license here]
