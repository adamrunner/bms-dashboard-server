#!/usr/bin/env bash
#
# Provision dedicated Anton MQTT credentials without printing secrets.
#
# Usage:
#   configure-mqtt-credentials.sh provision <device-id>
#   configure-mqtt-credentials.sh retire-admin
#
# The provision command:
#   - backs up the current MQTT password file, ACL, and .env;
#   - generates separate logger and device passwords;
#   - changes the backend logger from admin to bms-logger;
#   - restarts and verifies the broker and logger;
#   - writes the device credential to a mode-0600 file in the caller's home.
#
# retire-admin is deliberately separate because older esp32-bms-monitor
# firmware may still use that account.

set -Eeuo pipefail
umask 077

app_dir="${BMS_APP_DIR:-/home/adamrunner/bms-dashboard-server}"
broker_container="${MQTT_BROKER_CONTAINER:-mosquitto-broker}"
logger_container="${MQTT_LOGGER_CONTAINER:-bms-mqtt-logger}"
password_file=/mosquitto/config/passwords/password_file
logger_username=bms-logger
backup_root="${BMS_BACKUP_DIR:-/home/adamrunner/bms-dashboard-backups}"
credential_output_dir="${MQTT_CREDENTIAL_OUTPUT_DIR:-/home/adamrunner}"

backup_dir=
rollback_armed=false

usage() {
    cat <<'EOF'
Usage:
  configure-mqtt-credentials.sh provision <device-id>
  configure-mqtt-credentials.sh retire-admin

The device ID must match the ESP32 telemetry identity exactly, for example
gw-a1b2c3. Passwords are generated locally and are never printed.
EOF
}

fail() {
    echo "Error: $*" >&2
    if [[ "$rollback_armed" == true ]]; then
        restore_backup
        rollback_armed=false
    fi
    exit 1
}

require_command() {
    command -v "$1" >/dev/null 2>&1 || fail "missing required command: $1"
}

container_running() {
    [[ "$(docker inspect -f '{{.State.Running}}' "$1" 2>/dev/null || true)" == true ]]
}

create_backup() {
    local timestamp
    timestamp="$(date +%Y%m%dT%H%M%S%z)"
    backup_dir="${backup_root}/mqtt-credentials-${timestamp}"
    install -d -m 0700 "$backup_dir"
    install -m 0600 "${app_dir}/.env" "${backup_dir}/env"
    install -m 0600 "${app_dir}/mosquitto/config/acl" "${backup_dir}/acl"
    docker cp \
        "${broker_container}:${password_file}" \
        "${backup_dir}/password_file" >/dev/null
    chmod 0600 "${backup_dir}/password_file"
    rollback_armed=true
}

restore_backup() {
    [[ "$rollback_armed" == true && -n "$backup_dir" ]] || return 0

    echo "Migration failed; restoring MQTT credentials from ${backup_dir}." >&2
    set +e
    install -m 0600 "${backup_dir}/env" "${app_dir}/.env"
    install -m 0640 "${backup_dir}/acl" \
        "${app_dir}/mosquitto/config/acl"
    docker cp \
        "${backup_dir}/password_file" \
        "${broker_container}:${password_file}" >/dev/null
    (
        cd "$app_dir" &&
            docker compose up -d --force-recreate mosquitto bms-logger
    )
    set -e
}

on_error() {
    local exit_code=$?
    trap - ERR
    restore_backup
    exit "$exit_code"
}

set_broker_password() {
    local username="$1"
    local password="$2"

    docker exec \
        -e CREDENTIAL_USERNAME="$username" \
        -e CREDENTIAL_PASSWORD="$password" \
        "$broker_container" \
        sh -eu -c \
        'mosquitto_passwd -b "$1" "$CREDENTIAL_USERNAME" "$CREDENTIAL_PASSWORD"' \
        sh "$password_file"
}

upsert_env_value() {
    local key="$1"
    local value="$2"
    local source_file="${app_dir}/.env"
    local temp_file
    local found=false
    local line

    temp_file="$(mktemp "${source_file}.XXXXXX")"
    while IFS= read -r line || [[ -n "$line" ]]; do
        if [[ "$line" == "${key}="* ]]; then
            if [[ "$found" == false ]]; then
                printf '%s=%s\n' "$key" "$value" >>"$temp_file"
                found=true
            fi
        else
            printf '%s\n' "$line" >>"$temp_file"
        fi
    done <"$source_file"

    if [[ "$found" == false ]]; then
        printf '%s=%s\n' "$key" "$value" >>"$temp_file"
    fi

    chmod 0600 "$temp_file"
    mv "$temp_file" "$source_file"
}

wait_for_broker_health() {
    local attempt
    local status

    for attempt in {1..30}; do
        status="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' \
            "$broker_container" 2>/dev/null || true)"
        case "$status" in
            healthy)
                return 0
                ;;
            unhealthy)
                return 1
                ;;
        esac
        sleep 2
    done
    return 1
}

verify_logger_connection() {
    local attempt
    local logs

    for attempt in {1..15}; do
        logs="$(docker logs --since 2m "$logger_container" 2>&1 || true)"
        if grep -q "Connected to MQTT broker" <<<"$logs" &&
            grep -q "Subscribed" <<<"$logs"; then
            return 0
        fi
        sleep 2
    done
    return 1
}

restart_and_verify() {
    (
        cd "$app_dir"
        docker compose config -q
        docker compose up -d --force-recreate mosquitto bms-logger
    )

    wait_for_broker_health ||
        fail "Mosquitto did not become healthy after credential migration"
    verify_logger_connection ||
        fail "the backend logger did not reconnect and subscribe"
}

provision() {
    local device_id="${1:-}"
    local logger_password
    local device_password
    local credential_file
    local credential_temp

    [[ -n "$device_id" ]] || fail "provision requires the ESP32 device ID"
    [[ ${#device_id} -le 32 ]] ||
        fail "device ID must be no more than 32 characters"
    [[ "$device_id" =~ ^[A-Za-z0-9][A-Za-z0-9_-]*$ ]] ||
        fail "device ID may contain only letters, numbers, hyphens, and underscores"
    [[ "$device_id" != admin && "$device_id" != "$logger_username" ]] ||
        fail "device ID conflicts with a reserved MQTT username"

    grep -q '^user bms-logger$' "${app_dir}/mosquitto/config/acl" ||
        fail "deployed ACL does not contain the dedicated logger policy; update the repository first"
    grep -q '^pattern write bms/telemetry/%u$' \
        "${app_dir}/mosquitto/config/acl" ||
        fail "deployed ACL does not contain the device topic policy"
    grep -q '^pattern write bms/status/%u$' \
        "${app_dir}/mosquitto/config/acl" ||
        fail "deployed ACL does not contain the device status topic policy"

    create_backup
    trap on_error ERR

    logger_password="$(openssl rand -hex 32)"
    # ESP32 firmware reserves one byte of its 64-byte password buffer for NUL.
    # Keep generated device credentials comfortably below its 63-character
    # input limit.
    device_password="$(openssl rand -hex 24)"

    set_broker_password "$logger_username" "$logger_password"
    set_broker_password "$device_id" "$device_password"
    upsert_env_value MQTT_USERNAME "$logger_username"
    upsert_env_value MQTT_PASSWORD "$logger_password"

    restart_and_verify

    install -d -m 0700 "$credential_output_dir"
    credential_file="${credential_output_dir}/mqtt-device-credentials-${device_id}.txt"
    credential_temp="$(mktemp "${credential_file}.XXXXXX")"
    {
        printf 'URI=mqtts://home.adamrunner.com:8883\n'
        printf 'USERNAME=%s\n' "$device_id"
        printf 'PASSWORD=%s\n' "$device_password"
        printf 'BASE_TOPIC=bms/telemetry\n'
    } >"$credential_temp"
    chmod 0600 "$credential_temp"
    mv "$credential_temp" "$credential_file"

    rollback_armed=false
    trap - ERR
    unset logger_password device_password

    echo "MQTT credential migration succeeded."
    echo "Backend logger account: ${logger_username}"
    echo "Device account: ${device_id}"
    echo "Device credential file: ${credential_file}"
    echo "Backup: ${backup_dir}"
    echo
    echo "Move the device credential into your password manager, configure the"
    echo "ESP32, then securely delete the credential file."
    echo "The legacy admin account remains active for esp32-bms-monitor."
}

retire_admin() {
    local configured_username
    local confirmation

    configured_username="$(sed -n 's/^MQTT_USERNAME=//p' "${app_dir}/.env" |
        tail -n 1)"
    [[ "$configured_username" == "$logger_username" ]] ||
        fail "backend logger has not been migrated to ${logger_username}"

    cat <<'EOF'
This permanently removes the admin password entry.

Do not continue until every esp32-bms-monitor using the legacy admin
credential has been reconfigured and verified.
EOF
    read -r -p 'Type "retire admin" to continue: ' confirmation
    [[ "$confirmation" == "retire admin" ]] || fail "confirmation did not match"

    create_backup
    trap on_error ERR

    docker exec "$broker_container" \
        mosquitto_passwd -D "$password_file" admin
    restart_and_verify

    if docker exec "$broker_container" sh -eu -c \
        'cut -d: -f1 "$1" | grep -Fx admin' sh "$password_file" \
        >/dev/null; then
        fail "admin still exists in the password file"
    fi

    rollback_armed=false
    trap - ERR
    echo "Legacy admin MQTT account removed."
    echo "Backup: ${backup_dir}"
}

main() {
    local command="${1:-}"

    case "$command" in
        -h | --help | help | "")
            usage
            return 0
            ;;
    esac

    require_command docker
    require_command openssl
    require_command install
    require_command mktemp

    [[ -d "$app_dir" ]] || fail "missing application directory: $app_dir"
    [[ -f "${app_dir}/.env" ]] || fail "missing ${app_dir}/.env"
    [[ -f "${app_dir}/mosquitto/config/acl" ]] ||
        fail "missing Mosquitto ACL"
    container_running "$broker_container" ||
        fail "${broker_container} is not running"

    case "$command" in
        provision)
            provision "${2:-}"
            ;;
        retire-admin)
            retire_admin
            ;;
        *)
            usage >&2
            fail "unknown command: $command"
            ;;
    esac
}

main "$@"
