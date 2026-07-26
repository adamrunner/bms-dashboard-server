# BMS Telemetry System - Docker Deployment

This guide explains how to deploy the BMS (Battery Management System) telemetry services using Docker.

## Architecture

The system consists of three containerized services:

- **Mosquitto MQTT Broker**: Eclipse Mosquitto MQTT broker for message handling
- **BMS MQTT Logger**: Subscribes to MQTT telemetry data and stores it in SQLite database
- **BMS Dashboard**: Flask web application providing real-time telemetry visualization

## Prerequisites

- Docker Engine 20.10+
- Docker Compose v2.0+
- No external dependencies (includes MQTT broker)

## Quick Start

1. **Clone and navigate to the project directory**
   ```bash
   cd /path/to/bms-telemetry
   ```

2. **Configure environment variables**
   ```bash
   cp .env.example .env
   # Edit .env file with your MQTT broker settings
   ```

3. **Start the services**
   ```bash
   docker-compose up -d
   ```

4. **Access the dashboard**
   - Open http://localhost:5000 in your browser
   - Real-time telemetry data will appear as MQTT messages arrive

## Configuration

### Environment Variables

Create a `.env` file based on `.env.example`:

```bash
# Runtime mode
APP_ENV=development

# MQTT Broker Configuration
MQTT_BROKER=mosquitto          # Use internal broker
MQTT_PORT=1883
MQTT_USERNAME=admin            # required outside development
MQTT_PASSWORD=password1234     # required outside development
MQTT_TOPIC=bms/telemetry/+     # subscribe to bms/telemetry/<bms-id>
MQTT_STATUS_TOPIC=bms/status/+ # retained boot/OTA status
MQTT_AVAILABILITY_TOPIC=bms/availability/+ # retained MQTT session state

# Mosquitto Service Configuration
MQTT_EXTERNAL_PORT=1883        # External port for MQTT
MQTTS_EXTERNAL_PORT=8883       # External port for MQTT over TLS

# Dashboard Configuration
DASHBOARD_PORT=5000
FLASK_DEBUG=false
FLASK_SECRET_KEY=change-me     # required outside development
```

Telemetry payloads published to `MQTT_TOPIC` use 23 fixed CSV columns starting
with `bms_id`, followed by `cell_count` cell values and `temp_count`
temperature values. The logger normalizes rows into the four-cell,
three-temperature commissioning schema, padding missing values with `NULL` and
rejecting inconsistent or larger rows.

Retained JSON boot and OTA records published to
`bms/status/<device_id>` are persisted in `device_status_checkins`. The
dashboard displays the latest record for the selected device. Schema v2 adds
exact boot-scoped event identity plus device time and its GNSS/SNTP source;
schema v1 remains accepted during rollout. Because these messages are sent at
boot, reconnect, time synchronization, and OTA verification rather than on a
fixed heartbeat, they are presented as status check-ins rather than current
online/offline state.

Retained MQTT session state on `bms/availability/<device_id>` is persisted as
deduplicated transitions. The dashboard labels it explicitly as MQTT
availability; it does not infer vehicle power from the broker session.

Telemetry captured before the gateway has synchronized its wall clock is
preserved with the original `timestamp=0` sentinel and
`timestamp_valid=false`. These unanchored samples remain available for audit
and diagnostics, but time-range queries and charts exclude them and report
their count separately. The same rule applies to a live MQTT row and to a row
replayed from the SD spool: the current CSV payload has no trustworthy replay
marker, and broker receipt time must not be substituted for capture time.

When `APP_ENV=development`, the Python services will log a warning and use development-only fallback values if `FLASK_SECRET_KEY` or MQTT credentials are omitted. In any other environment, those variables must be set explicitly or the services will fail to start.

### Using External MQTT Broker

To connect to an external MQTT broker instead of the internal Mosquitto service:

```bash
# In your .env file
MQTT_BROKER=anton                # Tailscale MagicDNS; use anton.local on LAN if needed
MQTT_PORT=1883
MQTT_USERNAME=your-username
MQTT_PASSWORD=your-password
```

Then comment out or remove the `mosquitto` service from `docker-compose.yml`.

### Mosquitto Configuration

The internal Mosquitto broker includes:
- MQTT on port 1883
- An optional runtime MQTTS listener on port 8883 after certificate setup
- Authentication required (username/password)
- Persistent message storage
- Bounded Docker log rotation (10 MB x 5 files by default)

To change the default admin password:
```bash
./mosquitto/config/create_users.sh
```

### Anton MQTTS activation

The tracked base configuration starts authenticated MQTT on port 1883 and
loads optional runtime snippets from `mosquitto/config/runtime.d`. On `anton`,
the root setup helper obtains a Let's Encrypt certificate with the existing
Cloudflare updater token, protects that token in root-only files, installs the
renewal hook, writes the ignored MQTTS runtime snippet, and recreates Mosquitto:

```bash
sudo /home/adamrunner/setup-anton-mqtts-root.sh you@example.com
```

Afterward, create a device account whose username exactly matches the gateway
device ID. Password entry is interactive:

```bash
docker exec -it mosquitto-broker \
  mosquitto_passwd /mosquitto/config/passwords/password_file gw-xxxxxx
docker kill --signal=HUP mosquitto-broker
```

The tracked ACL permits that account to publish only
`bms/telemetry/gw-xxxxxx`. The certificate, private key, generated runtime
snippet, password file, and environment secrets remain ignored by Git.

Before commissioning, switch the Python services out of development fallback
mode and generate a private Flask secret without printing existing secrets:

```bash
/home/adamrunner/bms-dashboard-server/deploy/anton/set-production-env.sh
```

## Docker Commands

### Start Services
```bash
# Start in background
docker-compose up -d

# Start with logs visible
docker-compose up
```

### View Logs
```bash
# View all logs
docker-compose logs

# View specific service logs
docker-compose logs mosquitto
docker-compose logs bms-logger
docker-compose logs bms-dashboard

# Follow logs in real-time
docker-compose logs -f
```

### Stop Services
```bash
# Stop services (keeps data)
docker-compose down

# Stop and remove volumes (deletes data)
docker-compose down -v
```

### Service Management
```bash
# Restart specific service
docker-compose restart bms-logger

# Check service status
docker-compose ps

# View resource usage
docker stats
```

## Data Persistence

- SQLite database is stored in Docker volume `bms_data`
- Data persists across container restarts
- To backup database: `docker cp bms-dashboard:/app/data/bms_telemetry.db ./backup.db`

## Health Monitoring

Both services include health checks:

```bash
# Check health status
docker-compose ps

# View health check details
docker inspect bms-dashboard --format='{{.State.Health}}'
```

## Troubleshooting

### MQTT Connection Issues
1. Check Mosquitto service status: `docker-compose ps mosquitto`
2. View Mosquitto logs: `docker-compose logs mosquitto`
3. Test MQTT connection: `docker exec mosquitto-broker mosquitto_pub -h localhost -t test -m "hello" -u admin -P password1234`
4. Verify credentials in `.env` file
5. For external broker: Check network connectivity: `docker exec bms-mqtt-logger ping your-broker-host`

### Dashboard Not Loading
1. Check if port 5000 is available: `netstat -tulpn | grep 5000`
2. Verify dashboard service is running: `docker-compose ps`
3. Check logs: `docker-compose logs bms-dashboard`

### Database Issues
1. Check database file permissions
2. Verify SQLite database creation: `docker exec bms-dashboard ls -la /app/data/`
3. Reset database: `docker-compose down -v && docker-compose up -d`

## Development

### Building Images Locally
```bash
# Build specific service
docker-compose build bms-logger

# Build all services
docker-compose build

# Build without cache
docker-compose build --no-cache
```

### Accessing Container Shell
```bash
# Access dashboard container
docker exec -it bms-dashboard /bin/bash

# Access logger container
docker exec -it bms-mqtt-logger /bin/bash
```

## Security Notes

- Services run as non-root user inside containers
- Sensitive data (passwords) should be managed via Docker secrets in production
- Consider using Docker networks to isolate services
- Regular security updates of base images recommended

## Production Deployment

For production deployments:

1. Use Docker secrets for sensitive configuration
2. Implement proper logging aggregation
3. Set up monitoring and alerting
4. Use reverse proxy (nginx) for HTTPS termination
5. Configure resource limits in docker-compose.yml
6. Implement backup strategy for database volume
