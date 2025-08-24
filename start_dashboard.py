#!/usr/bin/env python3
"""
Dashboard startup script
Checks dependencies and starts the BMS dashboard server
"""

import sys
import subprocess
import sqlite3
import os

def check_database():
    """Check if database exists and has data"""
    if not os.path.exists('bms_telemetry.db'):
        print("❌ Database file not found!")
        print("Make sure the MQTT logger has been running to collect data.")
        return False
    
    try:
        conn = sqlite3.connect('bms_telemetry.db')
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM bms_telemetry")
        count = cursor.fetchone()[0]
        conn.close()
        
        if count == 0:
            print("⚠️  Database exists but has no data yet.")
            print("The dashboard will work, but charts will be empty until data arrives.")
        else:
            print(f"✅ Database ready with {count} records")
        
        return True
    except Exception as e:
        print(f"❌ Database error: {e}")
        return False

def check_dependencies():
    """Check if required packages are installed"""
    required_packages = ['flask', 'flask_socketio', 'eventlet']
    missing_packages = []
    
    for package in required_packages:
        try:
            __import__(package)
        except ImportError:
            missing_packages.append(package)
    
    if missing_packages:
        print("❌ Missing required packages:")
        for pkg in missing_packages:
            print(f"   - {pkg}")
        print("\nInstall them with: pip install -r requirements.txt")
        return False
    
    print("✅ All required packages are installed")
    return True

def main():
    print("🔋 BMS Dashboard Startup Check")
    print("=" * 40)
    
    # Check dependencies
    if not check_dependencies():
        sys.exit(1)
    
    # Check database
    if not check_database():
        print("\n💡 To collect data, run: python bms_mqtt_logger.py")
        response = input("\nContinue anyway? (y/N): ").lower()
        if response != 'y':
            sys.exit(1)
    
    print("\n🚀 Starting BMS Dashboard Server...")
    print("   Dashboard URL: http://0.0.0.0:5000")
    print("   Press Ctrl+C to stop")
    print("-" * 40)
    
    try:
        # Start the dashboard server
        subprocess.run([sys.executable, 'dashboard_server.py'])
    except KeyboardInterrupt:
        print("\n👋 Dashboard stopped")
    except Exception as e:
        print(f"\n❌ Error starting dashboard: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()
