import sys
import os
import logging
import traceback
import json
import shutil
import socket
import time
import threading
import webview
from functools import wraps
from datetime import datetime

# ==================== CLEANUP ON STARTUP ====================
def cleanup_old_files():
    """Remove old log files and uploads folder on startup"""
    try:
        # Get the directory where the executable is located
        if getattr(sys, 'frozen', False):
            base_dir = os.path.dirname(sys.executable)
        else:
            base_dir = os.path.dirname(os.path.abspath(__file__))
        
        # Remove debug.log if it exists
        debug_log = os.path.join(base_dir, 'debug.log')
        if os.path.exists(debug_log):
            try:
                os.remove(debug_log)
                print(f"Removed old debug.log from {base_dir}")
            except Exception as e:
                print(f"Could not remove debug.log: {e}")
        
        # Remove uploads folder if it exists (and is empty or old)
        uploads_dir = os.path.join(base_dir, 'uploads')
        if os.path.exists(uploads_dir):
            try:
                # Check if folder is empty or older than 1 day
                should_remove = False
                if not os.listdir(uploads_dir):
                    should_remove = True  # Empty folder
                else:
                    # Check modification time
                    mod_time = os.path.getmtime(uploads_dir)
                    current_time = time.time()
                    if (current_time - mod_time) > 86400:  # 24 hours in seconds
                        should_remove = True
                
                if should_remove:
                    shutil.rmtree(uploads_dir)
                    print(f"Removed old uploads folder from {base_dir}")
            except Exception as e:
                print(f"Could not remove uploads folder: {e}")
        
        # Remove any .log files in the directory
        for file in os.listdir(base_dir):
            if file.endswith('.log'):
                try:
                    file_path = os.path.join(base_dir, file)
                    os.remove(file_path)
                    print(f"Removed log file: {file}")
                except Exception as e:
                    print(f"Could not remove {file}: {e}")
                    
    except Exception as e:
        print(f"Error during cleanup: {e}")

# Run cleanup before anything else
cleanup_old_files()

# Setup basic file logging immediately to catch startup errors
try:
    if getattr(sys, 'frozen', False):
        log_path = os.path.join(os.path.dirname(sys.executable), 'debug.log')
    else:
        log_path = 'debug.log'
    
    logging.basicConfig(
        filename=log_path,
        level=logging.DEBUG,
        format='%(asctime)s - %(levelname)s - %(message)s'
    )
    # Also add console handler for debugging
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.DEBUG)
    logging.getLogger().addHandler(console_handler)
except Exception as e:
    print(f"Logging setup error: {e}")

# Wrap imports in try/except to log import errors
try:
    from flask import Flask, render_template, request, jsonify, send_from_directory
    import serial.tools.list_ports
    import serial
    import subprocess
    import queue
    import atexit
    from werkzeug.utils import secure_filename
except Exception as e:
    logging.error(f"Startup Import Error: {e}")
    logging.error(traceback.format_exc())
    sys.exit(1)

# ==================== PATH HANDLING ====================
if getattr(sys, 'frozen', False):
    RESOURCE_DIR = sys._MEIPASS
    BASE_DIR = os.path.dirname(sys.executable)
else:
    RESOURCE_DIR = os.path.dirname(os.path.abspath(__file__))
    BASE_DIR = RESOURCE_DIR

TEMPLATE_DIR = os.path.join(RESOURCE_DIR, 'templates')
MODELS_DIR = os.path.join(RESOURCE_DIR, 'models')
UPLOAD_FOLDER = os.path.join(BASE_DIR, 'uploads')

# ==================== DISPATCHER LOGIC ====================
# Check if we are being called as the uploader subprocess
if len(sys.argv) > 1 and sys.argv[1] == '--firmware-upload':
    print("DEBUG: Entering firmware upload mode")
    try:
        # Clean arguments for stm32loader
        sys.argv.pop(1)  # Remove the '--firmware-upload' flag
        
        # Import the main function from stm32loader dynamically
        import stm32loader.main
        
        # Run the upload
        stm32loader.main.main()
        
    except ImportError:
        error_msg = "Error: stm32loader module not found in frozen bundle."
        print(error_msg)
        logging.error(error_msg)
        sys.exit(1)
    except Exception as e:
        error_msg = f"Error executing stm32loader: {e}"
        print(error_msg)
        logging.error(error_msg)
        logging.error(traceback.format_exc())
        sys.exit(1)
        
    # Exit immediately so the GUI does not start
    sys.exit(0)

print("DEBUG: Starting normal GUI mode")  # Add this to verify we're in GUI mode

# ==================== NORMAL GUI APP STARTS HERE ====================

# Configuration
DEFAULT_BAUD_RATE = 57600
MAX_FILE_SIZE = 16 * 1024 * 1024
SERIAL_RECONNECT_ATTEMPTS = 3
SERIAL_RECONNECT_DELAY = 2
DEFAULT_LAT = 22.719568
DEFAULT_LON = 75.857727
DEFAULT_ZOOM = 15

app = Flask(__name__, template_folder=TEMPLATE_DIR)
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
app.config['MAX_CONTENT_LENGTH'] = MAX_FILE_SIZE

try:
    os.makedirs(UPLOAD_FOLDER, exist_ok=True)
except Exception as e:
    logging.error(f"Failed to create upload folder: {e}")

# Global State
ser = None
monitor_thread = None
monitor_queue = queue.Queue()
monitor_running = False
monitor_lock = threading.Lock()
current_port = None
current_baud = DEFAULT_BAUD_RATE
map_session = {
    'lat': None, 'lon': None, 'altitude': None, 'speed': None,
    'satellites': 0, 'hdop': 0.0, 'vdop': 0.0, 'fix_quality': 0,
    'last_update': None, 'valid': False
}
map_track_history = []
map_data_lock = threading.Lock()

# Notification System State
notification_history = []
MAX_NOTIFICATION_HISTORY = 100
app_start_time = time.time()  # Track app start time

# ==================== CLEANUP ON SHUTDOWN ====================
def cleanup_on_shutdown():
    """Clean up files and folders when application shuts down"""
    try:
        logging.info("Starting shutdown cleanup...")
        
        # Remove uploads folder if empty
        if os.path.exists(UPLOAD_FOLDER):
            try:
                if not os.listdir(UPLOAD_FOLDER):
                    shutil.rmtree(UPLOAD_FOLDER)
                    logging.info("Removed empty uploads folder")
                else:
                    # Check if files are older than 1 hour
                    current_time = time.time()
                    old_files = []
                    for file in os.listdir(UPLOAD_FOLDER):
                        file_path = os.path.join(UPLOAD_FOLDER, file)
                        if os.path.isfile(file_path):
                            mod_time = os.path.getmtime(file_path)
                            if (current_time - mod_time) > 3600:  # 1 hour
                                old_files.append(file_path)
                    
                    if old_files:
                        for file_path in old_files:
                            os.remove(file_path)
                        logging.info(f"Removed {len(old_files)} old upload files")
            except Exception as e:
                logging.error(f"Error cleaning uploads folder: {e}")
        
        # Close serial connection
        cleanup_serial()
        
        logging.info("Shutdown cleanup complete")
        
    except Exception as e:
        logging.error(f"Error during shutdown cleanup: {e}")

# ==================== DECORATORS ====================
def api_response(success=True, message="", data=None, notification=None):
    """Standard API response format with notification support"""
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            try:
                result = func(*args, **kwargs)
                if isinstance(result, dict):
                    response = {
                        'success': result.get('success', success),
                        'message': result.get('message', message),
                        'data': result.get('data', data)
                    }
                    # Add notification data if provided
                    if 'notification' in result:
                        response['notification'] = result['notification']
                        add_notification_to_history(result['notification'])
                    elif notification:
                        response['notification'] = notification
                        add_notification_to_history(notification)
                    return jsonify(response)
                return result
            except Exception as e:
                logging.error(f"API Error in {func.__name__}: {e}")
                error_notification = {
                    'type': 'error',
                    'title': 'Server Error',
                    'message': 'An internal server error occurred',
                    'timestamp': datetime.now().isoformat()
                }
                add_notification_to_history(error_notification)
                return jsonify({
                    'success': False,
                    'message': f"Internal error: {str(e)}",
                    'notification': error_notification
                })
        return wrapper
    return decorator

def add_notification_to_history(notification):
    """Add notification to history for tracking"""
    global notification_history
    notification['timestamp'] = notification.get('timestamp', datetime.now().isoformat())
    notification['id'] = f"notif_{len(notification_history)}_{datetime.now().timestamp()}"
    notification_history.append(notification)
    
    # Limit history size
    if len(notification_history) > MAX_NOTIFICATION_HISTORY:
        notification_history = notification_history[-MAX_NOTIFICATION_HISTORY:]

# ==================== HELPER FUNCTIONS ====================
def safe_serial_close(serial_obj):
    if serial_obj and hasattr(serial_obj, 'is_open'):
        try:
            serial_obj.close()
        except:
            pass

def is_port_available(port_name):
    try:
        ports = [p.device for p in serial.tools.list_ports.comports()]
        if port_name not in ports: 
            return False
        try:
            s = serial.Serial(port_name)
            s.close()
            return True
        except:
            return False
    except:
        return False

def to_decimal(coord, direct):
    if not coord or not direct: 
        return None
    try:
        dot_index = coord.find('.')
        if dot_index > 0:
            deg = float(coord[:dot_index-2])
            min = float(coord[dot_index-2:])
        else:
            cutoff = 2 if len(coord.split('.')[0]) <= 4 else 3 
            deg = float(coord[:cutoff])
            min = float(coord[cutoff:])
        val = deg + (min / 60)
        return -val if direct in ['S', 'W'] else val
    except: 
        return None

def parse_nmea(sentence):
    try:
        if not sentence.startswith('$'): 
            return None
        parts = sentence.split('*')[0].split(',')
        if len(parts) < 6: 
            return None
        msg_type = parts[0][-3:]
        
        if msg_type == 'GGA':
            quality = int(parts[6]) if parts[6] else 0
            if quality > 0:
                return {
                    'lat': to_decimal(parts[2], parts[3]),
                    'lon': to_decimal(parts[4], parts[5]),
                    'altitude': float(parts[9]) if parts[9] else 0.0,
                    'satellites': int(parts[7]) if parts[7] else 0,
                    'hdop': float(parts[8]) if parts[8] else 0.0, 
                    'fix_quality': quality,
                    'valid': True
                }
        elif msg_type == 'RMC':
            if parts[2] == 'A': 
                speed = float(parts[7]) if parts[7] else 0.0
                return {
                    'lat': to_decimal(parts[3], parts[4]),
                    'lon': to_decimal(parts[5], parts[6]),
                    'speed': speed * 1.852, 
                    'valid': True
                }
    except: 
        pass
    return None

def process_gps_data(line):
    parsed = parse_nmea(line)
    if parsed:
        with map_data_lock:
            map_session.update(parsed)
            map_session['last_update'] = datetime.now().strftime('%H:%M:%S')
            if 'lat' in parsed and 'lon' in parsed:
                map_track_history.append({
                    'time': map_session['last_update'],
                    'lat': parsed['lat'],
                    'lon': parsed['lon'],
                    'alt': parsed.get('altitude', 0),
                    'speed': parsed.get('speed', 0)
                })

def serial_reader():
    global ser, monitor_running
    while monitor_running:
        try:
            if ser and ser.is_open:
                line = ser.readline().decode('utf-8', errors='replace')
                if line:
                    monitor_queue.put(line)
                    if line.startswith('$'): 
                        process_gps_data(line.strip())
                else:
                    time.sleep(0.01)
            else:
                if current_port and monitor_running:
                    reconnect_serial()
                else:
                    time.sleep(0.1)
        except:
            time.sleep(1)

def reconnect_serial():
    global ser
    time.sleep(2)
    try:
        if is_port_available(current_port):
            ser = serial.Serial(port=current_port, baudrate=current_baud, timeout=0.1)
    except: 
        pass

def cleanup_serial():
    global ser, monitor_running
    with monitor_lock:
        monitor_running = False
        safe_serial_close(ser)

atexit.register(cleanup_on_shutdown)

# ==================== STM32 LOADER CLASS ====================
class STM32LoaderWeb:
    def get_serial_ports(self):
        return [port.device for port in serial.tools.list_ports.comports()]

    def upload_firmware(self, port, file_path, file_type, baud_rate):
        try:
            env = os.environ.copy()
            env['STM32LOADER_SERIAL_PORT'] = port
            args = ["-p", port, "-e", "-w", "-v", "-b", str(baud_rate)]
            if file_type == "bin":
                args.append("-B")
            args.append(file_path)
            
            # UPDATED: Use the custom flag to call ourself
            if getattr(sys, 'frozen', False):
                # Call the EXE itself with the flag
                cmd = [sys.executable, '--firmware-upload'] + args
            else:
                # Dev mode
                cmd = [sys.executable, "-m", "stm32loader"] + args
            
            logging.info(f"Running upload command: {cmd}")
            
            proc = subprocess.run(
                cmd,
                env=env, 
                capture_output=True, 
                text=True, 
                timeout=60,
                creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0
            )
            
            return {
                'success': proc.returncode == 0,
                'message': proc.stdout + "\n" + proc.stderr,
                'returncode': proc.returncode
            }
        except subprocess.TimeoutExpired:
            return {'success': False, 'message': 'Upload timeout (60 seconds)', 'returncode': -1}
        except Exception as e:
            return {'success': False, 'message': str(e), 'returncode': -1}

stm32_loader = STM32LoaderWeb()

# ==================== APPLICATION INITIALIZATION ====================
def initialize_application():
    """Initialize application components"""
    global app_start_time
    app_start_time = time.time()
    logging.info("Application initialized")
    
    # Add startup notification
    startup_notification = {
        'type': 'info',
        'title': 'System Started',
        'message': 'Drone Control Center initialized successfully',
        'timestamp': datetime.now().isoformat(),
        'duration': 3000
    }
    add_notification_to_history(startup_notification)

# ==================== FLASK ROUTES ====================
@app.route('/')
def index(): 
    # Initialize on first request if needed
    if 'initialized' not in globals():
        initialize_application()
        globals()['initialized'] = True
    return render_template('index.html')

@app.route('/program-uploader-content')
def program_uploader_content(): 
    return render_template('program_uploader.html')

@app.route('/dashboard-content')
def dashboard_content(): 
    return render_template('dashboard.html')

@app.route('/3d-sim-content')
def sim_content(): 
    return render_template('3d_sim.html')

@app.route('/map-content')
def map_content(): 
    return render_template('map.html', 
                          DEFAULT_LAT=DEFAULT_LAT, 
                          DEFAULT_LON=DEFAULT_LON, 
                          DEFAULT_ZOOM=DEFAULT_ZOOM)

@app.route('/models/<path:filename>')
def serve_model(filename): 
    return send_from_directory(MODELS_DIR, filename)

@app.route('/api/map/data')
def get_map_data():
    with map_data_lock: 
        return jsonify(map_session)

@app.route('/api/ports')
def get_ports(): 
    return jsonify(stm32_loader.get_serial_ports())

@app.route('/api/upload', methods=['POST'])
@api_response(success=False, message="Upload failed")
def handle_upload():
    file = request.files.get('file')
    if not file:
        return {
            'success': False,
            'message': 'No file provided',
            'notification': {
                'type': 'error',
                'title': 'No File',
                'message': 'Please select a firmware file',
                'duration': 4000
            }
        }
    
    # Validate file
    filename = secure_filename(file.filename)
    if not filename.lower().endswith(('.bin', '.hex')):
        return {
            'success': False,
            'message': 'Invalid file type. Please upload .bin or .hex files',
            'notification': {
                'type': 'error',
                'title': 'Invalid File',
                'message': 'Only .bin and .hex files are supported',
                'duration': 4000
            }
        }
    
    save_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
    
    try:
        file.save(save_path)
    except Exception as e:
        return {
            'success': False,
            'message': f'Failed to save file: {str(e)}',
            'notification': {
                'type': 'error',
                'title': 'Save Failed',
                'message': 'Could not save uploaded file',
                'duration': 4000
            }
        }
    
    port = request.form.get('port')
    ftype = request.form.get('file_type', 'bin')
    baud = request.form.get('baud_rate', DEFAULT_BAUD_RATE)
    
    if not port:
        try:
            os.remove(save_path)
        except:
            pass
        return {
            'success': False,
            'message': 'No COM port selected',
            'notification': {
                'type': 'error',
                'title': 'No Port',
                'message': 'Please select a COM port',
                'duration': 4000
            }
        }
    
    # Show progress notification
    res = stm32_loader.upload_firmware(port, save_path, ftype, baud)
    
    # Clean up uploaded file immediately after upload
    try:
        os.remove(save_path)
    except:
        pass
    
    # Add notification to response
    if res['success']:
        res['notification'] = {
            'type': 'success',
            'title': 'Upload Successful',
            'message': 'Firmware uploaded successfully',
            'duration': 5000
        }
    else:
        res['notification'] = {
            'type': 'error',
            'title': 'Upload Failed',
            'message': 'Firmware upload failed. Check connection and try again.',
            'details': res['message'],
            'duration': 6000
        }
    
    return res

@app.route('/api/serial/connect', methods=['POST'])
@api_response(success=False, message="Connection failed")
def connect_serial():
    global ser, monitor_thread, monitor_running, current_port, current_baud
    data = request.json
    port = data.get('port')
    baud = int(data.get('baud_rate', DEFAULT_BAUD_RATE))
    
    with monitor_lock:
        current_port = port
        current_baud = baud
        monitor_running = False
        safe_serial_close(ser)
        if monitor_thread: 
            monitor_thread.join(1.0)
        
        try:
            # Check if port is available
            if not is_port_available(port):
                return {
                    'success': False,
                    'message': f'Port {port} is not available',
                    'notification': {
                        'type': 'error',
                        'title': 'Port Unavailable',
                        'message': f'Port {port} is not available or in use',
                        'duration': 5000
                    }
                }
            
            ser = serial.Serial(port, baud, timeout=0.1)
            monitor_running = True
            monitor_thread = threading.Thread(target=serial_reader, daemon=True)
            monitor_thread.start()
            
            # Enhanced response with notification
            return {
                'success': True,
                'message': f'Connected to {port} at {baud} baud',
                'notification': {
                    'type': 'success',
                    'title': 'Connected',
                    'message': f'Serial connection established on {port}',
                    'duration': 3000
                }
            }
        except serial.SerialException as e:
            return {
                'success': False,
                'message': str(e),
                'notification': {
                    'type': 'error',
                    'title': 'Connection Failed',
                    'message': f'Failed to connect to {port}: {str(e)}',
                    'duration': 5000
                }
            }
        except Exception as e:
            return {
                'success': False,
                'message': str(e),
                'notification': {
                    'type': 'error',
                    'title': 'Connection Error',
                    'message': f'Unexpected error connecting to {port}',
                    'duration': 5000
                }
            }

@app.route('/api/serial/disconnect', methods=['POST'])
@api_response(success=False, message="Disconnect failed")
def disconnect_serial():
    global ser, monitor_running
    try:
        cleanup_serial()
        return {
            'success': True,
            'message': 'Disconnected successfully',
            'notification': {
                'type': 'info',
                'title': 'Disconnected',
                'message': 'Serial connection closed',
                'duration': 3000
            }
        }
    except Exception as e:
        return {
            'success': False,
            'message': str(e),
            'notification': {
                'type': 'error',
                'title': 'Disconnect Error',
                'message': 'Failed to disconnect properly',
                'duration': 4000
            }
        }

@app.route('/api/serial/data')
def get_data():
    out = []
    while not monitor_queue.empty():
        try: 
            out.append(monitor_queue.get_nowait())
        except: 
            break
    return jsonify({'data': ''.join(out)})

@app.route('/api/serial/send', methods=['POST'])
@api_response(success=False, message="Send failed")
def send_data():
    data = request.json
    msg = data.get('message', '').strip()
    line_ending = data.get('line_ending', 'lf')
    
    if not msg:
        return {
            'success': False,
            'message': 'No message provided',
            'notification': {
                'type': 'warning',
                'title': 'Empty Command',
                'message': 'Please enter a command to send',
                'duration': 3000
            }
        }
    
    try:
        if not ser or not ser.is_open:
            return {
                'success': False,
                'message': 'Serial port not connected',
                'notification': {
                    'type': 'error',
                    'title': 'Not Connected',
                    'message': 'Connect to a serial port first',
                    'duration': 4000
                }
            }
        
        # Add line ending
        if line_ending == 'lf':
            msg_to_send = msg + '\n'
        elif line_ending == 'cr':
            msg_to_send = msg + '\r'
        elif line_ending == 'crlf':
            msg_to_send = msg + '\r\n'
        else:
            msg_to_send = msg + '\n'
        
        ser.write(msg_to_send.encode())
        
        # Log the command
        logging.info(f"Sent command: {msg}")
        
        # Check for special commands that need feedback
        command_map = {
            'e': 'IMU Streaming',
            'i': 'GPS Activation', 
            'q': 'Stop Command',
            'm1': 'Motor 1 Test',
            'm2': 'Motor 2 Test',
            'm3': 'Motor 3 Test',
            'm4': 'Motor 4 Test',
            't': 'Tare IMU'
        }
        
        cmd_name = command_map.get(msg, msg)
        
        return {
            'success': True,
            'message': f'Command sent: {msg}',
            'notification': {
                'type': 'success',
                'title': 'Command Sent',
                'message': f'{cmd_name} command executed',
                'duration': 2000
            }
        }
    except Exception as e:
        logging.error(f"Send error: {e}")
        return {
            'success': False,
            'message': str(e),
            'notification': {
                'type': 'error',
                'title': 'Send Failed',
                'message': f'Failed to send: {msg}',
                'duration': 4000
            }
        }

@app.route('/api/notifications', methods=['GET'])
def get_notifications():
    """Get system notifications"""
    with app.app_context():
        notifications = [
            {
                'type': 'info',
                'title': 'System Status',
                'message': f'Application running on port 5003',
                'timestamp': datetime.now().isoformat(),
                'duration': 3000
            }
        ]
        
        # Add serial connection status
        if ser and ser.is_open:
            notifications.append({
                'type': 'success',
                'title': 'Serial Status',
                'message': f'Connected to {current_port} at {current_baud} baud',
                'timestamp': datetime.now().isoformat()
            })
        else:
            notifications.append({
                'type': 'warning',
                'title': 'Serial Status',
                'message': 'Not connected to any serial port',
                'timestamp': datetime.now().isoformat()
            })
        
        # Add GPS status
        with map_data_lock:
            if map_session['valid']:
                notifications.append({
                    'type': 'success',
                    'title': 'GPS Status',
                    'message': f'GPS lock with {map_session["satellites"]} satellites',
                    'timestamp': datetime.now().isoformat()
                })
        
        return jsonify({
            'success': True,
            'notifications': notifications,
            'history': notification_history[-10:],  # Last 10 notifications
            'unread_count': len([n for n in notification_history if not n.get('read', False)])
        })

@app.route('/api/notifications/clear', methods=['POST'])
def clear_notifications():
    """Clear notification history"""
    global notification_history
    notification_history = []
    return jsonify({
        'success': True,
        'message': 'Notification history cleared',
        'notification': {
            'type': 'info',
            'title': 'Notifications Cleared',
            'message': 'Notification history has been cleared',
            'duration': 3000
        }
    })

@app.route('/api/notifications/mark-read', methods=['POST'])
def mark_notifications_read():
    """Mark all notifications as read"""
    global notification_history
    for notification in notification_history:
        notification['read'] = True
    return jsonify({
        'success': True,
        'message': 'All notifications marked as read'
    })

@app.route('/api/system/status', methods=['GET'])
def system_status():
    """Get system status information"""
    status = {
        'connected': ser and ser.is_open,
        'port': current_port if ser and ser.is_open else None,
        'baudrate': current_baud if ser and ser.is_open else None,
        'monitor_running': monitor_running,
        'gps_valid': map_session['valid'],
        'gps_satellites': map_session['satellites'],
        'uptime': time.time() - app_start_time,
        'notification_count': len(notification_history)
    }
    return jsonify({
        'success': True,
        'data': status,
        'notification': {
            'type': 'info',
            'title': 'System Status',
            'message': 'System status retrieved',
            'duration': 2000
        } if ser and ser.is_open else None
    })

@app.route('/api/system/cleanup', methods=['POST'])
def system_cleanup():
    """Clean up temporary files"""
    try:
        # Remove uploads folder if empty
        if os.path.exists(UPLOAD_FOLDER):
            if not os.listdir(UPLOAD_FOLDER):
                shutil.rmtree(UPLOAD_FOLDER)
                os.makedirs(UPLOAD_FOLDER, exist_ok=True)
                message = "Uploads folder cleaned (was empty)"
            else:
                message = "Uploads folder not empty - skipping cleanup"
        else:
            message = "Uploads folder does not exist"
        
        return jsonify({
            'success': True,
            'message': message,
            'notification': {
                'type': 'info',
                'title': 'Cleanup Complete',
                'message': message,
                'duration': 3000
            }
        })
    except Exception as e:
        return jsonify({
            'success': False,
            'message': str(e),
            'notification': {
                'type': 'error',
                'title': 'Cleanup Failed',
                'message': 'Failed to clean up files',
                'duration': 4000
            }
        })

@app.route('/api/system/info', methods=['GET'])
def system_info():
    """Get system information"""
    info = {
        'python_version': sys.version,
        'platform': sys.platform,
        'app_directory': BASE_DIR,
        'upload_folder': UPLOAD_FOLDER if os.path.exists(UPLOAD_FOLDER) else 'Not created',
        'models_folder': MODELS_DIR,
        'max_file_size': MAX_FILE_SIZE,
        'default_baud_rate': DEFAULT_BAUD_RATE,
        'flask_debug': app.debug
    }
    return jsonify({
        'success': True,
        'data': info,
        'notification': {
            'type': 'info',
            'title': 'System Info',
            'message': 'System information retrieved',
            'duration': 2000
        }
    })

# ==================== ERROR HANDLERS ====================
@app.errorhandler(404)
def not_found_error(error):
    return jsonify({
        'success': False,
        'message': 'Resource not found',
        'notification': {
            'type': 'error',
            'title': '404 Not Found',
            'message': 'The requested resource was not found',
            'duration': 4000
        }
    }), 404

@app.errorhandler(500)
def internal_error(error):
    logging.error(f"Internal server error: {error}")
    return jsonify({
        'success': False,
        'message': 'Internal server error',
        'notification': {
            'type': 'error',
            'title': 'Server Error',
            'message': 'An internal server error occurred. Check logs for details.',
            'duration': 5000
        }
    }), 500

@app.errorhandler(400)
def bad_request_error(error):
    return jsonify({
        'success': False,
        'message': 'Bad request',
        'notification': {
            'type': 'error',
            'title': 'Bad Request',
            'message': 'The request was malformed or missing required parameters',
            'duration': 4000
        }
    }), 400

@app.errorhandler(413)
def request_too_large_error(error):
    return jsonify({
        'success': False,
        'message': 'File too large',
        'notification': {
            'type': 'error',
            'title': 'File Too Large',
            'message': 'The uploaded file exceeds the maximum allowed size (16MB)',
            'duration': 5000
        }
    }), 413

# ==================== APPLICATION TEARDOWN ====================
@app.teardown_appcontext
def teardown_appcontext(exception=None):
    """Clean up on app context teardown"""
    if exception:
        logging.error(f"App context teardown with exception: {exception}")

# ==================== MAIN ENTRY POINT ====================
def run_flask():
    """Run Flask server"""
    try:
        app.run(debug=False, host='127.0.0.1', port=5003, use_reloader=False)
    except Exception as e:
        logging.error(f"Flask server error: {e}")

def main():
    """Main application entry point"""
    print("DEBUG: Starting main()")
    logging.info("Application starting...")
    
    try:
        # Initialize application
        initialize_application()
        print("DEBUG: Application initialized")
        
        # Start Flask in a separate thread
        print("DEBUG: Starting Flask server...")
        flask_thread = threading.Thread(target=run_flask, daemon=True)
        flask_thread.start()
        
        # Wait for Flask to start
        print("DEBUG: Waiting for Flask to start...")
        time.sleep(2)
        
        # Check if Flask is running
        import socket
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        result = sock.connect_ex(('127.0.0.1', 5003))
        sock.close()
        
        if result == 0:
            print("DEBUG: Flask is running on port 5003")
        else:
            print("DEBUG: WARNING - Flask may not be running on port 5003")
        
        # Create and start webview window
        print("DEBUG: Creating webview window...")
        window = webview.create_window(
            'Drone Control Center', 
            'http://127.0.0.1:5003', 
            width=1280, 
            height=800,
            resizable=True,
            min_size=(800, 600),
            confirm_close=True
        )
        
        print(f"DEBUG: Window object created: {window}")
        print("DEBUG: Starting webview...")
        
        # Start webview (this blocks until window is closed)
        webview.start()
        
        print("DEBUG: Webview stopped")
        
    except Exception as e:
        logging.critical(f"Critical Error: {e}")
        logging.critical(traceback.format_exc())
        print(f"CRITICAL ERROR: {e}")
        print(traceback.format_exc())
        
        # Show error message if possible
        try:
            import tkinter as tk
            from tkinter import messagebox
            root = tk.Tk()
            root.withdraw()
            messagebox.showerror("Critical Error", 
                f"The application encountered a critical error:\n\n{str(e)}\n\nCheck debug.log for details.")
            root.destroy()
        except:
            pass
        
        time.sleep(5)
    finally:
        # Cleanup on exit
        print("DEBUG: Running cleanup...")
        cleanup_on_shutdown()
        logging.info("Application shutdown complete")

if __name__ == '__main__':
    main()