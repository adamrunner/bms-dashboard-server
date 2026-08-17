# Telemetry Duplication Plan

Status: `proposed`

Evidence date: 2026-08-17

Scope: duplicate rows in Anton's `bms_telemetry` table. Touches the firmware
publish path (`esp32-sim7670g/main/mqtt.c`), the logger ingest path
(`bms_mqtt_logger.py`), and the SQLite schema. This is the unimplemented
backend half of Phase 4 ("Delivery identity and production deduplication") in
`esp32-sim7670g/docs/FIELD_RELIABILITY_REMEDIATION_PLAN.md`.

## Evidence

Measured against device `gw-e3aba4`, firmware `bba2413`, boot
`18ac1062da0f5632` (booted 2026-08-14 12:18 PDT), sampled 2026-08-17:

| Source | Count |
| --- | --- |
| Device SD journal (`datalog.sd_rows`, authoritative capture count) | 25,678 |
| DB distinct `timestamp` values for the boot | 25,674 |
| DB total rows for the boot | **26,987** |

Distinct timestamps match the device's own capture count, so **no data is
being lost** — every sample the device recorded reached the database. The
excess **1,313 rows (+5.1%)** are duplicates. Over a rolling 24 h window: 251
duplicate groups, 327 extra rows out of 8,723 total (3.7%).

Duplicate rows are byte-identical in the fields that matter and land in the
same second:

```
id        t                    elapsed_seconds  wh        amps  created_at
2467802   2026-08-17 21:03:18  265530.0         1454.201  0.0   2026-08-17 21:03:21
2467803   2026-08-17 21:03:18  265530.0         1454.201  0.0   2026-08-17 21:03:21
2467804   2026-08-17 21:03:18  265530.0         1454.201  0.0   2026-08-17 21:03:21
```

Groups of two and three both occur. `created_at` being identical rules out a
delayed replay minutes later; these arrive within the same second.

## Root cause: one half confirmed, one half still open

### 1. Emission mechanism — NOT YET IDENTIFIED

A live QoS 0 probe against `bms/telemetry/+` on 2026-08-17 caught duplicates
directly. Because the broker never redelivers a QoS 0 subscription, each copy
the probe saw is a copy the broker independently received:

```
23:05:05 DUPLICATE gap=0.135s  gw-e3aba4,1787007903,272835,...
23:05:47 DUPLICATE gap=0.000s  gw-e3aba4,1787007944,272877,...
23:05:47 DUPLICATE gap=0.134s  gw-e3aba4,1787007944,272877,...
```

**Inter-arrival gaps are 0 to 135 ms**, and the second row shows a payload
delivered three times inside the same 135 ms.

This *disproves* the first theory considered — that esp-mqtt's default
`MQTT_DEFAULT_RETRANSMIT_TIMEOUT_MS` of 1000 ms
(`mqtt_config.h:103`, never overridden by the firmware) was retransmitting
unacked QoS 1 publishes. That timer would produce copies spaced ~1 s apart, not
0–135 ms. Do not start from that assumption.

Two further facts constrain the search:

- The device does not know it is happening. `/api/status` reports
  `published: 26386` against `datalog.rows: 26383` and `sd_rows: 26383` — the
  application believes it published each row exactly once. Whatever duplicates
  the packet sits **below** `mqtt_publish_telemetry()`.
- It cannot be TCP-level duplication; TCP would deduplicate. The same MQTT
  PUBLISH bytes must have been written to the socket more than once.

Leading candidate, untested: **a partial or interrupted socket write being
retried whole.** The modem pauses PPP for AT windows constantly —
`ppp.pause_count: 8744`, journaled as `pause_complete` with
`reason: at_window` and `duration_ms` of 24–45, roughly every 30 s. If a pause
lands mid-write and `esp_transport_write` returns short or errors, a retry that
resends the entire MQTT packet rather than the remainder would put two or three
identical PUBLISHes on the wire milliseconds apart. This is exactly the seam
that `dc8e450` ("Coordinate modem polling with MQTT delivery") addresses, and
it would explain the sub-second clustering.

**Next experiment:** correlate duplicate arrival timestamps against
`ppp/pause_complete` events in the device journal for the same seconds. Capture
`/api/events` while a duplicate-detecting probe runs, then line up the wall
clocks. If duplicates consistently coincide with AT windows, the mechanism is
found. If they do not, instrument `esp_transport_write` return values before
theorizing further.

Note also that the TLS path (`mqtts://`) sits between esp-mqtt and the socket
and is another place a whole-record retry could live.

### 2. The backend has no delivery identity — CONFIRMED

Anton's `bms_telemetry` table has no `delivery_boot_id` / `delivery_sequence`
columns and no partial unique index; the remediation plan's Phase 4 was
specified but never applied to the backend. `insert_telemetry_data()` inserts
every message unconditionally, so each retransmitted copy becomes a row.

Note that `tools/field_reconcile.py` already understands delivery identities
("preserves delivery identities when present"), and the firmware already
carries a `boot_id` and per-boot sequence for *status* events. The telemetry
path is the piece that stayed on legacy CSV.

## Impact

Modest but real, and worth fixing before it is designed around:

- `COUNT(*)`-based statistics are inflated (`/api/health` reported 2,467,595
  total records; roughly 5% of the recent portion is duplicate).
- Bucketed `AVG()` charts double-weight the duplicated samples. The effect is
  small because duplicates carry the same value, but it is not zero.
- The Phase 4/6 acceptance criterion "zero duplicate production telemetry rows
  for identified deliveries" currently fails, which blocks sign-off on the
  end-to-end field validation.
- Spool replay duplicates through the same gap (`spool_replayed: 15`).

## Options

### A. Raise the retransmit timeout (firmware, one line) — DEPRIORITIZED

```c
.session.message_retransmit_timeout = 10000,
```

This was the original "cheap fix" idea, on the theory that esp-mqtt's 1 s
retransmit timer was beating the 8 s application timeout. The 0–135 ms
measured gaps say that timer is not what is firing, so **this change would
probably do nothing for the observed duplicates**.

It is still defensible hygiene on its own merits — a transport that
retransmits eight times sooner than the application gives up is a latent
source of exactly this class of bug — but ship it as tidying, not as the fix,
and do not expect the duplicate rate to move.

### B. Delivery identity end to end (the real fix, Phase 4 as specified)

1. Firmware assigns a per-boot monotonic `delivery_sequence` before the first
   publish attempt, and does **not** allocate a new one on retry — the same
   logical row keeps its identity across retransmits and across spool replay.
2. Publish the schema-v2 envelope carrying `boot_id` + `delivery_sequence`
   alongside the existing CSV body. Keep the daily SD CSV human-readable and
   unchanged.
3. Backend migration adds nullable `delivery_boot_id` and `delivery_sequence`,
   plus a partial unique index:

   ```sql
   CREATE UNIQUE INDEX idx_telemetry_delivery_identity
       ON bms_telemetry(bms_id, delivery_boot_id, delivery_sequence)
       WHERE delivery_boot_id IS NOT NULL;
   ```

4. Logger inserts identified rows with `INSERT ... ON CONFLICT DO NOTHING` and
   counts suppressions in a metric. Unidentified legacy rows keep today's
   behavior so older firmware and historical data still ingest.

This is additive and backward compatible, which matters because the same
database holds `bms-404CCAFFFE43` and `gw-commissioning-test` history from
firmware that will never publish an identity.

### C. Content-hash dedup at the logger (backend only, no firmware change)

Hash the normalized payload and reject a row whose `(bms_id, timestamp, hash)`
was already seen inside a short window. Attractive because it ships without
touching the firmware or waiting for an OTA window.

Rejected as the primary fix: it cannot distinguish a retransmit from a
legitimately identical reading (a pack sitting at rest genuinely reports the
same values), and it silently drops real data if the device ever publishes two
samples in the same second. Reasonable as a **backfill tool** for existing
rows, not as the ingest rule.

## Recommended plan

1. **Implement Option B — it is the fix regardless of the emission mechanism.**
   Delivery identity suppresses duplicates whether they come from a retransmit
   timer, a retried socket write, or a spool replay, which is precisely why it
   is worth doing before the mechanism is fully understood. Ship it in the
   plan's usual slices: backend migration and logger tolerance first (accepting
   identity when present), then firmware identity, then enable the unique
   index. Deploy backward-compatible ingest before flashing.
2. **In parallel, finish diagnosing the emission mechanism** using the AT-window
   correlation experiment above. Identity makes the duplicates harmless; it does
   not explain why the device is writing the same packet two or three times, and
   that may matter for other traffic on the same socket.
3. **Backfill separately.** Existing duplicates are still in the table; decide
   deliberately whether to delete them (keeping `MIN(id)` per group) or leave
   them and document the affected range. Do this only after a fresh Anton
   `.backup` and `PRAGMA quick_check`.
4. Add a duplicate-rate check to `tools/field_reconcile.py` output so the
   harness reports it as a first-class number rather than requiring an ad-hoc
   query.

## Verification

Duplicate rate for a boot, comparable to `datalog.sd_rows` on the device:

```sql
SELECT COUNT(*) AS db_rows,
       COUNT(DISTINCT timestamp) AS distinct_ts,
       COUNT(*) - COUNT(DISTINCT timestamp) AS duplicates
FROM bms_telemetry
WHERE bms_id = 'gw-e3aba4' AND timestamp BETWEEN :boot_start AND :now;
```

Live duplicate detection without changing production — this is the probe that
produced the evidence above, and it should be rerun after any fix. Subscribe to
`bms/telemetry/+` at **QoS 0** and hash payloads; because the broker never
redelivers a QoS 0 subscription, a repeated hash proves a separately received
publish. Run it from inside the `bms-mqtt-logger` container so it inherits
`MQTT_USERNAME`/`MQTT_PASSWORD` rather than handling credentials directly.
Log the inter-arrival gap — the gap is the diagnostic signal, since it is what
ruled out the 1 s retransmit timer.

Definitive but invasive alternative: set `log_type all` on the broker briefly
and count incoming PUBLISH lines carrying the `d1` DUP flag from `gw-e3aba4`.
This would also settle whether the copies carry the MQTT DUP flag (a protocol
retransmit) or arrive as fresh packet IDs (an application or transport-level
re-send) — a genuinely useful discriminator. It requires a config reload on
production, so weigh it against the passive probe.

## Open questions

- **What actually emits the duplicate packet?** The open question, per the root
  cause section. Start with the AT-window correlation, then instrument
  `esp_transport_write` return values.
- Do the duplicate copies carry the MQTT DUP flag and reuse the packet ID, or
  are they distinct packet IDs? This single fact separates "protocol
  retransmit" from "something re-sent the bytes" and is cheap to obtain from a
  broker debug log.
- What is the actual PUBACK round-trip distribution over LTE? The device does
  not currently measure it. A latency histogram on `/api/status.mqtt` would
  show whether the >1 s tail exists at all — relevant because its absence is
  further evidence against the retransmit-timer theory.
- Should `spool_replayed` rows be identified with the *original* delivery
  sequence (correct — a replay is the same logical row) or a new one? Option B
  step 1 assumes the former; the firmware must not reallocate on the spool
  path either.
- Do the two legacy devices in the fleet need a documented cutoff date beyond
  which their rows are known to be identity-free?
