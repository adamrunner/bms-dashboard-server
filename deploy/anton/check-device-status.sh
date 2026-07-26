#!/usr/bin/env bash
#
# Print the retained boot/OTA status for one ESP32 gateway without exposing
# the backend MQTT credential.
#
# Usage:
#   check-device-status.sh gw-e3aba4

set -Eeuo pipefail

app_dir="${BMS_APP_DIR:-/home/adamrunner/bms-dashboard-server}"
broker_container="${MQTT_BROKER_CONTAINER:-mosquitto-broker}"
device_id="${1:-}"

if [[ -z "$device_id" || ! "$device_id" =~ ^[A-Za-z0-9][A-Za-z0-9_-]*$ ]]; then
    echo "Usage: $0 <device-id>" >&2
    exit 2
fi

env_file="${app_dir}/.env"
if [[ ! -r "$env_file" ]]; then
    echo "Cannot read ${env_file}" >&2
    exit 1
fi

mqtt_username="$(sed -n 's/^MQTT_USERNAME=//p' "$env_file" | tail -n 1)"
mqtt_password="$(sed -n 's/^MQTT_PASSWORD=//p' "$env_file" | tail -n 1)"
if [[ -z "$mqtt_username" || -z "$mqtt_password" ]]; then
    echo "MQTT_USERNAME or MQTT_PASSWORD is missing from ${env_file}" >&2
    exit 1
fi

docker exec \
    -e STATUS_MQTT_USERNAME="$mqtt_username" \
    -e STATUS_MQTT_PASSWORD="$mqtt_password" \
    "$broker_container" \
    sh -eu -c \
    'exec mosquitto_sub -h localhost -u "$STATUS_MQTT_USERNAME" \
        -P "$STATUS_MQTT_PASSWORD" -t "$1" -C 1 -W 10' \
    sh "bms/status/${device_id}"
