import network
import socket
import urequests
import utime as time
import machine
import json
import gc
import neopixel
from machine import ADC, Pin, I2C
import ssd1306
import framebuf
import os  # Added missing import

machine.freq(230_000_000)

# Import brightness settings from wifi_manager
try:
    from wifi_manager import STARTUP_BRIGHTNESS
except ImportError:
    STARTUP_BRIGHTNESS = 0.2  # Default if not available

try:
    import sans18
    import writer
    fonts_available = True
except ImportError:
    print("Warning: Font modules not found")
    fonts_available = False

# ===== CONFIGURATION =====
CONFIG_FILE = 'wifi_config.json'
AIRPORT_FILE = 'airports4.txt'  # Your airport file name
FORCE_AP_BUTTON_PIN = 15  # GPIO pin for the force AP mode button (adjust as needed)

# Display configuration - WILL BE OVERRIDDEN BY CONFIG FILE BELOW
DISPLAY_TYPE = "LED_MATRIX"  # Default value, will be changed
LED_MATRIX_WIDTH = 32
LED_MATRIX_HEIGHT = 8
LED_MATRIX_NUM_LEDS = LED_MATRIX_WIDTH * LED_MATRIX_HEIGHT  # 256 LEDs
LED_MATRIX_PIN = 1  # Change this to your desired pin for the LED matrix

# LED Matrix Brightness (0.0 to 1.0) - This will be overridden by auto-brightness
LED_MATRIX_BRIGHTNESS = 0.1  # Fallback value if auto-brightness fails

# LED Matrix Display Settings (SCROLL_SPEED overridden from config)
SCROLL_SPEED = 0.08  # Seconds between scroll steps; loaded from wifi_config.json if present
SCROLL_PAUSE_BEFORE = .75  # Seconds to pause before starting scroll

# ===== BATCH PROCESSING SETTINGS =====
BATCH_SIZE = 5  # Reduced from 5 for better memory management
BATCH_DELAY = 1  # Delay between batches in seconds
CYCLE_DELAY = 10  # Seconds between full airport list cycles; loaded from config

# ===== FIRMWARE VERSION (for OTA update check) =====
# Device reports this string; GitHub Pages version.json "version" must be higher to offer OTA.
# After you flash new code, this should match what you published (or stay lower until user updates).
FIRMWARE_VERSION = "1.0.1"

# ===== OTA UPDATE BUTTON (GPIO for "install update"); set -1 to use app/browser only =====
UPDATE_BUTTON_PIN = -1

# ===== DATA TIMEOUT SETTINGS =====
NO_DATA_TIMEOUT = 180  #  without any data before showing warning
last_successful_data_time = None  # Track when we last got ANY airport data
no_data_warning_active = False  # Track if we're currently showing the warning
update_available = False  # Set True when OTA check finds newer version
update_info = None  # Parsed version.json when update_available

# ===== MATRIX WIRING PATTERN =====
# CHANGE THIS TO THE PATTERN THAT WORKED FOR YOU:
# "ROW_MAJOR" = Standard rows (0-31, 32-63, etc.)
# "COLUMN_MAJOR" = Standard columns (0-7, 8-15, etc.)
# "SNAKE_ROW" = Snake rows (even L->R, odd R->L)
# "SNAKE_COLUMN" = Snake columns (even T->B, odd B->T)
MATRIX_WIRING = "SNAKE_COLUMN"  # CHANGE THIS based on test results

# Defaults for brightness (overridden by config); must exist before config load
MIN_BRIGHTNESS = 2
MAX_BRIGHTNESS = 15
# Single flight category color map (saves RAM vs defining in multiple functions)
FLIGHT_COLOR_MAP = {"VFR": (0, 255, 0), "MVFR": (0, 0, 255), "IFR": (255, 0, 0), "LIFR": (255, 0, 128), "": (255, 255, 255)}
# Weather tags list - defined once, used in config and weather check
WX_TAGS = ["BR", "-RA", "RA", "+RA", "-SN", "SN", "+SN", "SHSN", "LTG", "DSNT", "WND", "FG", "FZFG", "FZFD", "CLR", "CC", "CA", "CG", "VCTS", "TS", "$", "FC", "+FC", "TORNADO"]

# Pixel indices built only when LED_MATRIX is used (saves RAM for OLED/NONE)
PIXEL_INDICES = None

def init_pixel_indices():
    """Pre-calculate all pixel indices for ultra-fast access. Call only when DISPLAY_TYPE is LED_MATRIX."""
    global PIXEL_INDICES
    PIXEL_INDICES = [[0 for _ in range(LED_MATRIX_HEIGHT)] for _ in range(LED_MATRIX_WIDTH)]
    for x in range(LED_MATRIX_WIDTH):
        for y in range(LED_MATRIX_HEIGHT):
            if MATRIX_WIRING == "ROW_MAJOR":
                PIXEL_INDICES[x][y] = (y * LED_MATRIX_WIDTH) + x
            elif MATRIX_WIRING == "COLUMN_MAJOR":
                PIXEL_INDICES[x][y] = (x * LED_MATRIX_HEIGHT) + y
            elif MATRIX_WIRING == "SNAKE_ROW":
                if y % 2 == 0:
                    PIXEL_INDICES[x][y] = (y * LED_MATRIX_WIDTH) + x
                else:
                    PIXEL_INDICES[x][y] = (y * LED_MATRIX_WIDTH) + (LED_MATRIX_WIDTH - 1 - x)
            elif MATRIX_WIRING == "SNAKE_COLUMN":
                if x % 2 == 0:
                    PIXEL_INDICES[x][y] = (x * LED_MATRIX_HEIGHT) + y
                else:
                    PIXEL_INDICES[x][y] = (x * LED_MATRIX_HEIGHT) + (LED_MATRIX_HEIGHT - 1 - y)
            else:
                PIXEL_INDICES[x][y] = (y * LED_MATRIX_WIDTH) + x

def get_pixel_index(x, y):
    """Ultra-fast pixel index lookup from pre-calculated table"""
    if PIXEL_INDICES is None or not (0 <= x < LED_MATRIX_WIDTH and 0 <= y < LED_MATRIX_HEIGHT):
        return 0
    return PIXEL_INDICES[x][y]

# ===== LDR AUTO-BRIGHTNESS FUNCTIONS =====
def read_ldr_value():
    """Read current LDR value"""
    try:
        adc = machine.ADC(0)
        ldr_value = adc.read_u16()
        return ldr_value
    except Exception as e:
        print(f"Error reading LDR: {e}")
        return 32768  # Return middle value as default

def map_ldr_to_brightness(ldr_value, min_brightness, max_brightness):
    """Map LDR value to brightness range - SIMPLE DIRECT MAPPING"""
    # LDR reads: LOW in bright, HIGH in dark
    # We want: LOW brightness in bright, HIGH brightness in dark
    # This is a DIRECT mapping: high LDR (dark) = high brightness
    brightness = int((ldr_value / 65535) * (max_brightness - min_brightness) + min_brightness)
    if brightness < min_brightness:
        brightness = min_brightness
    if brightness > max_brightness:
        brightness = max_brightness
    return brightness

def get_led_matrix_brightness():
    """Get brightness factor (0.0-1.0) for LED matrix - SAME LOGIC AS MAIN LEDs.
    Also updates the main strip's floating LDR so strip and matrix stay in sync."""
    global current_ldr_brightness
    ldr_value = read_ldr_value()
    brightness_value = map_ldr_to_brightness(ldr_value, MIN_BRIGHTNESS, MAX_BRIGHTNESS)
    current_ldr_brightness = brightness_value
    try:
        refresh_strip_using_ldr()
    except Exception:
        pass
    brightness_factor = brightness_value / 255.0
    return max(0.01, min(1.0, brightness_factor))

def apply_auto_brightness(color):
    """Apply auto-brightness to a color for LED matrix"""
    brightness_factor = get_led_matrix_brightness()
    if brightness_factor <= 0:
        return (0, 0, 0)
    if brightness_factor >= 1.0:
        return color
    r, g, b = color
    r = int(r * brightness_factor)
    g = int(g * brightness_factor)
    b = int(b * brightness_factor)
    return (r, g, b)

def apply_brightness(color, brightness):
    """Apply fixed brightness to a color (0-255 values) - kept for backward compatibility"""
    if brightness <= 0:
        return (0, 0, 0)
    if brightness >= 1.0:
        return color
    r, g, b = color
    r = int(r * brightness)
    g = int(g * brightness)
    b = int(b * brightness)
    return (r, g, b)

# Test auto-brightness (for debugging)
def test_auto_brightness():
    """Test function to show current auto-brightness level"""
    ldr_value = read_ldr_value()
    brightness_value = map_ldr_to_brightness(ldr_value, MIN_BRIGHTNESS, MAX_BRIGHTNESS)
    matrix_factor = get_led_matrix_brightness()
    print(f"LDR: {ldr_value} (higher = darker, lower = brighter)")
    print(f"Brightness value: {brightness_value}/255 (range {MIN_BRIGHTNESS}-{MAX_BRIGHTNESS})")
    print(f"LED Matrix brightness factor: {matrix_factor:.3f}")
    print(f"Environment: {'DARK' if ldr_value > 40000 else 'BRIGHT' if ldr_value < 20000 else 'MEDIUM'}")
    print(f"Both LEDs will be: {'BRIGHTER' if ldr_value > 40000 else 'DIMMER' if ldr_value < 20000 else 'MEDIUM'}")
    return matrix_factor

# ===== FORCE AP MODE BUTTON =====
def check_force_ap_button():
    """Check if the force AP mode button is pressed during startup. Polls for 5 seconds."""
    try:
        ap_button = Pin(FORCE_AP_BUTTON_PIN, Pin.IN, Pin.PULL_UP)
        print("\n=== Checking for force AP mode button press ===")
        print("Hold button for 3 seconds to force AP mode...")
        check_seconds = 5
        poll_interval = 0.1
        hold_threshold = 3.0
        elapsed = 0
        hold_time = 0
        while elapsed < check_seconds:
            if ap_button.value() == 0:
                hold_time += poll_interval
                if hold_time >= hold_threshold:
                    print("Button pressed detected!")
                    print(f"Button held for {hold_time:.1f} seconds - forcing AP mode!")
                    return True
            else:
                hold_time = 0
            time.sleep(poll_interval)
            elapsed += poll_interval
        print("No button press detected, continuing normal startup...")
        return False
    except Exception as e:
        print(f"Error checking force AP button: {e}")
        return False

# Check for force AP mode button press
force_ap_mode = check_force_ap_button()

def check_wifi_config():
    try:
        return CONFIG_FILE in os.listdir()
    except:
        return False

# If not configured OR force AP button was pressed, start WiFi manager
if not check_wifi_config() or force_ap_mode:
    print("Starting setup mode...")
    if force_ap_mode:
        print("(Forced by button press)")
    if 'led' in locals():
        for pulse in range(3):
            for led_index in range(NUM_LEDS):
                led[led_index] = (10 + pulse*5, 0, 0)
            led.write()
            time.sleep(0.3)
    import wifi_manager
    wifi_manager.start()

# Initialize system (minimal startup prints to save RAM)
print("\n===== MetarMap Starting =====")
gc.collect()
print(f"Free memory at start: {gc.mem_free()} bytes")

# Set up GPIO pins
try:
    OLED_pin = machine.Pin(18, machine.Pin.OUT)
    OLED_pin.value(1)
    time.sleep(0.1)
    LDR_output_pin = machine.Pin(21, machine.Pin.OUT)
    LDR_output_pin.value(1)
    time.sleep(0.5)
except Exception as e:
    print(f"Error initializing pins: {e}")

# WS2811 LED configuration
LED_PIN = 0
NUM_LEDS = 200

try:
    led = neopixel.NeoPixel(machine.Pin(LED_PIN), NUM_LEDS)
    print(f"NeoPixel initialized with {NUM_LEDS} LEDs")
    brightness_factor = STARTUP_BRIGHTNESS
    color_value = int(10 * brightness_factor)
    for i in range(NUM_LEDS):
        led[i] = (color_value, color_value, color_value)
    led.write()
except Exception as e:
    print(f"Error initializing NeoPixels: {e}")
    machine.reset()

# Floating LDR: one brightness value for whole strip, refreshed periodically
logical_colors = [(0, 0, 0)] * NUM_LEDS
current_ldr_brightness = 128
last_ldr_refresh_time = 0

# ===== LOAD CONFIGURATION FROM FILE =====
try:
    with open(CONFIG_FILE, 'r') as f:
        config = json.load(f)
        WIFI_SSID = str(config.get('ssid', '') or '').strip()
        WIFI_PASSWORD = str(config.get('password', '') or '')
        DISPLAY_TYPE = config.get('display_type', 'LED_MATRIX')
        LED_MATRIX_BRIGHTNESS = config.get('led_matrix_brightness', 0.01)
        LED_MATRIX_PIN = config.get('led_matrix_pin', 1)
        BATCH_SIZE = max(1, min(20, config.get('batch_size', 3)))
        MIN_BRIGHTNESS = max(0, min(255, config.get('min_brightness', 2)))
        MAX_BRIGHTNESS = max(0, min(255, config.get('max_brightness', 15)))
        _mo = config.get('matrix_only', False)
        MATRIX_ONLY = _mo.lower() in ('true', '1', 'yes') if isinstance(_mo, str) else bool(_mo)
        try:
            SCROLL_SPEED = max(0.03, min(0.2, float(config.get('scroll_speed', 0.08))))
        except (TypeError, ValueError):
            pass  # keep default 0.08
        _mw = str(config.get('matrix_wiring', 'SNAKE_COLUMN')).upper()
        if _mw in ('ROW_MAJOR', 'COLUMN_MAJOR', 'SNAKE_ROW', 'SNAKE_COLUMN'):
            MATRIX_WIRING = _mw
        try:
            SCROLL_PAUSE_BEFORE = max(0, min(2, float(config.get('scroll_pause_before', 0.75))))
        except (TypeError, ValueError):
            pass
        try:
            CYCLE_DELAY = max(5, min(1800, int(config.get('cycle_delay', 10))))
        except (TypeError, ValueError):
            pass
        weather_enabled_raw = config.get('weather_enabled', {})
        if isinstance(weather_enabled_raw, dict):
            WEATHER_ENABLED = {str(k): bool(v) for k, v in weather_enabled_raw.items()}
            for code in WX_TAGS:
                if code not in WEATHER_ENABLED:
                    WEATHER_ENABLED[code] = True
        else:
            WEATHER_ENABLED = {code: True for code in WX_TAGS}
        print(f"Loaded WiFi configuration for: {WIFI_SSID}")
        print(f"Display Type: {DISPLAY_TYPE}")
        print(f"LED Matrix Brightness: {LED_MATRIX_BRIGHTNESS}")
        print(f"Matrix only (no strip weather): {MATRIX_ONLY}")
        off_codes = [c for c, v in WEATHER_ENABLED.items() if not v]
        if off_codes:
            print(f"Weather effects OFF for: {off_codes}")
        # Display sleep schedule (turn off matrix/LEDs/OLED at night)
        SLEEP_ENABLED = bool(config.get("sleep_enabled", False))
        SLEEP_AT_HOUR = max(0, min(23, int(config.get("sleep_at_hour", 22))))
        SLEEP_AT_MIN = max(0, min(59, int(config.get("sleep_at_minute", 0))))
        WAKE_AT_HOUR = max(0, min(23, int(config.get("wake_at_hour", 6))))
        WAKE_AT_MIN = max(0, min(59, int(config.get("wake_at_minute", 0))))
        SLEEP_MATRIX = bool(config.get("sleep_matrix", True))
        SLEEP_LEDS = bool(config.get("sleep_leds", True))
        SLEEP_OLED = bool(config.get("sleep_oled", True))
        try:
            TIMEZONE_OFFSET_HOURS = max(-12, min(14, int(config.get("timezone_offset_hours", -5))))
        except (TypeError, ValueError):
            TIMEZONE_OFFSET_HOURS = -5
        del config
        gc.collect()
except Exception as e:
    print(f"Error loading configuration: {e}")
    WEATHER_ENABLED = {code: True for code in WX_TAGS}
    SLEEP_ENABLED = False
    SLEEP_AT_HOUR = 22
    SLEEP_AT_MIN = 0
    WAKE_AT_HOUR = 6
    WAKE_AT_MIN = 0
    SLEEP_MATRIX = SLEEP_LEDS = SLEEP_OLED = True
    TIMEZONE_OFFSET_HOURS = -5
    r_scaled = int(20 * STARTUP_BRIGHTNESS)
    for i in range(NUM_LEDS):
        led[i] = (r_scaled, 0, 0)
    led.write()
    time.sleep(2)
    import wifi_manager
    wifi_manager.start()

# NOW initialize displays based on configuration from file
oled = None
led_matrix = None
print(f"\n=== Initializing {DISPLAY_TYPE} display ===")

if DISPLAY_TYPE == "OLED":
    try:
        i2c = I2C(0, sda=Pin(16), scl=Pin(17))
        oled = ssd1306.SSD1306_I2C(128, 64, i2c)
        print("OLED display initialized successfully")
        oled.fill(0)
        oled.show()
        oled.fill(0)
        if fonts_available:
            try:
                writ = writer.Writer(oled, sans18)
                writ.set_textpos(0, 0)
                writ.printstring("OLED Mode")
                writ.set_textpos(18, 20)
                writ.printstring("ACTIVE")
            except Exception as e:
                print(f"Error displaying sans18 font in test: {e}")
                oled.text("OLED Mode", 0, 5, 1)
                oled.text("Active", 0, 17, 1)
                oled.text(f"WiFi: {WIFI_SSID[:12]}", 0, 30, 1)
        else:
            oled.text("OLED Mode", 0, 5, 1)
            oled.text("Active", 0, 17, 1)
            oled.text(f"WiFi: {WIFI_SSID[:12]}", 0, 30, 1)
        oled.show()
        time.sleep(3)
        oled.fill(0)
        oled.show()
    except Exception as e:
        print(f"Error initializing OLED display: {e}")
        DISPLAY_TYPE = "NONE"
elif DISPLAY_TYPE == "LED_MATRIX":
    try:
        init_pixel_indices()
        try:
            from font_4x6_data import font_4x6
        except ImportError:
            font_4x6 = {}
        led_matrix = neopixel.NeoPixel(machine.Pin(LED_MATRIX_PIN), LED_MATRIX_NUM_LEDS)
        print(f"LED Matrix initialized with {LED_MATRIX_NUM_LEDS} LEDs")
        led_matrix.fill((0, 0, 0))
        led_matrix.write()
        print("Testing matrix with colored corners...")
        corners = [
            (0, 0, (20, 0, 0)),
            (LED_MATRIX_WIDTH-1, 0, (0, 20, 0)),
            (0, LED_MATRIX_HEIGHT-1, (0, 0, 20)),
            (LED_MATRIX_WIDTH-1, LED_MATRIX_HEIGHT-1, (20, 20, 0))
        ]
        for x, y, color in corners:
            pixel_index = get_pixel_index(x, y)
            if pixel_index < LED_MATRIX_NUM_LEDS:
                led_matrix[pixel_index] = color
        led_matrix.write()
        time.sleep(1)
        led_matrix.fill((0, 0, 0))
        led_matrix.write()
        print(f"Matrix wiring pattern: {MATRIX_WIRING}")
        print("LED Matrix initialized successfully")
    except Exception as e:
        print(f"Error initializing LED Matrix: {e}")
        DISPLAY_TYPE = "NONE"
elif DISPLAY_TYPE == "NONE":
    print("No display selected - running in LED strip only mode")

def update_data_success():
    global last_successful_data_time, no_data_warning_active
    last_successful_data_time = time.time()
    if no_data_warning_active:
        no_data_warning_active = False
        print("Data restored - clearing NO DATA warning")

def check_data_timeout():
    global last_successful_data_time, no_data_warning_active
    if last_successful_data_time is None:
        last_successful_data_time = time.time()
        return
    time_since_last_data = time.time() - last_successful_data_time
    if time_since_last_data > NO_DATA_TIMEOUT and not no_data_warning_active:
        print(f"=== NO DATA TIMEOUT ===")
        print(f"No airport data received for {time_since_last_data:.1f} seconds")
        print(f"Timeout: {NO_DATA_TIMEOUT} seconds")
        if DISPLAY_TYPE == "LED_MATRIX":
            print("Displaying NO DATA warning on LED matrix...")
            display_no_data_warning()
        else:
            print("NO DATA warning (no display available for warning)")
        no_data_warning_active = True
    elif time_since_last_data <= NO_DATA_TIMEOUT and no_data_warning_active:
        no_data_warning_active = False
        print("Data connection restored")

def display_no_data_warning():
    if led_matrix is None or DISPLAY_TYPE != "LED_MATRIX":
        print("Cannot display NO DATA warning - LED matrix not available")
        return
    try:
        led_matrix.fill((0, 0, 0))
        led_matrix.write()
        warning_color = apply_auto_brightness((255, 140, 0))
        warning_text = f"NO DATA AFTER {NO_DATA_TIMEOUT} SEC REBOOT METARMAP IF CONTINUES"
        print(f"Displaying: {warning_text}")
        scroll_single_text_ultra_smooth(warning_text, warning_color)
        time.sleep(2)
    except Exception as e:
        print(f"Error displaying NO DATA warning: {e}")

def read_airports(file_path):
    airports = []
    try:
        if file_path not in os.listdir():
            print(f"ERROR: File {file_path} not found")
            raise OSError(f"File {file_path} not found")
        with open(file_path, 'r') as file:
            for line in file:
                airports.append(line.strip())
        active_count = sum(1 for a in airports if a)
        print(f"Loaded {len(airports)} airports ({active_count} active)")
        gc.collect()
    except OSError as e:
        print(f"Error reading airport file: {e}")
        airports = []
        print(f"Using {len(airports)} default airports")
    return airports

def display_on_oled(line1, line2, scroll_speed=0.1):
    if DISPLAY_TYPE != "OLED" or not fonts_available or oled is None:
        return
    try:
        oled.fill(0)
        writ = writer.Writer(oled, sans18)
        wri = writer.Writer(oled, sans18)
        writ.set_textpos(11, 0)
        writ.printstring(line1)
        if len(line2) > 6:
            scroll_text = line2 + "  " * 6
            for i in range(len(scroll_text) - 11):
                oled.fill_rect(0, 14, 128, 32, 0)
                wri.set_textpos(128, 10)
                wri.printstring(scroll_text[i:i+11])
                oled.show()
                time.sleep(scroll_speed)
        else:
            wri.set_textpos(32, 0)
            wri.printstring(line2)
        oled.show()
    except Exception as e:
        print(f"Error displaying on OLED: {e}")

# font_4x6 set in LED_MATRIX init block; keep empty when not using matrix to save RAM
if DISPLAY_TYPE != "LED_MATRIX":
    font_4x6 = {}

def display_airport_on_matrix(airport, flight_category, metar_text):
    if led_matrix is None or DISPLAY_TYPE != "LED_MATRIX":
        return
    try:
        print(f"\n=== LED MATRIX DISPLAY: {airport} ===")
        flight_category = flight_category.strip().upper()
        base_text_color = FLIGHT_COLOR_MAP.get(flight_category, (255, 255, 255))
        text_color = apply_auto_brightness(base_text_color)
        header = f"{airport}={flight_category}"
        scroll_header_with_metar(header, flight_category, metar_text)
    except Exception as e:
        print(f"Error displaying airport on matrix: {e}")
        import sys
        sys.print_exception(e)

def scroll_header_with_metar(header, flight_category, metar_text):
    if led_matrix is None or DISPLAY_TYPE != "LED_MATRIX":
        return
    try:
        base_text_color = FLIGHT_COLOR_MAP.get(flight_category, (255, 255, 255))
        text_color = apply_auto_brightness(base_text_color)
        scroll_single_text_ultra_smooth(header, text_color)
        time.sleep(1)
        if metar_text and len(metar_text.strip()) > 0:
            scroll_single_text_ultra_smooth(metar_text.strip(), text_color)
        else:
            scroll_single_text_ultra_smooth("NO METAR DATA", text_color)
    except Exception as e:
        print(f"Error in scroll_header_with_metar: {e}")

def scroll_single_text_ultra_smooth(text, text_color):
    if led_matrix is None or DISPLAY_TYPE != "LED_MATRIX" or PIXEL_INDICES is None:
        return
    try:
        text = text.upper()
        columns = []
        default_char_width = 4
        spacing = 1
        vertical_offset = 1
        for char in text:
            if font_4x6 and char in font_4x6:
                char_bitmap = font_4x6[char]
                current_char_width = len(char_bitmap[0]) if char_bitmap and char_bitmap[0] else default_char_width
                for col in range(current_char_width):
                    column_data = 0
                    for row in range(6):
                        if row < len(char_bitmap) and col < len(char_bitmap[row]) and char_bitmap[row][col]:
                            matrix_row = row + vertical_offset
                            if matrix_row < LED_MATRIX_HEIGHT:
                                column_data |= (1 << matrix_row)
                    columns.append(column_data)
                columns.append(0)
            else:
                for _ in range(default_char_width + spacing):
                    columns.append(0)
        for _ in range(int(LED_MATRIX_WIDTH * 1.5)):
            columns.append(0)
        total_frames = max(0, len(columns) - LED_MATRIX_WIDTH)
        if SCROLL_PAUSE_BEFORE > 0 and total_frames > 0:
            led_matrix.fill((0, 0, 0))
            for x in range(LED_MATRIX_WIDTH):
                if x >= len(columns):
                    continue
                col_data = columns[x]
                if col_data == 0:
                    continue
                for y in range(LED_MATRIX_HEIGHT):
                    if col_data & (1 << y):
                        pixel_index = PIXEL_INDICES[x][y]
                        led_matrix[pixel_index] = text_color
            led_matrix.write()
            time.sleep(SCROLL_PAUSE_BEFORE)
        frame_target_ms = int(SCROLL_SPEED * 1000)
        for start_col in range(total_frames):
            frame_start = time.ticks_ms()
            led_matrix.fill((0, 0, 0))
            max_x = min(LED_MATRIX_WIDTH, len(columns) - start_col)
            for x in range(max_x):
                col_data = columns[start_col + x]
                if col_data == 0:
                    continue
                for y in range(LED_MATRIX_HEIGHT):
                    if col_data & (1 << y):
                        led_matrix[PIXEL_INDICES[x][y]] = text_color
            led_matrix.write()
            frame_end = time.ticks_ms()
            draw_time = frame_end - frame_start
            if draw_time < frame_target_ms:
                remaining_ms = frame_target_ms - draw_time
                if remaining_ms > 0:
                    time.sleep_ms(remaining_ms)
        del columns
        gc.collect()
    except Exception as e:
        print(f"Error in scroll_single_text_ultra_smooth: {e}")

def display_info(line1, line2, flight_category="", airport=""):
    if DISPLAY_TYPE == "OLED":
        display_on_oled(line1, line2)
    elif DISPLAY_TYPE == "LED_MATRIX" and airport:
        display_airport_on_matrix(airport, flight_category, line2)

def connect_to_wifi(WIFI_SSID, WIFI_PASSWORD):
    for i in range(NUM_LEDS):
        led[i] = (4, 4, 4)
    led.write()
    # Ensure AP is off so STA can connect (e.g. after previous AP mode + reboot)
    try:
        ap = network.WLAN(network.AP_IF)
        if ap.active():
            ap.active(False)
            time.sleep(0.5)
    except Exception:
        pass
    wlan = network.WLAN(network.STA_IF)
    wlan.active(True)
    time.sleep(1)  # let radio stabilize before connect
    try:
        print(f"Connecting to WiFi: {WIFI_SSID} (password length: {len(WIFI_PASSWORD) if WIFI_PASSWORD else 0})")
        if not WIFI_SSID or (WIFI_PASSWORD is None or (isinstance(WIFI_PASSWORD, str) and not WIFI_PASSWORD)):
            print("Missing SSID or password in config")
            return False
        wlan.connect(WIFI_SSID, WIFI_PASSWORD)
        retries = 0
        max_retries = 20  # allow up to 20s for slow DHCP
        while not wlan.isconnected() and retries < max_retries:
            retries += 1
            print(f"Connection attempt {retries}/{max_retries}...")
            time.sleep(1)
        if wlan.isconnected():
            ip_address = wlan.ifconfig()[0]
            print("Connected to WiFi:", wlan.config("essid"))
            print("IP Address:", ip_address)
            conn_green = (0, 255, 0)
            for i in range(NUM_LEDS):
                logical_colors[i] = conn_green
                led[i] = _scale_color(conn_green, current_ldr_brightness)
            led.write()
            if DISPLAY_TYPE == "OLED" and oled is not None and fonts_available:
                try:
                    oled.fill(0)
                    wri_title = writer.Writer(oled, sans18)
                    wri_ip = writer.Writer(oled, sans18)
                    wri_title.set_textpos(0, 0)
                    wri_title.printstring("IP Address:")
                    wri_ip.set_textpos(0, 20)
                    wri_ip.printstring(ip_address)
                    wri_ip.set_textpos(0, 40)
                    wri_ip.printstring(WIFI_SSID)
                    oled.show()
                    time.sleep(3)
                    oled.fill(0)
                    oled.show()
                except Exception as e:
                    print(f"Error displaying IP on OLED: {e}")
            elif DISPLAY_TYPE == "LED_MATRIX" and led_matrix is not None:
                ip_text_color = apply_auto_brightness((255, 255, 255))
                scroll_single_text_ultra_smooth(f"IP: {ip_address}", ip_text_color)
                time.sleep(2)
            time.sleep(3)
            # Verify we have a route to the internet (avoids EHOSTUNREACH when fetching METAR)
            try:
                ai = socket.getaddrinfo("aviationweather.gov", 443)[0][-1]
                s = socket.socket()
                s.settimeout(8)
                s.connect(ai)
                s.close()
                print("Internet reachable (aviationweather.gov)")
            except Exception as e:
                print("WARNING: No route to internet:", e)
                print("  Check: router gateway/DNS, 2.4GHz network, no client isolation.")
            for i in range(NUM_LEDS):
                logical_colors[i] = (0, 0, 0)
                led[i] = (0, 0, 0)
            led.write()
            return True
        else:
            print("Unable to connect to Wi-Fi")
            for i in range(NUM_LEDS):
                logical_colors[i] = (7, 0, 0)
                led[i] = (7, 0, 0)
            led.write()
            time.sleep(2)
            return False
    except Exception as e:
        print("Error connecting to Wi-Fi:", e)
        return False

# NTP servers to try in order (aviation-friendly, reliable; fallback if one fails)
NTP_SERVERS = ("time.google.com", "pool.ntp.org", "time.nist.gov")

def _try_ntp_sync():
    """Try to set RTC from NTP using NTP_SERVERS. Returns True if any server succeeds."""
    import ntptime
    for host in NTP_SERVERS:
        try:
            ntptime.host = host
            ntptime.settime()
            print("NTP time synced from", host)
            return True
        except Exception as e:
            print("NTP failed", host, ":", e)
    return False

def sync_ntp_once():
    """Sync RTC from NTP once so time.time() is correct before any timestamps (avoids false 180s timeout)."""
    if _try_ntp_sync():
        return True
    print("NTP sync at startup failed (all servers)")
    return False

def local_time():
    """Return local time tuple (year, month, day, hour, min, sec, ...) using TIMEZONE_OFFSET_HOURS (UTC + offset)."""
    return time.localtime(time.time() + TIMEZONE_OFFSET_HOURS * 3600)

def ensure_wifi_connected():
    """If STA is disconnected, try to reconnect. Call periodically from main loop."""
    wlan = network.WLAN(network.STA_IF)
    if wlan.isconnected():
        return True
    print("WiFi disconnected - attempting reconnect...")
    try:
        if not WIFI_SSID or (WIFI_PASSWORD is None or (isinstance(WIFI_PASSWORD, str) and not WIFI_PASSWORD)):
            return False
        wlan.connect(WIFI_SSID, WIFI_PASSWORD)
        for _ in range(20):
            time.sleep(1)
            if wlan.isconnected():
                print("WiFi reconnected:", wlan.ifconfig()[0])
                return True
        print("WiFi reconnect failed (timeout)")
        return False
    except Exception as e:
        print("WiFi reconnect error:", e)
        return False

MAX_RETRIES = 3

# mbedTLS on Pico W often gets this through hotspots with cellular backhaul; treat as transient
SSL_EOF_MAX_EXTRA_TRIES = 5   # extra connection attempts per "retry" when we see SSL EOF
SSL_EOF_RETRY_DELAY = 4       # seconds between those attempts (carrier often needs a moment)

def _is_ssl_eof(e):
    if e is None:
        return False
    errno = getattr(e, "errno", None)
    if errno is None and getattr(e, "args", None) and e.args:
        errno = e.args[0]
    return errno == -29312 or "MBEDTLS_ERR_SSL" in str(e)

def _metar_obs_time(raw_line):
    """Parse observation time from METAR (DDHHMMZ). Returns (day, minutes_since_midnight) or (0, 0) if unparseable."""
    if not raw_line or not isinstance(raw_line, str):
        return (0, 0)
    parts = raw_line.strip().upper().split()
    for i, tok in enumerate(parts):
        if len(tok) >= 7 and tok.endswith("Z") and tok[:2].isdigit() and tok[2:6].isdigit():
            try:
                day = int(tok[:2])
                hour = int(tok[2:4])
                mins = int(tok[4:6])
                return (day, hour * 60 + mins)
            except ValueError:
                pass
    return (0, 0)

def _parse_flight_category_from_raw(raw_text):
    """Derive VFR/MVFR/IFR/LIFR from raw METAR. Returns '' on parse failure. Uses less memory than XML."""
    if not raw_text or not isinstance(raw_text, str):
        return ""
    raw = raw_text.strip().upper()
    vis_m = 10.0   # default VFR
    ceiling_ft = 10000  # default high
    # Parse visibility: token ending with SM (e.g. 10SM, 3SM, 1/2SM, M1/4SM, P6SM, 1 1/2SM)
    i = 0
    while i < len(raw):
        j = raw.find("SM", i)
        if j < 0:
            break
        start = j
        while start > 0 and (raw[start - 1].isdigit() or raw[start - 1] in "/.M "):
            start -= 1
        tok = raw[start:j].strip()
        if tok.startswith("P"):
            tok = tok[1:]
        if tok.startswith("M"):
            tok = tok[1:]
        try:
            if "/" in tok:
                # "1/2" or "1 1/2" (mixed number)
                if " " in tok:
                    parts = tok.split(None, 1)
                    whole = int(parts[0]) if parts[0].isdigit() else 0
                    frac = parts[1] if len(parts) > 1 else "0/1"
                    a, b = frac.split("/", 1)
                    vis_m = whole + int(a.strip()) / max(1, int(b.strip()))
                else:
                    a, b = tok.split("/", 1)
                    vis_m = int(a.strip()) / max(1, int(b.strip()))
            else:
                vis_m = float(tok)
        except (ValueError, ZeroDivisionError):
            pass
        i = j + 2
        break  # use first visibility only
    # Parse ceiling: BKNnnn, OVCnnn (nnn = hundreds of ft), or VVnnn (vertical visibility = indefinite ceiling)
    for prefix in ("BKN", "OVC", "VV"):
        plen = len(prefix)
        idx = 0
        while True:
            idx = raw.find(prefix, idx)
            if idx < 0:
                break
            idx += plen
            while idx < len(raw) and not raw[idx].isdigit():
                idx += 1
            if idx + 3 <= len(raw) and raw[idx:idx + 3].isdigit():
                h = int(raw[idx:idx + 3]) * 100
                if h < ceiling_ft:
                    ceiling_ft = h
            idx += 1
    if ceiling_ft > 5000:
        ceiling_ft = 5000  # cap for "no ceiling" case
    # Apply flight category (worst of vis and ceiling)
    if ceiling_ft < 500 or vis_m < 1.0:
        return "LIFR"
    if ceiling_ft < 1000 or vis_m < 3.0:
        return "IFR"
    if ceiling_ft < 3000 or vis_m < 5.0:
        return "MVFR"
    return "VFR"

def get_metar_data_with_retry(airport):
    """Returns (flight_category, raw_text) or (None, None). Tries raw format first (smaller response, less memory)."""
    retries = 0
    while retries < MAX_RETRIES:
        last_error = None
        ssl_retries = 0
        while True:  # inner loop: extra quick retries on SSL EOF (common on mobile hotspots)
            try:
                gc.collect()
                url_raw = "https://aviationweather.gov/api/data/metar?ids={}&hours=1&format=raw".format(airport)
                response = urequests.get(url_raw, timeout=10)
                data = response.text
                response.close()
                gc.collect()
                raw_text = None
                for line in data.split("\n"):
                    line = line.strip()
                    if line and (line.startswith("METAR ") or line.startswith("SPECI ")):
                        raw_text = line
                        break
                if raw_text:
                    flight_category = _parse_flight_category_from_raw(raw_text)
                    del data
                    gc.collect()
                    return flight_category, raw_text
                del data
                gc.collect()
            except OSError as e:
                last_error = e
                if getattr(e, "errno", None) == 113:
                    print(f"Raw fetch failed for {airport}: No route to host (check WiFi internet/gateway/DNS)")
                else:
                    print(f"Raw fetch failed for {airport}: {e}")
            except Exception as e:
                last_error = e
                print(f"Raw fetch failed for {airport}: {e}")
            # Fallback to XML if raw failed or no line found
            try:
                gc.collect()
                url_xml = "https://aviationweather.gov/api/data/metar?ids={}&hours=1&format=xml".format(airport)
                response = urequests.get(url_xml, timeout=10)
                data = response.text
                response.close()
                gc.collect()
                rt_start = data.find("<raw_text>") + 10
                rt_end = data.find("</raw_text>", rt_start)
                fc_start = data.find("<flight_category>") + 17
                fc_end = data.find("</flight_category>", fc_start)
                if rt_start >= 10 and rt_end != -1 and fc_start >= 17 and fc_end != -1:
                    raw_text = data[rt_start:rt_end]
                    flight_category = data[fc_start:fc_end]
                    del data
                    gc.collect()
                    return flight_category, raw_text
                del data
                gc.collect()
            except OSError as e:
                last_error = e
                if getattr(e, "errno", None) == 113:
                    print(f"Error retrieving data for {airport}: No route to host (WiFi connected but no internet)")
                else:
                    print(f"Error retrieving data for {airport}: {e}")
            except Exception as e:
                last_error = e
                print(f"Error retrieving data for {airport}: {e}")
            # Both raw and XML failed
            if last_error and _is_ssl_eof(last_error) and ssl_retries < SSL_EOF_MAX_EXTRA_TRIES:
                ssl_retries += 1
                print("SSL closed (hotspot/cellular), retry {} in {}s...".format(ssl_retries, SSL_EOF_RETRY_DELAY))
                time.sleep(SSL_EOF_RETRY_DELAY)
                gc.collect()
                continue
            break
        retries += 1
        gc.collect()
        time.sleep(2 * retries)
    print(f"Unable to retrieve data for {airport} after {MAX_RETRIES} retries")
    return None, None

BULK_CHUNK_SIZE = 20  # airports per request; smaller = more reliable full response

def fetch_all_metars_once(airports):
    """Fetch METARs for all airports in chunked requests. Returns list of (flight_category, raw_text) per index, or None on failure.
    Uses order=ids so response order matches our list."""
    n = min(len(airports), NUM_LEDS)
    if n == 0:
        return []
    results = [(None, None)] * n
    chunk_start = 0
    total_got = 0
    total_requested = 0
    while chunk_start < n:
        chunk_end = min(chunk_start + BULK_CHUNK_SIZE, n)
        chunk_airports = [airports[i].strip() for i in range(chunk_start, chunk_end) if airports[i] and airports[i].strip()]
        total_requested += len(chunk_airports)
        if not chunk_airports:
            chunk_start = chunk_end
            continue
        ids = ",".join(chunk_airports)
        chunk_ok = False
        for ssl_attempt in range(SSL_EOF_MAX_EXTRA_TRIES + 1):  # extra tries on SSL EOF (hotspot/cellular)
            try:
                gc.collect()
                url = "https://aviationweather.gov/api/data/metar?ids={}&hours=1&format=raw&order=ids".format(ids)
                response = urequests.get(url, timeout=12)
                data = response.text
                response.close()
                gc.collect()
                for line in data.split("\n"):
                    line = line.strip()
                    if not line or not (line.startswith("METAR ") or line.startswith("SPECI ")):
                        continue
                    parts = line.split()
                    station = parts[1].upper() if len(parts) > 1 else None
                    if not station:
                        continue
                    fc = _parse_flight_category_from_raw(line)
                    new_time = _metar_obs_time(line)
                    for idx in range(chunk_start, min(chunk_end, n)):
                        if airports[idx] and airports[idx].strip().upper() == station:
                            existing_fc, existing_raw = results[idx]
                            if existing_raw is None:
                                results[idx] = (fc if fc else None, line)
                                total_got += 1
                                update_data_success()
                            else:
                                old_time = _metar_obs_time(existing_raw)
                                if new_time > old_time:
                                    results[idx] = (fc if fc else None, line)
                del data
                gc.collect()
                chunk_ok = True
                break
            except Exception as e:
                print("Bulk METAR chunk failed ({}–{}): {}".format(chunk_start, chunk_end, e))
                gc.collect()
                if _is_ssl_eof(e) and ssl_attempt < SSL_EOF_MAX_EXTRA_TRIES:
                    print("SSL closed (hotspot/cellular), chunk retry {} in {}s...".format(ssl_attempt + 1, SSL_EOF_RETRY_DELAY))
                    time.sleep(SSL_EOF_RETRY_DELAY)
                else:
                    return None
        if not chunk_ok:
            return None
        chunk_start = chunk_end
        if chunk_start < n:
            time.sleep(0.5)
    missing = total_requested - total_got
    if missing > 0:
        print("Bulk METAR fetch: got {} of {} requested ({} slots). {} missing from API for this chunk; will fetch individually.".format(total_got, total_requested, n, missing))
    else:
        print("Bulk METAR fetch: got {} of {} airports".format(total_got, n))
    return results

def get_weather_conditions_with_retry(raw_text, airport, led, index, min_brightness, max_brightness, weather_enabled=None):
    if weather_enabled is None:
        weather_enabled = WEATHER_ENABLED
    unwanted_conditions = []
    retries = 0
    while retries < MAX_RETRIES:
        try:
            if raw_text:
                if raw_text == "UNKNOWN":
                    print("Retrying for {} (UNKNOWN category)".format(airport))
                    time.sleep(1)
                else:
                    print("{} Raw METAR Text: {}".format(airport, raw_text))
                    unwanted_present = any(unwanted_condition in raw_text for unwanted_condition in unwanted_conditions)
                    if unwanted_present:
                        print("Excluding {} at {}".format(unwanted_conditions, airport))
                        return False
                    conditions_present = [wx_tag in raw_text.split() for wx_tag in WX_TAGS]
                    time.sleep(5)
                    if any(conditions_present):
                        print("Weather conditions observed at {}: {}".format(airport, conditions_present))
                        had_effective = (
                            (conditions_present[0] and weather_enabled.get("BR", True)) or
                            (conditions_present[1] and weather_enabled.get("-RA", True)) or
                            (conditions_present[2] and weather_enabled.get("RA", True)) or
                            (conditions_present[3] and weather_enabled.get("+RA", True)) or
                            (conditions_present[4] and weather_enabled.get("-SN", True)) or
                            (conditions_present[5] and weather_enabled.get("SN", True)) or
                            (conditions_present[6] and weather_enabled.get("+SN", True)) or
                            (conditions_present[8] and weather_enabled.get("LTG", True)) or (conditions_present[9] and weather_enabled.get("DSNT", True)) or
                            (conditions_present[10] and weather_enabled.get("WND", True)) or
                            (conditions_present[11] and weather_enabled.get("FG", True)) or
                            (conditions_present[12] and weather_enabled.get("FZFG", True)) or
                            (conditions_present[13] and weather_enabled.get("FZFD", True)) or
                            (conditions_present[14] and weather_enabled.get("CLR", True)) or
                            (conditions_present[15] and weather_enabled.get("CC", True)) or (conditions_present[16] and weather_enabled.get("CA", True)) or
                            (conditions_present[17] and weather_enabled.get("CG", True)) or (conditions_present[18] and weather_enabled.get("VCTS", True)) or
                            (conditions_present[19] and weather_enabled.get("TS", True)) or (conditions_present[20] and weather_enabled.get("$", True)) or (conditions_present[21] and weather_enabled.get("FC", True)) or
                            (conditions_present[21] and weather_enabled.get("FC", True)) or (conditions_present[22] and weather_enabled.get("+FC", True)) or (conditions_present[23] and weather_enabled.get("TORNADO", True))
                        )
                        if weather_enabled.get("BR", True) and any(conditions_present[0:1]):
                            for flash_count in range(12):
                                logical_colors[index] = (0, 255, 240)
                                led[index] = _scale_color((0, 255, 240), current_ldr_brightness)
                                led.write()
                                time.sleep(0.1)
                                logical_colors[index] = (0, 0, 0)
                                led[index] = (0, 0, 0)
                                led.write()
                                time.sleep(0.1)
                        if weather_enabled.get("-RA", True) and any(conditions_present[1:2]):
                            for flash_count in range(6):
                                logical_colors[index] = (0, 255, 139)
                                led[index] = _scale_color((0, 255, 139), current_ldr_brightness)
                                led.write()
                                time.sleep(.5)
                                logical_colors[index] = (0, 0, 0)
                                led[index] = (0, 0, 0)
                                led.write()
                                time.sleep(.5)
                        if weather_enabled.get("RA", True) and any(conditions_present[2:3]):
                            for flash_count in range(5):
                                logical_colors[index] = (0, 255, 139)
                                led[index] = _scale_color((0, 255, 139), current_ldr_brightness)
                                led.write()
                                time.sleep(1.2)
                                logical_colors[index] = (0, 0, 0)
                                led[index] = (0, 0, 0)
                                led.write()
                                time.sleep(.5)
                        if weather_enabled.get("+RA", True) and any(conditions_present[3:4]):
                            for flash_count in range(4):
                                logical_colors[index] = (0, 255, 139)
                                led[index] = _scale_color((0, 255, 139), current_ldr_brightness)
                                led.write()
                                time.sleep(2.2)
                                logical_colors[index] = (0, 0, 0)
                                led[index] = (0, 0, 0)
                                led.write()
                                time.sleep(.5)
                        if weather_enabled.get("-SN", True) and any(conditions_present[4:5]):
                            for flash_count in range(6):
                                logical_colors[index] = (255, 255, 255)
                                led[index] = _scale_color((255, 255, 255), current_ldr_brightness)
                                led.write()
                                time.sleep(.3)
                                logical_colors[index] = (0, 0, 0)
                                led[index] = (0, 0, 0)
                                led.write()
                                time.sleep(.5)
                        if weather_enabled.get("SN", True) and any(conditions_present[5:6]):
                            for flash_count in range(5):
                                logical_colors[index] = (255, 255, 255)
                                led[index] = _scale_color((255, 255, 255), current_ldr_brightness)
                                led.write()
                                time.sleep(1.2)
                                logical_colors[index] = (0, 0, 0)
                                led[index] = (0, 0, 0)
                                led.write()
                                time.sleep(.5)
                        if weather_enabled.get("+SN", True) and any(conditions_present[6:7]):
                            for flash_count in range(4):
                                logical_colors[index] = (255, 255, 255)
                                led[index] = _scale_color((255, 255, 255), current_ldr_brightness)
                                led.write()
                                time.sleep(2.2)
                                logical_colors[index] = (0, 0, 0)
                                led[index] = (0, 0, 0)
                                led.write()
                                time.sleep(.5)
                        if (weather_enabled.get("LTG", True) and conditions_present[8]) or (weather_enabled.get("DSNT", True) and conditions_present[9]):
                            for flash_count in range(35):
                                logical_colors[index] = (255, 255, 0)
                                led[index] = _scale_color((255, 255, 0), current_ldr_brightness)
                                led.write()
                                time.sleep(.09)
                                logical_colors[index] = (0, 0, 0)
                                led[index] = (0, 0, 0)
                                led.write()
                                time.sleep(.02)
                        if weather_enabled.get("WND", True) and any(conditions_present[10:11]):
                            for flash_count in range(4):
                                logical_colors[index] = (255, 247, 0)
                                led[index] = _scale_color((255, 247, 0), current_ldr_brightness)
                                led.write()
                                time.sleep(1)
                                logical_colors[index] = (0, 0, 0)
                                led[index] = (0, 0, 0)
                                led.write()
                                time.sleep(.5)
                        def fade_to_white(color, target_brightness, fade_time):
                            start_brightness = 10
                            brightness_steps = int((target_brightness - start_brightness) / fade_time)
                            for step in range(fade_time):
                                new_brightness = start_brightness + brightness_steps * step
                                faded_color = (new_brightness, new_brightness, new_brightness)
                                yield faded_color
                        if weather_enabled.get("FG", True) and any(conditions_present[11:12]):
                            for flash_count in range(1):
                                white_color = (255, 255, 255)
                                fade_time = 10
                                for faded_color in fade_to_white(white_color, 0, fade_time):
                                    logical_colors[index] = faded_color
                                    led[index] = _scale_color(faded_color, current_ldr_brightness)
                                    led.write()
                                    time.sleep(.4)
                                logical_colors[index] = (0, 0, 0)
                                led[index] = (0, 0, 0)
                                led.write()
                                time.sleep(.5)
                        if weather_enabled.get("FZFG", True) and any(conditions_present[12:13]):
                            num_steps = 5000
                            white_color = (255, 255, 255)
                            blue_color = (0, 0, 255)
                            step_size = tuple((b - w) / num_steps for w, b in zip(white_color, blue_color))
                            for step in range(num_steps + 6):
                                t = step / num_steps if num_steps else 1
                                # _scale_color expects 0-255; start at app max setpoint, end at LDR; keep at least half max during fade
                                brightness = (1.0 - t) * max_brightness + t * current_ldr_brightness
                                brightness = max(brightness, (1.0 - t) * (max_brightness * 0.5))
                                current_color = tuple(int(w + step_size[i] * step) for i, w in enumerate(white_color))
                                logical_colors[index] = current_color
                                led[index] = _scale_color(current_color, int(brightness))
                                led.write()
                                time.sleep(0 / num_steps)
                            logical_colors[index] = (0, 0, 0)
                            led[index] = (0, 0, 0)
                            led.write()
                            time.sleep(.5)
                        if weather_enabled.get("FZFD", True) and any(conditions_present[13:14]):
                            num_steps = 5000
                            white_color = (255, 255, 255)
                            cyan_color = (0, 255, 180)
                            step_size = tuple((b - w) / num_steps for w, b in zip(white_color, cyan_color))
                            for step in range(num_steps + 6):
                                t = step / num_steps if num_steps else 1
                                brightness = (1.0 - t) * max_brightness + t * current_ldr_brightness
                                brightness = max(brightness, (1.0 - t) * (max_brightness * 0.5))
                                current_color = tuple(int(w + step_size[i] * step) for i, w in enumerate(white_color))
                                logical_colors[index] = current_color
                                led[index] = _scale_color(current_color, int(brightness))
                                led.write()
                                time.sleep(1 / num_steps)
                            logical_colors[index] = (0, 0, 0)
                            led[index] = (0, 0, 0)
                            led.write()
                            time.sleep(.5)
                        if weather_enabled.get("CLR", True) and any(conditions_present[14:15]):
                            num_steps = 1000
                            white_color = (255, 255, 255)
                            green_color = (0, 255, 0)
                            step_size = tuple((b - w) / num_steps for w, b in zip(white_color, green_color))
                            for step in range(num_steps + 6):
                                t = step / num_steps if num_steps else 1
                                brightness = (1.0 - t) * max_brightness + t * current_ldr_brightness
                                brightness = max(brightness, (1.0 - t) * (max_brightness * 0.5))
                                current_color = tuple(int(w + step_size[i] * step) for i, w in enumerate(white_color))
                                logical_colors[index] = current_color
                                led[index] = _scale_color(current_color, int(brightness))
                                led.write()
                                time.sleep(0 / num_steps)
                            logical_colors[index] = (0, 0, 0)
                            led[index] = (0, 0, 0)
                            led.write()
                            time.sleep(0)
                        if ((weather_enabled.get("CC", True) and conditions_present[15]) or (weather_enabled.get("CA", True) and conditions_present[16]) or (weather_enabled.get("CG", True) and conditions_present[17]) or (weather_enabled.get("VCTS", True) and conditions_present[18])):
                            for flash_count in range(35):
                                logical_colors[index] = (255, 255, 255)
                                led[index] = _scale_color((255, 255, 255), current_ldr_brightness)
                                led.write()
                                time.sleep(.09)
                                logical_colors[index] = (0, 0, 0)
                                led[index] = (0, 0, 0)
                                led.write()
                                time.sleep(.02)
                        if ((weather_enabled.get("TS", True) and conditions_present[19]) or (weather_enabled.get("$", True) and conditions_present[20]) or (weather_enabled.get("FC", True) and conditions_present[21])):
                            for flash_count in range(5):
                                logical_colors[index] = (255, 0, 0)
                                led[index] = _scale_color((255, 0, 0), current_ldr_brightness)
                                led.write()
                                time.sleep(.05)
                                logical_colors[index] = (0, 0, 0)
                                led[index] = (0, 0, 0)
                                led.write()
                                time.sleep(.5)
                        manual_brightness = 10
                        if ((weather_enabled.get("FC", True) and conditions_present[21]) or (weather_enabled.get("+FC", True) and conditions_present[22]) or (weather_enabled.get("TORNADO", True) and conditions_present[23])):
                            for flash_count in range(15):
                                num_steps = 200
                                red_color = (int(255 * manual_brightness / 255), 0, 0)
                                blue_color = (0, 0, 255)
                                step_size = tuple((b - w) / num_steps for w, b in zip(red_color, blue_color))
                                for step in range(num_steps + 6):
                                    current_color = tuple(int(w + step_size[i] * step) for i, w in enumerate(red_color))
                                    logical_colors[index] = current_color
                                    led[index] = _scale_color(current_color, current_ldr_brightness)
                                    led.write()
                                    time.sleep(0 / num_steps)
                                logical_colors[index] = (0, 0, 0)
                                led[index] = (0, 0, 0)
                                led.write()
                                time.sleep(.1)
                        if not had_effective:
                            print(f"No unblocked weather conditions at {airport}")
                            flash_once(led, index, (255, 165, 0), min_brightness, max_brightness)
                    else:
                        print(f"No specified weather conditions at {airport}")
                        flash_once(led, index, (255, 165, 0), min_brightness, max_brightness)
                    return any(conditions_present)
            else:
                print("No raw METAR text for {}".format(airport))
                return False
        except Exception as e:
            print("Error processing METAR data for {}: {}".format(airport, e))
            retries += 1
    print("Unable to retrieve valid data for {} after retries".format(airport))
    return False

def _scale_color(color, brightness):
    """Scale (r,g,b) 0-255 by brightness 0-255."""
    if brightness <= 0:
        return (0, 0, 0)
    return tuple(min(255, int(c * brightness / 255)) for c in color)

def refresh_strip_using_ldr():
    """Re-apply current_ldr_brightness to all LEDs from logical_colors."""
    for i in range(NUM_LEDS):
        led[i] = _scale_color(logical_colors[i], current_ldr_brightness)
    led.write()

def check_ldr_and_refresh():
    """Every 2s read LDR, update current_ldr_brightness, refresh whole strip."""
    global current_ldr_brightness, last_ldr_refresh_time
    if time.time() - last_ldr_refresh_time >= 2.0:
        current_ldr_brightness = map_ldr_to_brightness(read_ldr_value(), MIN_BRIGHTNESS, MAX_BRIGHTNESS)
        refresh_strip_using_ldr()
        last_ldr_refresh_time = time.time()

def set_led_color(led, flight_category, index, min_brightness, max_brightness):
    if index < 0 or index >= NUM_LEDS:
        print("Invalid LED index: {}".format(index))
        return
    flight_category = flight_category.strip().upper()
    color = (0, 0, 0)
    if flight_category == "VFR":
        color = (0, 255, 0)
    elif flight_category == "MVFR":
        color = (0, 0, 255)
    elif flight_category == "IFR":
        color = (255, 0, 0)
    elif flight_category == "LIFR":
        color = (255, 0, 130)
    logical_colors[index] = color
    led[index] = _scale_color(color, current_ldr_brightness)
    led.write()

def flash_once(led, index, color, min_brightness, max_brightness):
    logical_colors[index] = color
    led[index] = _scale_color(color, current_ldr_brightness)
    led.write()
    time.sleep(0.3)
    logical_colors[index] = (0, 0, 0)
    led[index] = (0, 0, 0)
    led.write()
    time.sleep(0.1)

def control_leds():
    pass

def turn_off_leds():
    for i in range(NUM_LEDS):
        logical_colors[i] = (0, 0, 0)
        led[i] = (0, 0, 0)
    led.write()

# Read airports
print(f"\n=== Reading airports from {AIRPORT_FILE} ===")
airports = read_airports(AIRPORT_FILE)
print(f"Airports: {len(airports)}")
gc.collect()

if not connect_to_wifi(WIFI_SSID, WIFI_PASSWORD):
    print("WiFi connection failed. Starting setup mode...")
    import wifi_manager
    wifi_manager.start()

sync_ntp_once()
flight_categories = {}
last_successful_data_time = time.time()
print(f"Firmware version: {FIRMWARE_VERSION}")
print(f"Data timeout monitoring started. Timeout: {NO_DATA_TIMEOUT} seconds")

print("\n=== Testing auto-brightness ===")
brightness = test_auto_brightness()
print(f"Initial auto-brightness: {brightness:.3f}")

def process_airports_in_batches(airports, process_function, batch_size=BATCH_SIZE, description="Processing", skip_batch_delay=False, poll_callback=None):
    gc.collect()
    total_airports = len(airports)
    num_batches = (total_airports + batch_size - 1) // batch_size
    print(f"\n=== {description} airports in {num_batches} batches of {batch_size} ===")
    for batch_num in range(num_batches):
        batch_start = batch_num * batch_size
        batch_end = min(batch_start + batch_size, total_airports)
        batch_airports = airports[batch_start:batch_end]
        print(f"\n--- Batch {batch_num + 1}/{num_batches} (Airports {batch_start}-{batch_end-1}) ---")
        print(f"Free memory before batch: {gc.mem_free()} bytes")
        for i_in_batch, airport in enumerate(batch_airports):
            index = batch_start + i_in_batch
            if index >= NUM_LEDS:
                print(f"Warning: Airport {airport} at index {index} exceeds LED count ({NUM_LEDS}). Skipping.")
                continue
            print(f"Processing {airport} (LED index {index})...")
            process_function(airport, index)
            if poll_callback is not None:
                poll_callback()
            gc.collect()
            time.sleep(0.1)
        print(f"Free memory after batch: {gc.mem_free()} bytes")
        if batch_num < num_batches - 1 and not skip_batch_delay:
            print(f"Waiting {BATCH_DELAY} seconds before next batch...")
            if poll_callback is not None:
                rem = float(BATCH_DELAY)
                while rem > 0:
                    poll_callback()
                    ch = 0.25 if rem >= 0.25 else rem
                    time.sleep(ch)
                    rem -= ch
            else:
                time.sleep(BATCH_DELAY)

def process_first_pass(airport, index):
    if not airport or airport.strip() == "":
        print(f"First pass - LED {index}: [skip]")
        logical_colors[index] = (0, 0, 0)
        led[index] = (0, 0, 0)
        led.write()
        return
    flight_category, raw_text = get_metar_data_with_retry(airport)
    if flight_category is not None:
        update_data_success()
        print(f"First pass - {airport}: {flight_category}")
        set_led_color(led, flight_category, index, MIN_BRIGHTNESS, MAX_BRIGHTNESS)
    else:
        print(f"First pass - {airport}: No data received")

def process_second_pass(airport, index):
    check_ldr_and_refresh()
    if not airport or airport.strip() == "":
        print(f"Second pass - LED {index}: [skip]")
        return
    flight_category, raw_text = get_metar_data_with_retry(airport)
    if flight_category is not None:
        update_data_success()
        if not MATRIX_ONLY:
            get_weather_conditions_with_retry(raw_text, airport, led, index, MIN_BRIGHTNESS, MAX_BRIGHTNESS)
        line1 = f"{airport}={flight_category}"
        line2 = f" {raw_text}" if raw_text is not None else "Raw Text: N/A"
        print(f"Second pass - {line1}")
        print(line2)
        set_led_color(led, flight_category, index, MIN_BRIGHTNESS, MAX_BRIGHTNESS)
        display_info(line1, line2, flight_category, airport)
        gc.collect()
    else:
        print(f"Second pass - {airport}: No data received")

def process_main_loop_batch(batch_airports, batch_start_index, poll_callback=None):
    check_ldr_and_refresh()
    any_data_received = False
    for i_in_batch, airport in enumerate(batch_airports):
        if poll_callback is not None:
            poll_callback()
        index = batch_start_index + i_in_batch
        if index >= NUM_LEDS:
            continue
        if not airport or airport.strip() == "":
            continue
        flight_category, raw_text = get_metar_data_with_retry(airport)
        if flight_category is not None:
            update_data_success()
            any_data_received = True
            if not MATRIX_ONLY:
                get_weather_conditions_with_retry(raw_text, airport, led, index, MIN_BRIGHTNESS, MAX_BRIGHTNESS)
            line1 = f"{airport}={flight_category}"
            line2 = f" {raw_text}" if raw_text is not None else "Raw Text: N/A"
            print(f"Main loop - {line1}")
            print(line2)
            set_led_color(led, flight_category, index, MIN_BRIGHTNESS, MAX_BRIGHTNESS)
            display_info(line1, line2, flight_category, airport)
            gc.collect()
        else:
            print(f"No data for {airport}")
        if poll_callback is not None:
            poll_callback()
    return any_data_received

try:
    # OTA: check before long METAR batches so serial shows result within seconds of WiFi
    print("OTA: checking GitHub Pages for newer firmware...")
    try:
        import updater
        gc.collect()
        has_update, version_info = updater.check_for_new_version(FIRMWARE_VERSION)
        if has_update and version_info:
            update_available = True
            update_info = version_info
            print("OTA: New version available", version_info.get("version"))
            if led_matrix is not None and DISPLAY_TYPE == "LED_MATRIX":
                msg_color = apply_auto_brightness((255, 140, 0))
                scroll_single_text_ultra_smooth("NEW UPDATE AVAILABLE PRESS BUTTON TO INSTALL", msg_color)
    except SyntaxError as e:
        print("OTA check error: invalid syntax in updater.py — re-copy pico/updater.py to the Pico.")
        print(e)
    except Exception as e:
        print("OTA check error:", e)
    gc.collect()

    # OTA HTTP on :8080 — bind NOW so browser/app work during long first/second passes (not only after).
    update_button = None
    if UPDATE_BUTTON_PIN >= 0:
        try:
            update_button = Pin(UPDATE_BUTTON_PIN, Pin.IN, Pin.PULL_UP)
        except Exception:
            update_button = None

    UPDATE_PAGE_HTML = """<!DOCTYPE html><html><head><meta name="viewport" content="width=device-width"><title>MetarMap Update</title></head><body><h1>MetarMap</h1><p>Install latest firmware from GitHub.</p><form method="post" action="/start-update"><button type="submit">Install update</button></form><p><small>Device will reboot and apply after download.</small></p></body></html>"""

    def open_ota_listen_socket():
        try:
            s = socket.socket()
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind(("0.0.0.0", 8080))
            s.listen(1)
            s.settimeout(0.1)
            return s
        except Exception as ex:
            print("OTA bind 8080 failed:", ex)
            return None

    update_socket = open_ota_listen_socket()
    if update_socket is not None:
        print("OTA update server on port 8080 (listening during METAR startup)")
    else:
        print("OTA: will retry binding 8080 every few seconds")

    try:
        import network
        _w = network.WLAN(network.STA_IF)
        if _w.isconnected():
            _ip = _w.ifconfig()[0]
            print("MetarMap LAN IP:", _ip, "— open http://%s:8080 for OTA page" % (_ip,))
    except Exception:
        pass

    _ota_rebind_after = 0.0

    def service_ota_http_and_button():
        global update_socket, _ota_rebind_after
        """OTA button + port 8080."""
        if update_button is not None and update_button.value() == 0:
            if update_button.value() == 0:
                time.sleep_ms(50)
                if update_button.value() == 0:
                    try:
                        import updater
                        if update_available and update_info:
                            updater.install_pending_update(update_info)
                        else:
                            has_update, version_info = updater.check_for_new_version(FIRMWARE_VERSION)
                            if has_update and version_info:
                                updater.install_pending_update(version_info)
                            else:
                                print("OTA button: no update available")
                    except Exception as e:
                        print("OTA install error:", e)
        if update_socket is None:
            tnow = time.time()
            if tnow >= _ota_rebind_after:
                _ota_rebind_after = tnow + 5.0
                update_socket = open_ota_listen_socket()
                if update_socket is not None:
                    print("OTA: port 8080 listen (re)started")
        if update_socket is not None:
            try:
                conn, _ = update_socket.accept()
                conn.settimeout(5.0)
                req = conn.recv(2048).decode("utf-8", "ignore")
                if not req:
                    conn.close()
                    return
                first = req.split("\n")[0].strip() if req else ""
                if first.startswith("POST ") and "/start-update" in first:
                    try:
                        import updater
                        if update_available and update_info:
                            has_update = True
                            version_info = update_info
                        else:
                            has_update, version_info = updater.check_for_new_version(FIRMWARE_VERSION)
                        if has_update and version_info:
                            conn.send(b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nConnection: close\r\n\r\nInstalling...")
                            try:
                                conn.flush()
                            except Exception:
                                pass
                            time.sleep_ms(200)
                            conn.close()
                            updater.install_pending_update(version_info)
                        else:
                            print("OTA POST /start-update: no newer firmware (recheck)")
                            conn.send(b"HTTP/1.1 409 Conflict\r\nContent-Type: text/plain\r\nConnection: close\r\n\r\nNo update available.")
                            conn.close()
                    except Exception as e:
                        print("OTA POST /start-update error:", e)
                        try:
                            conn.send(b"HTTP/1.1 500 Internal Server Error\r\nContent-Type: text/plain\r\nConnection: close\r\n\r\nUpdate error.")
                            conn.close()
                        except Exception:
                            pass
                else:
                    _html = UPDATE_PAGE_HTML.encode("utf-8")
                    _cl = len(_html)
                    conn.send(
                        (
                            "HTTP/1.1 200 OK\r\nContent-Type: text/html; charset=utf-8\r\n"
                            "Connection: close\r\nContent-Length: %d\r\n\r\n" % _cl
                        ).encode("utf-8")
                    )
                    conn.send(_html)
                    conn.close()
            except OSError:
                pass
            except Exception as e:
                print("OTA server handle:", e)

    def sleep_with_ota_poll(total_seconds):
        remaining = float(total_seconds)
        while remaining > 0:
            service_ota_http_and_button()
            chunk = 0.25 if remaining >= 0.25 else remaining
            time.sleep(chunk)
            remaining -= chunk

    current_ldr_brightness = map_ldr_to_brightness(read_ldr_value(), MIN_BRIGHTNESS, MAX_BRIGHTNESS)
    last_ldr_refresh_time = time.time()
    bulk_ok = False
    if not MATRIX_ONLY:
        bulk_results = fetch_all_metars_once(airports)
        if bulk_results:
            n = min(len(airports), len(bulk_results), NUM_LEDS)
            for index in range(n):
                if (bulk_results[index][0] is None or bulk_results[index][1] is None) and airports[index] and airports[index].strip():
                    fc, raw = get_metar_data_with_retry(airports[index])
                    if fc is not None:
                        bulk_results[index] = (fc, raw or bulk_results[index][1])
                        update_data_success()
                    gc.collect()
                    time.sleep(0.3)
                service_ota_http_and_button()
            any_set = False
            for index in range(n):
                fc, _ = bulk_results[index]
                if fc and airports[index] and airports[index].strip():
                    set_led_color(led, fc, index, MIN_BRIGHTNESS, MAX_BRIGHTNESS)
                    any_set = True
                else:
                    logical_colors[index] = (0, 0, 0)
                    led[index] = (0, 0, 0)
            led.write()
            if any_set:
                update_data_success()
                bulk_ok = True
                print("All airport LEDs set from bulk fetch (displaying 3s)")
                sleep_with_ota_poll(3)
    if not bulk_ok:
        if not MATRIX_ONLY:
            process_airports_in_batches(airports, process_first_pass, description="First pass", poll_callback=service_ota_http_and_button)
    process_airports_in_batches(airports, process_second_pass, description="Second pass", poll_callback=service_ota_http_and_button)

    # NTP sync once for sleep schedule (time.localtime())
    ntptime_synced = False
    def maybe_sync_ntp():
        global ntptime_synced
        if ntptime_synced or not SLEEP_ENABLED:
            return
        if _try_ntp_sync():
            ntptime_synced = True
            t = local_time()
            print("NTP time synced for sleep schedule")
            print("Current time (local): %04d-%02d-%02d %02d:%02d:%02d" % (t[0], t[1], t[2], t[3], t[4], t[5]))

    def is_in_sleep_window():
        try:
            t = local_time()
            h, mi = t[3], t[4]
            now_m = h * 60 + mi
            sleep_m = SLEEP_AT_HOUR * 60 + SLEEP_AT_MIN
            wake_m = WAKE_AT_HOUR * 60 + WAKE_AT_MIN
            if sleep_m > wake_m:  # e.g. 22:00 to 06:00
                return now_m >= sleep_m or now_m < wake_m
            return sleep_m <= now_m < wake_m
        except Exception:
            return False

    def clear_displays_for_sleep():
        try:
            if SLEEP_LEDS and led is not None:
                for i in range(len(led)):
                    led[i] = (0, 0, 0)
                led.write()
            if SLEEP_MATRIX and led_matrix is not None:
                led_matrix.fill((0, 0, 0))
                led_matrix.write()
            if SLEEP_OLED and oled is not None and DISPLAY_TYPE == "OLED":
                oled.fill(0)
                oled.show()
        except Exception as e:
            print("Sleep clear err:", e)

    displays_sleeping = False

    while True:
        service_ota_http_and_button()
        check_ldr_and_refresh()
        maybe_sync_ntp()
        ensure_wifi_connected()
        if SLEEP_ENABLED and is_in_sleep_window():
            if not displays_sleeping:
                clear_displays_for_sleep()
                displays_sleeping = True
                t = local_time()
                print("Current time (local): %04d-%02d-%02d %02d:%02d:%02d - Display sleep: off until %02d:%02d" % (t[0], t[1], t[2], t[3], t[4], t[5], WAKE_AT_HOUR, WAKE_AT_MIN))
            sleep_with_ota_poll(CYCLE_DELAY)
            continue
        just_woke_from_sleep = displays_sleeping
        displays_sleeping = False
        if just_woke_from_sleep:
            update_data_success()
            t = local_time()
            print("Display wake: %02d:%02d - resuming METAR fetch" % (t[3], t[4]))
        check_data_timeout()
        if no_data_warning_active and DISPLAY_TYPE == "LED_MATRIX":
            print("NO DATA warning active - displaying warning and checking connection...")
            ensure_wifi_connected()
            display_no_data_warning()
            sleep_with_ota_poll(5)
            continue
        elif no_data_warning_active and DISPLAY_TYPE != "LED_MATRIX":
            print("NO DATA warning active (no display to show warning)")
            ensure_wifi_connected()
            sleep_with_ota_poll(5)
            continue
        any_data_received = False
        total_airports = len(airports)
        num_batches = (total_airports + BATCH_SIZE - 1) // BATCH_SIZE
        for batch_num in range(num_batches):
            service_ota_http_and_button()
            batch_start = batch_num * BATCH_SIZE
            batch_end = min(batch_start + BATCH_SIZE, total_airports)
            batch_airports = airports[batch_start:batch_end]
            print(f"\n--- Main Loop Batch {batch_num + 1}/{num_batches} (Airports {batch_start}-{batch_end-1}) ---")
            print(f"Free memory before batch: {gc.mem_free()} bytes")
            batch_data_received = process_main_loop_batch(
                batch_airports, batch_start, poll_callback=service_ota_http_and_button
            )
            any_data_received = any_data_received or batch_data_received
            print(f"Free memory after batch: {gc.mem_free()} bytes")
            if batch_num < num_batches - 1:
                print(f"Waiting {BATCH_DELAY} seconds before next batch...")
                sleep_with_ota_poll(BATCH_DELAY)
        if not any_data_received:
            print("No data received for any airport in this cycle")
            check_data_timeout()
        print(f"\nCompleted full cycle of {len(airports)} airports")
        print(f"Waiting {CYCLE_DELAY} seconds before next cycle...")
        sleep_with_ota_poll(CYCLE_DELAY)

except Exception as main_exception:
    print("Main script exception:", main_exception)
    machine.reset()

finally:
    try:
        OLED_pin.value(0)
        LDR_output_pin.value(0)
        led.fill((0, 0, 0))
        led.write()
        turn_off_leds()
        if oled is not None:
            oled.fill(0)
            oled.show()
        if led_matrix is not None:
            led_matrix.fill((0, 0, 0))
            led_matrix.write()
    except:
        pass






