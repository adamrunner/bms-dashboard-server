let fleetDevices = [];
let selectedDeviceId = '';
let nextHistoryBeforeId = null;

function formatAge(seconds) {
    if (seconds === null || seconds === undefined) {
        return 'Unknown';
    }
    const age = Math.max(0, Math.floor(Number(seconds) || 0));
    if (age < 60) return `${age}s ago`;
    if (age < 3600) return `${Math.floor(age / 60)}m ago`;
    if (age < 86400) return `${Math.floor(age / 3600)}h ago`;
    return `${Math.floor(age / 86400)}d ago`;
}

function formatTimestamp(epochSeconds) {
    if (!epochSeconds) return 'Unknown';
    return new Date(epochSeconds * 1000).toLocaleString();
}

function formatLabel(value) {
    if (!value) return 'Unknown';
    return value
        .split('_')
        .map(word => word.charAt(0).toUpperCase() + word.slice(1))
        .join(' ');
}

function availabilityState(device) {
    if (device.mqtt_online === true) return 'online';
    if (device.mqtt_online === false) return 'offline';
    return 'unknown';
}

function badge(text, className) {
    const element = document.createElement('span');
    element.className = `badge ${className}`;
    element.textContent = text;
    return element;
}

function tableCell(text) {
    const cell = document.createElement('td');
    cell.textContent = text;
    return cell;
}

async function fetchJson(url) {
    const response = await fetch(url, {headers: {'Accept': 'application/json'}});
    const payload = await response.json();
    if (!response.ok) {
        throw new Error(payload.error || `HTTP ${response.status}`);
    }
    return payload;
}

function showError(error) {
    const element = document.getElementById('fleetError');
    element.textContent = error.message || String(error);
    element.classList.remove('d-none');
}

function clearError() {
    document.getElementById('fleetError').classList.add('d-none');
}

function renderSummary(summary) {
    document.getElementById('fleetDeviceCount').textContent = summary.device_count;
    document.getElementById('fleetOnlineCount').textContent = summary.online_count;
    document.getElementById('fleetPendingCount').textContent =
        summary.pending_verify_count;
    document.getElementById('fleetVersionCount').textContent =
        summary.firmware_versions.filter(item => item.version !== 'Unknown').length;

    const distribution = document.getElementById('firmwareDistribution');
    distribution.replaceChildren();
    summary.firmware_versions.forEach(item => {
        const versionBadge = badge(
            `${item.version}: ${item.count}`,
            item.version === 'Unknown' ? 'text-bg-secondary' : 'text-bg-primary'
        );
        versionBadge.classList.add('me-2', 'mb-1');
        distribution.appendChild(versionBadge);
    });

    const firmwareFilter = document.getElementById('firmwareFilter');
    const selected = firmwareFilter.value;
    firmwareFilter.replaceChildren(new Option('All versions', ''));
    summary.firmware_versions.forEach(item => {
        firmwareFilter.add(new Option(item.version, item.version));
    });
    if ([...firmwareFilter.options].some(option => option.value === selected)) {
        firmwareFilter.value = selected;
    }
}

function renderFleetTable() {
    const deviceQuery = document.getElementById('deviceFilter').value
        .trim()
        .toLowerCase();
    const firmware = document.getElementById('firmwareFilter').value;
    const availability = document.getElementById('availabilityFilter').value;
    const filtered = fleetDevices.filter(device => {
        const deviceMatches = !deviceQuery
            || device.device_id.toLowerCase().includes(deviceQuery);
        const deviceFirmware = device.firmware_version || 'Unknown';
        return deviceMatches
            && (!firmware || deviceFirmware === firmware)
            && (!availability || availabilityState(device) === availability);
    });

    const body = document.getElementById('fleetTableBody');
    body.replaceChildren();
    filtered.forEach(device => {
        const row = document.createElement('tr');
        if (device.device_id === selectedDeviceId) {
            row.classList.add('table-primary');
        }

        const deviceCell = document.createElement('td');
        const selectButton = document.createElement('button');
        selectButton.type = 'button';
        selectButton.className = 'btn btn-link p-0 fw-semibold';
        selectButton.textContent = device.device_id;
        selectButton.addEventListener('click', () => selectDevice(device.device_id));
        deviceCell.appendChild(selectButton);
        row.appendChild(deviceCell);

        const availabilityCell = document.createElement('td');
        const state = availabilityState(device);
        availabilityCell.appendChild(badge(
            `${formatLabel(state)} · ${formatAge(device.availability_age_seconds)}`,
            state === 'online'
                ? 'text-bg-success'
                : state === 'offline' ? 'text-bg-danger' : 'text-bg-secondary'
        ));
        row.appendChild(availabilityCell);

        row.appendChild(tableCell(device.firmware_version || 'Unknown'));

        const otaCell = document.createElement('td');
        otaCell.appendChild(badge(
            device.pending_verify === true ? 'Pending' :
                device.status_id ? 'Verified' : 'Unknown',
            device.pending_verify === true ? 'text-bg-warning' :
                device.status_id ? 'text-bg-success' : 'text-bg-secondary'
        ));
        if (device.ota_slot) {
            otaCell.append(` · ${device.ota_slot}`);
        }
        row.appendChild(otaCell);

        row.appendChild(tableCell(formatLabel(device.reset_reason)));
        row.appendChild(tableCell(formatAge(device.status_age_seconds)));
        row.appendChild(tableCell(formatAge(device.telemetry_age_seconds)));
        body.appendChild(row);
    });

    document.getElementById('fleetEmptyState').classList.toggle(
        'd-none',
        filtered.length > 0
    );
}

function historyCell(text) {
    const cell = document.createElement('td');
    cell.textContent = text;
    return cell;
}

function appendHistory(records, reset) {
    const body = document.getElementById('historyTableBody');
    if (reset) body.replaceChildren();

    records.forEach(status => {
        const row = document.createElement('tr');
        row.appendChild(historyCell(formatTimestamp(status.received_at)));
        row.appendChild(historyCell(formatLabel(status.status_reason || 'checkin')));
        row.appendChild(historyCell(status.firmware_version || 'Unknown'));
        row.appendChild(historyCell(
            `${status.pending_verify ? 'Pending' : 'Verified'}`
            + (status.ota_slot ? ` · ${status.ota_slot}` : '')
        ));
        row.appendChild(historyCell(formatLabel(status.reset_reason)));
        row.appendChild(historyCell(
            `${status.boot_id || 'Unknown'}`
            + (status.status_seq ? ` / ${status.status_seq}` : '')
        ));
        row.appendChild(historyCell(
            status.reported_at
                ? `${formatTimestamp(status.reported_at)} (${status.time_source || 'unknown'})`
                : 'Not synchronized'
        ));
        row.appendChild(historyCell(
            status.mqtt_retained ? 'Retained snapshot' : 'Live check-in'
        ));
        body.appendChild(row);
    });

    const hasRows = body.children.length > 0;
    const emptyState = document.getElementById('historyEmptyState');
    emptyState.textContent = selectedDeviceId
        ? 'No status history received for this device.'
        : 'No device selected.';
    emptyState.classList.toggle('d-none', hasRows);
}

async function loadHistory(reset = false) {
    if (!selectedDeviceId) {
        appendHistory([], true);
        return;
    }
    if (reset) nextHistoryBeforeId = null;

    const params = new URLSearchParams({
        bms_id: selectedDeviceId,
        limit: '50'
    });
    if (!reset && nextHistoryBeforeId) {
        params.set('before_id', String(nextHistoryBeforeId));
    }

    try {
        const payload = await fetchJson(`/api/device-status/history?${params}`);
        appendHistory(payload.records, reset);
        nextHistoryBeforeId = payload.next_before_id;
        document.getElementById('loadMoreHistory').classList.toggle(
            'd-none',
            !nextHistoryBeforeId
        );
    } catch (error) {
        showError(error);
    }
}

function selectDevice(deviceId) {
    selectedDeviceId = deviceId;
    const params = new URLSearchParams(window.location.search);
    params.set('bms_id', deviceId);
    history.replaceState(null, '', `${window.location.pathname}?${params}`);
    document.getElementById('historyDeviceLabel').textContent = deviceId;
    const telemetryLink = document.getElementById('openTelemetry');
    telemetryLink.href = `/?bms_id=${encodeURIComponent(deviceId)}`;
    telemetryLink.classList.remove('d-none');
    renderFleetTable();
    loadHistory(true);
}

async function loadFleet() {
    clearError();
    try {
        const payload = await fetchJson('/api/fleet/status');
        fleetDevices = payload.devices;
        renderSummary(payload.summary);
        renderFleetTable();

        if (!selectedDeviceId) {
            const requested = new URLSearchParams(window.location.search).get('bms_id');
            const initial = fleetDevices.find(device => device.device_id === requested)
                || fleetDevices[0];
            if (initial) selectDevice(initial.device_id);
        }
    } catch (error) {
        showError(error);
    }
}

document.addEventListener('DOMContentLoaded', () => {
    document.getElementById('deviceFilter').addEventListener('input', renderFleetTable);
    document.getElementById('firmwareFilter').addEventListener('change', renderFleetTable);
    document.getElementById('availabilityFilter').addEventListener(
        'change',
        renderFleetTable
    );
    document.getElementById('refreshFleet').addEventListener('click', async () => {
        await loadFleet();
        if (selectedDeviceId) await loadHistory(true);
    });
    document.getElementById('loadMoreHistory').addEventListener(
        'click',
        () => loadHistory(false)
    );
    loadFleet();
});
