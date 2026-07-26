# Device Status Persistence and Dashboard Plan

## Goal

Persist ESP32 boot and OTA status messages published to
`bms/status/<device_id>` and display the latest status for the selected device
on the BMS dashboard.

The first implementation must not disturb the existing telemetry schema or
CSV ingestion path.

## Status message contract

Firmware schema version 1 publishes retained QoS 1 JSON containing:

- `schema_version`
- `device_id`
- `online`
- `firmware_version`
- `ota_slot`
- `pending_verify`
- `boot_id`
- `reset_reason`
- `idf_version`
- `build_date`
- `build_time`

The payload does not currently contain a device timestamp or unique check-in
sequence. The server therefore records its own receipt time, and status
history remains at-least-once until firmware adds a durable `checkin_id` or
per-boot sequence.

`online: true` means the device was online when it published the record. It is
not a heartbeat and must not be presented as proof that the device is
currently online. Current reporting health should continue to use telemetry
age. The dashboard's existing Connected badge continues to describe the
browser's Socket.IO connection.

## Phase 1: Schema and ingestion

Add an additive `device_status_checkins` table with normalized status fields,
the server receipt timestamp, MQTT retained flag, payload fingerprint, and
raw JSON. Index it for latest-per-device and per-boot queries.

Extend the logger to subscribe to:

- `bms/telemetry/+` using the existing `MQTT_TOPIC` setting.
- `bms/status/+` using a new `MQTT_STATUS_TOPIC` setting.

Dispatch by topic. Telemetry remains on the existing CSV path. Status
ingestion validates required fields and types, requires schema version 1, and
requires the payload device ID to match the topic suffix.

Live status messages are recorded as check-ins. A retained replay is inserted
only when the same device, boot, verification state, firmware version, and
payload have not already been observed. This makes an initial retained
snapshot visible while preventing logger restarts from manufacturing status
history.

## Phase 2: Queries and API

Add database functions for:

- Inserting a validated status check-in.
- Fetching the latest status for a device.
- Fetching the latest status row ID for change detection.
- Returning the union of device IDs found in telemetry or status.

Add:

```text
GET /api/device-status/latest?bms_id=<device_id>
```

Return the normalized record plus a server-calculated
`received_age_seconds`. Return an empty object for a device with no status.

## Phase 3: Dashboard presentation

Add a Device Status card associated with the selected BMS. Display:

- Firmware version
- OTA slot
- Verified or pending-verification state
- Last status check-in and age
- Abbreviated boot ID
- Reset reason
- ESP-IDF version
- Firmware build date and time

Fetch the latest status on initial load and whenever the selected device
changes. Missing status is a normal empty state, not a dashboard error.

Extend the existing database monitor to track the latest status row ID and
emit a selection-aware `device_status_update` Socket.IO event. This provides
live updates without coupling the separate MQTT logger process directly to
the web server.

## Phase 4: Tests

Cover:

- Additive schema creation
- Valid status insertion
- Malformed JSON and missing or invalid fields
- Topic/payload device mismatch
- Unsupported schema version
- Retained replay deduplication
- Distinct live check-ins from the same boot
- Latest-status query and API filtering
- Device ID union behavior
- Status Socket.IO emission
- Unchanged telemetry ingestion

## Deployment and validation

1. Run the automated test suite locally.
2. Take a SQLite `.backup` on Anton and validate it with
   `PRAGMA quick_check`.
3. Deploy the schema, logger, server, template, JavaScript, CSS, and Compose
   configuration.
4. Rebuild and restart the logger and dashboard containers.
5. Confirm both MQTT subscriptions are active.
6. Confirm the retained `gw-e3aba4` record is inserted.
7. Verify the API, dashboard card, and live update behavior.
8. Confirm telemetry continues to arrive throughout the rollout.

Rollback restores the prior application files and containers. The additive
status table can remain in SQLite because older code ignores it.

## Completed foundation

The original four phases are implemented as of 2026-07-25. The backend
accepts schema-version-1 retained status documents, persists normalized
check-ins, suppresses duplicate retained replays heuristically, exposes the
latest status through the API and Socket.IO, and displays it on the selected
device's dashboard.

The remaining work below deliberately separates:

- Status events: durable facts emitted by firmware about a boot or OTA state.
- Availability events: broker-observed MQTT connection state.
- Alerts: server-derived findings based on status, availability, and fleet
  policy.

Keeping these separate prevents an MQTT last will from overwriting the richer
retained status document and gives each history an unambiguous meaning.

## Next-phase contract

### Status schema version 2

Extend `bms/status/<device_id>` additively with:

- `status_seq`: an unsigned sequence beginning at 1 for each `boot_id`.
- `reported_at`: Unix epoch seconds, or JSON `null` before time is valid.
- `time_source`: `sntp`, `gnss`, or JSON `null`.
- `status_reason`: `boot`, `time_synchronized`, `mqtt_reconnected`,
  `ota_pending_verify`, `ota_verified`, or `rollback_detected`.
- `rollback_from_version` and `rollback_target_version`, populated only for a
  detected rollback.

The exact event identity is `(device_id, boot_id, status_seq)`. Firmware must
allocate `status_seq` once when it creates a logical status event and reuse
the same serialized document for MQTT retries. An explicit later event, such
as reconnection or OTA verification, receives the next sequence.

`boot_id` remains a per-boot random identifier. `status_seq` can remain in RAM
because it is scoped to that boot. The backend continues to accept schema
version 1 throughout the rollout, but only schema version 2 receives exact
deduplication and device-time semantics.

If MQTT connects before time synchronization, firmware publishes the boot
event with `reported_at: null`, then publishes a `time_synchronized` event
when the clock first becomes valid. Later events carry device time.

### Availability schema version 1

Use a separate retained QoS 1 topic:

```text
bms/availability/<device_id>
```

Before connecting, firmware configures a retained last will with
`online: false`, the current `device_id`, and `boot_id`. Immediately after
each successful connection it publishes the matching retained
`online: true` document. A deliberate MQTT client stop publishes offline
before disconnecting; an unexpected link loss lets the broker publish the
will.

Server receipt time is authoritative for availability transitions because a
last-will document is prepared before a device timestamp may exist. The
dashboard must label this as MQTT availability, not vehicle power state.

### Firmware expectation policy

Unexpected-version alerts need an explicit desired state rather than an
assumption that the lexically newest version is correct. Store an optional
expected firmware version per device, with an optional fleet-wide default.
A mismatch is expected during a configured rollout grace period and becomes
alertable after that period expires.

The first alert delivery target is the dashboard itself. Email, SMS, or other
out-of-band notification channels should be a separate decision after the
rules have produced useful, low-noise results in production.

## Phase 5: Exact identity and device time

Implementation status (2026-07-25): schema-v2 event identity, device time,
backward-compatible SQLite migration, exact backend deduplication, API clock
skew, and compatibility tests are implemented. Explicit rollback evidence is
still pending and remains coupled to the alerting slice.

### Firmware

1. Add a boot-scoped status-event builder that owns `status_seq`, status
   reason, and serialization. Do not allocate a new sequence inside a
   transport retry.
2. Publish schema version 2 from boot, MQTT reconnect, first time sync, OTA
   pending-verification, OTA verification, and rollback-detection paths.
3. Persist the attempted source and target firmware versions in NVS before
   booting a new OTA slot. Clear the marker only after verification. If the
   previous image boots with an uncleared marker and the running version is
   not the target, emit `rollback_detected`.
4. Expose the current time source from `timesync` and trigger exactly one
   first-sync status event per boot.

### Backend

1. Introduce an explicit SQLite migration mechanism; `CREATE TABLE IF NOT
   EXISTS` cannot add columns to Anton's existing table.
2. Add nullable `status_seq`, `reported_at`, `time_source`, `status_reason`,
   and rollback-version columns.
3. Add a partial unique index on `(device_id, boot_id, status_seq)` for rows
   with a sequence. Insert schema-version-2 rows with conflict-ignore
   semantics. Retain the schema-version-1 replay heuristic for older devices.
4. Return both `reported_at` and `received_at`, plus calculated clock skew,
   from status APIs. Do not reorder ingestion history by device time.

### Acceptance

- Delivering the same schema-version-2 MQTT document repeatedly creates one
  row.
- Two logical events from one boot create two rows with consecutive sequence
  values.
- A new boot may restart at sequence 1 because its `boot_id` differs.
- A device that connects before time sync produces an undated boot event and
  one dated `time_synchronized` event.
- Schema-version-1 devices continue to ingest and display.

## Phase 6: MQTT availability

Implementation status (2026-07-25): firmware last will and graceful offline
publication, broker ACL/configuration, transition persistence, latest-state
API and Socket.IO updates, and the selected-device availability badge are
implemented locally. Anton deployment and forced-link canary validation remain
pending.

### Firmware and broker

1. Configure the ESP-IDF MQTT last will before client initialization.
2. Publish retained online state on every successful connection and retained
   offline state before intentional client replacement or shutdown.
3. Add least-privilege ACL rules so a device may write only
   `bms/availability/<its MQTT username>`, and the logger may read
   `bms/availability/+`.
4. Extend the Anton credential/configuration validation script to check the
   new ACL and subscription without printing credentials.

### Backend and UI

1. Add `device_availability_events` with device ID, boot ID, online state,
   retained flag, payload fingerprint, and server receipt time.
2. Deduplicate retained replays while preserving real online/offline
   transitions.
3. Add latest-availability and availability-history queries.
4. Display `Online`, `Offline`, `Unknown`, and last-transition age separately
   from the browser Socket.IO badge and telemetry freshness.

### Acceptance

- Forcibly dropping a device connection causes the broker's retained state to
  become offline within the negotiated MQTT keepalive/session interval.
- Reconnection changes the retained state to online without restarting the
  logger or dashboard.
- Restarting the logger consumes retained state without creating a false
  transition.
- A graceful client reconfiguration records offline before the old session
  ends.
- Existing status and telemetry topics continue to pass their ACL tests.

## Phase 7: Status history and fleet overview

Add APIs with bounded pagination:

```text
GET /api/device-status/history?bms_id=<device_id>&limit=<n>&before_id=<id>
GET /api/fleet/status
```

Add a dedicated status page rather than expanding the telemetry chart page:

- Per-device timeline of boots, reset reasons, OTA pending/verified/rollback
  transitions, firmware versions, device time, receipt time, and MQTT
  availability changes.
- Fleet table with one row per device: current firmware, expected firmware,
  OTA verification state, last reset reason, MQTT availability, last
  telemetry age, last status age, and active-alert count.
- Filters for device, firmware version, reset class, availability, and alert
  state.
- Clear `Unknown` states for legacy devices and devices that have never sent
  availability.

The fleet query should select the latest row per device in SQL and remain
bounded; it must not load each device's complete history to render one page.

### Acceptance

- Pagination is stable when new check-ins arrive.
- Status-only and telemetry-only devices both appear in the fleet.
- Latest status and availability agree with the selected device's existing
  card.
- The page handles no devices, one device, and mixed schema versions.
- Query-count and response-time tests prevent an N+1 query regression.

## Phase 8: Alert rules and lifecycle

Add a `device_alerts` table containing alert type, severity, device ID,
source status row, a unique deduplication key, structured details, detection
time, and acknowledgement/resolution timestamps.

Evaluate rules when a new exact status event is committed:

- `firmware_rollback` (critical): firmware explicitly reports
  `rollback_detected`. Retain a conservative backend inference only for legacy
  sequences that show pending verification followed by the prior version.
- `watchdog_reset` (warning or critical): reset reason is
  `interrupt_watchdog`, `task_watchdog`, `watchdog`, or `cpu_lockup`.
- `unexpected_firmware` (warning): running version differs from the effective
  per-device or fleet expectation after its rollout grace period.

Each source event may create at most one alert of a given type. Reprocessing,
retained replay, logger restart, or repeated fleet-page loads must not create
duplicates. Acknowledgement records operator action without deleting history;
resolution occurs when a later exact event proves the condition has cleared.

Expose active and historical alerts in the fleet page, with acknowledge and
filter controls. Keep outbound notifications out of this phase until alert
noise has been assessed.

### Acceptance

- Replaying an alert-triggering check-in produces one alert.
- Watchdog reset classes trigger; ordinary software and power-on resets do
  not.
- A simulated explicit rollback produces one critical alert with source and
  target versions.
- A version mismatch inside its rollout grace period does not alert; the same
  mismatch after expiry does.
- Acknowledgement survives restarts, and a later healthy event resolves
  rather than deletes the alert.

## Delivery order and commits

Implement in small cross-repository slices:

1. Document and test schema-version-2 fixtures in the backend.
2. Add firmware event identity and time, then enable exact backend
   deduplication.
3. Add availability firmware, Mosquitto ACL, persistence, and the current-state
   badge as one end-to-end slice.
4. Add history/fleet read models and page.
5. Add firmware rollback evidence, expectation policy, and in-app alerts.
6. Update the Anton runbook and deploy only after local tests and firmware
   build pass.

Use focused commits in `esp32-sim7670g`, `bms-dashboard-server`, and
`4runner-telematics`; do not combine firmware, database migration, UI, and
production activation into one unreviewable commit.

## Production rollout

1. Run backend unit/system tests and build the firmware with the pinned ESP-IDF
   environment.
2. Publish synthetic version-1, version-2, availability, rollback, and
   watchdog fixtures to a local broker.
3. Take an Anton SQLite `.backup`, restore it to a temporary database, and run
   `PRAGMA quick_check`.
4. Deploy backward-compatible backend ingestion and migrations before flashing
   schema-version-2 firmware.
5. Update Mosquitto ACLs with the existing guarded credential/configuration
   workflow, and validate current telemetry/status access after the change.
6. Flash one canary device. Exercise time sync, forced network loss,
   reconnect, reboot, OTA verification, and duplicate replay.
7. Observe the canary through at least one normal offline/online cycle before
   enabling fleet views and alerts.
8. Enable alert rules initially in dashboard-only mode and tune them before
   selecting an external notification channel.

Rollback application code and containers independently of the additive
database migration. Older firmware remains compatible because the backend
continues to accept schema version 1 and the existing status topic is
unchanged.
