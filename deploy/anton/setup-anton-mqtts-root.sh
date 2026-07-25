#!/usr/bin/env bash
#
# Run on anton as root after the updated bms-dashboard-server checkout has
# been deployed:
#
#   sudo /home/adamrunner/setup-anton-mqtts-root.sh you@example.com
#
# The script reuses the existing Cloudflare DNS updater token without printing
# it, moves it into root-only credential files, issues the certificate, installs
# the Mosquitto TLS runtime snippet and renewal hook, and activates port 8883.

set -euo pipefail
umask 077

if [[ $EUID -ne 0 ]]; then
    echo "Run this script with sudo." >&2
    exit 1
fi

email="${1:-}"
if [[ -z "$email" ]]; then
    read -r -p "Email for Let's Encrypt expiration notices: " email
fi
if [[ "$email" != *@* ]]; then
    echo "A valid email address is required." >&2
    exit 1
fi

app_dir=/home/adamrunner/bms-dashboard-server
updater=/usr/local/bin/cloudflare-dns-updater.sh
cloudflare_dir=/etc/cloudflare
dns_env="${cloudflare_dir}/dns-updater.env"
certbot_credentials="${cloudflare_dir}/certbot.ini"
cert_name=home.adamrunner.com
cert_dir="${app_dir}/mosquitto/config/certs"
runtime_dir="${app_dir}/mosquitto/config/runtime.d"
deploy_hook=/etc/letsencrypt/renewal-hooks/deploy/50-anton-mosquitto

if [[ ! -d "$app_dir" ]]; then
    echo "Missing deployment checkout: $app_dir" >&2
    exit 1
fi

install -d -m 0700 "$cloudflare_dir"

if [[ ! -s "$dns_env" || ! -s "$certbot_credentials" ]]; then
    if [[ ! -r "$updater" ]]; then
        echo "Cannot read the existing updater token from $updater" >&2
        exit 1
    fi
    token_line="$(grep -m1 '^CF_API_TOKEN=' "$updater" || true)"
    token="${token_line#CF_API_TOKEN=}"
    token="${token#\"}"
    token="${token%\"}"
    if [[ ! "$token" =~ ^[A-Za-z0-9_-]+$ ]]; then
        echo "The existing updater token is missing or has an unexpected format." >&2
        exit 1
    fi
    printf "CF_API_TOKEN='%s'\n" "$token" >"$dns_env"
    printf "dns_cloudflare_api_token = %s\n" "$token" >"$certbot_credentials"
    chmod 0600 "$dns_env" "$certbot_credentials"
    unset token token_line
fi

# Replace the world-readable embedded token with a root-only sourced secret,
# while keeping the existing path used by cron.
if ! grep -q '^source /etc/cloudflare/dns-updater.env$' "$updater"; then
    cp -a "$updater" "${updater}.pre-secret-file"
    chmod 0600 "${updater}.pre-secret-file"
    python3 - "$updater" <<'PY'
from pathlib import Path
import sys

path = Path(sys.argv[1])
text = path.read_text()
lines = text.splitlines()
for index, line in enumerate(lines):
    if line.startswith("CF_API_TOKEN="):
        lines[index] = "source /etc/cloudflare/dns-updater.env"
        break
else:
    raise SystemExit("CF_API_TOKEN assignment not found")
path.write_text("\n".join(lines) + "\n")
PY
fi
chown root:root "$updater"
chmod 0755 "$updater"

snap set certbot trust-plugin-with-root=ok
if ! snap list certbot-dns-cloudflare >/dev/null 2>&1; then
    snap install certbot-dns-cloudflare
fi

certbot certonly \
    --non-interactive \
    --agree-tos \
    --email "$email" \
    --cert-name "$cert_name" \
    --dns-cloudflare \
    --dns-cloudflare-credentials "$certbot_credentials" \
    --dns-cloudflare-propagation-seconds 30 \
    -d "$cert_name"

install -d -o 1883 -g 1883 -m 0750 "$cert_dir" "$runtime_dir"
install -m 0644 "/etc/letsencrypt/live/${cert_name}/fullchain.pem" \
    "${cert_dir}/fullchain.pem"
install -o 1883 -g 1883 -m 0640 \
    "/etc/letsencrypt/live/${cert_name}/privkey.pem" \
    "${cert_dir}/privkey.pem"

cat >"${runtime_dir}/50-mqtts.conf" <<'EOF'
listener 8883 0.0.0.0
protocol mqtt
certfile /mosquitto/config/certs/fullchain.pem
keyfile /mosquitto/config/certs/privkey.pem
tls_version tlsv1.2
EOF
chown root:1883 "${runtime_dir}/50-mqtts.conf"
chmod 0640 "${runtime_dir}/50-mqtts.conf"

install -d -m 0755 "$(dirname "$deploy_hook")"
cat >"$deploy_hook" <<EOF
#!/usr/bin/env bash
set -euo pipefail
if [[ "\${RENEWED_LINEAGE:-}" != "/etc/letsencrypt/live/${cert_name}" ]]; then
    exit 0
fi
install -m 0644 "\${RENEWED_LINEAGE}/fullchain.pem" "${cert_dir}/fullchain.pem"
install -o 1883 -g 1883 -m 0640 "\${RENEWED_LINEAGE}/privkey.pem" "${cert_dir}/privkey.pem"
/usr/bin/docker kill --signal=HUP mosquitto-broker >/dev/null
EOF
chmod 0755 "$deploy_hook"

cd "$app_dir"
docker compose config -q
docker compose up -d --force-recreate mosquitto

tls_ok=0
for _ in {1..20}; do
    if openssl s_client \
        -connect 127.0.0.1:8883 \
        -servername "$cert_name" \
        -verify_hostname "$cert_name" \
        -verify_return_error </dev/null 2>/dev/null |
        grep -q 'Verify return code: 0 (ok)'; then
        echo "MQTTS certificate and hostname validation succeeded."
        tls_ok=1
        break
    fi
    sleep 1
done

if [[ $tls_ok -ne 1 ]]; then
    echo "MQTTS certificate or hostname validation failed." >&2
    exit 1
fi

if ! ss -lnt | grep -q ':8883 '; then
    echo "Mosquitto did not open port 8883; inspect docker compose logs mosquitto." >&2
    exit 1
fi

echo "Anton MQTTS setup complete."
echo "Next: create the device user interactively and run the external acceptance tests."
