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
# MQTT Configuration
MQTT_BROKER=mosquitto          # Use internal broker or external hostname
MQTT_PORT=1883
MQTT_USERNAME=admin
MQTT_PASSWORD=password1234
MQTT_TOPIC=bms/telemetry

# Service Ports
MQTT_EXTERNAL_PORT=1883        # MQTT port
MQTT_WEBSOCKET_PORT=9001       # WebSocket port
DASHBOARD_PORT=5000            # Web dashboard port
FLASK_DEBUG=false
```

### Data Format

The system expects CSV telemetry data with 29 columns:
- Timestamps and timing data
- Pack voltage, current, power, and state of charge
- Individual cell voltages (1-4)
- Temperature measurements (1-3)
- Capacity and peak power metrics
- Charging/discharging status

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