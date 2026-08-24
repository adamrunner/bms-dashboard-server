#!/usr/bin/env python3
"""Container healthcheck for the BMS MQTT logger.

Asserts that the logger is connected to the broker and that its monitor thread
is still refreshing the heartbeat. The previous check only opened SQLite, which
succeeds even when the MQTT link is dead -- during the 2026-08-24 outage the
logger reported healthy while crash-looping on broker DNS resolution.

A device that is legitimately silent does NOT fail this check. That condition is
reported as a telemetry_stale alert instead, so "the logger is broken" and "the
gateway stopped talking" stay distinguishable.
"""

import json
import os
import sys
import time

HEALTH_STATE_PATH = os.getenv("LOGGER_HEALTH_PATH", "/tmp/bms-logger-health.json")
MONITOR_INTERVAL_SECONDS = int(os.getenv("MONITOR_INTERVAL_SECONDS", "60"))
# Tolerate one missed monitor pass before declaring the heartbeat stale.
HEARTBEAT_MAX_AGE_SECONDS = int(
    os.getenv("HEARTBEAT_MAX_AGE_SECONDS", str(MONITOR_INTERVAL_SECONDS * 2 + 30))
)


def main() -> int:
    try:
        with open(HEALTH_STATE_PATH, 'r', encoding='utf-8') as handle:
            state = json.load(handle)
    except FileNotFoundError:
        print(f"unhealthy: no health state at {HEALTH_STATE_PATH}")
        return 1
    except (OSError, ValueError) as e:
        print(f"unhealthy: cannot read health state: {e}")
        return 1

    if not state.get('mqtt_connected'):
        print("unhealthy: not connected to MQTT broker")
        return 1

    updated_at = state.get('updated_at')
    if not isinstance(updated_at, int):
        print("unhealthy: health state has no usable updated_at")
        return 1

    age = int(time.time()) - updated_at
    if age > HEARTBEAT_MAX_AGE_SECONDS:
        print(f"unhealthy: heartbeat is {age}s old (max {HEARTBEAT_MAX_AGE_SECONDS}s)")
        return 1

    print(f"healthy: connected, heartbeat {age}s old")
    return 0


if __name__ == "__main__":
    sys.exit(main())
