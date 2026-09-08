// BMS Dashboard JavaScript
let socket;
let charts = {};
let currentTimeRange = 0.017; // hours (1 minute default); in absolute mode this keeps the last live preset
let currentRangeMode = 'live'; // 'live' | 'absolute'
let absoluteStartTs = null; // epoch seconds (int) when currentRangeMode === 'absolute'
let absoluteEndTs = null; // epoch seconds (int) when currentRangeMode === 'absolute'
let currentBmsId = '';
let currentResolution = 'auto';
let targetPoints = 300;
let currentBucketSeconds = 10;
let activeDataRequestController = null;
let latestDataRequestId = 0;
let fetchStatusTimer = null;
let bmsSelectionReady = false;
let latestDeviceStatus = null;
let latestDeviceAvailability = null;
let deviceStatusAgeTimer = null;

const VALID_RESOLUTIONS = ['auto', '10s', '30s', '1m', '3m', '5m', '10m', '15m', '30m'];
const MIN_ABSOLUTE_WINDOW_SECONDS = 10;
const DRAG_SELECT_MIN_PIXELS = 10;
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

// Inline Chart.js plugin: draws the drag-to-zoom selection rectangle.
const dragSelectPlugin = {
    id: 'dragSelect',
    afterDraw(chart) {
        const drag = chart.$dragSelect;
        const area = chart.chartArea;
        if (!drag || !drag.active || !area) {
            return;
        }

        const left = Math.max(area.left, Math.min(drag.startX, drag.currentX));
        const right = Math.min(area.right, Math.max(drag.startX, drag.currentX));
        if (right - left < 1) {
            return;
        }

        const ctx = chart.ctx;
        ctx.save();
        ctx.fillStyle = 'rgba(0,123,255,0.15)';
        ctx.fillRect(left, area.top, right - left, area.bottom - area.top);
        ctx.strokeStyle = 'rgba(0,123,255,0.6)';
        ctx.lineWidth = 1;
        ctx.strokeRect(left + 0.5, area.top + 0.5, right - left - 1, area.bottom - area.top - 1);
        ctx.restore();
    }
};
Chart.register(dragSelectPlugin);

// Hand-rolled drag-to-zoom: pointer events on each chart canvas. Releasing a
// drag wider than DRAG_SELECT_MIN_PIXELS converts the selected pixel span to an
// absolute time window and refetches every chart server-side.
function setupDragToZoom(chart) {
    const canvas = chart.canvas;
    canvas.style.cursor = 'crosshair';
    canvas.style.touchAction = 'none';

    const state = { active: false, pointerId: null, captured: false, startX: 0, currentX: 0 };
    chart.$dragSelect = state;

    const cancelDrag = () => {
        if (!state.active) {
            return;
        }
        state.active = false;
        if (state.pointerId !== null) {
            try {
                if (canvas.hasPointerCapture(state.pointerId)) {
                    canvas.releasePointerCapture(state.pointerId);
                }
            } catch (err) {
                // Pointer already released; nothing to clean up.
            }
        }
        state.pointerId = null;
        state.captured = false;
        chart.draw();
    };
    chart.$cancelDragSelect = cancelDrag;

    canvas.addEventListener('pointerdown', event => {
        if (event.button !== 0) {
            return;
        }
        const area = chart.chartArea;
        if (!area
            || event.offsetX < area.left || event.offsetX > area.right
            || event.offsetY < area.top || event.offsetY > area.bottom) {
            return;
        }
        state.active = true;
        state.pointerId = event.pointerId;
        state.startX = event.offsetX;
        state.currentX = event.offsetX;
        try {
            canvas.setPointerCapture(event.pointerId);
            state.captured = true;
        } catch (err) {
            // Capture is best-effort; drag still works inside the canvas.
        }
        chart.draw();
    });

    canvas.addEventListener('pointermove', event => {
        if (!state.active || event.pointerId !== state.pointerId) {
            return;
        }
        state.currentX = event.offsetX;
        chart.draw();
    });

    canvas.addEventListener('pointerup', event => {
        if (!state.active || event.pointerId !== state.pointerId) {
            return;
        }
        const area = chart.chartArea;
        const startX = state.startX;
        const endX = event.offsetX;
        cancelDrag();

        if (!area || Math.abs(endX - startX) < DRAG_SELECT_MIN_PIXELS) {
            return;
        }

        const clampPixel = px => Math.min(area.right, Math.max(area.left, px));
        // The time scale reports values in milliseconds.
        const timeA = chart.scales.x.getValueForPixel(clampPixel(startX));
        const timeB = chart.scales.x.getValueForPixel(clampPixel(endX));
        if (!Number.isFinite(timeA) || !Number.isFinite(timeB)) {
            return;
        }

        const startTs = Math.floor(Math.min(timeA, timeB) / 1000);
        const endTs = Math.ceil(Math.max(timeA, timeB) / 1000);
        enterAbsoluteMode(startTs, endTs, 'time');
    });

    canvas.addEventListener('pointercancel', event => {
        if (state.active && event.pointerId === state.pointerId) {
            cancelDrag();
        }
    });

    canvas.addEventListener('pointerleave', () => {
        // Capture keeps events coming while dragging past the chart edge, so
        // only treat leaving as a cancel when capture was unavailable.
        if (state.active && !state.captured) {
            cancelDrag();
        }
    });
}

function cancelAllChartDrags() {
    Object.values(charts).forEach(chart => {
        if (chart.$dragSelect && chart.$dragSelect.active && chart.$cancelDragSelect) {
            chart.$cancelDragSelect();
        }
    });
}

// Initialize dashboard
document.addEventListener('DOMContentLoaded', function() {
    initializeCharts();
    Object.values(charts).forEach(setupDragToZoom);
    document.addEventListener('keydown', event => {
        if (event.key === 'Escape') {
            cancelAllChartDrags();
        }
    });
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
        if (latestDeviceAvailability) {
            renderDeviceAvailability(latestDeviceAvailability);
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

    const startParam = params.get('start');
    const endParam = params.get('end');
    if (startParam !== null && endParam !== null) {
        const parsedStart = Number.parseInt(startParam, 10);
        const parsedEnd = Number.parseInt(endParam, 10);
        if (Number.isFinite(parsedStart) && Number.isFinite(parsedEnd) && parsedEnd > parsedStart) {
            currentRangeMode = 'absolute';
            absoluteStartTs = parsedStart;
            absoluteEndTs = parsedEnd;
        }
    }

    resolutionSelect.value = currentResolution;
    updateActiveTimeRangeButton();
    updateRangeUiState();
}

function updateUrlState() {
    const url = new URL(window.location.href);
    url.searchParams.set('hours', String(currentTimeRange));
    url.searchParams.set('resolution', currentResolution);
    url.searchParams.set('auto_refresh', document.getElementById('autoRefresh').checked ? '1' : '0');

    if (currentRangeMode === 'absolute' && absoluteStartTs !== null && absoluteEndTs !== null) {
        url.searchParams.set('start', String(absoluteStartTs));
        url.searchParams.set('end', String(absoluteEndTs));
    } else {
        url.searchParams.delete('start');
        url.searchParams.delete('end');
    }

    if (currentBmsId) {
        url.searchParams.set('bms_id', currentBmsId);
    } else {
        url.searchParams.delete('bms_id');
    }

    history.replaceState(null, '', url);
}

function updateActiveTimeRangeButton() {
    const selectedHours = Number(currentTimeRange);
    const isAbsolute = currentRangeMode === 'absolute';
    document.querySelectorAll('.time-range-btn').forEach(btn => {
        if (btn.id === 'customRangeToggle') {
            btn.classList.toggle('active', isAbsolute);
            return;
        }
        const buttonHours = Number.parseFloat(btn.dataset.hours || '');
        const isActive = !isAbsolute
            && Number.isFinite(buttonHours)
            && Math.abs(buttonHours - selectedHours) < 0.0001;
        btn.classList.toggle('active', isActive);
    });
}

function tsToLocalInputValue(ts) {
    const date = new Date(ts * 1000);
    const pad = value => String(value).padStart(2, '0');
    return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`
        + `T${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`;
}

function formatLiveRangeLabel(hours) {
    const minutes = Math.round(hours * 60);
    if (minutes < 60) {
        return `last ${minutes} minute${minutes === 1 ? '' : 's'}`;
    }
    const wholeHours = Math.round(hours * 10) / 10;
    return `last ${wholeHours} hour${wholeHours === 1 ? '' : 's'}`;
}

function formatAbsoluteRangeEndpoint(ts) {
    return new Date(ts * 1000).toLocaleString(undefined, {
        month: 'short',
        day: 'numeric',
        hour: '2-digit',
        minute: '2-digit',
        second: '2-digit',
        hour12: false
    });
}

function setCustomRangeError(message) {
    const errorElement = document.getElementById('customRangeError');
    if (!errorElement) {
        return;
    }
    errorElement.textContent = message;
    errorElement.hidden = !message;
}

function updateRangeUiState() {
    const summary = document.getElementById('rangeSummary');
    const backButton = document.getElementById('backToLiveBtn');
    const startInput = document.getElementById('customRangeStart');
    const endInput = document.getElementById('customRangeEnd');
    if (!summary || !backButton) {
        return;
    }

    if (currentRangeMode === 'absolute') {
        summary.textContent = `${formatAbsoluteRangeEndpoint(absoluteStartTs)} → `
            + formatAbsoluteRangeEndpoint(absoluteEndTs);
        backButton.hidden = false;
        if (startInput && endInput) {
            startInput.value = tsToLocalInputValue(absoluteStartTs);
            endInput.value = tsToLocalInputValue(absoluteEndTs);
        }
    } else {
        summary.textContent = `Live · ${formatLiveRangeLabel(Number(currentTimeRange))}`;
        backButton.hidden = true;
    }
    setCustomRangeError('');
}

function toggleCustomRangePanel() {
    const panel = document.getElementById('customRangePanel');
    panel.hidden = !panel.hidden;
    if (panel.hidden) {
        return;
    }

    const startInput = document.getElementById('customRangeStart');
    const endInput = document.getElementById('customRangeEnd');
    if (!startInput.value || !endInput.value) {
        const endTs = currentRangeMode === 'absolute'
            ? absoluteEndTs
            : Math.floor(Date.now() / 1000);
        const startTs = currentRangeMode === 'absolute'
            ? absoluteStartTs
            : endTs - Math.max(60, Math.round(Number(currentTimeRange) * 3600));
        startInput.value = tsToLocalInputValue(startTs);
        endInput.value = tsToLocalInputValue(endTs);
    }
}

function applyCustomRange() {
    const startInput = document.getElementById('customRangeStart');
    const endInput = document.getElementById('customRangeEnd');

    if (!startInput.value || !endInput.value) {
        setCustomRangeError('Both From and To are required.');
        return;
    }

    // datetime-local values have no timezone suffix, so Date parses them as local time.
    const startMs = new Date(startInput.value).getTime();
    const endMs = new Date(endInput.value).getTime();
    if (!Number.isFinite(startMs) || !Number.isFinite(endMs)) {
        setCustomRangeError('Enter valid dates and times.');
        return;
    }

    const startTs = Math.floor(startMs / 1000);
    const endTs = Math.ceil(endMs / 1000);
    if (endTs <= startTs) {
        setCustomRangeError('To must be after From.');
        return;
    }

    const nowTs = Math.ceil(Date.now() / 1000);
    if (endTs > nowTs + 60) {
        setCustomRangeError('To cannot be in the future.');
        return;
    }

    setCustomRangeError('');
    enterAbsoluteMode(startTs, endTs, 'time');
}

function enterAbsoluteMode(startTs, endTs, sourceModule = 'time') {
    let start = Math.floor(Number(startTs));
    let end = Math.ceil(Number(endTs));
    if (!Number.isFinite(start) || !Number.isFinite(end) || end <= start) {
        return;
    }

    if (end - start < MIN_ABSOLUTE_WINDOW_SECONDS) {
        const midpoint = Math.round((start + end) / 2);
        start = midpoint - Math.floor(MIN_ABSOLUTE_WINDOW_SECONDS / 2);
        end = start + MIN_ABSOLUTE_WINDOW_SECONDS;
    }

    currentRangeMode = 'absolute';
    absoluteStartTs = start;
    absoluteEndTs = end;

    updateActiveTimeRangeButton();
    updateRangeUiState();
    refreshDashboardData(sourceModule);
}

function returnToLive() {
    if (currentRangeMode === 'live') {
        return;
    }

    currentRangeMode = 'live';
    absoluteStartTs = null;
    absoluteEndTs = null;

    updateActiveTimeRangeButton();
    updateRangeUiState();
    refreshDashboardData('time');
}

function setLoadingState(isLoading, message = 'Loading telemetry data...', sourceModule = 'time') {
    const moduleMap = {
        bms: 'bmsModuleCard',
        time: 'timeRangeModuleCard',
        refresh: 'refreshModuleCard'
    };
    const controls = document.querySelectorAll(
        '.time-range-btn, #bmsIdSelect, #resolutionSelect, '
        + '#customRangeStart, #customRangeEnd, #customRangeApply, #backToLiveBtn'
    );
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
    if (meta.unanchored_record_count) {
        pointInfo.textContent +=
            ` · ${meta.unanchored_record_count} samples preserved without capture time (not charted)`;
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

    if (currentRangeMode === 'absolute') {
        // When start/end are present the server ignores `hours`.
        params.append('start', String(absoluteStartTs));
        params.append('end', String(absoluteEndTs));
    }

    if (currentBmsId) {
        params.append('bms_id', currentBmsId);
    }

    return `/api/data?${params.toString()}`;
}

function normalizeDataPayload(payload) {
    if (Array.isArray(payload)) {
        return { records: payload, latestReading: null, meta: {} };
    }

    return {
        records: payload.records || [],
        latestReading: payload.latest_reading || null,
        meta: payload.meta || {}
    };
}

function sendViewSubscription() {
    if (!socket || !socket.connected || !document.getElementById('autoRefresh').checked) {
        return;
    }

    const view = {
        hours: currentTimeRange,
        bms_id: currentBmsId,
        resolution: currentResolution,
        target_points: targetPoints
    };

    if (currentRangeMode === 'absolute') {
        // Absolute windows make the server stop pushing telemetry_update to us.
        view.start = absoluteStartTs;
        view.end = absoluteEndTs;
    }

    socket.emit('set_view', view);
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
        if (currentRangeMode === 'absolute') {
            // The server stops pushing to absolute-mode clients; ignore any
            // stragglers defensively so a frozen window never gains points.
            return;
        }

        const point = payload?.point;
        const meta = payload?.meta || {};
        if (!point) {
            return;
        }

        updateChartsWithNewData(point, meta);
        updateCurrentValues(payload.latest_reading);
        triggerGlowEffect();
    });

    socket.on('view_data', function(payload) {
        const normalized = normalizeDataPayload(payload);
        setLoadingState(false);
        clearFetchStatus();
        updateChartsWithHistoricalData(normalized.records, normalized.meta);
        updateCurrentValues(normalized.latestReading);
    });

    socket.on('statistics', function(stats) {
        updateStatistics(stats);
    });

    socket.on('device_status_update', function(status) {
        if (status?.device_id === currentBmsId) {
            renderDeviceStatus(status);
        }
    });

    socket.on('device_availability_update', function(availability) {
        if (availability?.device_id === currentBmsId) {
            renderDeviceAvailability(availability);
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
    loadDeviceAvailability();
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
            updateCurrentValues(normalized.latestReading);
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

function renderDeviceAvailability(availability) {
    latestDeviceAvailability =
        availability && availability.device_id ? availability : null;
    const badge = document.getElementById('deviceAvailabilityBadge');

    if (!latestDeviceAvailability) {
        badge.textContent = 'MQTT unknown';
        badge.className = 'badge text-bg-secondary';
        badge.title = 'No MQTT availability record received';
        return;
    }

    const receivedAt = new Date(latestDeviceAvailability.received_at * 1000);
    const ageSeconds = Math.max(
        0,
        Math.floor((Date.now() - receivedAt.getTime()) / 1000)
    );
    const state = latestDeviceAvailability.online ? 'online' : 'offline';
    badge.textContent = `MQTT ${state} · ${formatAge(ageSeconds)}`;
    badge.className = latestDeviceAvailability.online
        ? 'badge text-bg-success'
        : 'badge text-bg-danger';
    badge.title =
        `Broker session ${state} since ${receivedAt.toLocaleString()}`;
}

function loadDeviceAvailability() {
    if (!currentBmsId) {
        renderDeviceAvailability(null);
        return;
    }

    const url = '/api/device-availability/latest?bms_id='
        + encodeURIComponent(currentBmsId);
    fetchJsonWithTimeout(url, {}, 8000)
        .then(availability => {
            if (!availability.device_id || availability.device_id === currentBmsId) {
                renderDeviceAvailability(availability);
            }
        })
        .catch(error => {
            console.error('Error loading device availability:', error);
            setFetchStatus(
                error.name === 'AbortError'
                    ? 'Device availability fetch timed out'
                    : 'Device availability fetch failed',
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

// Pin the x-axis to the absolute window so the charts show exactly the
// requested span (and drag-to-zoom pixel→time conversion stays accurate on
// repeated zooms). Live mode restores auto-fitting to the data.
function applyXAxisWindow() {
    Object.values(charts).forEach(chart => {
        if (currentRangeMode === 'absolute') {
            chart.options.scales.x.min = absoluteStartTs * 1000;
            chart.options.scales.x.max = absoluteEndTs * 1000;
        } else {
            delete chart.options.scales.x.min;
            delete chart.options.scales.x.max;
        }
    });
}

function updateChartsWithHistoricalData(data, meta = {}) {
    applyXAxisWindow();

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
    if (currentRangeMode === 'absolute') {
        return;
    }
    const minTimestampMs = timeWindowStart.getTime();
    Object.values(charts).forEach(chart => {
        chart.data.datasets.forEach(dataset => {
            trimDatasetToWindow(dataset, minTimestampMs);
        });
    });
}

// Update charts with new real-time data
function updateChartsWithNewData(data, meta = {}) {
    if (currentRangeMode === 'absolute') {
        return;
    }

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

// Metric cards show the newest raw reading only. They must never be fed a
// bucket average from an aggregated view, whatever the selected time range.
function updateCurrentValues(data) {
    if (!data) {
        return;
    }

    document.getElementById('currentVoltage').textContent = (data.pack_voltage_v || 0).toFixed(2);
    document.getElementById('currentCurrent').textContent = (data.pack_current_a || 0).toFixed(2);
    document.getElementById('currentSoC').textContent = (data.state_of_charge_pct || 0).toFixed(1);
    document.getElementById('currentPower').textContent = (data.power_w || 0).toFixed(1);

    const readingTime = data.timestamp ? new Date(data.timestamp * 1000) : new Date();
    document.getElementById('lastUpdate').textContent = readingTime.toLocaleTimeString();
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

    // Clicking any preset always returns to live mode.
    if (currentRangeMode === 'absolute') {
        currentRangeMode = 'live';
        absoluteStartTs = null;
        absoluteEndTs = null;
    }

    updateActiveTimeRangeButton();
    updateRangeUiState();

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
