# MetarMap WiFi Manager for Pico W
# Copy this file to your Pico W. Fix: parse_request_data returns 5 values for JSON
# so Android app handler "ssid, password, _, _, _ = parse_request_data(request)" works.

import network
import socket
import utime as time
import json
import machine
import neopixel
import gc

try:
    import urequests as requests
except ImportError:
    import requests

try:
    import ujson as json
except ImportError:
    import json

# Configuration
AP_SSID = 'MetarMap-Setup'
AP_PASSWORD = 'metar123'
CONFIG_FILE = 'wifi_config.json'
# Same file name as main.py uses for airport codes
AIRPORT_FILE = 'airports4.txt'

# LED configuration
LED_PIN = 0
NUM_LEDS = 49
LED_BRIGHTNESS = 0.2
STARTUP_BRIGHTNESS = 0.2

# Default display settings
DEFAULT_DISPLAY_TYPE = "OLED"
DEFAULT_LED_MATRIX_BRIGHTNESS = 0.1
DEFAULT_LED_MATRIX_PIN = 1
DEFAULT_MIN_BRIGHTNESS = 2
DEFAULT_MAX_BRIGHTNESS = 15
DEFAULT_BATCH_SIZE = 3
DEFAULT_MATRIX_ONLY = False  # When False: strip shows weather effects; when True: only LED matrix scrolls
DEFAULT_SCROLL_SPEED = 0.08  # Seconds between scroll steps (lower = faster)
DEFAULT_MATRIX_WIRING = "SNAKE_COLUMN"  # ROW_MAJOR, COLUMN_MAJOR, SNAKE_ROW, SNAKE_COLUMN
DEFAULT_SCROLL_PAUSE_BEFORE = 0.75  # Seconds to pause before text scrolls
DEFAULT_CYCLE_DELAY = 10  # Seconds between full airport list cycles (5-1800)
VALID_MATRIX_WIRING = ("ROW_MAJOR", "COLUMN_MAJOR", "SNAKE_ROW", "SNAKE_COLUMN")
# All 24 wx codes from main.py - all enabled by default
WX_TAGS = ["BR", "-RA", "RA", "+RA", "-SN", "SN", "+SN", "SHSN", "LTG", "DSNT", "WND", "FG", "FZFG", "FZFD", "CLR", "CC", "CA", "CG", "VCTS", "TS", "$", "FC", "+FC", "TORNADO"]
DEFAULT_WEATHER_ENABLED = {code: True for code in WX_TAGS}

# Initialize NeoPixels with error handling
try:
    led = neopixel.NeoPixel(machine.Pin(LED_PIN), NUM_LEDS)
    leds_initialized = True
    print("LEDs initialized for WiFi manager")
except Exception as e:
    leds_initialized = False
    print("Error initializing LEDs:", e)

def set_leds(r, g, b, brightness_override=None):
    if not leds_initialized:
        return
    brightness_factor = brightness_override if brightness_override is not None else LED_BRIGHTNESS
    r_scaled = max(0, min(255, int(r * brightness_factor)))
    g_scaled = max(0, min(255, int(g * brightness_factor)))
    b_scaled = max(0, min(255, int(b * brightness_factor)))
    for i in range(NUM_LEDS):
        led[i] = (r_scaled, g_scaled, b_scaled)
    led.write()

def clear_leds():
    if leds_initialized:
        for i in range(NUM_LEDS):
            led[i] = (0, 0, 0)
        led.write()

# LED matrix dimensions (same as main.py)
MATRIX_WIDTH = 32
MATRIX_HEIGHT = 8
MATRIX_NUM_LEDS = MATRIX_WIDTH * MATRIX_HEIGHT

def get_matrix_pixel_index(x, y, wiring):
    """Return linear pixel index for (x, y) given wiring pattern."""
    w = MATRIX_WIDTH
    h = MATRIX_HEIGHT
    if not (0 <= x < w and 0 <= y < h):
        return 0
    wiring = (wiring or "SNAKE_COLUMN").upper()
    if wiring == "ROW_MAJOR":
        return y * w + x
    if wiring == "COLUMN_MAJOR":
        return x * h + y
    if wiring == "SNAKE_ROW":
        return (y * w + x) if (y % 2 == 0) else (y * w + (w - 1 - x))
    if wiring == "SNAKE_COLUMN":
        return (x * h + y) if (x % 2 == 0) else (x * h + (h - 1 - y))
    return y * w + x

def set_matrix_corners_blue():
    """Light the 4 corners of the LED matrix blue in AP mode. Uses config or defaults."""
    try:
        config = {}
        try:
            with open(CONFIG_FILE, 'r') as f:
                config = json.load(f)
        except Exception:
            pass
        pin = int(config.get('led_matrix_pin', DEFAULT_LED_MATRIX_PIN))
        wiring = str(config.get('matrix_wiring', DEFAULT_MATRIX_WIRING)).upper()
        if wiring not in VALID_MATRIX_WIRING:
            wiring = DEFAULT_MATRIX_WIRING
        brightness = float(config.get('led_matrix_brightness', DEFAULT_LED_MATRIX_BRIGHTNESS))
        matrix = neopixel.NeoPixel(machine.Pin(pin), MATRIX_NUM_LEDS)
        r, g, b = 0, 0, int(40 * max(0.1, min(1.0, brightness)))
        corners = [(0, 0), (MATRIX_WIDTH - 1, 0), (0, MATRIX_HEIGHT - 1), (MATRIX_WIDTH - 1, MATRIX_HEIGHT - 1)]
        for (cx, cy) in corners:
            idx = get_matrix_pixel_index(cx, cy, wiring)
            matrix[idx] = (r, g, b)
        matrix.write()
        print("LED matrix: 4 corners set to blue (AP mode)")
    except Exception as e:
        print("Could not set matrix corners (AP mode):", e)

def create_ap():
    print("Setting up access point...")
    set_leds(10, 10, 0, STARTUP_BRIGHTNESS)
    sta_if = network.WLAN(network.STA_IF)
    if sta_if.active():
        sta_if.active(False)
        time.sleep(1)
    ap_if = network.WLAN(network.AP_IF)
    ap_if.active(False)
    time.sleep(1)
    try:
        ap_if.config(essid=AP_SSID, password=AP_PASSWORD, authmode=network.AUTH_WPA_WPA2_PSK)
    except Exception as e:
        print("Error configuring AP:", e)
        try:
            ap_if.config(essid=AP_SSID, password=AP_PASSWORD)
        except Exception as e2:
            print("Error with simpler AP config:", e2)
    ap_if.active(True)
    time.sleep(2)
    if ap_if.active():
        print("AP active with SSID:", ap_if.config('essid'))
        print("AP IP address:", ap_if.ifconfig()[0])
        set_leds(0, 0, 15)
        set_matrix_corners_blue()
        return ap_if
    else:
        print("Failed to activate AP")
        set_leds(15, 0, 0)
        return None

def urldecode(string):
    string = string.replace('+', ' ')
    result = ''
    i = 0
    while i < len(string):
        if string[i] == '%':
            try:
                result += chr(int(string[i+1:i+3], 16))
                i += 3
            except:
                result += string[i]
                i += 1
        elif string[i] == '&':
            break
        else:
            result += string[i]
            i += 1
    return result

def parse_request_data(request):
    """Parse both form data and JSON requests. Always returns 5 values for unpacking."""
    try:
        print("Parsing request data...")
        # Check if it's a JSON request (from Android app)
        if "Content-Type: application/json" in request:
            print("Detected JSON request")
            json_start = request.find('\r\n\r\n') + 4
            json_data = request[json_start:]
            print("JSON body length:", len(json_data))
            try:
                config = json.loads(json_data)
                ssid_val = (config.get('ssid') or '').strip() if config.get('ssid') is not None else ''
                password_val = config.get('password')
                if password_val is not None and not isinstance(password_val, str):
                    password_val = str(password_val)
                elif password_val is None:
                    password_val = ''
                if ssid_val and 'password' in config:
                    display_type = config.get('display_type', DEFAULT_DISPLAY_TYPE)
                    try:
                        led_matrix_brightness = float(config.get('led_matrix_brightness', DEFAULT_LED_MATRIX_BRIGHTNESS))
                    except (TypeError, ValueError):
                        led_matrix_brightness = DEFAULT_LED_MATRIX_BRIGHTNESS
                    try:
                        led_matrix_pin = int(config.get('led_matrix_pin', DEFAULT_LED_MATRIX_PIN))
                    except (TypeError, ValueError):
                        led_matrix_pin = DEFAULT_LED_MATRIX_PIN
                    try:
                        batch_size = int(config.get('batch_size', DEFAULT_BATCH_SIZE))
                        batch_size = max(1, min(20, batch_size))
                    except (TypeError, ValueError):
                        batch_size = DEFAULT_BATCH_SIZE
                    try:
                        min_brightness = int(config.get('min_brightness', DEFAULT_MIN_BRIGHTNESS))
                        min_brightness = max(0, min(255, min_brightness))
                    except (TypeError, ValueError):
                        min_brightness = DEFAULT_MIN_BRIGHTNESS
                    try:
                        max_brightness = int(config.get('max_brightness', DEFAULT_MAX_BRIGHTNESS))
                        max_brightness = max(0, min(255, max_brightness))
                    except (TypeError, ValueError):
                        max_brightness = DEFAULT_MAX_BRIGHTNESS
                    weather_enabled = config.get('weather_enabled', DEFAULT_WEATHER_ENABLED)
                    if isinstance(weather_enabled, dict):
                        weather_enabled = {str(k): bool(v) for k, v in weather_enabled.items()}
                    else:
                        weather_enabled = dict(DEFAULT_WEATHER_ENABLED)
                    matrix_only = bool(config.get('matrix_only', DEFAULT_MATRIX_ONLY))
                    try:
                        scroll_speed = float(config.get('scroll_speed', DEFAULT_SCROLL_SPEED))
                        scroll_speed = max(0.03, min(0.2, scroll_speed))
                    except (TypeError, ValueError):
                        scroll_speed = DEFAULT_SCROLL_SPEED
                    matrix_wiring = str(config.get('matrix_wiring', DEFAULT_MATRIX_WIRING)).upper()
                    if matrix_wiring not in VALID_MATRIX_WIRING:
                        matrix_wiring = DEFAULT_MATRIX_WIRING
                    try:
                        scroll_pause_before = max(0, min(2, float(config.get('scroll_pause_before', DEFAULT_SCROLL_PAUSE_BEFORE))))
                    except (TypeError, ValueError):
                        scroll_pause_before = DEFAULT_SCROLL_PAUSE_BEFORE
                    try:
                        cycle_delay = max(5, min(1800, int(float(config.get('cycle_delay', DEFAULT_CYCLE_DELAY)))))
                    except (TypeError, ValueError):
                        cycle_delay = DEFAULT_CYCLE_DELAY
                    print("Parsed password length:", len(password_val))
                    return (ssid_val, password_val, display_type,
                            led_matrix_brightness, led_matrix_pin, min_brightness, max_brightness, batch_size, weather_enabled, matrix_only, scroll_speed, matrix_wiring, scroll_pause_before, cycle_delay)
            except Exception as e:
                print("JSON parsing error:", e)
            return (None, None, DEFAULT_DISPLAY_TYPE, DEFAULT_LED_MATRIX_BRIGHTNESS, DEFAULT_LED_MATRIX_PIN,
                    DEFAULT_MIN_BRIGHTNESS, DEFAULT_MAX_BRIGHTNESS, DEFAULT_BATCH_SIZE, DEFAULT_WEATHER_ENABLED, DEFAULT_MATRIX_ONLY, DEFAULT_SCROLL_SPEED, DEFAULT_MATRIX_WIRING, DEFAULT_SCROLL_PAUSE_BEFORE, DEFAULT_CYCLE_DELAY)

        # Check if it's form data (from browser)
        if "Content-Type: application/x-www-form-urlencoded" in request:
            print("Detected form data request")
            form_data_start = request.find('\r\n\r\n') + 4
            form_data = request[form_data_start:]
            params = {}
            for param in form_data.split('&'):
                if '=' in param:
                    key, value = param.split('=', 1)
                    params[key] = urldecode(value)
            ssid = (params.get('ssid') or '').strip() or None
            password = params.get('password') or None
            display_type = params.get('display_type', DEFAULT_DISPLAY_TYPE)
            try:
                led_matrix_brightness = float(params.get('led_matrix_brightness', DEFAULT_LED_MATRIX_BRIGHTNESS))
            except (TypeError, ValueError):
                led_matrix_brightness = DEFAULT_LED_MATRIX_BRIGHTNESS
            try:
                led_matrix_pin = int(params.get('led_matrix_pin', DEFAULT_LED_MATRIX_PIN))
            except (TypeError, ValueError):
                led_matrix_pin = DEFAULT_LED_MATRIX_PIN
            try:
                batch_size = max(1, min(20, int(params.get('batch_size', DEFAULT_BATCH_SIZE))))
            except (TypeError, ValueError):
                batch_size = DEFAULT_BATCH_SIZE
            try:
                min_brightness = max(0, min(255, int(params.get('min_brightness', DEFAULT_MIN_BRIGHTNESS))))
            except (TypeError, ValueError):
                min_brightness = DEFAULT_MIN_BRIGHTNESS
            try:
                max_brightness = max(0, min(255, int(params.get('max_brightness', DEFAULT_MAX_BRIGHTNESS))))
            except (TypeError, ValueError):
                max_brightness = DEFAULT_MAX_BRIGHTNESS
            matrix_only = params.get('matrix_only', str(DEFAULT_MATRIX_ONLY)).lower() in ('true', '1', 'yes', 'on')
            try:
                scroll_speed = max(0.03, min(0.2, float(params.get('scroll_speed', DEFAULT_SCROLL_SPEED))))
            except (TypeError, ValueError):
                scroll_speed = DEFAULT_SCROLL_SPEED
            matrix_wiring = str(params.get('matrix_wiring', DEFAULT_MATRIX_WIRING)).upper()
            if matrix_wiring not in VALID_MATRIX_WIRING:
                matrix_wiring = DEFAULT_MATRIX_WIRING
            try:
                scroll_pause_before = max(0, min(2, float(params.get('scroll_pause_before', DEFAULT_SCROLL_PAUSE_BEFORE))))
            except (TypeError, ValueError):
                scroll_pause_before = DEFAULT_SCROLL_PAUSE_BEFORE
            try:
                cycle_delay = max(5, min(1800, int(float(params.get('cycle_delay', DEFAULT_CYCLE_DELAY)))))
            except (TypeError, ValueError):
                cycle_delay = DEFAULT_CYCLE_DELAY
            return (ssid, password, display_type, led_matrix_brightness, led_matrix_pin, min_brightness, max_brightness, batch_size, DEFAULT_WEATHER_ENABLED, matrix_only, scroll_speed, matrix_wiring, scroll_pause_before, cycle_delay)

        print("No recognized Content-Type in request")
        return (None, None, DEFAULT_DISPLAY_TYPE, DEFAULT_LED_MATRIX_BRIGHTNESS, DEFAULT_LED_MATRIX_PIN,
                DEFAULT_MIN_BRIGHTNESS, DEFAULT_MAX_BRIGHTNESS, DEFAULT_BATCH_SIZE, DEFAULT_WEATHER_ENABLED, DEFAULT_MATRIX_ONLY, DEFAULT_SCROLL_SPEED, DEFAULT_MATRIX_WIRING, DEFAULT_SCROLL_PAUSE_BEFORE, DEFAULT_CYCLE_DELAY)
    except Exception as e:
        print("Error parsing request data:", e)
        return (None, None, DEFAULT_DISPLAY_TYPE, DEFAULT_LED_MATRIX_BRIGHTNESS, DEFAULT_LED_MATRIX_PIN,
                DEFAULT_MIN_BRIGHTNESS, DEFAULT_MAX_BRIGHTNESS, DEFAULT_BATCH_SIZE, DEFAULT_WEATHER_ENABLED, DEFAULT_MATRIX_ONLY, DEFAULT_SCROLL_SPEED, DEFAULT_MATRIX_WIRING, DEFAULT_SCROLL_PAUSE_BEFORE, DEFAULT_CYCLE_DELAY)

def save_wifi_config(ssid, password, display_type=DEFAULT_DISPLAY_TYPE,
                     led_matrix_brightness=DEFAULT_LED_MATRIX_BRIGHTNESS,
                     led_matrix_pin=DEFAULT_LED_MATRIX_PIN,
                     min_brightness=DEFAULT_MIN_BRIGHTNESS,
                     max_brightness=DEFAULT_MAX_BRIGHTNESS,
                     batch_size=DEFAULT_BATCH_SIZE,
                     weather_enabled=None,
                     matrix_only=DEFAULT_MATRIX_ONLY,
                     scroll_speed=DEFAULT_SCROLL_SPEED,
                     matrix_wiring=DEFAULT_MATRIX_WIRING,
                     scroll_pause_before=DEFAULT_SCROLL_PAUSE_BEFORE,
                     cycle_delay=DEFAULT_CYCLE_DELAY):
    try:
        if weather_enabled is None:
            weather_enabled = DEFAULT_WEATHER_ENABLED
        if not isinstance(weather_enabled, dict):
            weather_enabled = dict(DEFAULT_WEATHER_ENABLED)
        scroll_speed = max(0.03, min(0.2, float(scroll_speed)))
        matrix_wiring = str(matrix_wiring).upper()
        if matrix_wiring not in VALID_MATRIX_WIRING:
            matrix_wiring = DEFAULT_MATRIX_WIRING
        scroll_pause_before = max(0, min(2, float(scroll_pause_before)))
        cycle_delay = max(5, min(1800, int(cycle_delay)))
        config = {
            'ssid': ssid,
            'password': password,
            'display_type': display_type,
            'led_matrix_brightness': led_matrix_brightness,
            'led_matrix_pin': led_matrix_pin,
            'min_brightness': max(0, min(255, int(min_brightness))),
            'max_brightness': max(0, min(255, int(max_brightness))),
            'batch_size': max(1, min(20, int(batch_size))),
            'weather_enabled': {str(k): bool(v) for k, v in weather_enabled.items()},
            'matrix_only': bool(matrix_only),
            'scroll_speed': scroll_speed,
            'matrix_wiring': matrix_wiring,
            'scroll_pause_before': scroll_pause_before,
            'cycle_delay': cycle_delay
        }
        with open(CONFIG_FILE, 'w') as f:
            json.dump(config, f)
        print("WiFi configuration saved for SSID:", ssid, "password length:", len(password) if password else 0)
        return True
    except Exception as e:
        print("Error saving WiFi config:", e)
        return False

def update_display_config_only(display_type, led_matrix_brightness, led_matrix_pin, min_brightness, max_brightness,
                               batch_size, matrix_only, scroll_speed, matrix_wiring, scroll_pause_before, cycle_delay):
    """Update only display/batch settings; keep existing ssid/password. Used when browser form has no WiFi fields."""
    try:
        config = {}
        try:
            with open(CONFIG_FILE, 'r') as f:
                config = json.load(f)
        except Exception:
            pass
        config['display_type'] = display_type
        config['led_matrix_brightness'] = led_matrix_brightness
        config['led_matrix_pin'] = int(led_matrix_pin)
        config['min_brightness'] = max(0, min(255, int(min_brightness)))
        config['max_brightness'] = max(0, min(255, int(max_brightness)))
        config['batch_size'] = max(1, min(20, int(batch_size)))
        config['matrix_only'] = bool(matrix_only)
        config['scroll_speed'] = max(0.03, min(0.2, float(scroll_speed)))
        config['matrix_wiring'] = str(matrix_wiring).upper() if str(matrix_wiring).upper() in VALID_MATRIX_WIRING else DEFAULT_MATRIX_WIRING
        config['scroll_pause_before'] = max(0, min(2, float(scroll_pause_before)))
        config['cycle_delay'] = max(5, min(1800, int(cycle_delay)))
        if 'weather_enabled' not in config:
            config['weather_enabled'] = dict(DEFAULT_WEATHER_ENABLED)
        with open(CONFIG_FILE, 'w') as f:
            json.dump(config, f)
        print("Display/config updated (no WiFi change)")
        return True
    except Exception as e:
        print("Error updating display config:", e)
        return False

def send_json_response(conn, success, message=None, ip=None):
    response = {
        'success': success,
        'message': message if message else ('Success' if success else 'Failed'),
    }
    if ip:
        response['ip'] = ip
    body = json.dumps(response)
    conn.send('HTTP/1.1 200 OK\r\n')
    conn.send('Content-Type: application/json; charset=utf-8\r\n')
    conn.send('Content-Length: %d\r\n\r\n' % len(body))
    conn.sendall(body)

def test_wifi_connection(ssid, password):
    print("Testing connection to:", ssid)
    set_leds(15, 15, 0, STARTUP_BRIGHTNESS)
    sta_if = network.WLAN(network.STA_IF)
    sta_if.active(False)
    time.sleep(1)
    sta_if.active(True)
    sta_if.connect(ssid, password)
    max_wait = 20
    while not sta_if.isconnected() and max_wait > 0:
        max_wait -= 1
        time.sleep(1)
    if sta_if.isconnected():
        ip = sta_if.ifconfig()[0]
        print("Successfully connected to", ssid, "IP:", ip)
        set_leds(0, 10, 0)
        time.sleep(1)
        sta_if.active(False)
        set_leds(0, 0, 15)
        return True, ip
    else:
        print("Failed to connect to", ssid)
        set_leds(15, 0, 0)
        time.sleep(1)
        set_leds(0, 0, 15)
        sta_if.active(False)
        return False, None

# Generate HTML for setup page (same options as app)
def get_html_setup_page():
    html = """<!DOCTYPE html>
    <html>
    <head>
        <title>MetarMap Setup</title>
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <style>
            body { font-family: Arial; margin: 0; padding: 20px; text-align: center; }
            h1 { color: #0066cc; margin-bottom: 30px; }
            .config-section { background: #f5f5f5; padding: 20px; margin: 20px 0; border-radius: 5px; max-width: 420px; margin-left: auto; margin-right: auto; text-align: left; }
            .section-title { font-size: 18px; color: #0066cc; margin-bottom: 15px; font-weight: bold; }
            .form-group { margin-bottom: 20px; }
            label { display: block; margin-bottom: 5px; font-weight: bold; color: #333; }
            input, select { width: 100%; padding: 10px; font-size: 16px; border: 1px solid #ddd; border-radius: 4px; box-sizing: border-box; }
            .range-container { display: flex; align-items: center; gap: 15px; }
            .range-container input[type="range"] { flex: 1; }
            .range-value { min-width: 50px; font-weight: bold; color: #0066cc; }
            .note { font-size: 12px; color: #666; margin-top: 5px; }
            input[type=checkbox] { width: auto; }
            .btn { background: #0066cc; color: white; border: none; cursor: pointer; padding: 12px 20px; font-size: 16px; border-radius: 5px; margin: 5px; }
            .btn:hover { background: #0052a3; }
            .btn-secondary { background: #5a6268; }
            .btn-secondary:hover { background: #545b62; }
            .info-box { background: #e7f3ff; border: 1px solid #b6d4fe; color: #0c5460; padding: 15px; border-radius: 5px; margin: 20px auto; max-width: 420px; text-align: left; }
        </style>
        <script>
            function toggleMatrix() {
                var d = document.getElementById('display_type').value;
                document.getElementById('led_matrix_settings').style.display = (d === 'LED_MATRIX') ? 'block' : 'none';
            }
            function scrollSpeedToDelay(v) { return Math.round((0.2 - (v - 1) * 0.17 / 9) * 1000) / 1000; }
            function updateScrollLabel() {
                var v = parseInt(document.getElementById('scroll_speed_slider').value, 10);
                document.getElementById('scroll_speed_val').innerText = v + ' (1=slow, 10=fast)';
                document.getElementById('scroll_speed').value = scrollSpeedToDelay(v);
            }
            window.onload = function() {
                toggleMatrix();
                updateScrollLabel();
                fetch('/config').then(function(r) { return r.json(); }).then(function(c) {
                    if (c.display_type) document.getElementById('display_type').value = c.display_type;
                    if (c.led_matrix_pin != null) document.getElementById('led_matrix_pin').value = c.led_matrix_pin;
                    if (c.min_brightness != null) document.getElementById('min_brightness').value = c.min_brightness;
                    if (c.max_brightness != null) document.getElementById('max_brightness').value = c.max_brightness;
                    if (c.batch_size != null) document.getElementById('batch_size').value = c.batch_size;
                    if (c.cycle_delay != null) document.getElementById('cycle_delay').value = c.cycle_delay;
                    if (c.scroll_pause_before != null) document.getElementById('scroll_pause_before').value = c.scroll_pause_before;
                    if (c.matrix_only != null) document.getElementById('matrix_only').checked = c.matrix_only;
                    if (c.matrix_wiring) document.getElementById('matrix_wiring').value = c.matrix_wiring;
                    if (c.scroll_speed != null) {
                        var delay = c.scroll_speed;
                        var slider = Math.round(1 + (0.2 - delay) * 9 / 0.17);
                        slider = Math.max(1, Math.min(10, slider));
                        document.getElementById('scroll_speed_slider').value = slider;
                        document.getElementById('scroll_speed').value = scrollSpeedToDelay(slider);
                        updateScrollLabel();
                    }
                    toggleMatrix();
                }).catch(function() {});
            };
        </script>
    </head>
    <body>
        <h1>MetarMap Setup</h1>
        <div class="info-box">
            <p><strong>MetarMap setup Wi‑Fi (connect your phone here):</strong> SSID <strong>""" + AP_SSID + """</strong> &mdash; password <strong>""" + AP_PASSWORD + """</strong> (defaults in this firmware; edit <code>wifi_manager.py</code> if you changed them).</p>
            <p><strong>Same settings as the app.</strong> The fields below are your <em>home router</em> Wi‑Fi for the Pico to join, not the AP password above. Leave WiFi blank to update display/brightness (device reboots to apply). Fill WiFi + tap Save &amp; Restart to set network and reboot.</p>
            <p><strong>IP:</strong> 192.168.4.1 &nbsp;|&nbsp; <a href="/">Setup</a> &nbsp; <a href="/page/airports">Airports</a> &nbsp; <a href="/page/weather">Weather</a> &nbsp; <a href="/page/help">Help</a> &nbsp; <a href="/page/update">Update</a></p>
        </div>
        <form action="/configure" method="post">
            <div class="config-section">
                <div class="section-title">Home Wi‑Fi (optional)</div>
                <div class="form-group">
                    <label for="ssid">Router network name (SSID)</label>
                    <input type="text" id="ssid" name="ssid" placeholder="Your home/office router—not """ + AP_SSID + """">
                    <div class="note">The network MetarMap should join after setup—not the MetarMap AP password.</div>
                </div>
                <div class="form-group">
                    <label for="password">Router Wi‑Fi password</label>
                    <input type="password" id="password" name="password" placeholder="Router password—not """ + AP_PASSWORD + """ unless that is your router password">
                    <div class="note">Password for your router above, not the setup AP password (unless they match by coincidence).</div>
                </div>
            </div>
            <div class="config-section">
                <div class="section-title">Display</div>
                <div class="form-group">
                    <label for="display_type">Display type</label>
                    <select id="display_type" name="display_type" onchange="toggleMatrix()">
                        <option value="OLED">OLED (128x64)</option>
                        <option value="LED_MATRIX">LED Matrix (8x32)</option>
                        <option value="NONE">No display (strip only)</option>
                    </select>
                </div>
                <div id="led_matrix_settings" style="display:none">
                    <div class="form-group">
                        <label for="matrix_wiring">Matrix layout</label>
                        <select id="matrix_wiring" name="matrix_wiring">
                            <option value="ROW_MAJOR">Row major</option>
                            <option value="COLUMN_MAJOR">Column major</option>
                            <option value="SNAKE_ROW">Snake row</option>
                            <option value="SNAKE_COLUMN">Snake column</option>
                        </select>
                        <div class="note">Change if text looks wrong</div>
                    </div>
                    <div class="form-group">
                        <label for="led_matrix_pin">Matrix data pin (0-28)</label>
                        <input type="number" id="led_matrix_pin" name="led_matrix_pin" min="0" max="28" value="1">
                    </div>
                </div>
                <div class="form-group">
                    <label>Brightness (0-255). With LDR: range. No LDR: set Min = Max for static.</label>
                    <div style="display:flex;gap:10px">
                        <input type="number" id="min_brightness" name="min_brightness" min="0" max="255" value="2" placeholder="Min" style="width:50%">
                        <input type="number" id="max_brightness" name="max_brightness" min="0" max="255" value="15" placeholder="Max" style="width:50%">
                    </div>
                </div>
                <div class="form-group">
                    <label for="batch_size">Batch size (1-20)</label>
                    <input type="number" id="batch_size" name="batch_size" min="1" max="20" value="3">
                </div>
                <div class="form-group">
                    <label for="cycle_delay">Seconds between refreshes (5-1800)</label>
                    <input type="number" id="cycle_delay" name="cycle_delay" min="5" max="1800" value="10">
                </div>
                <div class="form-group">
                    <label>Scroll speed: <span id="scroll_speed_val">5 (1=slow, 10=fast)</span></label>
                    <input type="range" id="scroll_speed_slider" min="1" max="10" value="5" oninput="updateScrollLabel()">
                    <input type="hidden" id="scroll_speed" name="scroll_speed" value="0.08">
                </div>
                <div class="form-group">
                    <label for="scroll_pause_before">Pause before scroll (0-2 sec)</label>
                    <input type="number" id="scroll_pause_before" name="scroll_pause_before" min="0" max="2" step="0.1" value="0.75">
                </div>
                <div class="form-group">
                    <label><input type="checkbox" id="matrix_only" name="matrix_only" value="on"> Strip: flight colors only (no rain/snow effects)</label>
                </div>
            </div>
            <button type="submit" class="btn">Save &amp; Restart</button>
        </form>
        <p style="margin-top:20px;font-size:14px;color:#666">Leave WiFi blank and tap Save &amp; Restart to apply display settings only (device will reboot to apply).</p>
    </body>
    </html>
    """
    return html

# Generate HTML for success page
def get_html_success_page(ssid, display_type, led_matrix_brightness, led_matrix_pin, test_success, ip_address=None):
    if test_success:
        status_html = """
        <h2 style="color: green;">Success!</h2>
        <p>Successfully connected to WiFi network: <strong>""" + ssid + """</strong></p>
        <p>IP address: <strong>""" + (ip_address or "") + """</strong></p>
        """
    else:
        status_html = """
        <h2 style="color: orange;">Configuration Saved</h2>
        <p>WiFi credentials saved for: <strong>""" + ssid + """</strong></p>
        <p>However, we could not establish a connection during testing.</p>
        <p>The device will restart and try to connect.</p>
        """
    display_info = ""
    if display_type == "LED_MATRIX":
        display_info = """
        <p>Display Type: <strong>LED Matrix (8x32)</strong></p>
        <p>LED Matrix Brightness: <strong>""" + str(led_matrix_brightness) + """</strong></p>
        <p>LED Matrix GPIO Pin: <strong>""" + str(led_matrix_pin) + """</strong></p>
        """
    elif display_type == "OLED":
        display_info = "<p>Display Type: <strong>OLED Display (128x64)</strong></p>"
    else:
        display_info = "<p>Display Type: <strong>No Display (LED strip only)</strong></p>"
    html = """<!DOCTYPE html>
    <html>
    <head>
        <title>MetarMap Setup Complete</title>
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <style>
            body { font-family: Arial; margin: 0; padding: 20px; text-align: center; }
            h1 { color: #0066cc; }
            .success-box {
                background: #f5f5f5;
                padding: 20px;
                margin: 20px auto;
                max-width: 400px;
                border-radius: 5px;
                text-align: left;
            }
            .note {
                background-color: #fff3cd;
                border: 1px solid #ffeeba;
                color: #856404;
                padding: 10px;
                margin: 20px auto;
                max-width: 400px;
                border-radius: 4px;
            }
        </style>
    </head>
    <body>
        <h1>MetarMap Setup</h1>
        """ + status_html + """
        <div class="success-box">
            <p><strong>Configuration Summary:</strong></p>
            <p>WiFi Network: <strong>""" + ssid + """</strong></p>
            """ + display_info + """
        </div>
        <div class="note">
            <p><strong>Important:</strong></p>
            <p>You will be disconnected from the MetarMap-Setup network.</p>
            <p>Please reconnect to your WiFi network: <strong>""" + ssid + """</strong></p>
        </div>
        <p>The device will restart in 10 seconds...</p>
    </body>
    </html>
    """
    return html

# Page shown after display-only save (optionally rebooting)
def get_html_display_saved_page(success, message):
    color = "green" if success else "#dc3545"
    html = """<!DOCTYPE html><html><head><meta name="viewport" content="width=device-width, initial-scale=1"><title>MetarMap</title></head><body style="font-family:Arial;text-align:center;padding:20px;">
    <h2 style="color:""" + color + """">""" + ("Settings saved" if success else "Error") + """</h2>
    <p>""" + message + """</p>
    <p><a href="/" style="color:#0066cc">Back to setup</a></p>
    </body></html>"""
    return html

# Airports page: list + fetch/save (same as app Airports tab)
def get_html_airports_page():
    html = """<!DOCTYPE html><html><head><meta name="viewport" content="width=device-width, initial-scale=1"><title>MetarMap Airports</title>
    <style>body{font-family:Arial;margin:0;padding:20px;max-width:500px;margin:0 auto;} h1{color:#0066cc;}
    .nav{margin-bottom:20px;} .nav a{margin-right:15px;color:#0066cc;}
    label{display:block;margin-bottom:5px;font-weight:bold;} textarea{width:100%;height:200px;padding:10px;box-sizing:border-box;}
    .btn{background:#0066cc;color:white;border:none;padding:12px 20px;cursor:pointer;margin:5px 5px 5px 0;} .btn:hover{background:#0052a3;}
    .note{font-size:12px;color:#666;margin-top:5px;} #msg{margin-top:10px;font-weight:bold;}
    </style></head><body>
    <h1>Airports</h1>
    <div class="nav"><a href="/">Setup</a> <a href="/page/airports">Airports</a> <a href="/page/weather">Weather</a> <a href="/page/help">Help</a> <a href="/page/update">Update</a></div>
    <p class="note">One airport code per line (3-4 letters/digits, e.g. KORD, LAX, 0A0). Order = LED order. Use empty line or SKIP for a blank slot.</p>
    <button type="button" class="btn" onclick="fetchList()">Fetch from MetarMap</button>
    <form id="f" onsubmit="return saveList(event)">
      <label for="list">Airport list</label>
      <textarea id="list" name="list" placeholder="Fetch or type one code per line"></textarea>
      <button type="submit" class="btn">Save to MetarMap</button>
    </form>
    <div id="msg"></div>
    <script>
      function fetchList(){ var m=document.getElementById('msg'); m.textContent='Loading...';
        fetch('/airports').then(function(r){return r.text();}).then(function(t){
          document.getElementById('list').value=t.trim();
          m.textContent='Loaded.';
        }).catch(function(){ m.textContent='Fetch failed. Connect to MetarMap WiFi.'; });
      }
      function saveList(e){ e.preventDefault();
        var m=document.getElementById('msg'); m.textContent='Saving...';
        var body=document.getElementById('list').value.replace(/\\r/g,'').trim();
        fetch('/airports',{method:'POST',body:body,headers:{'Content-Type':'text/plain'}}).then(function(r){return r.json();}).then(function(j){
          if(j.success){ m.textContent='Saved. Rebooting...'; fetch('/reboot',{method:'POST'}).catch(function(){}); }
          else m.textContent=j.message || 'Failed';
        }).catch(function(){ m.textContent='Save failed. Connect to MetarMap WiFi.'; });
        return false;
      }
    </script>
    </body></html>"""
    return html

# Weather page: toggles per code (same as app Weather tab)
def get_html_weather_page():
    codes_js = json.dumps(WX_TAGS)
    html = """<!DOCTYPE html><html><head><meta name="viewport" content="width=device-width, initial-scale=1"><title>MetarMap Weather</title>
    <style>body{font-family:Arial;margin:0;padding:20px;max-width:500px;margin:0 auto;} h1{color:#0066cc;}
    .nav{margin-bottom:20px;} .nav a{margin-right:15px;color:#0066cc;}
    .row{display:flex;justify-content:space-between;align-items:center;padding:8px 0;border-bottom:1px solid #eee;}
    .btn{background:#0066cc;color:white;border:none;padding:12px 20px;cursor:pointer;margin-top:15px;} .btn:hover{background:#0052a3;}
    #msg{margin-top:10px;font-weight:bold;} .note{font-size:12px;color:#666;}
    </style></head><body>
    <h1>Weather conditions</h1>
    <div class="nav"><a href="/">Setup</a> <a href="/page/airports">Airports</a> <a href="/page/weather">Weather</a> <a href="/page/help">Help</a> <a href="/page/update">Update</a></div>
    <p class="note">ON = this condition can light the LEDs. OFF = effect disabled.</p>
    <div id="toggles"></div>
    <button type="button" class="btn" onclick="saveWeather()">Save</button>
    <div id="msg"></div>
    <script>
      var WX_TAGS = """ + codes_js + """;
      function load(){
        fetch('/config').then(function(r){return r.json();}).then(function(c){
          var we = c.weather_enabled || {};
          var html = '';
          WX_TAGS.forEach(function(code){
            var checked = we[code] !== false ? 'checked' : '';
            html += '<div class="row"><label>'+code+'</label><input type="checkbox" id="w_'+code+'" '+checked+'></div>';
          });
          document.getElementById('toggles').innerHTML = html;
        }).catch(function(){ document.getElementById('toggles').innerHTML = '<p>Load failed. Connect to MetarMap WiFi.</p>'; });
      }
      function saveWeather(){
        var we = {}; WX_TAGS.forEach(function(c){ we[c] = document.getElementById('w_'+c).checked; });
        document.getElementById('msg').textContent = 'Saving...';
        fetch('/update-config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({weather_enabled:we,reboot:true})})
          .then(function(r){return r.json();}).then(function(j){
            document.getElementById('msg').textContent = j.success ? 'Saved. Rebooting...' : (j.message || 'Failed');
          }).catch(function(){ document.getElementById('msg').textContent = 'Save failed.'; });
      }
      load();
    </script>
    </body></html>"""
    return html

# Help page: same content as app Help tab
def get_html_help_page():
    html = """<!DOCTYPE html><html><head><meta name="viewport" content="width=device-width, initial-scale=1"><title>MetarMap Help</title>
    <style>body{font-family:Arial;margin:0;padding:20px;max-width:600px;margin:0 auto;line-height:1.5;}
    h1{color:#000;} h2{color:#0066cc;font-size:1.1em;margin-top:20px;} .nav{margin-bottom:20px;} .nav a{margin-right:15px;color:#0066cc;}
    .card{background:#f5f5f5;padding:15px;margin:12px 0;border-radius:8px;} .card h3{color:#0066cc;margin-top:0;}
    ul{margin:8px 0;padding-left:20px;} p{margin:8px 0;}
    </style></head><body>
    <h1>Help &amp; Instructions</h1>
    <div class="nav"><a href="/">Setup</a> <a href="/page/airports">Airports</a> <a href="/page/weather">Weather</a> <a href="/page/help">Help</a> <a href="/page/update">Update</a></div>
    <div class="card"><h3>Quick start</h3>
    <p>1. Join MetarMap setup Wi‑Fi: SSID <strong>""" + AP_SSID + """</strong>, password <strong>""" + AP_PASSWORD + """</strong> (unless you changed AP in firmware).<br>2. Setup: enter your <em>home router</em> name and password—not the AP password—then Save &amp; Restart (or leave blank to only change display).<br>3. Airports: add codes (e.g. KORD, LAX), Save to MetarMap.<br>Done.</p></div>
    <div class="card"><h3>What is a MetarMap?</h3>
    <p>MetarMap is a hardware project with a Raspberry Pi Pico W. It fetches real-time aviation weather (METAR), shows flight categories (VFR/MVFR/IFR/LIFR) and weather on an LED strip and optional matrix/OLED. On startup it shows all airports' flight categories at once for a few seconds, then cycles with weather effects.</p></div>
    <div class="card"><h3>Setup (WiFi / display)</h3>
    <p><strong>Router vs AP:</strong> The WiFi fields on Setup are your <em>router</em> credentials so the Pico can reach the internet—not the password for joining <strong>""" + AP_SSID + """</strong> on your phone (default AP password: <strong>""" + AP_PASSWORD + """</strong>).</p>
    <p>Leave WiFi blank to update display/brightness (device reboots to apply). Fill WiFi to set network and restart. Display type, matrix layout, min/max brightness (use same for no LDR), batch size, cycle delay, scroll speed, &quot;Strip: flight colors only&quot; = matrix only.</p></div>
    <div class="card"><h3>Firmware updates (manual)</h3>
    <p>After boot, MetarMap <em>checks</em> online whether a newer firmware exists; it does <strong>not</strong> install by itself. Use <strong>Update</strong> &rarr; Install, open <code>http://&lt;pico-ip&gt;:8080</code> on your home network, or the Android app&apos;s install button when you want to upgrade.</p></div>
    <div class="card"><h3>Airports</h3>
    <p>One code per line. Order = LED order. Fetch from MetarMap loads current list; Save to MetarMap writes your list. Use empty line or SKIP for blank slot. 3&ndash;4 letters or digits (e.g. KORD, 0A0).</p></div>
    <div class="card"><h3>Weather</h3>
    <p>Each code toggles whether that condition lights the LEDs. ON = effect enabled, OFF = disabled. Save sends to device and reboots to apply.</p></div>
    <div class="card"><h3>Weather codes &amp; LED effects</h3>
    <p>Rain: BR, -RA, RA, +RA (cyan flashes). Snow: -SN, SN, +SN, SHSN (white). Lightning: LTG, DSNT (yellow); CC, CA, CG, VCTS (white). Wind: WND (yellow). Fog: FG, FZFG, FZFD (fades). Clear: CLR (white to green). Storms: TS, $, FC, +FC, TORNADO (red/blue).</p></div>
    <div class="card"><h3>Troubleshooting</h3>
    <p><b>App / browser:</b> Connect to MetarMap WiFi (192.168.4.1). Save fails if not on that network.</p>
    <p><b>Mobile data / hotspot:</b> Using cellular often causes SSL errors; use Wi-Fi when possible.</p>
    <p><b>NO DATA AFTER 180 SEC:</b> Check WiFi and internet; power-cycle router and device.</p>
    <p><b>Some airports never show data:</b> API may not have that station; try removing or replacing the code.</p>
    <p><b>Matrix text wrong:</b> Try a different Matrix layout in Setup.</p></div>
    </body></html>"""
    return html

def get_html_update_page():
    html = """<!DOCTYPE html>
    <html><head><meta name="viewport" content="width=device-width"><title>MetarMap Update</title>
    <style>body{font-family:Arial;margin:12px;} .nav{margin-bottom:12px;} a{margin-right:8px;}
    .card{background:#f5f5f5;padding:12px;margin:8px 0;border-radius:6px;}
    button{background:#0d6efd;color:#fff;border:none;padding:10px 16px;border-radius:6px;font-size:16px;}
    </style></head><body>
    <h1>MetarMap firmware update</h1>
    <div class="nav"><a href="/">Setup</a> <a href="/page/airports">Airports</a> <a href="/page/weather">Weather</a> <a href="/page/help">Help</a> <a href="/page/update">Update</a></div>
    <div class="card">
    <p><b>Not automatic:</b> Updates are only <em>detected</em> at boot; nothing installs until you start it (this button, <code>:8080</code> on your LAN, or the app).</p>
    <p><b>When MetarMap is connected to your WiFi</b> (after Save &amp; Reboot), open <code>http://&lt;pico-ip&gt;:8080</code> in a browser or use the app&apos;s &quot;Install firmware update&quot; button. The device must have internet to download.</p>
    <p>From this page (AP mode): try Install update below. It only works if the Pico has internet access.</p>
    <form method="post" action="/start-update"><button type="submit">Install update now</button></form>
    </div>
    </body></html>"""
    return html

# Generate HTML for error page
def get_html_error_page(message):
    html = """<!DOCTYPE html>
    <html>
    <head>
        <title>MetarMap Setup Error</title>
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <style>
            body { font-family: Arial; margin: 0; padding: 20px; text-align: center; }
            h1 { color: #dc3545; }
            .error-box {
                background: #f8d7da;
                border: 1px solid #f5c6cb;
                color: #721c24;
                padding: 15px;
                border-radius: 5px;
                margin: 20px auto;
                max-width: 400px;
            }
        </style>
    </head>
    <body>
        <h1>Setup Error</h1>
        <div class="error-box">
            <p>""" + message + """</p>
        </div>
        <p><a href="/">Go back to setup page</a></p>
    </body>
    </html>
    """
    return html

def run_server():
    ap_if = create_ap()
    if not ap_if:
        print("Failed to set up access point")
        return
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        s.bind(('0.0.0.0', 80))
        s.listen(5)
        print("Server started on port 80. Connect to", AP_SSID, "and visit 192.168.4.1")
    except Exception as e:
        print("Error starting server:", e)
        set_leds(15, 0, 0)
        return
    while True:
        try:
            conn, addr = s.accept()
            print("Client connected from", addr[0])
            set_leds(0, 10, 10)
            time.sleep(0.2)
            set_leds(0, 0, 15)
            conn.settimeout(30)
            request = conn.recv(4096).decode('utf-8')
            # For POST, read full body (may be split across packets) so password is never truncated
            if "POST " in request and "Content-Length:" in request:
                try:
                    cl_start = request.find("Content-Length:")
                    cl_end = request.find("\r\n", cl_start)
                    if cl_end > cl_start:
                        content_len = int(request[cl_start + 14:cl_end].strip())
                        body_start = request.find("\r\n\r\n") + 4
                        if body_start >= 4:
                            body_so_far = request[body_start:] if body_start <= len(request) else ''
                            while len(body_so_far) < content_len:
                                to_read = min(1024, content_len - len(body_so_far))
                                chunk = conn.recv(to_read).decode('utf-8')
                                if not chunk:
                                    break
                                body_so_far += chunk
                            body_so_far = body_so_far[:content_len]
                            request = request[:body_start] + body_so_far
                except (ValueError, IndexError):
                    pass
            # Match GET /config (Android app or any client); first line is request line e.g. "GET /config HTTP/1.1"
            first_line = request.split("\n")[0].strip() if request else ""
            if first_line.startswith("GET ") and "/config" in first_line:
                print("Handling GET /config - fetch current configuration")
                try:
                    config = {}
                    try:
                        with open(CONFIG_FILE, 'r') as f:
                            config = json.load(f)
                    except:
                        pass
                    # Return config with defaults for missing keys
                    response_config = {
                        'display_type': config.get('display_type', DEFAULT_DISPLAY_TYPE),
                        'led_matrix_brightness': config.get('led_matrix_brightness', DEFAULT_LED_MATRIX_BRIGHTNESS),
                        'led_matrix_pin': config.get('led_matrix_pin', DEFAULT_LED_MATRIX_PIN),
                        'min_brightness': config.get('min_brightness', DEFAULT_MIN_BRIGHTNESS),
                        'max_brightness': config.get('max_brightness', DEFAULT_MAX_BRIGHTNESS),
                        'batch_size': config.get('batch_size', DEFAULT_BATCH_SIZE),
                        'matrix_only': config.get('matrix_only', DEFAULT_MATRIX_ONLY),
                        'scroll_speed': config.get('scroll_speed', DEFAULT_SCROLL_SPEED),
                        'matrix_wiring': config.get('matrix_wiring', DEFAULT_MATRIX_WIRING),
                        'scroll_pause_before': config.get('scroll_pause_before', DEFAULT_SCROLL_PAUSE_BEFORE),
                        'cycle_delay': config.get('cycle_delay', DEFAULT_CYCLE_DELAY),
                        'weather_enabled': config.get('weather_enabled', DEFAULT_WEATHER_ENABLED),
                        'sleep_enabled': config.get('sleep_enabled', False),
                        'sleep_at_hour': config.get('sleep_at_hour', 22),
                        'sleep_at_minute': config.get('sleep_at_minute', 0),
                        'wake_at_hour': config.get('wake_at_hour', 6),
                        'wake_at_minute': config.get('wake_at_minute', 0),
                        'sleep_matrix': config.get('sleep_matrix', True),
                        'sleep_leds': config.get('sleep_leds', True),
                        'sleep_oled': config.get('sleep_oled', True),
                        'timezone_offset_hours': config.get('timezone_offset_hours', 0)
                    }
                    response_body = json.dumps(response_config)
                    conn.send('HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n')
                    conn.send('Content-Length: ' + str(len(response_body)) + '\r\n\r\n')
                    conn.sendall(response_body)
                    we = response_config.get('weather_enabled', {})
                    on_count = sum(1 for v in we.values() if v)
                    print("Sent config (weather_enabled: %d on, %d off)" % (on_count, len(we) - on_count))
                except Exception as e:
                    print("Error reading config:", e)
                    send_json_response(conn, False, str(e))
                conn.close()
            elif first_line.startswith("GET ") and "/page/airports" in first_line:
                page = get_html_airports_page()
                conn.send('HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nContent-Length: ' + str(len(page)) + '\r\n\r\n')
                conn.sendall(page)
                conn.close()
            elif first_line.startswith("GET ") and "/page/weather" in first_line:
                page = get_html_weather_page()
                conn.send('HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nContent-Length: ' + str(len(page)) + '\r\n\r\n')
                conn.sendall(page)
                conn.close()
            elif first_line.startswith("GET ") and "/page/help" in first_line:
                page = get_html_help_page()
                conn.send('HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nContent-Length: ' + str(len(page)) + '\r\n\r\n')
                conn.sendall(page)
                conn.close()
            elif first_line.startswith("GET ") and "/page/update" in first_line:
                page = get_html_update_page()
                conn.send('HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nContent-Length: ' + str(len(page)) + '\r\n\r\n')
                conn.sendall(page)
                conn.close()
            elif first_line.startswith("POST ") and "/start-update" in first_line:
                print("Handling POST /start-update - OTA install from browser/app")
                try:
                    import updater
                    ok = updater.install_latest()
                    if ok:
                        send_json_response(conn, True, 'Installing update; device will reboot.')
                        conn.close()
                        set_leds(10, 0, 10)
                        time.sleep(2)
                        clear_leds()
                        machine.reset()
                    else:
                        send_json_response(conn, False, 'Update failed (no internet or download error). Connect Pico to WiFi and use http://<pico-ip>:8080 or the app.')
                        conn.close()
                except Exception as e:
                    print("start-update error:", e)
                    send_json_response(conn, False, 'Update error: ' + str(e))
                    conn.close()
            elif first_line.startswith("GET ") and "/airports" in first_line:
                print("Handling GET /airports - fetch airport list from Pico")
                try:
                    with open(AIRPORT_FILE, 'r') as f:
                        content = f.read()
                    conn.send('HTTP/1.1 200 OK\r\nContent-Type: text/plain; charset=utf-8\r\n')
                    conn.send('Content-Length: ' + str(len(content)) + '\r\n\r\n')
                    conn.sendall(content)
                except Exception as e:
                    print("Error reading airports file:", e)
                    conn.send('HTTP/1.1 200 OK\r\nContent-Type: text/plain; charset=utf-8\r\nContent-Length: 0\r\n\r\n')
                conn.close()
            elif first_line.startswith("POST ") and "/airports" in first_line:
                print("Handling POST /airports - save airport list to Pico")
                try:
                    body_start = request.find('\r\n\r\n') + 4
                    body = request[body_start:].strip() if body_start >= 4 else ''
                    lines = [line.strip().upper() for line in body.split('\n') if line.strip()]
                    with open(AIRPORT_FILE, 'w') as f:
                        f.write('\n'.join(lines))
                    print("Saved", len(lines), "airports to", AIRPORT_FILE)
                    send_json_response(conn, True, 'Saved %d airports' % len(lines))
                except Exception as e:
                    print("Error writing airports file:", e)
                    send_json_response(conn, False, str(e))
                conn.close()
            elif first_line.startswith("POST ") and "/update-config" in first_line:
                print("Handling POST /update-config - update settings only (keep SSID/password)")
                try:
                    body_start = request.find('\r\n\r\n') + 4
                    body = request[body_start:].strip() if body_start >= 4 else '{}'
                    print("Update-config body:", body)
                    updates = json.loads(body) if body else {}
                    config = {}
                    try:
                        with open(CONFIG_FILE, 'r') as f:
                            config = json.load(f)
                    except:
                        pass
                    if 'display_type' in updates:
                        config['display_type'] = str(updates['display_type'])
                    if 'led_matrix_brightness' in updates:
                        config['led_matrix_brightness'] = float(updates['led_matrix_brightness'])
                    if 'led_matrix_pin' in updates:
                        config['led_matrix_pin'] = int(updates['led_matrix_pin'])
                    if 'batch_size' in updates:
                        config['batch_size'] = max(1, min(20, int(float(updates['batch_size']))))
                    if 'min_brightness' in updates:
                        config['min_brightness'] = max(0, min(255, int(updates['min_brightness'])))
                    if 'max_brightness' in updates:
                        config['max_brightness'] = max(0, min(255, int(updates['max_brightness'])))
                    if 'matrix_only' in updates:
                        mo = updates['matrix_only']
                        config['matrix_only'] = mo.lower() in ('true', '1', 'yes') if isinstance(mo, str) else bool(mo)
                    if 'scroll_speed' in updates:
                        try:
                            config['scroll_speed'] = max(0.03, min(0.2, float(updates['scroll_speed'])))
                        except (TypeError, ValueError):
                            pass
                    if 'matrix_wiring' in updates:
                        mw = str(updates['matrix_wiring']).upper()
                        if mw in VALID_MATRIX_WIRING:
                            config['matrix_wiring'] = mw
                    if 'scroll_pause_before' in updates:
                        try:
                            config['scroll_pause_before'] = max(0, min(2, float(updates['scroll_pause_before'])))
                        except (TypeError, ValueError):
                            pass
                    if 'cycle_delay' in updates:
                        try:
                            config['cycle_delay'] = max(5, min(1800, int(float(updates['cycle_delay']))))
                        except (TypeError, ValueError):
                            pass
                    if 'sleep_enabled' in updates:
                        config['sleep_enabled'] = bool(updates['sleep_enabled'])
                    if 'sleep_at_hour' in updates:
                        config['sleep_at_hour'] = max(0, min(23, int(updates['sleep_at_hour'])))
                    if 'sleep_at_minute' in updates:
                        config['sleep_at_minute'] = max(0, min(59, int(updates['sleep_at_minute'])))
                    if 'wake_at_hour' in updates:
                        config['wake_at_hour'] = max(0, min(23, int(updates['wake_at_hour'])))
                    if 'wake_at_minute' in updates:
                        config['wake_at_minute'] = max(0, min(59, int(updates['wake_at_minute'])))
                    if 'sleep_matrix' in updates:
                        config['sleep_matrix'] = bool(updates['sleep_matrix'])
                    if 'sleep_leds' in updates:
                        config['sleep_leds'] = bool(updates['sleep_leds'])
                    if 'sleep_oled' in updates:
                        config['sleep_oled'] = bool(updates['sleep_oled'])
                    if 'timezone_offset_hours' in updates:
                        try:
                            config['timezone_offset_hours'] = max(-12, min(14, int(updates['timezone_offset_hours'])))
                        except (TypeError, ValueError):
                            pass
                    if 'weather_enabled' in updates:
                        we = updates['weather_enabled']
                        if isinstance(we, dict):
                            config['weather_enabled'] = {str(k): bool(v) for k, v in we.items()}
                            for code in WX_TAGS:
                                if code not in config['weather_enabled']:
                                    config['weather_enabled'][code] = True
                            on_count = sum(1 for v in config['weather_enabled'].values() if v)
                            print("Updated weather_enabled: %d on, %d off" % (on_count, len(config['weather_enabled']) - on_count))
                    with open(CONFIG_FILE, 'w') as f:
                        json.dump(config, f)
                    print("Config saved to", CONFIG_FILE)
                    # Check if reboot is requested (default True for backward compatibility)
                    do_reboot = updates.get('reboot', True)
                    if isinstance(do_reboot, str):
                        do_reboot = do_reboot.lower() in ('true', '1', 'yes')
                    if do_reboot:
                        send_json_response(conn, True, 'Settings updated, rebooting')
                        conn.close()
                        set_leds(10, 0, 10)
                        time.sleep(2)
                        clear_leds()
                        machine.reset()
                    else:
                        send_json_response(conn, True, 'Settings saved (no reboot)')
                        conn.close()
                except Exception as e:
                    print("Error updating config:", e)
                    send_json_response(conn, False, str(e))
                conn.close()
            elif first_line.startswith("POST ") and "/configure-wifi" in first_line:
                print("Handling Android app request to /configure-wifi")
                save_success = False
                test_success = False
                do_reboot = False
                ssid, password, display_type, led_matrix_brightness, led_matrix_pin, min_brightness, max_brightness, batch_size, weather_enabled, matrix_only, scroll_speed, matrix_wiring, scroll_pause_before, cycle_delay = parse_request_data(request)
                if ssid and password:
                    print("Received credentials - SSID:", ssid, "Display:", display_type, "Batch size:", batch_size, "Matrix only:", matrix_only, "Scroll speed:", scroll_speed, "Matrix wiring:", matrix_wiring, "Cycle delay:", cycle_delay)
                    save_success = save_wifi_config(ssid, password, display_type, led_matrix_brightness, led_matrix_pin, min_brightness, max_brightness, batch_size, weather_enabled, matrix_only, scroll_speed, matrix_wiring, scroll_pause_before, cycle_delay)
                    # Parse reboot flag and optional sleep settings from body
                    try:
                        body_start = request.find('\r\n\r\n') + 4
                        body = request[body_start:].strip() if body_start >= 4 else '{}'
                        body_json = json.loads(body) if body else {}
                        if save_success and any(k in body_json for k in ('sleep_enabled', 'sleep_at_hour', 'wake_at_hour', 'sleep_matrix', 'sleep_leds', 'sleep_oled', 'timezone_offset_hours')):
                            with open(CONFIG_FILE, 'r') as f:
                                config = json.load(f)
                            if 'sleep_enabled' in body_json: config['sleep_enabled'] = bool(body_json['sleep_enabled'])
                            if 'sleep_at_hour' in body_json: config['sleep_at_hour'] = max(0, min(23, int(body_json['sleep_at_hour'])))
                            if 'sleep_at_minute' in body_json: config['sleep_at_minute'] = max(0, min(59, int(body_json['sleep_at_minute'])))
                            if 'wake_at_hour' in body_json: config['wake_at_hour'] = max(0, min(23, int(body_json['wake_at_hour'])))
                            if 'wake_at_minute' in body_json: config['wake_at_minute'] = max(0, min(59, int(body_json['wake_at_minute'])))
                            if 'sleep_matrix' in body_json: config['sleep_matrix'] = bool(body_json['sleep_matrix'])
                            if 'sleep_leds' in body_json: config['sleep_leds'] = bool(body_json['sleep_leds'])
                            if 'sleep_oled' in body_json: config['sleep_oled'] = bool(body_json['sleep_oled'])
                            if 'timezone_offset_hours' in body_json:
                                try:
                                    config['timezone_offset_hours'] = max(-12, min(14, int(body_json['timezone_offset_hours'])))
                                except (TypeError, ValueError):
                                    pass
                            with open(CONFIG_FILE, 'w') as f:
                                json.dump(config, f)
                        do_reboot = body_json.get('reboot', True)
                        if isinstance(do_reboot, str):
                            do_reboot = do_reboot.lower() in ('true', '1', 'yes')
                    except:
                        do_reboot = True
                    # Reply to app immediately so it doesn't time out (app has ~10s read timeout; connection test can take 20s)
                    if do_reboot:
                        send_json_response(conn, success=save_success,
                            message='Config saved. Testing connection and rebooting…',
                            ip=None)
                    else:
                        send_json_response(conn, success=save_success,
                            message='Configuration saved (no reboot)',
                            ip=None)
                    conn.close()
                    # Now run connection test and reboot (app already got success)
                    if save_success:
                        test_success, ip_address = test_wifi_connection(ssid, password)
                        if test_success:
                            print("Connection test OK, IP:", ip_address)
                        else:
                            print("Connection test failed – device will try again on reboot.")
                    if save_success and do_reboot:
                        set_leds(10, 0, 10)
                        print("Configuration complete. Restarting device...")
                        time.sleep(5)
                        clear_leds()
                        machine.reset()
                else:
                    print("Failed to parse credentials")
                    send_json_response(conn, False, 'Invalid request data')
                    conn.close()
            elif first_line.startswith("POST ") and "/configure" in first_line:
                print("Handling browser request to /configure")
                ssid, password, display_type, led_matrix_brightness, led_matrix_pin, min_brightness, max_brightness, batch_size, weather_enabled, matrix_only, scroll_speed, matrix_wiring, scroll_pause_before, cycle_delay = parse_request_data(request)
                if ssid and password:
                    save_success = save_wifi_config(ssid, password, display_type, led_matrix_brightness, led_matrix_pin, min_brightness, max_brightness, batch_size, weather_enabled, matrix_only, scroll_speed, matrix_wiring, scroll_pause_before, cycle_delay)
                    if save_success:
                        test_success, ip_address = test_wifi_connection(ssid, password)
                    else:
                        test_success, ip_address = False, None
                    success_page = get_html_success_page(ssid, display_type, led_matrix_brightness, led_matrix_pin, test_success, ip_address)
                    conn.send('HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nContent-Length: ' + str(len(success_page)) + '\r\n\r\n')
                    conn.sendall(success_page)
                    conn.close()
                    if save_success and test_success:
                        set_leds(10, 0, 10)
                        time.sleep(5)
                        clear_leds()
                        machine.reset()
                else:
                    # Display-only update (no WiFi): save settings, then reboot
                    ok = update_display_config_only(display_type, led_matrix_brightness, led_matrix_pin, min_brightness, max_brightness, batch_size, matrix_only, scroll_speed, matrix_wiring, scroll_pause_before, cycle_delay)
                    msg = "Settings saved. Rebooting..." if ok else "Failed to save settings."
                    page = get_html_display_saved_page(ok, msg)
                    conn.send('HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nContent-Length: ' + str(len(page)) + '\r\n\r\n')
                    conn.sendall(page)
                    conn.close()
                    if ok:
                        set_leds(10, 0, 10)
                        time.sleep(2)
                        clear_leds()
                        machine.reset()
                # conn already closed in both branches
            elif first_line.startswith("GET ") and "/status" in first_line:
                send_json_response(conn, True, 'MetarMap is online')
                conn.close()
            elif first_line.startswith("POST ") and "/reboot" in first_line:
                send_json_response(conn, True, 'Rebooting')
                conn.close()
                set_leds(10, 0, 10)
                time.sleep(2)
                clear_leds()
                machine.reset()
            else:
                conn.send('HTTP/1.1 200 OK\r\nContent-Type: text/html\r\n\r\n')
                conn.sendall(get_html_setup_page())
                conn.close()
        except Exception as e:
            print("Error handling request:", e)
            try:
                conn.close()
            except:
                pass
        time.sleep(0.1)
        gc.collect()

def start():
    print("===== Starting WiFi Manager =====")
    gc.collect()
    set_leds(12, 12, 0, STARTUP_BRIGHTNESS)
    run_server()

