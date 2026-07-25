#!/usr/bin/env bash
#
# Set non-development runtime mode and create a Flask secret without exposing
# existing MQTT credentials or the generated secret.

set -euo pipefail
umask 077

app_dir=/home/adamrunner/bms-dashboard-server
env_file="${app_dir}/.env"

if [[ ! -f "$env_file" ]]; then
    echo "Missing $env_file" >&2
    exit 1
fi

upsert_env() {
    local key="$1"
    local value="$2"
    local temp
    temp="$(mktemp "${env_file}.XXXXXX")"
    awk -v key="$key" -v value="$value" '
        BEGIN { replaced = 0 }
        index($0, key "=") == 1 {
            if (!replaced) {
                print key "=" value
                replaced = 1
            }
            next
        }
        { print }
        END {
            if (!replaced) {
                print key "=" value
            }
        }
    ' "$env_file" >"$temp"
    chmod 0600 "$temp"
    mv "$temp" "$env_file"
}

upsert_env APP_ENV production

if ! grep -Eq '^FLASK_SECRET_KEY=.+$' "$env_file"; then
    upsert_env FLASK_SECRET_KEY "$(openssl rand -hex 32)"
fi

chmod 0600 "$env_file"

cd "$app_dir"
docker compose config -q
docker compose up -d --force-recreate bms-logger bms-dashboard

echo "Production environment mode is active; secret values were not displayed."
