# BMS Dashboard Backlog

This file tracks the prioritized engineering backlog for this repository.

Status values:
- `open`
- `in_progress`
- `done`
- `blocked`

## P0

### 1. Replace database polling with event-driven live updates
- Status: `done`
- Why: The live update loop does `COUNT(*)` every 2 seconds, then runs additional per-client queries when data changes. That is the main scaling bottleneck in the current architecture.
- Scope:
  - Stop polling when no clients are connected.
  - Avoid global table counts as the primary change detector.
  - Push updates from the ingest path or switch to a lighter latest-timestamp strategy.
- Refs:
  - [dashboard_server.py](/home/adamrunner/bms-dashboard-server/dashboard_server.py#L66)
  - [dashboard_server.py](/home/adamrunner/bms-dashboard-server/dashboard_server.py#L273)
  - [database_queries.py](/home/adamrunner/bms-dashboard-server/database_queries.py#L492)

### 2. Add the right SQLite indexes for real query patterns
- Status: `done`
- Why: Most reads filter by `timestamp` and `bms_id`, or fetch the latest row by `bms_id`, but the schema only has single-column indexes.
- Scope:
  - Add composite indexes such as `(bms_id, timestamp)`.
  - Validate query plans against the current dashboard read patterns.
- Refs:
  - [bms_schema.sql](/home/adamrunner/bms-dashboard-server/bms_schema.sql#L1)
  - [database_queries.py](/home/adamrunner/bms-dashboard-server/database_queries.py#L53)
  - [database_queries.py](/home/adamrunner/bms-dashboard-server/database_queries.py#L266)

### 3. Improve SQLite concurrency and ingest efficiency
- Status: `done`
- Why: The logger inserts row-by-row with a new connection per message while the dashboard reads continuously. That will increase write contention as data volume grows.
- Scope:
  - Enable WAL mode.
  - Set `busy_timeout`.
  - Reuse connections where reasonable.
  - Batch inserts with `executemany`.
- Refs:
  - [bms_mqtt_logger.py](/home/adamrunner/bms-dashboard-server/bms_mqtt_logger.py#L110)
  - [database_queries.py](/home/adamrunner/bms-dashboard-server/database_queries.py#L244)

## P1

### 4. Remove redundant initial data fetches
- Status: `done`
- Why: The client loads initial data over HTTP and Socket.IO during startup, creating duplicate work and extra load.
- Scope:
  - Keep one canonical initial-load path.
  - Make `initial_data` and `set_view` semantics consistent.
- Refs:
  - [static/js/dashboard.js](/home/adamrunner/bms-dashboard-server/static/js/dashboard.js#L85)
  - [static/js/dashboard.js](/home/adamrunner/bms-dashboard-server/static/js/dashboard.js#L445)
  - [dashboard_server.py](/home/adamrunner/bms-dashboard-server/dashboard_server.py#L294)
  - [dashboard_server.py](/home/adamrunner/bms-dashboard-server/dashboard_server.py#L355)

### 5. Remove schema checks from the query hot path
- Status: `done`
- Why: Every DB connection checks `sqlite_master`, which should not happen in the hot path for normal reads.
- Scope:
  - Move schema validation to startup and health-check paths only.
- Refs:
  - [database_queries.py](/home/adamrunner/bms-dashboard-server/database_queries.py#L244)

### 6. Reduce dashboard payload size
- Status: `done`
- Why: Raw dashboard queries use `SELECT *`, but the charts only need a subset of columns.
- Scope:
  - Return only chart-required fields for dashboard views.
  - Keep wider queries only where they are actually needed.
- Refs:
  - [database_queries.py](/home/adamrunner/bms-dashboard-server/database_queries.py#L46)
  - [database_queries.py](/home/adamrunner/bms-dashboard-server/database_queries.py#L290)

### 7. Optimize chart update logic for larger windows
- Status: `done`
- Why: Historical loads rebuild all datasets from scratch, and live updates filter every dataset array on each point.
- Scope:
  - Use bounded buffers.
  - Consolidate trim logic.
  - Evaluate Chart.js decimation for larger ranges.
- Refs:
  - [static/js/dashboard.js](/home/adamrunner/bms-dashboard-server/static/js/dashboard.js#L527)
  - [static/js/dashboard.js](/home/adamrunner/bms-dashboard-server/static/js/dashboard.js#L579)

## P2

### 8. Fix `test_websocket.py`
- Status: `done`
- Why: The current test insert omits `bms_id`, which no longer matches the live schema.
- Scope:
  - Make the script insert valid telemetry rows, or replace it with a real integration test.
- Refs:
  - [test_websocket.py](/home/adamrunner/bms-dashboard-server/test_websocket.py#L22)
  - [bms_schema.sql](/home/adamrunner/bms-dashboard-server/bms_schema.sql#L3)

### 9. Add automated tests
- Status: `done`
- Why: The repository has no meaningful automated test coverage today.
- Scope:
  - Add tests for CSV parsing.
  - Add tests for bucket resolution and aggregation logic.
  - Add API tests for `/api/data`.
  - Add WebSocket view-behavior tests.
- Refs:
  - [bms_mqtt_logger.py](/home/adamrunner/bms-dashboard-server/bms_mqtt_logger.py#L81)
  - [database_queries.py](/home/adamrunner/bms-dashboard-server/database_queries.py#L24)
  - [dashboard_server.py](/home/adamrunner/bms-dashboard-server/dashboard_server.py#L146)

### 10. Fix documentation drift
- Status: `done`
- Why: The README says the MQTT CSV has 29 columns, but the logger expects 30 including `bms_id`.
- Scope:
  - Align README and Docker docs with the actual schema and ingest format.
- Refs:
  - [README.md](/home/adamrunner/bms-dashboard-server/README.md#L76)
  - [bms_mqtt_logger.py](/home/adamrunner/bms-dashboard-server/bms_mqtt_logger.py#L25)

### 11. Clean up secrets and config defaults
- Status: `done`
- Why: Secret key and default credentials are hardcoded in code and compose config.
- Scope:
  - Read secrets from environment variables.
  - Fail clearly when required values are missing outside dev mode.
  - Update compose and docs.
- Refs:
  - [dashboard_server.py](/home/adamrunner/bms-dashboard-server/dashboard_server.py#L17)
  - [bms_mqtt_logger.py](/home/adamrunner/bms-dashboard-server/bms_mqtt_logger.py#L17)
  - [docker-compose.yml](/home/adamrunner/bms-dashboard-server/docker-compose.yml#L26)

## Recommended Order

1. P0.2 Add the right SQLite indexes for real query patterns
2. P1.5 Remove schema checks from the query hot path
3. P0.3 Improve SQLite concurrency and ingest efficiency
4. P1.4 Remove redundant initial data fetches
5. P0.1 Replace database polling with event-driven live updates
