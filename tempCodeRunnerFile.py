import sys
import os
import logging
import traceback

# Setup basic file logging immediately to catch startup errors
# This creates a 'debug.log' file next to the exe if it crashes
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
except Exception:
    pass # If logging setup fails, just continue

# Wrap imports in try/except to log import errors
try:
    from flask import Flask, render_template, request, jsonify, send_from_directory, Response
    import serial.tools.list_ports
    import serial
    import subprocess
    import threading
    import queue
    import time
    import atexit
    import csv
    import io
    import webview
    from werkzeug.utils import secure_filename
    from datetime import datetime
    
    # Import stm32loader conditionally later to avoid startup crashes if possible
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

# ==================== DISPATCHER LOGIC (FIX FOR NEW WINDOW) ====================
if __name__ == '__main__':
    # Check if we are being called as the uploader subprocess
    if len(sys.argv) > 1 and sys.argv[1] == '--firmware-upload':
        try:
            # Clean arguments for stm32loader (remove the exe path and our custom flag)
            # sys.argv looks like: ['exe_path', '--firmware-upload', '-p', 'COM3', ...]
            # We need to transform it to what stm32loader expects.
            
            # Remove the '--firmware-upload' flag
            sys.argv.pop(1)
            
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

def cleanup_serial():
    global ser, monitor_running
    with monitor_lock:
        monitor_running = False
        safe_serial_close(ser)

atexit.register(cleanup_serial)

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

def safe_serial_close(serial_obj):
    if serial_obj and hasattr(serial_obj, 'is_open'):
        try:
            serial_obj.close()
        except:
            pass

def is_port_available(port_name):
    try:
        ports = [p.device for p in serial.tools.list_ports.comports()]
        if port_name not in ports: return False
        try:
            s = serial.Serial(port_name)
            s.close()
            return True
        except:
            return False
    except:
        return False

# --- GPS/NMEA Helpers ---
def to_decimal(coord, direct):
    if not coord or not direct: return None
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
    except: return None

def parse_nmea(sentence):
    try:
        if not sentence.startswith('$'): return None
        parts = sentence.split('*')[0].split(',')
        if len(parts) < 6: return None
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
    except: pass
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
                    if line.startswith('$'): process_gps_data(line.strip())
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
    except: pass

# ==================== FLASK ROUTES ====================
@app.route('/')
def index(): return render_template('index.html')
@app.route('/program-uploader-content')
def program_uploader_content(): return render_template('program_uploader.html')
@app.route('/dashboard-content')
def dashboard_content(): return render_template('dashboard.html')
@app.route('/3d-sim-content')
def sim_content(): return render_template('3d_sim.html')
@app.route('/map-content')
def map_content(): return render_template('map.html', DEFAULT_LAT=DEFAULT_LAT, DEFAULT_LON=DEFAULT_LON, DEFAULT_ZOOM=DEFAULT_ZOOM)
@app.route('/models/<path:filename>')
def serve_model(filename): return send_from_directory(MODELS_DIR, filename)

@app.route('/api/map/data')
def get_map_data():
    with map_data_lock: return jsonify(map_session)

@app.route('/api/ports')
def get_ports(): return jsonify(stm32_loader.get_serial_ports())

@app.route('/api/upload', methods=['POST'])
def handle_upload():
    file = request.files.get('file')
    if not file: return jsonify({'success': False, 'message': 'No file'})
    
    filename = secure_filename(file.filename)
    save_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
    file.save(save_path)
    
    port = request.form.get('port')
    ftype = request.form.get('file_type', 'bin')
    baud = request.form.get('baud_rate', DEFAULT_BAUD_RATE)
    
    res = stm32_loader.upload_firmware(port, save_path, ftype, baud)
    try: os.remove(save_path)
    except: pass
    return jsonify(res)

@app.route('/api/serial/connect', methods=['POST'])
def connect_serial():
    global ser, monitor_thread, monitor_running, current_port, current_baud
    data = request.json
    port = data.get('port')
    baud = int(data.get('baud_rate', 57600))
    
    with monitor_lock:
        current_port = port
        current_baud = baud
        monitor_running = False
        safe_serial_close(ser)
        if monitor_thread: monitor_thread.join(1.0)
        
        try:
            ser = serial.Serial(port, baud, timeout=0.1)
            monitor_running = True
            monitor_thread = threading.Thread(target=serial_reader, daemon=True)
            monitor_thread.start()
            return jsonify({'success': True, 'message': f'Connected {port}'})
        except Exception as e:
            return jsonify({'success': False, 'message': str(e)})

@app.route('/api/serial/disconnect', methods=['POST'])
def disconnect_serial():
    cleanup_serial()
    return jsonify({'success': True})

@app.route('/api/serial/data')
def get_data():
    out = []
    while not monitor_queue.empty():
        try: out.append(monitor_queue.get_nowait())
        except: break
    return jsonify({'data': ''.join(out)})

@app.route('/api/serial/send', methods=['POST'])
def send_data():
    data = request.json
    msg = data.get('message', '')
    try:
        if ser and ser.is_open:
            ser.write((msg + '\n').encode())
            return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})
    return jsonify({'success': False})

# ==================== MAIN ENTRY POINT ====================
if __name__ == '__main__':
    # Log startup
    logging.info("Application starting...")
    
    try:
        def start_server(): 
            app.run(debug=False, host='127.0.0.1', port=5050, use_reloader=False)
        
        t = threading.Thread(target=start_server, daemon=True)
        t.start()
        time.sleep(1) # Wait for Flask
        
        # Create Window
        window = webview.create_window(
            'Drone Control Center', 
            'http://127.0.0.1:5050', 
            width=1280, height=800
        )
        webview.start()
        
    except Exception as e:
        logging.critical(f"Critical Error: {e}")
        logging.critical(traceback.format_exc())
        # If possible, show a message box (requires simpledialog or tkinter, but keep it simple)
        print(f"CRITICAL ERROR: {e}")
        time.sleep(5)