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

## Deferred enhancements

- Firmware-generated `checkin_id` or per-boot status sequence
- Device-generated timestamp after clock synchronization
- MQTT last-will availability topic for reliable online/offline state
- Status history and fleet firmware views
- Alerts for rollback, watchdog resets, or unexpected firmware versions
