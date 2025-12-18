// BMS Dashboard JavaScript
let socket;
let charts = {};
let currentTimeRange = 0.017; // hours (1 minute default)
let currentBmsId = ''; // current BMS ID filter

// Helper function to trigger glow effect on connection status badge
function triggerGlowEffect() {
    const statusElement = document.getElementById('connectionStatus');
    // Remove any existing glow classes
    statusElement.classList.remove('glow-pulse', 'glow-flash');
    // Force a reflow to reset the CSS animation (required for the animation to replay)
    void statusElement.offsetWidth;
    // Add flash effect
    statusElement.classList.add('glow-flash');
    // Remove class after animation completes
    setTimeout(() => {
        statusElement.classList.remove('glow-flash');
    }, 500);
}

// Helper function to trigger pulse effect (for connected state)
function triggerPulseEffect() {
    const statusElement = document.getElementById('connectionStatus');
    // Remove any existing glow classes
    statusElement.classList.remove('glow-pulse', 'glow-flash');
    // Trigger reflow to reset animation
    void statusElement.offsetWidth;
    // Add pulse effect
    statusElement.classList.add('glow-pulse');
    // Remove class after animation completes
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
    loadInitialData();
    
    // Auto-refresh toggle
    document.getElementById('autoRefresh').addEventListener('change', function() {
        if (this.checked) {
            connectWebSocket();
        } else {
            if (socket) {
                socket.disconnect();
            }
        }
    });
});

// Initialize all charts
function initializeCharts() {
    // Pack Voltage and Current Chart
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

    // State of Charge Chart
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

    // Cell Voltages Chart
    const cellCtx = document.getElementById('cellChart').getContext('2d');
    charts.cell = new Chart(cellCtx, {
        ...chartConfig,
        data: {
            datasets: [
                {
                    label: 'Cell 1 (V)',
                    borderColor: '#dc3545',
                    backgroundColor: 'rgba(220,53,69,0.1)',
                    data: []
                },
                {
                    label: 'Cell 2 (V)',
                    borderColor: '#007bff',
                    backgroundColor: 'rgba(0,123,255,0.1)',
                    data: []
                },
                {
                    label: 'Cell 3 (V)',
                    borderColor: '#28a745',
                    backgroundColor: 'rgba(40,167,69,0.1)',
                    data: []
                },
                {
                    label: 'Cell 4 (V)',
                    borderColor: '#ffc107',
                    backgroundColor: 'rgba(255,193,7,0.1)',
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
                    title: { display: true, text: 'Voltage (V)' }
                }
            }
        }
    });

    // Temperature Chart
    const tempCtx = document.getElementById('tempChart').getContext('2d');
    charts.temp = new Chart(tempCtx, {
        ...chartConfig,
        data: {
            datasets: [
                {
                    label: 'Temp 1 (°C)',
                    borderColor: '#fd7e14',
                    backgroundColor: 'rgba(253,126,20,0.1)',
                    data: []
                },
                {
                    label: 'Temp 2 (°C)',
                    borderColor: '#e83e8c',
                    backgroundColor: 'rgba(232,62,140,0.1)',
                    data: []
                },
                {
                    label: 'Temp 3 (°C)',
                    borderColor: '#6610f2',
                    backgroundColor: 'rgba(102,16,242,0.1)',
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
                    title: { display: true, text: 'Temperature (°C)' }
                }
            }
        }
    });

    // Power Chart
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
    });
    
    socket.on('disconnect', function() {
        document.getElementById('connectionStatus').textContent = 'Disconnected';
        document.getElementById('connectionStatus').className = 'badge status-disconnected';
        console.log('WebSocket disconnected');

        triggerGlowEffect();
    });
    
    socket.on('telemetry_update', function(data) {
        console.log('Received telemetry update:', data);
        updateChartsWithNewData(data);
        updateCurrentValues(data);
        
        triggerGlowEffect();
    });
    
    socket.on('initial_data', function(data) {
        console.log('Received initial data:', data.length, 'records');
        updateChartsWithHistoricalData(data);
    });
    
    socket.on('statistics', function(stats) {
        console.log('Received statistics:', stats);
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
    fetch('/api/bms-ids')
        .then(response => response.json())
        .then(bmsIds => {
            const select = document.getElementById('bmsIdSelect');
            select.innerHTML = '';
            
            if (bmsIds.length > 0) {
                // Set the first BMS ID as default
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
        })
        .catch(error => console.error('Error loading BMS IDs:', error));
}

// Load initial data
function loadInitialData() {
    console.log('Loading data for time range:', currentTimeRange, 'hours, BMS ID:', currentBmsId);
    
    let url = '/api/data?hours=' + currentTimeRange;
    if (currentBmsId) {
        url += '&bms_id=' + encodeURIComponent(currentBmsId);
    }
    
    fetch(url)
        .then(response => response.json())
        .then(data => {
            console.log('Received', data.length, 'records for', currentTimeRange, 'hours');
            updateChartsWithHistoricalData(data);
        })
        .catch(error => console.error('Error loading initial data:', error));
    
    loadStatistics();
}

function updateChartsWithHistoricalData(data) {
    if (!data || data.length === 0) return;
    
    // Clear existing data
    Object.values(charts).forEach(chart => {
        chart.data.datasets.forEach(dataset => {
            dataset.data = [];
        });
    });
    
    // Process data
    data.forEach(record => {
        const timestamp = new Date(record.timestamp * 1000);
        
        // Pack chart
        charts.pack.data.datasets[0].data.push({
            x: timestamp,
            y: record.pack_voltage_v
        });
        charts.pack.data.datasets[1].data.push({
            x: timestamp,
            y: record.pack_current_a
        });
        
        // SoC chart
        charts.soc.data.datasets[0].data.push({
            x: timestamp,
            y: record.state_of_charge_pct
        });
        
        // Cell voltages
        charts.cell.data.datasets[0].data.push({
            x: timestamp,
            y: record.cells_v_1
        });
        charts.cell.data.datasets[1].data.push({
            x: timestamp,
            y: record.cells_v_2
        });
        charts.cell.data.datasets[2].data.push({
            x: timestamp,
            y: record.cells_v_3
        });
        charts.cell.data.datasets[3].data.push({
            x: timestamp,
            y: record.cells_v_4
        });
        
        // Temperature
        charts.temp.data.datasets[0].data.push({
            x: timestamp,
            y: record.temps_c_1
        });
        charts.temp.data.datasets[1].data.push({
            x: timestamp,
            y: record.temps_c_2
        });
        charts.temp.data.datasets[2].data.push({
            x: timestamp,
            y: record.temps_c_3
        });
        
        // Power
        charts.power.data.datasets[0].data.push({
            x: timestamp,
            y: record.power_w
        });
    });
    
    // Update all charts
    Object.values(charts).forEach(chart => {
        chart.update('none');
    });
    
    // Update current values with latest data
    if (data.length > 0) {
        updateCurrentValues(data[data.length - 1]);
    }
}

// Update charts with new real-time data
function updateChartsWithNewData(data) {
    const timestamp = new Date(data.timestamp * 1000);
    
    // Calculate the time window based on current selection
    const now = new Date();
    const timeWindowStart = new Date(now.getTime() - (currentTimeRange * 3600 * 1000));
    
    // Add new data points
    charts.pack.data.datasets[0].data.push({x: timestamp, y: data.pack_voltage_v});
    charts.pack.data.datasets[1].data.push({x: timestamp, y: data.pack_current_a});
    
    charts.soc.data.datasets[0].data.push({x: timestamp, y: data.state_of_charge_pct});
    
    charts.cell.data.datasets[0].data.push({x: timestamp, y: data.cells_v_1});
    charts.cell.data.datasets[1].data.push({x: timestamp, y: data.cells_v_2});
    charts.cell.data.datasets[2].data.push({x: timestamp, y: data.cells_v_3});
    charts.cell.data.datasets[3].data.push({x: timestamp, y: data.cells_v_4});
    
    charts.temp.data.datasets[0].data.push({x: timestamp, y: data.temps_c_1});
    charts.temp.data.datasets[1].data.push({x: timestamp, y: data.temps_c_2});
    charts.temp.data.datasets[2].data.push({x: timestamp, y: data.temps_c_3});
    
    charts.power.data.datasets[0].data.push({x: timestamp, y: data.power_w});
    
    // Filter data to maintain the selected time window
    Object.values(charts).forEach(chart => {
        chart.data.datasets.forEach(dataset => {
            // Remove data points outside the time window
            dataset.data = dataset.data.filter(point => point.x >= timeWindowStart);
        });
    });
    
    // Update charts with appropriate animation for time range
    const animationDuration = currentTimeRange <= 0.5 ? 0 : 300; // No animation for very short ranges
    Object.values(charts).forEach(chart => {
        chart.options.animation.duration = animationDuration;
        chart.update('active');
    });
}

// Update current value displays
function updateCurrentValues(data) {
    document.getElementById('currentVoltage').textContent = (data.pack_voltage_v || 0).toFixed(2);
    document.getElementById('currentCurrent').textContent = (data.pack_current_a || 0).toFixed(2);
    document.getElementById('currentSoC').textContent = (data.state_of_charge_pct || 0).toFixed(1);
    document.getElementById('currentPower').textContent = (data.power_w || 0).toFixed(1);
    
    document.getElementById('lastUpdate').textContent = 'Last Update: ' + new Date().toLocaleTimeString();
}

// Load and display statistics
function loadStatistics() {
    let url = '/api/statistics';
    if (currentBmsId) {
        url += '?bms_id=' + encodeURIComponent(currentBmsId);
    }
    
    fetch(url)
        .then(response => response.json())
        .then(stats => updateStatistics(stats))
        .catch(error => console.error('Error loading statistics:', error));
}

// Update statistics display
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

// Set time range
function setTimeRange(hours) {
    currentTimeRange = hours;
    
    // Update button states
    document.querySelectorAll('.btn-group button').forEach(btn => {
        btn.classList.remove('active');
    });
    event.target.classList.add('active');
    
    // Reload data
    loadInitialData();
}

// Handle BMS ID selection change
function onBmsIdChange() {
    const select = document.getElementById('bmsIdSelect');
    currentBmsId = select.value;
    
    console.log('BMS ID changed to:', currentBmsId);
    
    // Reload all data with new BMS ID filter
    loadInitialData();
}

// Utility functions
function formatTimestamp(timestamp) {
    return new Date(timestamp * 1000).toLocaleString();
}