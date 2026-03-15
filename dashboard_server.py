#!/usr/bin/env python3
"""
BMS Dashboard Web Server
Flask application with WebSocket support for real-time telemetry display
"""

import sys
import time
import os
from flask import Flask, render_template, jsonify, request
from flask_socketio import SocketIO, emit
from database_queries import (
    get_latest_reading, get_statistics, get_data_count, get_available_bms_ids,
    get_telemetry_data_for_view, get_latest_point_for_view, resolve_bucket_seconds,
    ensure_database_schema, validate_database_schema, get_latest_record_id
)

DEVELOPMENT_ENV_NAMES = {'development', 'dev', 'local'}


def get_app_env() -> str:
    """Return the current application environment name."""
    return os.getenv('APP_ENV', 'development').strip().lower()


def is_development_env() -> bool:
    """Return True when running in development mode."""
    return get_app_env() in DEVELOPMENT_ENV_NAMES


def resolve_flask_secret_key() -> str:
    """Resolve the Flask secret key with explicit development behavior."""
    secret_key = os.getenv('FLASK_SECRET_KEY')
    if secret_key:
        return secret_key

    if is_development_env():
        print("FLASK_SECRET_KEY not set; using development-only fallback secret key")
        return 'dev-dashboard-secret-key'

    raise RuntimeError(
        "FLASK_SECRET_KEY must be set when APP_ENV is not development"
    )


app = Flask(__name__)
app.config['SECRET_KEY'] = resolve_flask_secret_key()
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='eventlet')

# Global variables for real-time monitoring
last_seen_record_id = None
monitoring_thread = None
monitoring_active = False
connected_clients = set()
client_view_config = {}
last_stats_update = 0

DEFAULT_VIEW_CONFIG = {
    'hours': 0.017,
    'bms_id': None,
    'resolution': 'auto',
    'target_points': 300
}


def normalize_view_config(data=None):
    """Normalize view configuration payload from client or API params."""
    payload = data or {}
    try:
        hours = float(payload.get('hours', DEFAULT_VIEW_CONFIG['hours']))
    except (TypeError, ValueError):
        hours = DEFAULT_VIEW_CONFIG['hours']

    bms_id = payload.get('bms_id', DEFAULT_VIEW_CONFIG['bms_id'])
    if bms_id == '':
        bms_id = None

    resolution = payload.get('resolution', DEFAULT_VIEW_CONFIG['resolution']) or 'auto'
    if resolution not in ['auto', '10s', '30s', '1m', '3m', '5m']:
        resolution = 'auto'

    try:
        target_points = int(payload.get('target_points', DEFAULT_VIEW_CONFIG['target_points']))
    except (TypeError, ValueError):
        target_points = DEFAULT_VIEW_CONFIG['target_points']
    target_points = max(50, min(1000, target_points))

    return {
        'hours': hours,
        'bms_id': bms_id,
        'resolution': resolution,
        'target_points': target_points
    }


def background_monitor():
    """Background thread to monitor database for new data"""
    global last_seen_record_id, monitoring_active, last_stats_update
    
    print(f"Background monitor started, initial record id: {last_seen_record_id}")
    
    while monitoring_active:
        try:
            if not connected_clients:
                print("No connected clients remaining, stopping background monitor")
                monitoring_active = False
                break

            current_time = int(time.time())
            current_latest_record_id = get_latest_record_id()

            if current_latest_record_id and current_latest_record_id != last_seen_record_id:
                print(
                    "New data detected! "
                    f"Record ID: {last_seen_record_id} -> {current_latest_record_id}"
                )

                if connected_clients:
                    print("Broadcasting view-aware telemetry updates")
                    try:
                        for sid in list(connected_clients):
                            view_cfg = client_view_config.get(sid, DEFAULT_VIEW_CONFIG)
                            bms_filter = view_cfg.get('bms_id')

                            latest_point = get_latest_point_for_view(
                                view_cfg['hours'],
                                bms_filter,
                                view_cfg['resolution'],
                                view_cfg['target_points']
                            )
                            if not latest_point:
                                continue

                            bucket_seconds = resolve_bucket_seconds(
                                view_cfg['hours'],
                                view_cfg['resolution'],
                                view_cfg['target_points']
                            )
                            socketio.server.emit('telemetry_update', {
                                'point': latest_point,
                                'meta': {
                                    'bucket_seconds': bucket_seconds,
                                    'is_aggregated': bucket_seconds > 10,
                                    'resolution': view_cfg['resolution']
                                }
                            }, room=sid)
                    except Exception as emit_error:
                        print(f"WebSocket emit failed: {emit_error}")
                        import traceback
                        traceback.print_exc()
                last_seen_record_id = current_latest_record_id
            
            # Update statistics every 60 seconds
            if current_time - last_stats_update >= 60:
                if connected_clients:
                    try:
                        print("Updating statistics...")
                        stats = get_statistics(24)
                        socketio.server.emit('statistics', stats)
                        print("Statistics updated")
                        last_stats_update = current_time
                    except Exception as stats_error:
                        print(f"Failed to update statistics: {stats_error}")
            
            socketio.sleep(2)  # Use socketio.sleep instead of time.sleep
            
        except Exception as e:
            print(f"Error in background monitor: {e}")
            import traceback
            traceback.print_exc()
            socketio.sleep(5)


@app.route('/')
def dashboard():
    """Serve the main dashboard page"""
    return render_template('dashboard.html')


@app.route('/api/data')
def api_data():
    """API endpoint to get telemetry data"""
    view_config = normalize_view_config({
        'hours': request.args.get('hours', default=1, type=float),
        'bms_id': request.args.get('bms_id', default=None, type=str),
        'resolution': request.args.get('resolution', default='auto', type=str),
        'target_points': request.args.get('target_points', default=300, type=int)
    })
    
    try:
        print(
            "API: Fetching data for "
            f"{view_config['hours']} hours, BMS ID: {view_config['bms_id']}, "
            f"resolution: {view_config['resolution']}"
        )
        data, meta = get_telemetry_data_for_view(
            view_config['hours'],
            view_config['bms_id'],
            view_config['resolution'],
            view_config['target_points']
        )
        print(f"API: Returning {len(data)} records (bucket {meta['bucket_seconds']}s)")
        return jsonify({'records': data, 'meta': meta})
    except Exception as e:
        print(f"Error fetching data: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/latest')
def api_latest():
    """API endpoint to get latest reading"""
    bms_id = request.args.get('bms_id', default=None, type=str)
    try:
        data = get_latest_reading(bms_id)
        return jsonify(data if data else {})
    except Exception as e:
        print(f"Error fetching latest data: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/statistics')
def api_statistics():
    """API endpoint to get statistics"""
    hours = request.args.get('hours', default=24, type=int)
    bms_id = request.args.get('bms_id', default=None, type=str)
    
    try:
        stats = get_statistics(hours, bms_id)
        return jsonify(stats)
    except Exception as e:
        print(f"Error fetching statistics: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/bms-ids')
def api_bms_ids():
    """API endpoint to get available BMS IDs"""
    try:
        bms_ids = get_available_bms_ids()
        return jsonify(bms_ids)
    except Exception as e:
        print(f"Error fetching BMS IDs: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/health')
def api_health():
    """Health check endpoint"""
    bms_id = request.args.get('bms_id', default=None, type=str)
    try:
        validate_database_schema()
        count = get_data_count(bms_id)
        latest = get_latest_reading(bms_id)
        
        return jsonify({
            'status': 'healthy',
            'total_records': count,
            'latest_timestamp': latest['timestamp'] if latest else None,
            'monitoring_active': monitoring_active,
            'bms_id': bms_id
        })
    except Exception as e:
        return jsonify({
            'status': 'error',
            'error': str(e)
        }), 500


@app.route('/api/test-websocket')
def test_websocket():
    """Test endpoint to manually trigger a WebSocket message"""
    try:
        test_data = {
            'timestamp': int(time.time()),
            'pack_voltage_v': 13.25,
            'pack_current_a': -1.5,
            'state_of_charge_pct': 85.0,
            'power_w': -19.9,
            'cells_v_1': 3.31,
            'cells_v_2': 3.32,
            'cells_v_3': 3.30,
            'cells_v_4': 3.32,
            'temps_c_1': 23.5,
            'temps_c_2': 24.0,
            'temps_c_3': 23.8
        }
        
        print("Manual WebSocket test triggered")
        if connected_clients:
            socketio.server.emit('telemetry_update', test_data)
            print(f"Test WebSocket message sent to {len(connected_clients)} clients")
        else:
            print("No connected clients to send test message to")
        
        return jsonify({
            'status': 'success',
            'message': 'Test WebSocket message sent',
            'data': test_data
        })
    except Exception as e:
        print(f"Test WebSocket failed: {e}")
        return jsonify({
            'status': 'error',
            'error': str(e)
        }), 500


# WebSocket event handlers
@socketio.on('connect')
def handle_connect(auth):
    """Handle client connection"""
    global monitoring_active, monitoring_thread, connected_clients, last_seen_record_id
    
    print(f'Client connected: {request.sid}')
    sid = request.sid
    connected_clients.add(sid)
    client_view_config[sid] = DEFAULT_VIEW_CONFIG.copy()
    print(f"Total connected clients: {len(connected_clients)}")
    
    # Start monitoring thread if not already running
    if not monitoring_active:
        print("Starting background monitoring thread...")
        last_seen_record_id = get_latest_record_id()
        monitoring_active = True
        # Use Flask-SocketIO's background task instead of threading
        socketio.start_background_task(background_monitor)
    else:
        print("Background monitoring thread already running")
    
    try:
        stats = get_statistics(24)
        emit('statistics', stats)
        print("Sent statistics")
    except Exception as e:
        print(f"Error sending initial statistics: {e}")
        emit('error', {'message': 'Failed to load initial statistics'})


@socketio.on('disconnect')
def handle_disconnect():
    """Handle client disconnection"""
    global connected_clients, monitoring_active
    
    print(f'Client disconnected: {request.sid}')
    connected_clients.discard(request.sid)
    client_view_config.pop(request.sid, None)
    print(f"Total connected clients: {len(connected_clients)}")
    if not connected_clients:
        monitoring_active = False


@socketio.on('test_message')
def handle_test_message(data):
    """Handle test message from client"""
    print(f"Received test message from client: {data}")
    # Echo back to confirm two-way communication
    emit('test_response', {'message': 'Hello from server', 'received': data})


@socketio.on('request_data')
def handle_request_data(data):
    """Handle client request for specific data"""
    try:
        view_config = normalize_view_config(data)
        telemetry_data, meta = get_telemetry_data_for_view(
            view_config['hours'],
            view_config['bms_id'],
            view_config['resolution'],
            view_config['target_points']
        )
        emit('historical_data', {'records': telemetry_data, 'meta': meta})
        
    except Exception as e:
        print(f"Error handling data request: {e}")
        emit('error', {'message': 'Failed to fetch requested data'})


@socketio.on('set_view')
def handle_set_view(data):
    """Update the client's live view preferences and return snapshot data."""
    try:
        sid = request.sid
        view_config = normalize_view_config(data)
        client_view_config[sid] = view_config
        records, meta = get_telemetry_data_for_view(
            view_config['hours'],
            view_config['bms_id'],
            view_config['resolution'],
            view_config['target_points']
        )
        emit('view_data', {'records': records, 'meta': meta})
    except Exception as e:
        print(f"Error handling set_view: {e}")
        emit('error', {'message': 'Failed to update live view settings'})


@socketio.on('request_statistics')
def handle_request_statistics(data):
    """Handle client request for statistics"""
    try:
        hours = data.get('hours', 24)
        stats = get_statistics(hours)
        emit('statistics', stats)
        
    except Exception as e:
        print(f"Error handling statistics request: {e}")
        emit('error', {'message': 'Failed to fetch statistics'})


# Error handlers
@app.errorhandler(404)
def not_found_error(error):
    return jsonify({'error': 'Not found'}), 404


@app.errorhandler(500)
def internal_error(error):
    return jsonify({'error': 'Internal server error'}), 500


def initialize_monitoring():
    """Initialize the monitoring system"""
    global last_seen_record_id, last_stats_update
    
    try:
        ensure_database_schema()
        validate_database_schema()
        last_seen_record_id = get_latest_record_id()
        last_stats_update = int(time.time())  # Initialize stats timer
        print(f"Monitoring initialized with latest record id {last_seen_record_id}")
    except Exception as e:
        print(f"Error initializing monitoring: {e}")
        last_seen_record_id = None
        last_stats_update = int(time.time())


if __name__ == '__main__':
    # Check for debug flag
    debug_mode = '--debug' in sys.argv
    
    mode = "DEBUG" if debug_mode else "PRODUCTION"
    print(f"Starting BMS Dashboard Server in {mode} mode...")
    print("Dashboard will be available at: http://0.0.0.0:5000")
    
    # Initialize monitoring
    initialize_monitoring()
    
    # Start the Flask-SocketIO server
    try:
        socketio.run(
            app,
            host='0.0.0.0',
            port=5000,
            debug=debug_mode,  # Set to True for development
            use_reloader=debug_mode  # Enable reloader for template reloading in debug mode
        )
    except KeyboardInterrupt:
        print("\nShutting down dashboard server...")
        monitoring_active = False
        if monitoring_thread:
            monitoring_thread.join(timeout=5)
    except Exception as e:
        print(f"Error starting server: {e}")
    finally:
        monitoring_active = False
