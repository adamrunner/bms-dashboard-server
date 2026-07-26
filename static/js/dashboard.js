// BMS Dashboard JavaScript
let socket;
let charts = {};
let currentTimeRange = 0.017; // hours (1 minute default)
let currentBmsId = '';
let currentResolution = 'auto';
let targetPoints = 300;
let currentBucketSeconds = 10;
let activeDataRequestController = null;
let latestDataRequestId = 0;
let fetchStatusTimer = null;
let bmsSelectionReady = false;
let latestDeviceStatus = null;
let deviceStatusAgeTimer = null;

const VALID_RESOLUTIONS = ['auto', '10s', '30s', '1m', '3m', '5m', '10m', '15m', '30m'];
const CHART_DATA_FIELDS = {
    voltage: ['pack_voltage_v'],
    current: ['pack_current_a'],
    soc: ['state_of_charge_pct'],
    cell: ['cells_v_1', 'cells_v_2', 'cells_v_3', 'cells_v_4'],
    temp: ['temps_c_1', 'temps_c_2', 'temps_c_3'],
    cellDelta: ['cell_voltage_delta_v'],
    power: ['power_w']
};

// Helper function to trigger glow effect on connection status badge
function triggerGlowEffect() {
    const statusElement = document.getElementById('connectionStatus');
    statusElement.classList.remove('glow-pulse', 'glow-flash');
    void statusElement.offsetWidth;
    statusElement.classList.add('glow-flash');
    setTimeout(() => {
        statusElement.classList.remove('glow-flash');
    }, 500);
}

// Helper function to trigger pulse effect (for connected state)
function triggerPulseEffect() {
    const statusElement = document.getElementById('connectionStatus');
    statusElement.classList.remove('glow-pulse', 'glow-flash');
    void statusElement.offsetWidth;
    statusElement.classList.add('glow-pulse');
    setTimeout(() => {
        statusElement.classList.remove('glow-pulse');
    }, 1500);
}

// Chart configurations
const chartConfig = {
    type: 'line',
    options: {
        responsive: true,
        maintainAspectRatio: false,
        scales: {
            x: {
                type: 'time',
                time: {
                    displayFormats: {
                        second: 'HH:mm:ss',
                        minute: 'HH:mm',
                        hour: 'MM/dd HH:mm'
                    }
                },
                title: {
                    display: true,
                    text: 'Time'
                }
            },
            y: {
                beginAtZero: false,
                title: {
                    display: true,
                    text: 'Value'
                }
            }
        },
        plugins: {
            decimation: {
                enabled: true,
                algorithm: 'lttb',
                samples: 300,
                threshold: 600
            },
            legend: {
                position: 'top'
            },
            tooltip: {
                mode: 'index',
                intersect: false
            }
        },
        interaction: {
            mode: 'nearest',
            axis: 'x',
            intersect: false
        },
        animation: {
            duration: 300
        }
    }
};

// Initialize dashboard
document.addEventListener('DOMContentLoaded', function() {
    initializeCharts();
    applyInitialStateFromUrl();
    loadBmsIds();

    document.getElementById('autoRefresh').addEventListener('change', function() {
        updateUrlState();
        if (this.checked) {
            connectWebSocket();
            refreshDashboardData('refresh');
        } else if (socket) {
            socket.disconnect();
        }
    });

    if (document.getElementById('autoRefresh').checked) {
        connectWebSocket();
    }

    deviceStatusAgeTimer = setInterval(() => {
        if (latestDeviceStatus) {
            updateDeviceStatusReceivedTime(latestDeviceStatus);
        }
    }, 30000);
});

function applyInitialStateFromUrl() {
    const params = new URLSearchParams(window.location.search);
    const autoRefreshToggle = document.getElementById('autoRefresh');
    const resolutionSelect = document.getElementById('resolutionSelect');

    const hoursParam = params.get('hours');
    if (hoursParam !== null) {
        const parsedHours = Number.parseFloat(hoursParam);
        if (Number.isFinite(parsedHours) && parsedHours > 0) {
            currentTimeRange = parsedHours;
        }
    }

    const bmsIdParam = params.get('bms_id');
    if (bmsIdParam !== null) {
        currentBmsId = bmsIdParam;
    }

    const resolutionParam = params.get('resolution');
    if (resolutionParam && VALID_RESOLUTIONS.includes(resolutionParam)) {
        currentResolution = resolutionParam;
    }

    const autoRefreshParam = params.get('auto_refresh');
    if (autoRefreshParam !== null) {
        autoRefreshToggle.checked = !['0', 'false', 'off', 'no'].includes(autoRefreshParam.toLowerCase());
    }

    resolutionSelect.value = currentResolution;
    updateActiveTimeRangeButton();
}

function updateUrlState() {
    const url = new URL(window.location.href);
    url.searchParams.set('hours', String(currentTimeRange));
    url.searchParams.set('resolution', currentResolution);
    url.searchParams.set('auto_refresh', document.getElementById('autoRefresh').checked ? '1' : '0');

    if (currentBmsId) {
        url.searchParams.set('bms_id', currentBmsId);
    } else {
        url.searchParams.delete('bms_id');
    }

    history.replaceState(null, '', url);
}

function updateActiveTimeRangeButton() {
    const selectedHours = Number(currentTimeRange);
    document.querySelectorAll('.time-range-btn').forEach(btn => {
        const buttonHours = Number.parseFloat(btn.dataset.hours || '');
        const isActive = Number.isFinite(buttonHours) && Math.abs(buttonHours - selectedHours) < 0.0001;
        btn.classList.toggle('active', isActive);
    });
}

function setLoadingState(isLoading, message = 'Loading telemetry data...', sourceModule = 'time') {
    const moduleMap = {
        bms: 'bmsModuleCard',
        time: 'timeRangeModuleCard',
        refresh: 'refreshModuleCard'
    };
    const controls = document.querySelectorAll('.time-range-btn, #bmsIdSelect, #resolutionSelect');
    controls.forEach(control => {
        control.disabled = isLoading;
    });

    document.querySelectorAll('.module-loading-overlay').forEach(overlay => {
        overlay.classList.remove('show');
    });

    if (!isLoading) {
        return;
    }

    const cardId = moduleMap[sourceModule] || moduleMap.time;
    const moduleCard = document.getElementById(cardId);
    if (!moduleCard) {
        return;
    }

    const overlay = moduleCard.querySelector('.module-loading-overlay');
    const overlayText = moduleCard.querySelector('.module-loading-text');
    if (overlayText) {
        overlayText.textContent = message;
    }
    if (overlay) {
        overlay.classList.add('show');
    }
}

function updateResolutionAndPointInfo(meta = {}) {
    const effectiveSeconds = meta.bucket_seconds || currentBucketSeconds;
    currentBucketSeconds = effectiveSeconds;

    const resolutionInfo = document.getElementById('dataResolutionInfo');
    const pointInfo = document.getElementById('dataPointInfo');

    const resolutionLabel = effectiveSeconds < 60
        ? `${effectiveSeconds}s`
        : `${effectiveSeconds / 60}m`;

    resolutionInfo.textContent = `Resolution: ${resolutionLabel} ${meta.is_aggregated ? '(aggregated)' : '(raw)'}`;
    if (meta.is_aggregated && meta.source_record_count) {
        pointInfo.textContent = `Points: ${meta.point_count || 0} from ${meta.source_record_count} records`;
    } else {
        pointInfo.textContent = `Points: ${meta.point_count || 0}`;
    }
}

function setFetchStatus(message, level = 'warning', autoHideMs = 5000) {
    const badge = document.getElementById('fetchStatus');
    if (!badge) {
        return;
    }

    badge.textContent = message;
    badge.classList.add('fetch-status-visible');
    badge.classList.remove('fetch-status-warning', 'fetch-status-error');
    badge.classList.add(level === 'error' ? 'fetch-status-error' : 'fetch-status-warning');

    if (fetchStatusTimer) {
        clearTimeout(fetchStatusTimer);
    }
    fetchStatusTimer = setTimeout(() => {
        badge.classList.remove('fetch-status-visible');
    }, autoHideMs);
}

function clearFetchStatus() {
    const badge = document.getElementById('fetchStatus');
    if (!badge) {
        return;
    }
    if (fetchStatusTimer) {
        clearTimeout(fetchStatusTimer);
        fetchStatusTimer = null;
    }
    badge.classList.remove('fetch-status-visible');
}

async function fetchJsonWithTimeout(url, options = {}, timeoutMs = 10000) {
    const timeoutController = new AbortController();
    const timeoutId = setTimeout(() => timeoutController.abort(), timeoutMs);

    let combinedSignal = timeoutController.signal;
    if (options.signal) {
        const combinedController = new AbortController();
        const abortCombined = () => combinedController.abort();
        options.signal.addEventListener('abort', abortCombined, { once: true });
        timeoutController.signal.addEventListener('abort', abortCombined, { once: true });
        combinedSignal = combinedController.signal;
    }

    try {
        const response = await fetch(url, { ...options, signal: combinedSignal });
        if (!response.ok) {
            throw new Error(`HTTP ${response.status}`);
        }
        return await response.json();
    } finally {
        clearTimeout(timeoutId);
    }
}

function getDataRequestUrl() {
    const params = new URLSearchParams({
        hours: currentTimeRange,
        resolution: currentResolution,
        target_points: targetPoints
    });

    if (currentBmsId) {
        params.append('bms_id', currentBmsId);
    }

    return `/api/data?${params.toString()}`;
}

function normalizeDataPayload(payload) {
    if (Array.isArray(payload)) {
        return { records: payload, meta: {} };
    }

    return {
        records: payload.records || [],
        meta: payload.meta || {}
    };
}

function sendViewSubscription() {
    if (!socket || !socket.connected || !document.getElementById('autoRefresh').checked) {
        return;
    }

    socket.emit('set_view', {
        hours: currentTimeRange,
        bms_id: currentBmsId,
        resolution: currentResolution,
        target_points: targetPoints
    });
}

// Initialize all charts
function initializeCharts() {
    const voltageCtx = document.getElementById('voltageChart').getContext('2d');
    charts.voltage = new Chart(voltageCtx, {
        ...chartConfig,
        data: {
            datasets: [{
                label: 'Pack Voltage (V)',
                borderColor: '#007bff',
                backgroundColor: 'rgba(0,123,255,0.1)',
                data: []
            }]
        },
        options: {
            ...chartConfig.options,
            scales: {
                ...chartConfig.options.scales,
                y: {
                    ...chartConfig.options.scales.y,
                    title: { display: true, text: 'Voltage (V)' }
                }
            }
        }
    });

    const currentCtx = document.getElementById('currentChart').getContext('2d');
    charts.current = new Chart(currentCtx, {
        ...chartConfig,
        data: {
            datasets: [{
                label: 'Current (A)',
                borderColor: '#28a745',
                backgroundColor: 'rgba(40,167,69,0.1)',
                data: []
            }]
        },
        options: {
            ...chartConfig.options,
            scales: {
                ...chartConfig.options.scales,
                y: {
                    ...chartConfig.options.scales.y,
                    title: { display: true, text: 'Current (A)' }
                }
            }
        }
    });

    const socCtx = document.getElementById('socChart').getContext('2d');
    charts.soc = new Chart(socCtx, {
        ...chartConfig,
        data: {
            datasets: [{
                label: 'State of Charge (%)',
                borderColor: '#ffc107',
                backgroundColor: 'rgba(255,193,7,0.1)',
                data: []
            }]
        },
        options: {
            ...chartConfig.options,
            scales: {
                ...chartConfig.options.scales,
                y: {
                    ...chartConfig.options.scales.y,
                    title: { display: true, text: 'SoC (%)' },
                    min: 0,
                    max: 100
                }
            }
        }
    });

    const cellCtx = document.getElementById('cellChart').getContext('2d');
    charts.cell = new Chart(cellCtx, {
        ...chartConfig,
        data: {
            datasets: [
                { label: 'Cell 1 (V)', borderColor: '#dc3545', backgroundColor: 'rgba(220,53,69,0.1)', data: [] },
                { label: 'Cell 2 (V)', borderColor: '#007bff', backgroundColor: 'rgba(0,123,255,0.1)', data: [] },
                { label: 'Cell 3 (V)', borderColor: '#28a745', backgroundColor: 'rgba(40,167,69,0.1)', data: [] },
                { label: 'Cell 4 (V)', borderColor: '#ffc107', backgroundColor: 'rgba(255,193,7,0.1)', data: [] }
            ]
        },
        options: {
            ...chartConfig.options,
            scales: {
                ...chartConfig.options.scales,
                y: {
                    ...chartConfig.options.scales.y,
                    title: { display: true, text: 'Voltage (V)' }
                }
            }
        }
    });

    const tempCtx = document.getElementById('tempChart').getContext('2d');
    charts.temp = new Chart(tempCtx, {
        ...chartConfig,
        data: {
            datasets: [
                { label: 'Temp 1 (°C)', borderColor: '#fd7e14', backgroundColor: 'rgba(253,126,20,0.1)', data: [] },
                { label: 'Temp 2 (°C)', borderColor: '#e83e8c', backgroundColor: 'rgba(232,62,140,0.1)', data: [] },
                { label: 'Temp 3 (°C)', borderColor: '#6610f2', backgroundColor: 'rgba(102,16,242,0.1)', data: [] }
            ]
        },
        options: {
            ...chartConfig.options,
            scales: {
                ...chartConfig.options.scales,
                y: {
                    ...chartConfig.options.scales.y,
                    title: { display: true, text: 'Temperature (°C)' }
                }
            }
        }
    });

    const cellDeltaCtx = document.getElementById('cellDeltaChart').getContext('2d');
    charts.cellDelta = new Chart(cellDeltaCtx, {
        ...chartConfig,
        data: {
            datasets: [{
                label: 'Cell Delta (V)',
                borderColor: '#6f42c1',
                backgroundColor: 'rgba(111,66,193,0.1)',
                data: []
            }]
        },
        options: {
            ...chartConfig.options,
            scales: {
                ...chartConfig.options.scales,
                y: {
                    ...chartConfig.options.scales.y,
                    title: { display: true, text: 'Delta (V)' }
                }
            }
        }
    });

    const powerCtx = document.getElementById('powerChart').getContext('2d');
    charts.power = new Chart(powerCtx, {
        ...chartConfig,
        data: {
            datasets: [{
                label: 'Power (W)',
                borderColor: '#17a2b8',
                backgroundColor: 'rgba(23,162,184,0.1)',
                data: []
            }]
        },
        options: {
            ...chartConfig.options,
            scales: {
                ...chartConfig.options.scales,
                y: {
                    ...chartConfig.options.scales.y,
                    title: { display: true, text: 'Power (W)' }
                }
            }
        }
    });
}

// Connect to WebSocket
function connectWebSocket() {
    if (socket && socket.connected) {
        return;
    }

    console.log('Attempting to connect WebSocket...');
    socket = io({
        transports: ['websocket', 'polling'],
        upgrade: true,
        rememberUpgrade: true
    });

    socket.on('connect', function() {
        document.getElementById('connectionStatus').textContent = 'Connected';
        document.getElementById('connectionStatus').className = 'badge status-connected';
        console.log('WebSocket connected successfully!', socket.id);
        triggerPulseEffect();
        if (bmsSelectionReady) {
            refreshDashboardData('refresh');
        }
    });

    socket.on('disconnect', function() {
        document.getElementById('connectionStatus').textContent = 'Disconnected';
        document.getElementById('connectionStatus').className = 'badge status-disconnected';
        console.log('WebSocket disconnected');
        triggerGlowEffect();
    });

    socket.on('telemetry_update', function(payload) {
        const point = payload?.point;
        const meta = payload?.meta || {};
        if (!point) {
            return;
        }

        updateChartsWithNewData(point, meta);
        updateCurrentValues(point);
        triggerGlowEffect();
    });

    socket.on('view_data', function(payload) {
        const normalized = normalizeDataPayload(payload);
        setLoadingState(false);
        clearFetchStatus();
        updateChartsWithHistoricalData(normalized.records, normalized.meta);
    });

    socket.on('statistics', function(stats) {
        updateStatistics(stats);
    });

    socket.on('device_status_update', function(status) {
        if (status?.device_id === currentBmsId) {
            renderDeviceStatus(status);
        }
    });

    socket.on('connect_error', function(error) {
        console.error('WebSocket connection error:', error);
    });

    socket.on('error', function(error) {
        setLoadingState(false);
        console.error('WebSocket error:', error);
    });
}

// Load available BMS IDs
function loadBmsIds() {
    fetchJsonWithTimeout('/api/bms-ids')
        .then(bmsIds => {
            clearFetchStatus();
            const select = document.getElementById('bmsIdSelect');
            select.innerHTML = '';

            if (bmsIds.length > 0) {
                const selectedBmsId = currentBmsId && bmsIds.includes(currentBmsId)
                    ? currentBmsId
                    : bmsIds[0];
                currentBmsId = selectedBmsId;
                bmsIds.forEach(bmsId => {
                    const option = document.createElement('option');
                    option.value = bmsId;
                    option.textContent = bmsId;
                    if (bmsId === currentBmsId) {
                        option.selected = true;
                    }
                    select.appendChild(option);
                });
            }

            bmsSelectionReady = true;
            updateUrlState();
            refreshDashboardData('bms');
        })
        .catch(error => {
            console.error('Error loading BMS IDs:', error);
            setFetchStatus('BMS list fetch failed', error.name === 'AbortError' ? 'warning' : 'error');
            bmsSelectionReady = true;
            updateUrlState();
            refreshDashboardData('time');
        });
}

function refreshDashboardData(sourceModule = 'time') {
    updateUrlState();
    loadDeviceStatus();
    if (document.getElementById('autoRefresh').checked && socket && socket.connected) {
        setLoadingState(true, 'Loading telemetry data...', sourceModule);
        sendViewSubscription();
        loadStatistics();
        return;
    }

    loadInitialData(sourceModule);
}

// Load initial data
function loadInitialData(sourceModule = 'time') {
    const requestId = ++latestDataRequestId;

    if (activeDataRequestController) {
        activeDataRequestController.abort();
    }

    activeDataRequestController = new AbortController();
    setLoadingState(true, 'Loading telemetry data...', sourceModule);

    fetchJsonWithTimeout(
        getDataRequestUrl(),
        { signal: activeDataRequestController.signal },
        12000
    )
        .then(payload => {
            if (requestId !== latestDataRequestId) {
                return;
            }
            clearFetchStatus();
            const normalized = normalizeDataPayload(payload);
            updateChartsWithHistoricalData(normalized.records, normalized.meta);
        })
        .catch(error => {
            if (error.name === 'AbortError') {
                if (requestId === latestDataRequestId) {
                    setFetchStatus('Telemetry fetch timed out', 'warning');
                }
                return;
            }
            console.error('Error loading telemetry data:', error);
            setFetchStatus('Telemetry fetch failed', 'error');
        })
        .finally(() => {
            if (requestId === latestDataRequestId) {
                setLoadingState(false);
            }
        });

    loadStatistics();
}

function formatAge(seconds) {
    const age = Math.max(0, Math.floor(Number(seconds) || 0));
    if (age < 60) {
        return `${age}s ago`;
    }
    if (age < 3600) {
        return `${Math.floor(age / 60)}m ago`;
    }
    if (age < 86400) {
        return `${Math.floor(age / 3600)}h ago`;
    }
    return `${Math.floor(age / 86400)}d ago`;
}

function updateDeviceStatusReceivedTime(status) {
    const receivedElement = document.getElementById('deviceStatusReceived');
    if (!status?.received_at) {
        receivedElement.textContent = '--';
        return;
    }

    const receivedAt = new Date(status.received_at * 1000);
    const ageSeconds = Math.max(0, Math.floor((Date.now() - receivedAt.getTime()) / 1000));
    receivedElement.textContent = `${receivedAt.toLocaleString()} (${formatAge(ageSeconds)})`;
}

function abbreviateBootId(bootId) {
    if (!bootId || bootId.length <= 14) {
        return bootId || '--';
    }
    return `${bootId.slice(0, 8)}…${bootId.slice(-4)}`;
}

function formatResetReason(reason) {
    if (!reason) {
        return '--';
    }
    return reason
        .split('_')
        .map(word => word.charAt(0).toUpperCase() + word.slice(1))
        .join(' ');
}

function renderDeviceStatus(status) {
    latestDeviceStatus = status && status.device_id ? status : null;

    const badge = document.getElementById('deviceVerificationBadge');
    if (!latestDeviceStatus) {
        badge.textContent = 'No status received';
        badge.className = 'badge text-bg-secondary';
        document.getElementById('deviceFirmwareVersion').textContent = '--';
        document.getElementById('deviceOtaSlot').textContent = '--';
        document.getElementById('deviceStatusReceived').textContent = '--';
        document.getElementById('deviceResetReason').textContent = '--';
        document.getElementById('deviceBootId').textContent = '--';
        document.getElementById('deviceBootId').title = '';
        document.getElementById('deviceIdfVersion').textContent = '--';
        document.getElementById('deviceBuildTime').textContent = '--';
        document.getElementById('deviceStatusSource').textContent = '--';
        return;
    }

    badge.textContent = latestDeviceStatus.pending_verify
        ? 'Pending OTA verification'
        : 'OTA verified';
    badge.className = latestDeviceStatus.pending_verify
        ? 'badge text-bg-warning'
        : 'badge text-bg-success';

    document.getElementById('deviceFirmwareVersion').textContent =
        latestDeviceStatus.firmware_version || '--';
    document.getElementById('deviceOtaSlot').textContent =
        latestDeviceStatus.ota_slot || '--';
    updateDeviceStatusReceivedTime(latestDeviceStatus);
    document.getElementById('deviceResetReason').textContent =
        formatResetReason(latestDeviceStatus.reset_reason);

    const bootElement = document.getElementById('deviceBootId');
    bootElement.textContent = abbreviateBootId(latestDeviceStatus.boot_id);
    bootElement.title = latestDeviceStatus.boot_id || '';

    document.getElementById('deviceIdfVersion').textContent =
        latestDeviceStatus.idf_version || '--';
    document.getElementById('deviceBuildTime').textContent =
        [latestDeviceStatus.build_date, latestDeviceStatus.build_time]
            .filter(Boolean)
            .join(' ') || '--';
    document.getElementById('deviceStatusSource').textContent =
        latestDeviceStatus.mqtt_retained ? 'Retained snapshot' : 'Live check-in';
}

function loadDeviceStatus() {
    if (!currentBmsId) {
        renderDeviceStatus(null);
        return;
    }

    const url = '/api/device-status/latest?bms_id=' + encodeURIComponent(currentBmsId);
    fetchJsonWithTimeout(url, {}, 8000)
        .then(status => {
            if (!status.device_id || status.device_id === currentBmsId) {
                renderDeviceStatus(status);
            }
        })
        .catch(error => {
            console.error('Error loading device status:', error);
            setFetchStatus(
                error.name === 'AbortError'
                    ? 'Device status fetch timed out'
                    : 'Device status fetch failed',
                error.name === 'AbortError' ? 'warning' : 'error'
            );
        });
}

function clearChartData() {
    Object.values(charts).forEach(chart => {
        chart.data.datasets.forEach(dataset => {
            dataset.data = [];
        });
    });
}

function buildDatasetPoints(records, field) {
    return records.map(record => ({
        x: new Date(record.timestamp * 1000),
        y: record[field]
    }));
}

function assignChartData(chartKey, records) {
    const fields = CHART_DATA_FIELDS[chartKey];
    const chart = charts[chartKey];
    fields.forEach((field, datasetIndex) => {
        chart.data.datasets[datasetIndex].data = buildDatasetPoints(records, field);
    });
}

function assignAllChartData(records) {
    Object.keys(CHART_DATA_FIELDS).forEach(chartKey => {
        assignChartData(chartKey, records);
    });
}

function updateChartsWithHistoricalData(data, meta = {}) {
    if (!data || data.length === 0) {
        clearChartData();
        updateResolutionAndPointInfo(meta);
        Object.values(charts).forEach(chart => chart.update('none'));
        return;
    }

    assignAllChartData(data);

    updateResolutionAndPointInfo({ ...meta, point_count: data.length });

    Object.values(charts).forEach(chart => {
        chart.update('none');
    });

    updateCurrentValues(data[data.length - 1]);
}

function pushOrReplace(dataset, point, replaceTimestamp) {
    const dataPoints = dataset.data;
    if (replaceTimestamp && dataPoints.length > 0) {
        const lastPoint = dataPoints[dataPoints.length - 1];
        if (lastPoint.x.getTime() === point.x.getTime()) {
            dataPoints[dataPoints.length - 1] = point;
            return;
        }
    }
    dataPoints.push(point);
}

function appendPointToChartSeries(chartKey, timestamp, data, replaceLastPoint) {
    const chart = charts[chartKey];
    CHART_DATA_FIELDS[chartKey].forEach((field, datasetIndex) => {
        pushOrReplace(
            chart.data.datasets[datasetIndex],
            { x: timestamp, y: data[field] },
            replaceLastPoint
        );
    });
}

function trimDatasetToWindow(dataset, minTimestampMs) {
    const dataPoints = dataset.data;
    let trimCount = 0;
    while (trimCount < dataPoints.length && dataPoints[trimCount].x.getTime() < minTimestampMs) {
        trimCount += 1;
    }
    if (trimCount > 0) {
        dataPoints.splice(0, trimCount);
    }
}

function trimAllChartsToWindow(timeWindowStart) {
    const minTimestampMs = timeWindowStart.getTime();
    Object.values(charts).forEach(chart => {
        chart.data.datasets.forEach(dataset => {
            trimDatasetToWindow(dataset, minTimestampMs);
        });
    });
}

// Update charts with new real-time data
function updateChartsWithNewData(data, meta = {}) {
    const timestamp = new Date(data.timestamp * 1000);
    const now = new Date();
    const timeWindowStart = new Date(now.getTime() - (currentTimeRange * 3600 * 1000));

    if (meta.bucket_seconds) {
        currentBucketSeconds = meta.bucket_seconds;
    }

    const replaceLastPoint = Boolean(meta.is_aggregated);

    Object.keys(CHART_DATA_FIELDS).forEach(chartKey => {
        appendPointToChartSeries(chartKey, timestamp, data, replaceLastPoint);
    });

    trimAllChartsToWindow(timeWindowStart);

    const animationDuration = currentTimeRange <= 0.5 ? 0 : 300;
    Object.values(charts).forEach(chart => {
        chart.options.animation.duration = animationDuration;
        chart.update('active');
    });

    const pointCount = charts.voltage.data.datasets[0].data.length;
    updateResolutionAndPointInfo({
        bucket_seconds: currentBucketSeconds,
        is_aggregated: Boolean(meta.is_aggregated),
        point_count: pointCount
    });
}

function updateCurrentValues(data) {
    document.getElementById('currentVoltage').textContent = (data.pack_voltage_v || 0).toFixed(2);
    document.getElementById('currentCurrent').textContent = (data.pack_current_a || 0).toFixed(2);
    document.getElementById('currentSoC').textContent = (data.state_of_charge_pct || 0).toFixed(1);
    document.getElementById('currentPower').textContent = (data.power_w || 0).toFixed(1);

    document.getElementById('lastUpdate').textContent = 'Last Update: ' + new Date().toLocaleTimeString();
}

function loadStatistics() {
    let url = '/api/statistics';
    if (currentBmsId) {
        url += '?bms_id=' + encodeURIComponent(currentBmsId);
    }

    fetchJsonWithTimeout(url, {}, 8000)
        .then(stats => {
            clearFetchStatus();
            updateStatistics(stats);
        })
        .catch(error => {
            console.error('Error loading statistics:', error);
            setFetchStatus(
                error.name === 'AbortError' ? 'Statistics fetch timed out' : 'Statistics fetch failed',
                error.name === 'AbortError' ? 'warning' : 'error'
            );
        });
}

function updateStatistics(stats) {
    const statisticsRow = document.getElementById('statisticsRow');
    statisticsRow.innerHTML = `
        <div class="col-md-2">
            <div class="text-center">
                <h6>Records</h6>
                <p class="h5">${stats.record_count || 0}</p>
            </div>
        </div>
        <div class="col-md-2">
            <div class="text-center">
                <h6>Voltage Range</h6>
                <p class="h6">${(stats.min_pack_voltage || 0).toFixed(2)}V - ${(stats.max_pack_voltage || 0).toFixed(2)}V</p>
            </div>
        </div>
        <div class="col-md-2">
            <div class="text-center">
                <h6>Current Range</h6>
                <p class="h6">${(stats.min_current || 0).toFixed(2)}A - ${(stats.max_current || 0).toFixed(2)}A</p>
            </div>
        </div>
        <div class="col-md-2">
            <div class="text-center">
                <h6>SoC Range</h6>
                <p class="h6">${(stats.min_soc || 0).toFixed(1)}% - ${(stats.max_soc || 0).toFixed(1)}%</p>
            </div>
        </div>
        <div class="col-md-2">
            <div class="text-center">
                <h6>Temp Range</h6>
                <p class="h6">${(stats.min_temperature || 0).toFixed(1)}°C - ${(stats.max_temperature || 0).toFixed(1)}°C</p>
            </div>
        </div>
        <div class="col-md-2">
            <div class="text-center">
                <h6>Power Range</h6>
                <p class="h6">${(stats.min_power || 0).toFixed(1)}W - ${(stats.max_power || 0).toFixed(1)}W</p>
            </div>
        </div>
    `;
}

function setTimeRange(hours, button) {
    currentTimeRange = hours;
    updateActiveTimeRangeButton();

    refreshDashboardData('time');
}

function onBmsIdChange() {
    const select = document.getElementById('bmsIdSelect');
    currentBmsId = select.value;
    refreshDashboardData('bms');
}

function onResolutionChange() {
    const select = document.getElementById('resolutionSelect');
    currentResolution = select.value;
    refreshDashboardData('refresh');
}

function formatTimestamp(timestamp) {
    return new Date(timestamp * 1000).toLocaleString();
}
