#!/usr/bin/env python3
"""
BMS Dashboard Web Server
Flask application with WebSocket support for real-time telemetry display
"""

import sys
import threading
import time
from flask import Flask, render_template, jsonify, request
from flask_socketio import SocketIO, emit
import sqlite3
from database_queries import (
    get_telemetry_data, get_latest_reading, get_statistics,
    get_recent_data_for_websocket, get_data_count, get_available_bms_ids
)

app = Flask(__name__)
app.config['SECRET_KEY'] = 'your-secret-key-here'
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='eventlet')

# Global variables for real-time monitoring
last_data_count = 0
monitoring_thread = None
monitoring_active = False
connected_clients = set()
last_stats_update = 0


def background_monitor():
    """Background thread to monitor database for new data"""
    global last_data_count, monitoring_active, last_stats_update
    
    print(f"Background monitor started, initial count: {last_data_count}")
    
    while monitoring_active:
        try:
            current_time = int(time.time())
            current_count = get_data_count()
            
            if current_count > last_data_count:
                print(f"New data detected! Count: {last_data_count} -> {current_count}")
                
                # Get latest data
                latest = get_latest_reading()
                if latest:
                    print(f"Broadcasting telemetry update: {latest['timestamp']}")
                    try:
                        # Use socketio.start_background_task or emit with namespace
                        if connected_clients:
                            # Use the socketio server directly for background emissions
                            socketio.server.emit('telemetry_update', latest)
                            print(f"WebSocket emit successful to {len(connected_clients)} clients")
                        else:
                            print("No connected clients to broadcast to")
                    except Exception as emit_error:
                        print(f"WebSocket emit failed: {emit_error}")
                        import traceback
                        traceback.print_exc()
                else:
                    print("No latest reading found")
                
                last_data_count = current_count
            
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
    hours = request.args.get('hours', default=1, type=float)
    bms_id = request.args.get('bms_id', default=None, type=str)
    
    try:
        print(f"API: Fetching data for {hours} hours, BMS ID: {bms_id}")
        data = get_telemetry_data(hours, bms_id)
        print(f"API: Returning {len(data)} records")
        return jsonify(data)
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
    global monitoring_active, monitoring_thread, connected_clients
    
    print(f'Client connected: {request.sid}')
    connected_clients.add(request.sid)
    print(f"Total connected clients: {len(connected_clients)}")
    
    # Start monitoring thread if not already running
    if not monitoring_active:
        print("Starting background monitoring thread...")
        monitoring_active = True
        # Use Flask-SocketIO's background task instead of threading
        socketio.start_background_task(background_monitor)
    else:
        print("Background monitoring thread already running")
    
    # Send initial data to the newly connected client
    try:
        print("Sending initial data to client...")
        recent_data = get_recent_data_for_websocket(50)
        emit('initial_data', recent_data)
        print(f"Sent {len(recent_data)} initial records")
        
        stats = get_statistics(24)
        emit('statistics', stats)
        print("Sent statistics")
        
    except Exception as e:
        print(f"Error sending initial data: {e}")
        import traceback
        traceback.print_exc()
        emit('error', {'message': 'Failed to load initial data'})


@socketio.on('disconnect')
def handle_disconnect():
    """Handle client disconnection"""
    global connected_clients
    
    print(f'Client disconnected: {request.sid}')
    connected_clients.discard(request.sid)
    print(f"Total connected clients: {len(connected_clients)}")


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
        hours = data.get('hours', 1)
        telemetry_data = get_telemetry_data(hours)
        emit('historical_data', telemetry_data)
        
    except Exception as e:
        print(f"Error handling data request: {e}")
        emit('error', {'message': 'Failed to fetch requested data'})


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
    global last_data_count, last_stats_update
    
    try:
        last_data_count = get_data_count()
        last_stats_update = int(time.time())  # Initialize stats timer
        print(f"Monitoring initialized with {last_data_count} existing records")
    except Exception as e:
        print(f"Error initializing monitoring: {e}")
        last_data_count = 0
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
