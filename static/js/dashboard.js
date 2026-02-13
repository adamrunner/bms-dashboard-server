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
    loadBmsIds();
    connectWebSocket();

    document.getElementById('autoRefresh').addEventListener('change', function() {
        if (this.checked) {
            connectWebSocket();
            sendViewSubscription();
        } else if (socket) {
            socket.disconnect();
        }
    });
});

function setLoadingState(isLoading, message = 'Loading telemetry data...') {
    const loadingOverlay = document.getElementById('chartLoadingOverlay');
    const loadingText = document.getElementById('loadingText');
    const controls = document.querySelectorAll('.time-range-btn, #bmsIdSelect, #resolutionSelect');

    loadingOverlay.classList.toggle('show', isLoading);
    loadingText.textContent = message;
    controls.forEach(control => {
        control.disabled = isLoading;
    });
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
    pointInfo.textContent = `Points: ${meta.point_count || 0}`;
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
    const packCtx = document.getElementById('packChart').getContext('2d');
    charts.pack = new Chart(packCtx, {
        ...chartConfig,
        data: {
            datasets: [
                {
                    label: 'Pack Voltage (V)',
                    borderColor: '#007bff',
                    backgroundColor: 'rgba(0,123,255,0.1)',
                    yAxisID: 'y',
                    data: []
                },
                {
                    label: 'Current (A)',
                    borderColor: '#28a745',
                    backgroundColor: 'rgba(40,167,69,0.1)',
                    yAxisID: 'y1',
                    data: []
                }
            ]
        },
        options: {
            ...chartConfig.options,
            scales: {
                ...chartConfig.options.scales,
                y: {
                    ...chartConfig.options.scales.y,
                    title: { display: true, text: 'Voltage (V)' },
                    position: 'left'
                },
                y1: {
                    type: 'linear',
                    title: { display: true, text: 'Current (A)' },
                    position: 'right',
                    grid: { drawOnChartArea: false }
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
        sendViewSubscription();
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

    socket.on('initial_data', function(payload) {
        const normalized = normalizeDataPayload(payload);
        if (normalized.records.length > 0) {
            updateChartsWithHistoricalData(normalized.records, normalized.meta);
        }
    });

    socket.on('view_data', function(payload) {
        const normalized = normalizeDataPayload(payload);
        updateChartsWithHistoricalData(normalized.records, normalized.meta);
    });

    socket.on('statistics', function(stats) {
        updateStatistics(stats);
    });

    socket.on('connect_error', function(error) {
        console.error('WebSocket connection error:', error);
    });

    socket.on('error', function(error) {
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
                currentBmsId = bmsIds[0];
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

            loadInitialData();
            sendViewSubscription();
        })
        .catch(error => {
            console.error('Error loading BMS IDs:', error);
            setFetchStatus('BMS list fetch failed', error.name === 'AbortError' ? 'warning' : 'error');
            loadInitialData();
        });
}

// Load initial data
function loadInitialData() {
    const requestId = ++latestDataRequestId;

    if (activeDataRequestController) {
        activeDataRequestController.abort();
    }

    activeDataRequestController = new AbortController();
    setLoadingState(true);

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
                sendViewSubscription();
            }
        });

    loadStatistics();
}

function clearChartData() {
    Object.values(charts).forEach(chart => {
        chart.data.datasets.forEach(dataset => {
            dataset.data = [];
        });
    });
}

function updateChartsWithHistoricalData(data, meta = {}) {
    if (!data || data.length === 0) {
        clearChartData();
        updateResolutionAndPointInfo(meta);
        Object.values(charts).forEach(chart => chart.update('none'));
        return;
    }

    clearChartData();

    data.forEach(record => {
        const timestamp = new Date(record.timestamp * 1000);

        charts.pack.data.datasets[0].data.push({ x: timestamp, y: record.pack_voltage_v });
        charts.pack.data.datasets[1].data.push({ x: timestamp, y: record.pack_current_a });

        charts.soc.data.datasets[0].data.push({ x: timestamp, y: record.state_of_charge_pct });

        charts.cell.data.datasets[0].data.push({ x: timestamp, y: record.cells_v_1 });
        charts.cell.data.datasets[1].data.push({ x: timestamp, y: record.cells_v_2 });
        charts.cell.data.datasets[2].data.push({ x: timestamp, y: record.cells_v_3 });
        charts.cell.data.datasets[3].data.push({ x: timestamp, y: record.cells_v_4 });

        charts.temp.data.datasets[0].data.push({ x: timestamp, y: record.temps_c_1 });
        charts.temp.data.datasets[1].data.push({ x: timestamp, y: record.temps_c_2 });
        charts.temp.data.datasets[2].data.push({ x: timestamp, y: record.temps_c_3 });

        charts.power.data.datasets[0].data.push({ x: timestamp, y: record.power_w });
    });

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

// Update charts with new real-time data
function updateChartsWithNewData(data, meta = {}) {
    const timestamp = new Date(data.timestamp * 1000);
    const now = new Date();
    const timeWindowStart = new Date(now.getTime() - (currentTimeRange * 3600 * 1000));

    if (meta.bucket_seconds) {
        currentBucketSeconds = meta.bucket_seconds;
    }

    const replaceLastPoint = (meta.bucket_seconds || currentBucketSeconds) > 10;

    pushOrReplace(charts.pack.data.datasets[0], { x: timestamp, y: data.pack_voltage_v }, replaceLastPoint);
    pushOrReplace(charts.pack.data.datasets[1], { x: timestamp, y: data.pack_current_a }, replaceLastPoint);
    pushOrReplace(charts.soc.data.datasets[0], { x: timestamp, y: data.state_of_charge_pct }, replaceLastPoint);

    pushOrReplace(charts.cell.data.datasets[0], { x: timestamp, y: data.cells_v_1 }, replaceLastPoint);
    pushOrReplace(charts.cell.data.datasets[1], { x: timestamp, y: data.cells_v_2 }, replaceLastPoint);
    pushOrReplace(charts.cell.data.datasets[2], { x: timestamp, y: data.cells_v_3 }, replaceLastPoint);
    pushOrReplace(charts.cell.data.datasets[3], { x: timestamp, y: data.cells_v_4 }, replaceLastPoint);

    pushOrReplace(charts.temp.data.datasets[0], { x: timestamp, y: data.temps_c_1 }, replaceLastPoint);
    pushOrReplace(charts.temp.data.datasets[1], { x: timestamp, y: data.temps_c_2 }, replaceLastPoint);
    pushOrReplace(charts.temp.data.datasets[2], { x: timestamp, y: data.temps_c_3 }, replaceLastPoint);

    pushOrReplace(charts.power.data.datasets[0], { x: timestamp, y: data.power_w }, replaceLastPoint);

    Object.values(charts).forEach(chart => {
        chart.data.datasets.forEach(dataset => {
            dataset.data = dataset.data.filter(point => point.x >= timeWindowStart);
        });
    });

    const animationDuration = currentTimeRange <= 0.5 ? 0 : 300;
    Object.values(charts).forEach(chart => {
        chart.options.animation.duration = animationDuration;
        chart.update('active');
    });

    const pointCount = charts.pack.data.datasets[0].data.length;
    updateResolutionAndPointInfo({
        bucket_seconds: currentBucketSeconds,
        is_aggregated: currentBucketSeconds > 10,
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

    document.querySelectorAll('.time-range-btn').forEach(btn => {
        btn.classList.remove('active');
    });
    if (button) {
        button.classList.add('active');
    }

    loadInitialData();
}

function onBmsIdChange() {
    const select = document.getElementById('bmsIdSelect');
    currentBmsId = select.value;
    loadInitialData();
}

function onResolutionChange() {
    const select = document.getElementById('resolutionSelect');
    currentResolution = select.value;
    loadInitialData();
}

function formatTimestamp(timestamp) {
    return new Date(timestamp * 1000).toLocaleString();
}
