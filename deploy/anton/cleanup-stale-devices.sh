#!/usr/bin/env bash
#
# Back up the production telemetry database, verify the backup, then remove the
# records belonging to retired test devices and reclaim the freed pages.
#
# The fleet contains one real gateway. Rows left over from bench testing and
# commissioning dominate the table and distort every whole-database figure, so
# retiring them is a data-quality fix rather than a disk-space one.
#
# Defaults to a dry run. Nothing is written without --apply.
#
# Usage:
#   cleanup-stale-devices.sh                              # dry run, default targets
#   cleanup-stale-devices.sh --apply
#   cleanup-stale-devices.sh --apply --no-vacuum
#   cleanup-stale-devices.sh --apply gw-retired-one gw-retired-two
#
# Environment overrides:
#   BMS_APP_DIR               default /home/adamrunner/bms-dashboard-server
#   BMS_DASHBOARD_CONTAINER   default bms-dashboard
#   BMS_PROTECTED_DEVICES     space separated, default "gw-e3aba4"

set -Eeuo pipefail

app_dir="${BMS_APP_DIR:-/home/adamrunner/bms-dashboard-server}"
dashboard_container="${BMS_DASHBOARD_CONTAINER:-bms-dashboard}"
protected_devices="${BMS_PROTECTED_DEVICES:-gw-e3aba4}"

db_path="/app/data/bms_telemetry.db"
backup_dir_container="/app/data/backups"
backup_dir_host="${app_dir}/backups"
delete_batch_size=50000

apply=0
run_vacuum=1
devices=()

fail() {
    echo "ERROR: $*" >&2
    exit 1
}

# Every table that carries a device identifier, with the column it uses.
# firmware_expectations stores the fleet-wide default as a NULL device_id,
# which an equality match never touches.
tables="bms_telemetry:bms_id
device_status_checkins:device_id
device_availability_events:device_id
device_alerts:device_id
firmware_expectations:device_id"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --apply)      apply=1 ;;
        --dry-run)    apply=0 ;;
        --no-vacuum)  run_vacuum=0 ;;
        -h|--help)    sed -n '2,25p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        -*)           fail "unknown option: $1" ;;
        *)            devices+=("$1") ;;
    esac
    shift
done

if [[ ${#devices[@]} -eq 0 ]]; then
    devices=(bms-404CCAFFFE43 gw-commissioning-test)
fi

for device in "${devices[@]}"; do
    [[ "$device" =~ ^[A-Za-z0-9][A-Za-z0-9_-]*$ ]] \
        || fail "refusing device id with unsupported characters: ${device}"
    for protected in $protected_devices; do
        [[ "$device" == "$protected" ]] \
            && fail "refusing to delete protected device: ${device}"
    done
done

docker inspect "$dashboard_container" >/dev/null 2>&1 \
    || fail "container ${dashboard_container} is not available"

# A generous busy timeout matters: the logger writes to this database roughly
# every ten seconds and must not lose a sample to a transient lock. Use the
# .timeout dot-command rather than PRAGMA busy_timeout, which prints its new
# value and would corrupt every parsed result.
sqlite() {
    docker exec "$dashboard_container" \
        sqlite3 -cmd ".timeout 30000" "$db_path" "$@"
}

report_counts() {
    local label="$1" device table column count
    echo "  ${label}:"
    for device in "${devices[@]}"; do
        for entry in $tables; do
            table="${entry%%:*}"
            column="${entry##*:}"
            count="$(sqlite "SELECT COUNT(*) FROM ${table} WHERE ${column} = '${device}';")"
            [[ "$count" == "0" ]] && continue
            printf '    %-22s %-28s %s\n' "$device" "$table" "$count"
        done
    done
}

db_size() {
    sqlite "SELECT (SELECT * FROM pragma_page_count()) * (SELECT * FROM pragma_page_size());"
}

human() {
    awk -v b="$1" 'BEGIN { printf "%.1f MB", b / 1048576 }'
}

echo "Database:  ${dashboard_container}:${db_path}"
echo "Targets:   ${devices[*]}"
echo "Protected: ${protected_devices}"
echo "Size:      $(human "$(db_size)")"
echo
echo "Rows to remove"
report_counts "before"
echo

total_before="$(sqlite 'SELECT COUNT(*) FROM bms_telemetry;')"
doomed=0
for device in "${devices[@]}"; do
    count="$(sqlite "SELECT COUNT(*) FROM bms_telemetry WHERE bms_id = '${device}';")"
    doomed=$(( doomed + count ))
done
# Derive the survivor count by subtraction rather than a NOT IN list, so the
# expectation stays correct regardless of NULL bms_id rows or quoting.
kept_before=$(( total_before - doomed ))
echo "bms_telemetry total rows:     ${total_before}"
echo "rows to remove:               ${doomed}"
echo "rows that must survive:       ${kept_before}"
echo

if [[ $apply -eq 0 ]]; then
    echo "Dry run. Re-run with --apply to take a backup and delete."
    exit 0
fi

timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
# Neutral name: the script is safe to re-run, and a later run's backup is not
# a "pre-cleanup" snapshot of anything. Note .backup copies free pages too, so
# the file matches the live database size until a VACUUM has run.
backup_name="bms_telemetry-backup-${timestamp}.db"
backup_container="${backup_dir_container}/${backup_name}"
backup_host="${backup_dir_host}/${backup_name}"

echo "Backing up to ${backup_host}"
docker exec "$dashboard_container" mkdir -p "$backup_dir_container"
# .backup is an online backup; it is consistent without stopping the writers.
sqlite ".backup '${backup_container}'"

echo "Verifying backup"
check="$(docker exec "$dashboard_container" sqlite3 "$backup_container" 'PRAGMA quick_check;')"
[[ "$check" == "ok" ]] || fail "backup failed quick_check: ${check}"

backup_rows="$(docker exec "$dashboard_container" sqlite3 "$backup_container" 'SELECT COUNT(*) FROM bms_telemetry;')"
[[ "$backup_rows" -ge "$total_before" ]] \
    || fail "backup holds ${backup_rows} rows against ${total_before} in production"
echo "  quick_check ok, ${backup_rows} telemetry rows captured"
echo

# Delete in batches so each write lock is short and the logger can interleave
# its inserts, rather than blocking it behind one multi-million row statement.
for device in "${devices[@]}"; do
    for entry in $tables; do
        table="${entry%%:*}"
        column="${entry##*:}"
        removed_total=0
        while true; do
            removed="$(sqlite "DELETE FROM ${table} WHERE rowid IN (
                    SELECT rowid FROM ${table} WHERE ${column} = '${device}'
                    LIMIT ${delete_batch_size});
                SELECT changes();")"
            removed_total=$(( removed_total + removed ))
            [[ "$removed" -eq 0 ]] && break
            printf '\r  %-22s %-28s %s' "$device" "$table" "$removed_total"
        done
        [[ "$removed_total" -gt 0 ]] \
            && printf '\r  %-22s %-28s %s removed\n' "$device" "$table" "$removed_total"
    done
done
echo

kept_after="$(sqlite 'SELECT COUNT(*) FROM bms_telemetry;')"

# The logger keeps inserting throughout, so the survivor count is a floor, not
# an equality. What must hold exactly is that no targeted row is left behind.
[[ "$kept_after" -ge "$kept_before" ]] \
    || fail "expected at least ${kept_before} surviving rows, found ${kept_after} — restore from ${backup_host}"

for device in "${devices[@]}"; do
    for entry in $tables; do
        table="${entry%%:*}"
        column="${entry##*:}"
        left="$(sqlite "SELECT COUNT(*) FROM ${table} WHERE ${column} = '${device}';")"
        [[ "$left" -eq 0 ]] \
            || fail "${left} rows still present in ${table} for ${device} — restore from ${backup_host}"
    done
done

arrived=$(( kept_after - kept_before ))
echo "Surviving telemetry rows: ${kept_after} (${arrived} arrived during cleanup)"
echo "Targeted rows remaining:  0"

if [[ $run_vacuum -eq 1 ]]; then
    echo
    echo "Reclaiming space with VACUUM (takes an exclusive lock; the gateway"
    echo "spools to SD and replays if a sample cannot be written meanwhile)"
    size_before="$(db_size)"
    sqlite 'VACUUM;'
    echo "  $(human "$size_before") -> $(human "$(db_size)")"
fi

echo
echo "Integrity check"
check="$(sqlite 'PRAGMA quick_check;')"
[[ "$check" == "ok" ]] || fail "post-cleanup quick_check failed: ${check}"
echo "  ok"
echo
echo "Remaining devices"
# sqlite3 treats arguments after the filename as SQL, so format in the query
# rather than reaching for -column here.
sqlite "SELECT '  ' || bms_id || '  ' || COUNT(*) FROM bms_telemetry GROUP BY bms_id;"
echo
echo "Backup retained at ${backup_host}"
