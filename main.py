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
# When True: scroll ICAO=CATEGORY text before METAR; when False: scroll METAR only (colors still show category)
SCROLL_MATRIX_CATEGORY = True

# ===== BATCH PROCESSING SETTINGS =====
BATCH_SIZE = 5  # Reduced from 5 for better memory management
BATCH_DELAY = 1  # Delay between batches in seconds
CYCLE_DELAY = 10  # Seconds between full airport list cycles; loaded from config

# ===== FIRMWARE VERSION (for OTA update check) =====
# Device reports this string; GitHub Pages version.json "version" must be higher to offer OTA.
# After you flash new code, this should match what you published (or stay lower until user updates).
FIRMWARE_VERSION = "1.1.39"

# ===== OTA / PLAY BUTTON (GPIO) =====
# Same pin as force-AP at boot: long hold (3s) during startup = setup AP mode.
# While running (hold length on release — multi-click was unreliable with bounce):
#   tap  (< ~0.5s)     = OTA check/install
#   hold (~0.5–1.5s)   = past-24h history
#   hold (>= ~1.5s)    = next-24h TAF forecast
# While holding, matrix/OLED/strip show OTA → PAST → FUTURE as thresholds are crossed.
# Set to -1 to disable physical button (use app or http://<ip>:8080 only).
UPDATE_BUTTON_PIN = FORCE_AP_BUTTON_PIN
UPDATE_BUTTON_TAP_MS = 500  # release sooner than this → OTA
UPDATE_BUTTON_PAST_MS = 1500  # release before this → PAST; at/after → FUTURE
UPDATE_BUTTON_BOUNCE_MS = 40  # ignore chatter shorter than this
UPDATE_BUTTON_HINT_ARM_MS = 180  # delay before first hold hint (avoids flash on bounce)

# ===== 24h FLIGHT-CATEGORY HISTORY TRIGGER (PIR / extra button) =====
# SR602 on this GPIO: active-HIGH on motion -> play PAST 24h pack only.
# Keep separate from UPDATE_BUTTON_PIN (hold: tap=OTA, mid=PAST, long=FUTURE).
# Set to -1 to disable (app/HTTP / hold-button only).
HISTORY_TRIGGER_PIN = 14
HISTORY_TRIGGER_ACTIVE_HIGH = True  # SR602 OUT high on detect; False = active-low to GND
HISTORY_TRIGGER_COOLDOWN_MS = 8000  # ignore re-fires while PIR stays high / after queue
# Auto-download 24h history after startup METAR passes, then again on this interval (seconds). 0 = startup only.
HISTORY_REFRESH_INTERVAL_S = 3600
# Same interval for TAF forecast pack refresh (0 = startup only).
FORECAST_REFRESH_INTERVAL_S = 3600
# Saved from the Android replay-count slider; used by physical-button playback.
HISTORY_REPLAY_LOOPS = 1
# While strip history animation runs, scale frozen matrix pixels by this (0=off/dark, 1=unchanged). Scroll resumes after.
HISTORY_MATRIX_DIM = 0

# ===== DATA TIMEOUT SETTINGS =====
NO_DATA_TIMEOUT = 180  # seconds without any airport METAR before warning
NO_DATA_REBOOT_DELAY = 30  # seconds after warning before machine.reset()
last_successful_data_time = None  # Track when we last got ANY airport data
no_data_warning_active = False  # Track if we're currently showing the warning
no_data_warning_since = None  # time.time() when warning started (reboot countdown)
update_available = False  # Set True when OTA check finds newer version
update_info = None  # Parsed version.json when update_available
_ota_button_prev = 1  # 1 = released (pull-up)
_ota_service_hook = None  # set to service_ota_http_and_button so display loops can poll OTA
_ota_btn_down_ms = 0  # ticks when press started; 0 = not held
_ota_btn_pending_hold_ms = 0  # completed hold duration for service to act on
_ota_btn_ignore_until_ms = 0  # post-release bounce lockout
_ota_btn_hold_hint = None  # last live hint while held: None | "OTA" | "PAST" | "FUTURE"
# 24h history/forecast: set True while fetch/play runs (avoids re-entrant HTTP/GPIO triggers)
_history_busy = False
_history_trigger_prev = 0  # idle level for active-high PIR (SR602)
_history_trigger_ignore_until_ms = 0  # PIR cooldown after edge
_history_auto_anchor = 0  # time.time() of last auto refresh request (startup / hourly)
_forecast_auto_anchor = 0
_fetch_progress_last_ms = 0  # throttle matrix/strip "still fetching" pulse
_play_banner_label = None  # "PAST" / "FUTURE" — set by button, shown before play
# Set True in main loop when SLEEP_ENABLED and in OFF window and sleep_leds — blocks LDR/OTA from relighting strip from logical_colors
_strip_dark_for_sleep = False
# If boot happens inside the sleep window, keep displays awake until the next daily sleep_at; then normal schedule resumes.
_sleep_boot_override_active = False
_sleep_boot_override_clear_after = None  # (y, mo, d, h, mi) local civil — clear override when now >= this tuple
# Do not dim for sleep until NTP has set the RTC at least once (avoids false sleep / flash on bad default time).
_sleep_clock_trusted = False


def _ota_btn_irq_handler(pin):
    """Timestamp press/release in IRQ so short taps are not lost between slow polls."""
    global _ota_btn_down_ms, _ota_btn_pending_hold_ms, _ota_btn_ignore_until_ms
    now = time.ticks_ms()
    if _ota_btn_ignore_until_ms and time.ticks_diff(now, _ota_btn_ignore_until_ms) < 0:
        return
    try:
        v = pin.value()
    except Exception:
        return
    if v == 0:
        if not _ota_btn_down_ms:
            _ota_btn_down_ms = now
        return
    # Released
    if _ota_btn_down_ms:
        held = time.ticks_diff(now, _ota_btn_down_ms)
        _ota_btn_down_ms = 0
        _ota_btn_ignore_until_ms = time.ticks_add(now, UPDATE_BUTTON_BOUNCE_MS)
        if held >= UPDATE_BUTTON_BOUNCE_MS:
            _ota_btn_pending_hold_ms = held


def _maybe_service_ota():
    """Call OTA HTTP + button handler (use sparingly — e.g. throttled during matrix scroll)."""
    fn = _ota_service_hook
    if fn is not None:
        try:
            fn()
        except Exception:
            pass


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
# While showing airport N, also briefly flash nearby LEDs that have rain/snow/storm/lightning
# (LED-index neighbors, not true lat/lon). Set NEIGHBOR_WX_FLASH = False to disable / revert behavior.
NEIGHBOR_WX_FLASH = True
NEIGHBOR_WX_RADIUS = 5   # grow cluster at most this many indexes left/right of present
NEIGHBOR_WX_MAX = 12     # cap LEDs in one cluster flash
NEIGHBOR_WX_COOLDOWN_S = 45  # after a cluster flash, skip until cool (avoids one-by-one walk)

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
    When a main-strip NeoPixel chain is used, refresh it from LDR so strip and matrix stay in sync.
    When MATRIX_ONLY, skip strip refresh (no geographic strip — avoids spurious data on pin 0)."""
    global current_ldr_brightness
    ldr_value = read_ldr_value()
    brightness_value = map_ldr_to_brightness(ldr_value, MIN_BRIGHTNESS, MAX_BRIGHTNESS)
    current_ldr_brightness = brightness_value
    try:
        if not MATRIX_ONLY:
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


def _as_bool(value, default=False):
    """Parse booleans safely from bool/int/string config values."""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        v = value.strip().lower()
        if v in ("true", "1", "yes", "on"):
            return True
        if v in ("false", "0", "no", "off", ""):
            return False
    return bool(value)


def _parse_weekday_config(value, default):
    """Weekday for schedules: 0=Monday … 6=Sunday (same as time.gmtime weekday). Accept int/float or mon/tue/…"""
    d = int(default)
    d = max(0, min(6, d))
    if value is None:
        return d
    if isinstance(value, (int, float)):
        return max(0, min(6, int(value)))
    s = str(value).strip().lower()
    names = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
    if len(s) >= 3 and s[:3] in names:
        return names.index(s[:3])
    try:
        return max(0, min(6, int(float(s))))
    except (TypeError, ValueError):
        return d


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
    wifi_manager.start(force_ap=force_ap_mode)

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

# WS2811 LED configuration (initial count; wifi_config may resize after load)
# NUM_LEDS = pixels clocked on METAR strip GPIO (see wifi_config led_pin; default GPIO 0).
# STRIP_ACTIVE_LEDS = how many positions (0..active-1) may show airport colors; rest forced black.
LED_PIN = 0
NUM_LEDS = 256
STRIP_ACTIVE_LEDS = 256  # overridden from num_leds in wifi_config.json

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
# Per-LED interest bits from last METAR (rain=1 snow=2 ltg=4 storm=8); tiny RAM
_wx_interest = bytearray(NUM_LEDS)
# Per-LED WX_TAGS bitfield (bit i = WX_TAGS[i] present) for full neighbor animations
_wx_cond_flags = [0] * NUM_LEDS
_wx_neighbor_cool = []  # last flash time.time() per LED; grown lazily
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
        try:
            LED_MATRIX_PIN = max(0, min(28, int(config.get("led_matrix_pin", 1))))
        except (TypeError, ValueError):
            LED_MATRIX_PIN = 1
            print("wifi_config led_matrix_pin invalid, using GPIO 1 for matrix")
        try:
            # METAR strip only — must differ from LED_MATRIX_PIN when both are NeoPixel (same pin = strip freezes / shows garbage)
            LED_PIN = max(0, min(28, int(config.get("led_pin", 0))))
        except (TypeError, ValueError):
            LED_PIN = 0
            print("wifi_config led_pin invalid, using GPIO 0 for METAR strip")
        if DISPLAY_TYPE == "LED_MATRIX" and LED_PIN == LED_MATRIX_PIN:
            _alt = 1 if LED_PIN != 1 else 2
            print(
                "WARNING: led_pin and led_matrix_pin were both GPIO %d — two NeoPixels on one pin breaks the strip. "
                "Using GPIO %d for matrix (fix wifi_config.json to match wiring)." % (LED_PIN, _alt)
            )
            LED_MATRIX_PIN = _alt
        BATCH_SIZE = max(1, min(20, config.get('batch_size', 3)))
        MIN_BRIGHTNESS = max(0, min(255, config.get('min_brightness', 2)))
        MAX_BRIGHTNESS = max(0, min(255, config.get('max_brightness', 15)))
        _mo = config.get('matrix_only', False)
        MATRIX_ONLY = _mo.lower() in ('true', '1', 'yes') if isinstance(_mo, str) else bool(_mo)
        NEIGHBOR_WX_FLASH = _as_bool(config.get("neighbor_wx_flash", True), default=True)
        SCROLL_MATRIX_CATEGORY = _as_bool(config.get("matrix_scroll_category", True), default=True)
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
        try:
            HISTORY_REPLAY_LOOPS = max(1, min(10, int(config.get("history_replay_loops", 1))))
        except (TypeError, ValueError):
            HISTORY_REPLAY_LOOPS = 1
        print(f"Loaded WiFi configuration for: {WIFI_SSID}")
        print(f"Display Type: {DISPLAY_TYPE}")
        print(f"LED Matrix Brightness: {LED_MATRIX_BRIGHTNESS}")
        print(f"Matrix only (no strip weather): {MATRIX_ONLY}")
        print(f"Neighbor WX flash (nearby rain/storm LEDs): {NEIGHBOR_WX_FLASH}")
        print(f"Matrix scroll category line: {SCROLL_MATRIX_CATEGORY}")
        off_codes = [c for c, v in WEATHER_ENABLED.items() if not v]
        if off_codes:
            print(f"Weather effects OFF for: {off_codes}")
        # Display sleep schedule (turn off matrix/LEDs/OLED at night)
        SLEEP_ENABLED = _as_bool(config.get("sleep_enabled", False), default=False)
        SLEEP_AT_HOUR = max(0, min(23, int(config.get("sleep_at_hour", 22))))
        SLEEP_AT_MIN = max(0, min(59, int(config.get("sleep_at_minute", 0))))
        WAKE_AT_HOUR = max(0, min(23, int(config.get("wake_at_hour", 6))))
        WAKE_AT_MIN = max(0, min(59, int(config.get("wake_at_minute", 0))))
        SLEEP_MATRIX = _as_bool(config.get("sleep_matrix", True), default=True)
        SLEEP_LEDS = _as_bool(config.get("sleep_leds", True), default=True)
        SLEEP_OLED = _as_bool(config.get("sleep_oled", True), default=True)
        # Long "weekend" (or any weekly block): off from weekend_off_weekday/time until weekend_on_weekday/time (local).
        WEEKEND_MODE_ENABLED = _as_bool(config.get("weekend_mode_enabled", False), default=False)
        WEEKEND_OFF_WEEKDAY = _parse_weekday_config(config.get("weekend_off_weekday", 4), 4)
        try:
            WEEKEND_OFF_HOUR = max(0, min(23, int(config.get("weekend_off_hour", 18))))
        except (TypeError, ValueError):
            WEEKEND_OFF_HOUR = 18
        try:
            WEEKEND_OFF_MINUTE = max(0, min(59, int(config.get("weekend_off_minute", 0))))
        except (TypeError, ValueError):
            WEEKEND_OFF_MINUTE = 0
        WEEKEND_ON_WEEKDAY = _parse_weekday_config(config.get("weekend_on_weekday", 0), 0)
        try:
            WEEKEND_ON_HOUR = max(0, min(23, int(config.get("weekend_on_hour", 6))))
        except (TypeError, ValueError):
            WEEKEND_ON_HOUR = 6
        try:
            WEEKEND_ON_MINUTE = max(0, min(59, int(config.get("weekend_on_minute", 0))))
        except (TypeError, ValueError):
            WEEKEND_ON_MINUTE = 0
        try:
            TIMEZONE_OFFSET_HOURS = max(-12, min(14, int(config.get("timezone_offset_hours", -5))))
        except (TypeError, ValueError):
            TIMEZONE_OFFSET_HOURS = -5
        # num_leds = airport strip slots (only 0..num_leds-1 may show METAR colors).
        # physical_led_count = total WS2812 on led_pin. NeoPixel buffer = max(active, physical).
        # If physical is OMITTED: default to max(num_leds, 256) so 8×32-style panels still get
        # every pixel clocked off. (If you only clock 49 into a 256 chain, LEDs 50+ keep latched
        # data — looks like a duplicate second block.)
        _DEFAULT_STRIP_PHYS_OMITTED = 256
        try:
            _active = int(config.get("num_leds", NUM_LEDS))
            _active = max(1, min(480, _active))
        except (TypeError, ValueError):
            _active = NUM_LEDS
        _p_raw = config.get("physical_led_count", None)
        _p_effective = None
        try:
            if _p_raw is not None and str(_p_raw).strip() != "":
                _p_effective = int(_p_raw)
                # Guardrail: <=1 is almost always accidental and causes stale tail/duplicate block.
                if _p_effective <= 1:
                    _p_effective = None
                else:
                    _p_effective = max(1, min(480, _p_effective))
            if _p_effective is not None:
                _phys = _p_effective
            else:
                _phys = max(_active, min(480, _DEFAULT_STRIP_PHYS_OMITTED))
        except (TypeError, ValueError):
            _phys = max(_active, min(480, _DEFAULT_STRIP_PHYS_OMITTED))
        _phys = max(_active, _phys)
        STRIP_ACTIVE_LEDS = _active
        NUM_LEDS = _phys
        led = neopixel.NeoPixel(machine.Pin(LED_PIN), NUM_LEDS)
        logical_colors = [(0, 0, 0)] * NUM_LEDS
        _wx_interest = bytearray(NUM_LEDS)
        _wx_cond_flags = [0] * NUM_LEDS
        _wx_neighbor_cool = [0] * NUM_LEDS
        for i in range(NUM_LEDS):
            led[i] = (0, 0, 0)
        led.write()
        print(
            "METAR strip: GPIO %d, %d WS2812 clocked (physical); airport LEDs = first %d (num_leds). "
            "physical_led_count: %s"
            % (
                LED_PIN,
                NUM_LEDS,
                STRIP_ACTIVE_LEDS,
                _p_effective if _p_effective is not None else "(unset/<=1/invalid → use max(num_leds,256))",
            )
        )
        if DISPLAY_TYPE == "LED_MATRIX":
            print("LED matrix: GPIO %d (must differ from METAR strip GPIO %d)" % (LED_MATRIX_PIN, LED_PIN))
        del config
        gc.collect()
except Exception as e:
    print(f"Error loading configuration: {e}")
    STRIP_ACTIVE_LEDS = NUM_LEDS
    WEATHER_ENABLED = {code: True for code in WX_TAGS}
    SLEEP_ENABLED = False
    SLEEP_AT_HOUR = 22
    SLEEP_AT_MIN = 0
    WAKE_AT_HOUR = 6
    WAKE_AT_MIN = 0
    SLEEP_MATRIX = SLEEP_LEDS = SLEEP_OLED = True
    TIMEZONE_OFFSET_HOURS = -5
    WEEKEND_MODE_ENABLED = False
    WEEKEND_OFF_WEEKDAY = 4
    WEEKEND_OFF_HOUR = 18
    WEEKEND_OFF_MINUTE = 0
    WEEKEND_ON_WEEKDAY = 0
    WEEKEND_ON_HOUR = 6
    WEEKEND_ON_MINUTE = 0
    r_scaled = int(20 * STARTUP_BRIGHTNESS)
    for i in range(NUM_LEDS):
        led[i] = (r_scaled, 0, 0)
    led.write()
    time.sleep(2)
    import wifi_manager
    wifi_manager.start(force_ap=False)

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
        else:
            oled.text("OLED Mode", 0, 5, 1)
            oled.text("Active", 0, 17, 1)
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

def _clear_no_data_warning():
    global no_data_warning_active, no_data_warning_since
    no_data_warning_active = False
    no_data_warning_since = None


def pause_no_data_watchdog_for_sleep():
    """Scheduled sleep skips METAR fetches — pause countdown and avoid reboot."""
    global last_successful_data_time
    _clear_no_data_warning()
    last_successful_data_time = time.time()
    print("No-data watchdog paused for display sleep")


def update_data_success():
    global last_successful_data_time
    last_successful_data_time = time.time()
    if no_data_warning_active:
        _clear_no_data_warning()
        print("Data restored - clearing NO DATA warning")


def check_data_timeout():
    global last_successful_data_time, no_data_warning_active, no_data_warning_since
    if sleep_applies_to_displays_now():
        return
    # History/forecast pack or playback blocks METAR cycles — do not treat as "no data"
    if _history_busy:
        return
    if last_successful_data_time is None:
        last_successful_data_time = time.time()
        return
    time_since_last_data = time.time() - last_successful_data_time
    if time_since_last_data <= NO_DATA_TIMEOUT:
        if no_data_warning_active:
            _clear_no_data_warning()
            print("Data connection restored")
        return
    if not no_data_warning_active:
        print("=== NO DATA TIMEOUT ===")
        print("No airport data received for %.1f seconds (limit %d)" % (time_since_last_data, NO_DATA_TIMEOUT))
        if DISPLAY_TYPE == "LED_MATRIX":
            print("Displaying NO DATA warning on LED matrix...")
            display_no_data_warning()
        else:
            print("NO DATA warning (no matrix); auto-reboot in %d sec" % NO_DATA_REBOOT_DELAY)
        no_data_warning_active = True
        no_data_warning_since = time.time()
        return
    if no_data_warning_since is None:
        no_data_warning_since = time.time()
    elapsed_warning = time.time() - no_data_warning_since
    if elapsed_warning >= NO_DATA_REBOOT_DELAY:
        print("=== NO DATA AUTO REBOOT ===")
        print("No METAR data for %.0f sec; rebooting after %d sec warning" % (time_since_last_data, NO_DATA_REBOOT_DELAY))
        machine.reset()

def display_no_data_warning():
    if led_matrix is None or DISPLAY_TYPE != "LED_MATRIX":
        print("Cannot display NO DATA warning - LED matrix not available")
        return
    try:
        led_matrix.fill((0, 0, 0))
        led_matrix.write()
        warning_color = apply_auto_brightness((255, 140, 0))
        warning_text = f"NO DATA {NO_DATA_TIMEOUT}s AUTO REBOOT IN {NO_DATA_REBOOT_DELAY}s"
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
        if SCROLL_MATRIX_CATEGORY:
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
        start_col = 0
        while start_col < total_frames:
            # Poll often while button held so hold hints update; else every 10 frames
            if _ota_btn_down_ms or _ota_btn_hold_hint or (start_col % 10 == 0):
                _maybe_service_ota()
            frame_start = time.ticks_ms()
            # Pause METAR scroll while holding — otherwise scroll overwrites OTA/PAST/FUTURE instantly
            if _ota_btn_down_ms or _ota_btn_hold_hint:
                frame_end = time.ticks_ms()
                draw_time = frame_end - frame_start
                if draw_time < frame_target_ms:
                    remaining_ms = frame_target_ms - draw_time
                    if remaining_ms > 0:
                        time.sleep(remaining_ms / 1000.0)
                continue
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
                    time.sleep(remaining_ms / 1000.0)
            start_col += 1
        del columns
        gc.collect()
    except Exception as e:
        print(f"Error in scroll_single_text_ultra_smooth: {e}")


def show_static_matrix_text(text, text_color, hold_s=2.0):
    """Draw short uppercase text centered on the matrix and hold (no scroll)."""
    if led_matrix is None or DISPLAY_TYPE != "LED_MATRIX" or PIXEL_INDICES is None:
        return
    try:
        text = str(text).upper()
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
        # Drop trailing spacer column if present
        while columns and columns[-1] == 0:
            columns.pop()
        text_w = len(columns)
        start_x = max(0, (LED_MATRIX_WIDTH - text_w) // 2)
        led_matrix.fill((0, 0, 0))
        for i, col_data in enumerate(columns):
            x = start_x + i
            if x >= LED_MATRIX_WIDTH or col_data == 0:
                continue
            for y in range(LED_MATRIX_HEIGHT):
                if col_data & (1 << y):
                    led_matrix[PIXEL_INDICES[x][y]] = text_color
        led_matrix.write()
        del columns
        gc.collect()
        if hold_s > 0:
            time.sleep(hold_s)
    except Exception as e:
        print("show_static_matrix_text:", e)


def show_play_mode_banner(label, hold_s=2.0):
    """Static PAST / FUTURE cue on matrix and/or OLED before strip animation."""
    label = str(label or "").upper()
    if not label:
        return
    try:
        if led_matrix is not None and DISPLAY_TYPE == "LED_MATRIX":
            # Past = green-ish, Future = cyan-ish (matches VFR / “ahead” feel)
            if label.startswith("FUT"):
                base = (0, 220, 255)
            elif label.startswith("OTA"):
                base = (255, 140, 0)
            else:
                base = (0, 255, 80)
            show_static_matrix_text(label, apply_auto_brightness(base), hold_s=hold_s)
            led_matrix.fill((0, 0, 0))
            led_matrix.write()
        elif DISPLAY_TYPE == "OLED" and oled is not None:
            oled.fill(0)
            if fonts_available:
                try:
                    writ = writer.Writer(oled, sans18)
                    writ.set_textpos(20, 0)
                    writ.printstring(label[:10])
                except Exception:
                    oled.text(label[:16], 0, 24, 1)
            else:
                oled.text(label[:16], 0, 24, 1)
            oled.show()
            if hold_s > 0:
                time.sleep(hold_s)
                oled.fill(0)
                oled.show()
    except Exception as e:
        print("show_play_mode_banner:", e)


def _hold_hint_for_ms(held_ms):
    """Label for current hold duration, or None before hint arms."""
    if held_ms < UPDATE_BUTTON_HINT_ARM_MS:
        return None
    if held_ms < UPDATE_BUTTON_TAP_MS:
        return "OTA"
    if held_ms < UPDATE_BUTTON_PAST_MS:
        return "PAST"
    return "FUTURE"


def _hold_hint_rgb(label):
    if label == "OTA":
        return (255, 140, 0)
    if label == "PAST":
        return (0, 255, 80)
    if label == "FUTURE":
        return (0, 220, 255)
    return (0, 0, 0)


def _restore_strip_logical_colors():
    if led is None or MATRIX_ONLY:
        return
    try:
        n = min(STRIP_ACTIVE_LEDS, NUM_LEDS, len(led))
        for i in range(n):
            c = logical_colors[i] if i < len(logical_colors) else (0, 0, 0)
            led[i] = _scale_color(c, current_ldr_brightness)
        led.write()
    except Exception:
        pass


def _apply_strip_hold_tint(rgb):
    if led is None or MATRIX_ONLY:
        return
    try:
        n = min(STRIP_ACTIVE_LEDS, NUM_LEDS, len(led))
        for i in range(n):
            led[i] = _scale_color(rgb, current_ldr_brightness)
        led.write()
    except Exception:
        pass


def show_button_hold_hint(label):
    """Non-blocking live cue while the OTA/play button is held (OTA → PAST → FUTURE)."""
    global _ota_btn_hold_hint
    label = str(label).upper() if label else None
    if label == _ota_btn_hold_hint:
        return
    prev = _ota_btn_hold_hint
    _ota_btn_hold_hint = label
    try:
        if not label:
            if prev:
                try:
                    if led_matrix is not None and DISPLAY_TYPE == "LED_MATRIX":
                        led_matrix.fill((0, 0, 0))
                        led_matrix.write()
                except Exception:
                    pass
                try:
                    if DISPLAY_TYPE == "OLED" and oled is not None:
                        oled.fill(0)
                        oled.show()
                except Exception:
                    pass
                _restore_strip_logical_colors()
            return
        rgb = _hold_hint_rgb(label)
        if led_matrix is not None and DISPLAY_TYPE == "LED_MATRIX":
            show_static_matrix_text(label, apply_auto_brightness(rgb), hold_s=0)
        elif DISPLAY_TYPE == "OLED" and oled is not None:
            show_play_mode_banner(label, hold_s=0)
        else:
            _apply_strip_hold_tint(rgb)
        print("Button hold hint:", label)
    except Exception as e:
        print("show_button_hold_hint:", e)


def dim_led_matrix(factor=None):
    """Scale current matrix pixels down (used while strip history animation freezes the scroll)."""
    if led_matrix is None or DISPLAY_TYPE != "LED_MATRIX":
        return
    try:
        f = HISTORY_MATRIX_DIM if factor is None else float(factor)
        if f <= 0:
            led_matrix.fill((0, 0, 0))
            led_matrix.write()
            return
        if f >= 1.0:
            return
        for i in range(LED_MATRIX_NUM_LEDS):
            r, g, b = led_matrix[i]
            led_matrix[i] = (int(r * f), int(g * f), int(b * f))
        led_matrix.write()
    except Exception as e:
        print("dim_led_matrix:", e)


def _flash_strip_fetch_pulse():
    """Brief low white flash on airport LEDs, then restore live colors (strip-only / no matrix)."""
    if led is None or MATRIX_ONLY:
        return
    try:
        n = min(STRIP_ACTIVE_LEDS, NUM_LEDS, len(led))
        b = max(2, min(12, int(MIN_BRIGHTNESS) + 2))
        for i in range(n):
            led[i] = (b, b, b)
        led.write()
        time.sleep_ms(50)
        for i in range(n):
            c = logical_colors[i] if i < len(logical_colors) else (0, 0, 0)
            led[i] = _scale_color(c, current_ldr_brightness)
        led.write()
    except Exception:
        pass


def _pulse_matrix_fetch():
    """Cheap amber blink — no text allocation (used between fetch batches)."""
    if led_matrix is None or DISPLAY_TYPE != "LED_MATRIX":
        return
    try:
        b = max(2, min(24, int(current_ldr_brightness) if current_ldr_brightness else MIN_BRIGHTNESS))
        led_matrix.fill((b, max(1, b // 2), 0))
        led_matrix.write()
        time.sleep_ms(40)
        led_matrix.fill((0, 0, 0))
        led_matrix.write()
    except Exception:
        pass


def _show_fetch_banner(msg):
    """One short matrix scroll at fetch start; strip-only gets a single pulse."""
    try:
        if led_matrix is not None and DISPLAY_TYPE == "LED_MATRIX":
            # Short word keeps scroll fast; full LDR refresh once is fine here
            scroll_single_text_ultra_smooth(
                msg, apply_auto_brightness((255, 160, 0))
            )
            led_matrix.fill((0, 0, 0))
            led_matrix.write()
        else:
            _flash_strip_fetch_pulse()
    except Exception as e:
        print("fetch banner:", e)


def _history_fetch_poll():
    """Progress pulse only during history download — no nested HTTP/SSL (saves RAM)."""
    global _fetch_progress_last_ms
    update_data_success()
    now = time.ticks_ms()
    if _fetch_progress_last_ms and time.ticks_diff(now, _fetch_progress_last_ms) < 2000:
        return
    _fetch_progress_last_ms = now
    try:
        if led_matrix is not None and DISPLAY_TYPE == "LED_MATRIX":
            _pulse_matrix_fetch()
        else:
            _flash_strip_fetch_pulse()
    except Exception:
        pass


def _clear_fetch_indicator():
    try:
        if led_matrix is not None and DISPLAY_TYPE == "LED_MATRIX":
            led_matrix.fill((0, 0, 0))
            led_matrix.write()
    except Exception:
        pass
    try:
        if led is not None and not MATRIX_ONLY:
            n = min(STRIP_ACTIVE_LEDS, NUM_LEDS, len(led))
            for i in range(n):
                c = logical_colors[i] if i < len(logical_colors) else (0, 0, 0)
                led[i] = _scale_color(c, current_ldr_brightness)
            led.write()
    except Exception:
        pass


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
            # current_ldr_brightness defaults to 128 before any LDR read — far too bright for status LEDs.
            _bw = map_ldr_to_brightness(read_ldr_value(), MIN_BRIGHTNESS, MAX_BRIGHTNESS)
            if DISPLAY_TYPE == "OLED":
                # Strip is only a hint in OLED mode; keep it softer than normal strip METAR brightness.
                _cap = max(MIN_BRIGHTNESS, (MIN_BRIGHTNESS + MAX_BRIGHTNESS) // 2)
                _bw = min(_bw, _cap)
            global current_ldr_brightness
            current_ldr_brightness = _bw
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
    global _sleep_clock_trusted
    import ntptime
    for host in NTP_SERVERS:
        try:
            ntptime.host = host
            ntptime.settime()
            print("NTP time synced from", host)
            _sleep_clock_trusted = True
            return True
        except Exception as e:
            print("NTP failed", host, ":", e)
    return False

def sync_ntp_once():
    """Sync RTC from NTP so time is correct before METAR/sleep (retries while WiFi stabilizes)."""
    for attempt in range(3):
        if _try_ntp_sync():
            return True
        if attempt < 2:
            time.sleep(1)
    print("NTP sync at startup failed (all servers, 3 rounds)")
    return False

def local_time():
    """Return local civil time tuple (year, month, day, hour, min, sec, ...) using TIMEZONE_OFFSET_HOURS (UTC + offset).

    Use gmtime so behavior does not depend on MicroPython localtime() TZ (Pico is often UTC-only).
    """
    return time.gmtime(time.time() + TIMEZONE_OFFSET_HOURS * 3600)


def _in_daily_sleep_clock(hour, minute):
    """Daily night window using clock only (no weekday). hour, minute = local civil."""
    now_m = hour * 60 + minute
    sleep_m = SLEEP_AT_HOUR * 60 + SLEEP_AT_MIN
    wake_m = WAKE_AT_HOUR * 60 + WAKE_AT_MIN
    if sleep_m > wake_m:
        return now_m >= sleep_m or now_m < wake_m
    return sleep_m <= now_m < wake_m


def is_in_sleep_window_now():
    """Return True when current local time is within configured daily sleep window."""
    try:
        t = local_time()
        return _in_daily_sleep_clock(t[3], t[4])
    except Exception:
        return False


def _week_minutes(wd, hour, minute):
    """Minutes from start of ISO week (Mon 00:00) to this weekday + time (wd 0=Mon … 6=Sun)."""
    return wd * 1440 + hour * 60 + minute


def is_in_weekend_off_period_at(weekday, hour, minute):
    """True if (weekday, time) lies in the configured weekend block (may wrap past Sunday)."""
    if not WEEKEND_MODE_ENABLED:
        return False
    try:
        cur = _week_minutes(weekday, hour, minute)
        S = _week_minutes(WEEKEND_OFF_WEEKDAY, WEEKEND_OFF_HOUR, WEEKEND_OFF_MINUTE)
        E = _week_minutes(WEEKEND_ON_WEEKDAY, WEEKEND_ON_HOUR, WEEKEND_ON_MINUTE)
        if S < E:
            return S <= cur < E
        if S > E:
            return cur >= S or cur < E
        if WEEKEND_OFF_WEEKDAY != WEEKEND_ON_WEEKDAY:
            return False
        om = hour * 60 + minute
        o0 = WEEKEND_OFF_HOUR * 60 + WEEKEND_OFF_MINUTE
        o1 = WEEKEND_ON_HOUR * 60 + WEEKEND_ON_MINUTE
        if o0 < o1:
            return o0 <= om < o1
        if o0 > o1:
            return om >= o0 or om < o1
        return False
    except Exception:
        return False


def is_in_weekend_off_period_now():
    """True when local time is inside the weekend / long off block."""
    try:
        t = local_time()
        return is_in_weekend_off_period_at(t[6], t[3], t[4])
    except Exception:
        return False


def is_combined_scheduled_display_sleep_at(weekday, hour, minute):
    """Daily sleep and/or weekend block (used by boot wake scanner)."""
    daily = SLEEP_ENABLED and _in_daily_sleep_clock(hour, minute)
    block = WEEKEND_MODE_ENABLED and is_in_weekend_off_period_at(weekday, hour, minute)
    return daily or block


def is_combined_scheduled_display_sleep_now():
    """Whether any enabled schedule wants displays off (ignores boot override and clock trust)."""
    try:
        t = local_time()
        return is_combined_scheduled_display_sleep_at(t[6], t[3], t[4])
    except Exception:
        return False


_DIMS = (31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31)


def _is_leap_year(y):
    return (y % 4 == 0) and (y % 100 != 0 or y % 400 == 0)


def _ymd_add_one_day(y, mo, d):
    dim = _DIMS[mo - 1]
    if mo == 2 and _is_leap_year(y):
        dim = 29
    if d < dim:
        return (y, mo, d + 1)
    if mo < 12:
        return (y, mo + 1, 1)
    return (y + 1, 1, 1)


def _tick_local_minute(y, mo, d, h, mi, wd):
    """Advance local civil time by one minute; wd is ISO weekday 0..6."""
    mi += 1
    if mi < 60:
        return (y, mo, d, h, mi, wd)
    mi = 0
    h += 1
    if h < 24:
        return (y, mo, d, h, mi, wd)
    h = 0
    y, mo, d = _ymd_add_one_day(y, mo, d)
    wd = (wd + 1) % 7
    return (y, mo, d, h, mi, wd)


def _next_local_tuple_combined_sleep_false_strictly_after_now():
    """First local minute strictly after now when no schedule requests display sleep (or None)."""
    try:
        t = local_time()
        y, mo, d, h, mi = t[0], t[1], t[2], t[3], t[4]
        wd = t[6]
        for _ in range(10080):
            y, mo, d, h, mi, wd = _tick_local_minute(y, mo, d, h, mi, wd)
            if not is_combined_scheduled_display_sleep_at(wd, h, mi):
                return (y, mo, d, h, mi)
    except Exception as _e:
        print("next combined wake err:", _e)
    return None


def _next_local_sleep_at_tuple_strictly_after_now():
    """Next local civil (y,mo,d,h,mi) at SLEEP_AT_HOUR:MINUTE strictly after current local minute."""
    t = local_time()
    y, mo, d, h, mi = t[0], t[1], t[2], t[3], t[4]
    now_tuple = (y, mo, d, h, mi)
    cy, cmo, cd = y, mo, d
    for _ in range(370):
        cand = (cy, cmo, cd, SLEEP_AT_HOUR, SLEEP_AT_MIN)
        if cand > now_tuple:
            return cand
        cy, cmo, cd = _ymd_add_one_day(cy, cmo, cd)
    return (y + 1, mo, d, SLEEP_AT_HOUR, SLEEP_AT_MIN)


def _next_local_wake_at_tuple_strictly_after_now():
    """Next local civil (y,mo,d,h,mi) at WAKE_AT_HOUR:MINUTE strictly after current local minute."""
    t = local_time()
    y, mo, d, h, mi = t[0], t[1], t[2], t[3], t[4]
    now_tuple = (y, mo, d, h, mi)
    cy, cmo, cd = y, mo, d
    for _ in range(370):
        cand = (cy, cmo, cd, WAKE_AT_HOUR, WAKE_AT_MIN)
        if cand > now_tuple:
            return cand
        cy, cmo, cd = _ymd_add_one_day(cy, cmo, cd)
    return (y + 1, mo, d, WAKE_AT_HOUR, WAKE_AT_MIN)


def _refresh_sleep_boot_override():
    """End boot-time 'stay awake' override at stored clear time."""
    global _sleep_boot_override_active, _sleep_boot_override_clear_after
    if not _sleep_boot_override_active or _sleep_boot_override_clear_after is None:
        return
    try:
        lt = local_time()
        now5 = (lt[0], lt[1], lt[2], lt[3], lt[4])
        if now5 >= _sleep_boot_override_clear_after:
            _sleep_boot_override_active = False
            _sleep_boot_override_clear_after = None
            print(
                "Sleep: boot override ended — normal schedule (at %04d-%02d-%02d %02d:%02d)"
                % (now5[0], now5[1], now5[2], now5[3], now5[4])
            )
    except Exception as _e:
        print("Sleep boot override refresh err:", _e)


def _try_arm_sleep_boot_override():
    """If we boot inside a scheduled-off window, keep displays on until the next time all schedules are awake."""
    global _sleep_boot_override_active, _sleep_boot_override_clear_after
    if _sleep_boot_override_active:
        return
    if not (SLEEP_ENABLED or WEEKEND_MODE_ENABLED):
        return
    if not _sleep_clock_trusted:
        return
    if not is_combined_scheduled_display_sleep_now():
        return
    lt = local_time()
    now_m = lt[3] * 60 + lt[4]
    _sm = SLEEP_AT_HOUR * 60 + SLEEP_AT_MIN
    _wm = WAKE_AT_HOUR * 60 + WAKE_AT_MIN
    if SLEEP_ENABLED:
        if _sm > _wm and now_m == _sm:
            return
        if _sm <= _wm and now_m <= _sm:
            return
    if WEEKEND_MODE_ENABLED:
        if lt[6] == WEEKEND_OFF_WEEKDAY and now_m == (WEEKEND_OFF_HOUR * 60 + WEEKEND_OFF_MINUTE):
            return
    nxt = _next_local_tuple_combined_sleep_false_strictly_after_now()
    if nxt is None:
        return
    _sleep_boot_override_clear_after = nxt
    _sleep_boot_override_active = True
    print(
        "Sleep: boot inside scheduled-off window — displays stay on until %04d-%02d-%02d %02d:%02d"
        % (nxt[0], nxt[1], nxt[2], nxt[3], nxt[4])
    )


def sleep_applies_to_displays_now():
    """True when sleep schedule should dim/blank displays.

    Boot override keeps displays on after a cold boot inside the *daily* night window so the
    device does not look dead; weekend / long-off blackout must still turn LEDs off on schedule.
    """
    _refresh_sleep_boot_override()
    if not (SLEEP_ENABLED or WEEKEND_MODE_ENABLED):
        return False
    if not _sleep_clock_trusted:
        return False
    if WEEKEND_MODE_ENABLED and is_in_weekend_off_period_now():
        return True
    if _sleep_boot_override_active:
        return False
    return is_combined_scheduled_display_sleep_now()


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

MAX_RETRIES = 1

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

def get_metar_data_with_retry(airport, quick=False):
    """Returns (flight_category, raw_text) or (None, None). Tries raw format first (smaller response, less memory).
    If quick=True (first pass or bulk gap fill), skip SSL EOF backoff / outer sleep so slow airports do not block the strip."""
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
            if (
                not quick
                and last_error
                and _is_ssl_eof(last_error)
                and ssl_retries < SSL_EOF_MAX_EXTRA_TRIES
            ):
                ssl_retries += 1
                print("SSL closed (hotspot/cellular), retry {} in {}s...".format(ssl_retries, SSL_EOF_RETRY_DELAY))
                time.sleep(SSL_EOF_RETRY_DELAY)
                gc.collect()
                continue
            break
        retries += 1
        gc.collect()
        if not quick:
            time.sleep(2 * retries)
    if quick:
        print(f"Quick skip {airport} (no category yet); second pass / main loop will retry")
    else:
        print(f"Unable to retrieve data for {airport} after {MAX_RETRIES} retries")
    return None, None

BULK_CHUNK_SIZE = 20  # airports per request; smaller = more reliable full response
BULK_SSL_EXTRA_TRIES = 2  # fewer than live single-airport path — keep startup fast
BULK_SSL_RETRY_DELAY = 2


def fetch_all_metars_once(airports, on_chunk=None):
    """Fetch METARs for all airports in chunked requests. Returns list of (flight_category, raw_text) per index.
    Failed chunks are skipped (holes filled later); never aborts the whole bulk on one bad chunk.
    on_chunk(results, chunk_start, chunk_end) optional — paint LEDs as each chunk arrives."""
    n = min(len(airports), 480)
    if n == 0:
        return []
    results = [(None, None)] * n
    chunk_start = 0
    total_got = 0
    total_requested = 0
    failed_chunks = 0
    while chunk_start < n:
        chunk_end = min(chunk_start + BULK_CHUNK_SIZE, n)
        chunk_airports = [airports[i].strip() for i in range(chunk_start, chunk_end) if airports[i] and airports[i].strip()]
        total_requested += len(chunk_airports)
        if not chunk_airports:
            chunk_start = chunk_end
            continue
        ids = ",".join(chunk_airports)
        chunk_ok = False
        for ssl_attempt in range(BULK_SSL_EXTRA_TRIES + 1):
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
                if _is_ssl_eof(e) and ssl_attempt < BULK_SSL_EXTRA_TRIES:
                    print("SSL closed (hotspot/cellular), chunk retry {} in {}s...".format(ssl_attempt + 1, BULK_SSL_RETRY_DELAY))
                    time.sleep(BULK_SSL_RETRY_DELAY)
                else:
                    break
        if chunk_ok and on_chunk is not None:
            try:
                on_chunk(results, chunk_start, chunk_end)
            except Exception as _oc_e:
                print("Bulk METAR on_chunk:", _oc_e)
        if not chunk_ok:
            failed_chunks += 1
            print("Bulk METAR: skipping failed chunk {}–{}; continuing".format(chunk_start, chunk_end - 1))
        chunk_start = chunk_end
        if chunk_start < n:
            time.sleep(0.15)
    missing = total_requested - total_got
    if missing > 0 or failed_chunks:
        print(
            "Bulk METAR fetch: got {} of {} requested ({} slots), failed_chunks={}, missing={}; gap-fill individually.".format(
                total_got, total_requested, n, failed_chunks, missing
            )
        )
    else:
        print("Bulk METAR fetch: got {} of {} airports".format(total_got, n))
    return results

# Interesting WX for neighbor flash (not mist/wind/clear/maintenance)
_WX_RAIN = ("-RA", "RA", "+RA")
_WX_SNOW = ("-SN", "SN", "+SN", "SHSN")
_WX_LTG = ("LTG", "DSNT", "CC", "CA", "CG")
_WX_STORM = ("TS", "VCTS", "FC", "+FC", "TORNADO")


def _wx_interest_bits(raw_text):
    """Bitfield: rain=1, snow=2, lightning=4, storm/funnel=8. Exact METAR tokens only."""
    if not raw_text or not isinstance(raw_text, str):
        return 0
    bits = 0
    for tok in raw_text.upper().split():
        if tok in _WX_RAIN:
            bits |= 1
        elif tok in _WX_SNOW:
            bits |= 2
        elif tok in _WX_LTG:
            bits |= 4
        elif tok in _WX_STORM:
            bits |= 8
    return bits


def _wx_cond_bitfield(raw_text):
    """Bit i set when WX_TAGS[i] is an exact METAR token."""
    if not raw_text or not isinstance(raw_text, str):
        return 0
    toks = raw_text.upper().split()
    flags = 0
    for i, tag in enumerate(WX_TAGS):
        if tag in toks:
            flags |= 1 << i
    return flags


def update_wx_interest(index, raw_text):
    """Cache interest bits + full WX tag flags for neighbor cluster animations."""
    global _wx_interest, _wx_cond_flags
    if index < 0:
        return
    if index >= len(_wx_interest):
        extra = index + 1 - len(_wx_interest)
        _wx_interest = _wx_interest + bytearray(extra)
    while len(_wx_cond_flags) <= index:
        _wx_cond_flags.append(0)
    _wx_interest[index] = _wx_interest_bits(raw_text) & 0xFF
    _wx_cond_flags[index] = _wx_cond_bitfield(raw_text)


def _flash_indices(led_strip, idxs, color, on_s, off_s, times):
    """Run the same on/off flash pattern on many LEDs together."""
    if not idxs:
        return
    for _ in range(times):
        for i in idxs:
            logical_colors[i] = color
            led_strip[i] = _scale_color(color, current_ldr_brightness)
        led_strip.write()
        time.sleep(on_s)
        for i in idxs:
            logical_colors[i] = (0, 0, 0)
            led_strip[i] = (0, 0, 0)
        led_strip.write()
        time.sleep(off_s)


def flash_interesting_neighbors(center_index, led_strip):
    """
    Flash the contiguous nearby weather cluster using the *same* animations as the
    present airport (rain/snow/lightning/storm patterns), in parallel per condition.
    Center is skipped here (it already ran get_weather_conditions_with_retry).
    """
    global _wx_neighbor_cool, _wx_cond_flags
    if not NEIGHBOR_WX_FLASH or MATRIX_ONLY or led_strip is None:
        return
    if center_index < 0 or center_index >= len(_wx_interest):
        return
    if not _wx_interest[center_index]:
        return
    n = min(STRIP_ACTIVE_LEDS, len(airports), len(_wx_interest), len(logical_colors))
    if n <= 0:
        return
    if len(_wx_neighbor_cool) < n:
        _wx_neighbor_cool = list(_wx_neighbor_cool) + [0] * (n - len(_wx_neighbor_cool))
    while len(_wx_cond_flags) < n:
        _wx_cond_flags.append(0)
    try:
        now = time.time()
    except Exception:
        now = 0
    if now and center_index < len(_wx_neighbor_cool):
        last_c = _wx_neighbor_cool[center_index]
        if last_c and (now - last_c) < NEIGHBOR_WX_COOLDOWN_S:
            return

    def _slot_ok(j):
        if j < 0 or j >= n:
            return False
        if j >= len(airports) or not airports[j] or not str(airports[j]).strip():
            return False
        return bool(_wx_interest[j])

    lo = center_index
    while lo > 0 and (center_index - (lo - 1)) <= NEIGHBOR_WX_RADIUS and _slot_ok(lo - 1):
        lo -= 1
    hi = center_index
    while hi + 1 < n and ((hi + 1) - center_index) <= NEIGHBOR_WX_RADIUS and _slot_ok(hi + 1):
        hi += 1

    # Neighbors only — present airport already played full effects
    idxs = []
    for j in range(lo, hi + 1):
        if j == center_index:
            continue
        if not _slot_ok(j):
            continue
        idxs.append(j)
        if len(idxs) >= NEIGHBOR_WX_MAX:
            break
    if not idxs:
        return

    we = WEATHER_ENABLED
    print("Neighbor WX full anim @%d -> %s" % (center_index, idxs))

    def group(bit, code):
        if not we.get(code, True):
            return []
        out = []
        for j in idxs:
            if j < len(_wx_cond_flags) and (_wx_cond_flags[j] & (1 << bit)):
                out.append(j)
        return out

    saved = [logical_colors[i] for i in idxs]
    try:
        # Same order / timings as get_weather_conditions_with_retry (interesting tags)
        g = group(0, "BR")
        if g:
            _flash_indices(led_strip, g, (0, 255, 240), 0.1, 0.1, 12)
        g = group(1, "-RA")
        if g:
            _flash_indices(led_strip, g, (0, 255, 139), 0.5, 0.5, 6)
        g = group(2, "RA")
        if g:
            _flash_indices(led_strip, g, (0, 255, 139), 1.2, 0.5, 5)
        g = group(3, "+RA")
        if g:
            _flash_indices(led_strip, g, (0, 255, 139), 2.2, 0.5, 4)
        g = group(4, "-SN")
        if g:
            _flash_indices(led_strip, g, (255, 255, 255), 0.3, 0.5, 6)
        g = group(5, "SN")
        if g:
            _flash_indices(led_strip, g, (255, 255, 255), 1.2, 0.5, 5)
        g = group(6, "+SN")
        if g:
            _flash_indices(led_strip, g, (255, 255, 255), 2.2, 0.5, 4)
        g = group(7, "SHSN")
        if g:
            _flash_indices(led_strip, g, (255, 255, 255), 1.2, 0.5, 5)
        g = []
        if we.get("LTG", True) or we.get("DSNT", True):
            for j in idxs:
                f = _wx_cond_flags[j] if j < len(_wx_cond_flags) else 0
                if (we.get("LTG", True) and (f & (1 << 8))) or (we.get("DSNT", True) and (f & (1 << 9))):
                    g.append(j)
        if g:
            _flash_indices(led_strip, g, (255, 255, 0), 0.09, 0.02, 35)
        g = []
        for j in idxs:
            f = _wx_cond_flags[j] if j < len(_wx_cond_flags) else 0
            if ((we.get("CC", True) and (f & (1 << 15)))
                    or (we.get("CA", True) and (f & (1 << 16)))
                    or (we.get("CG", True) and (f & (1 << 17)))
                    or (we.get("VCTS", True) and (f & (1 << 18)))):
                g.append(j)
        if g:
            _flash_indices(led_strip, g, (255, 255, 255), 0.09, 0.02, 35)
        g = []
        for j in idxs:
            f = _wx_cond_flags[j] if j < len(_wx_cond_flags) else 0
            if ((we.get("TS", True) and (f & (1 << 19)))
                    or (we.get("FC", True) and (f & (1 << 21)))):
                g.append(j)
        if g:
            _flash_indices(led_strip, g, (255, 0, 0), 0.05, 0.5, 5)
        g = []
        for j in idxs:
            f = _wx_cond_flags[j] if j < len(_wx_cond_flags) else 0
            if ((we.get("FC", True) and (f & (1 << 21)))
                    or (we.get("+FC", True) and (f & (1 << 22)))
                    or (we.get("TORNADO", True) and (f & (1 << 23)))):
                g.append(j)
        if g:
            # Same red↔blue pulse family as present (simplified multi-LED steps)
            for _flash in range(15):
                for step in range(0, 201, 10):
                    t = step / 200.0
                    current_color = (
                        int(255 * (1.0 - t)),
                        0,
                        int(255 * t),
                    )
                    for i in g:
                        logical_colors[i] = current_color
                        led_strip[i] = _scale_color(current_color, current_ldr_brightness)
                    led_strip.write()
                    time.sleep(0.01)
                for i in g:
                    logical_colors[i] = (0, 0, 0)
                    led_strip[i] = (0, 0, 0)
                led_strip.write()
                time.sleep(0.1)
    finally:
        for k, i in enumerate(idxs):
            logical_colors[i] = saved[k]
            led_strip[i] = _scale_color(saved[k], current_ldr_brightness)
        # Cool whole cluster including center so strip walk does not repeat
        cool_set = list(idxs)
        if center_index not in cool_set:
            cool_set.append(center_index)
        for i in cool_set:
            if i < len(_wx_neighbor_cool):
                _wx_neighbor_cool[i] = now
        led_strip.write()


def get_weather_conditions_with_retry(raw_text, airport, led, index, min_brightness, max_brightness, weather_enabled=None):
    if index >= STRIP_ACTIVE_LEDS:
        return False
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
                        def fade_to_white(fade_time):
                            # Ramp gray -> white; scale peak with max_brightness like CLR
                            start_gray = 10
                            denom = max(1, fade_time - 1)
                            for step in range(fade_time):
                                t = step / denom
                                g = int(start_gray + (255 - start_gray) * t)
                                g = max(0, min(255, g))
                                # End at app max (CLR-bright); start from LDR so fog builds up
                                brightness = int((1.0 - t) * current_ldr_brightness + t * max_brightness)
                                brightness = max(brightness, int(t * (max_brightness * 0.5)))
                                yield (g, g, g), brightness
                        if weather_enabled.get("FG", True) and any(conditions_present[11:12]):
                            for flash_count in range(1):
                                fade_time = 10
                                for faded_color, brightness in fade_to_white(fade_time):
                                    logical_colors[index] = faded_color
                                    led[index] = _scale_color(faded_color, int(brightness))
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
                            num_steps = 200
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

def clear_unused_strip_leds(num_airport_slots):
    """Black out unused airport slots and everything past STRIP_ACTIVE_LEDS (entire physical tail)."""
    if MATRIX_ONLY or led is None:
        return
    try:
        n = int(num_airport_slots)
    except (TypeError, ValueError):
        n = 0
    # Within 0..active-1: clear from first unused airport line
    first_in_active = min(max(0, n), STRIP_ACTIVE_LEDS)
    for i in range(first_in_active, STRIP_ACTIVE_LEDS):
        logical_colors[i] = (0, 0, 0)
        led[i] = (0, 0, 0)
    # Past active cap through end of physical buffer (always, so tail never shows stale duplicate)
    for i in range(STRIP_ACTIVE_LEDS, NUM_LEDS):
        logical_colors[i] = (0, 0, 0)
        led[i] = (0, 0, 0)
    led.write()

def refresh_strip_using_ldr():
    """Re-apply current_ldr_brightness to all LEDs from logical_colors."""
    global _strip_dark_for_sleep
    if _strip_dark_for_sleep:
        return
    for i in range(NUM_LEDS):
        led[i] = _scale_color(logical_colors[i], current_ldr_brightness)
    led.write()

def check_ldr_and_refresh():
    """Every 2s read LDR, update current_ldr_brightness, refresh whole strip."""
    global current_ldr_brightness, last_ldr_refresh_time
    if MATRIX_ONLY:
        return
    if time.time() - last_ldr_refresh_time >= 2.0:
        current_ldr_brightness = map_ldr_to_brightness(read_ldr_value(), MIN_BRIGHTNESS, MAX_BRIGHTNESS)
        refresh_strip_using_ldr()
        last_ldr_refresh_time = time.time()

def set_led_color(led, flight_category, index, min_brightness, max_brightness):
    global _strip_dark_for_sleep
    if _strip_dark_for_sleep:
        return
    if index < 0 or index >= NUM_LEDS:
        print("Invalid LED index: {}".format(index))
        return
    if index >= STRIP_ACTIVE_LEDS:
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
    wifi_manager.start(force_ap=False)

_ntp_startup_ok = sync_ntp_once()
try:
    lt = local_time()
    print(
        "Startup local time (ntp_ok=%s): %04d-%02d-%02d %02d:%02d:%02d | timezone_offset_hours=%d | unix_utc=%d"
        % (
            _ntp_startup_ok,
            lt[0],
            lt[1],
            lt[2],
            lt[3],
            lt[4],
            lt[5],
            TIMEZONE_OFFSET_HOURS,
            int(time.time()),
        )
    )
except Exception as e:
    print("Startup time print error:", e)

try:
    _lt = local_time()
    _sleep_m = SLEEP_AT_HOUR * 60 + SLEEP_AT_MIN
    _wake_m = WAKE_AT_HOUR * 60 + WAKE_AT_MIN
    _now_m = _lt[3] * 60 + _lt[4]
    if _sleep_m > _wake_m:
        _in_window = (_now_m >= _sleep_m or _now_m < _wake_m)
    else:
        _in_window = (_sleep_m <= _now_m < _wake_m)
    _in_weekend = is_in_weekend_off_period_now()
    _in_combined = is_combined_scheduled_display_sleep_now()
    print(
        "Sleep schedule config: daily_enabled=%s sleep_at=%02d:%02d wake_at=%02d:%02d | weekend_mode=%s off=wd%d %02d:%02d on=wd%d %02d:%02d | tz=%d | matrix=%s strip=%s oled=%s | now=%02d:%02d daily_win=%s weekend_blk=%s combined=%s"
        % (
            SLEEP_ENABLED,
            SLEEP_AT_HOUR,
            SLEEP_AT_MIN,
            WAKE_AT_HOUR,
            WAKE_AT_MIN,
            WEEKEND_MODE_ENABLED,
            WEEKEND_OFF_WEEKDAY,
            WEEKEND_OFF_HOUR,
            WEEKEND_OFF_MINUTE,
            WEEKEND_ON_WEEKDAY,
            WEEKEND_ON_HOUR,
            WEEKEND_ON_MINUTE,
            TIMEZONE_OFFSET_HOURS,
            SLEEP_MATRIX,
            SLEEP_LEDS,
            SLEEP_OLED,
            _lt[3],
            _lt[4],
            _in_window,
            _in_weekend,
            _in_combined,
        )
    )
    if _sleep_m == _wake_m:
        print("Sleep schedule warning: sleep_at equals wake_at; sleep window is empty.")
except Exception as _sleep_cfg_err:
    print("Sleep schedule print error:", _sleep_cfg_err)

if _ntp_startup_ok:
    _try_arm_sleep_boot_override()

flight_categories = {}
last_successful_data_time = time.time()
print(f"Firmware version: {FIRMWARE_VERSION}")
print("Data timeout: %ds without METAR -> warning, auto-reboot %ds later" % (NO_DATA_TIMEOUT, NO_DATA_REBOOT_DELAY))

print("\n=== Testing auto-brightness ===")
brightness = test_auto_brightness()
print(f"Initial auto-brightness: {brightness:.3f}")

def process_airports_in_batches(airports, process_function, batch_size=BATCH_SIZE, description="Processing", skip_batch_delay=False, poll_callback=None):
    gc.collect()
    total_airports = len(airports)
    num_batches = (total_airports + batch_size - 1) // batch_size
    print(f"\n=== {description} airports in {num_batches} batches of {batch_size} ===")
    for batch_num in range(num_batches):
        if sleep_applies_to_displays_now():
            print(f"{description} paused: entered sleep window during batch processing")
            return True
        batch_start = batch_num * batch_size
        batch_end = min(batch_start + batch_size, total_airports)
        batch_airports = airports[batch_start:batch_end]
        print(f"\n--- Batch {batch_num + 1}/{num_batches} (Airports {batch_start}-{batch_end-1}) ---")
        print(f"Free memory before batch: {gc.mem_free()} bytes")
        for i_in_batch, airport in enumerate(batch_airports):
            if sleep_applies_to_displays_now():
                print(f"{description} paused: entered sleep window mid-batch")
                return True
            index = batch_start + i_in_batch
            if index >= NUM_LEDS:
                print(f"Warning: Airport {airport} at index {index} exceeds physical strip ({NUM_LEDS}). Skipping.")
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
                    if sleep_applies_to_displays_now():
                        print(f"{description} paused: entered sleep window during batch delay")
                        return True
                    poll_callback()
                    ch = 0.25 if rem >= 0.25 else rem
                    time.sleep(ch)
                    rem -= ch
            else:
                time.sleep(BATCH_DELAY)
    return False

def process_first_pass(airport, index):
    if not airport or airport.strip() == "":
        print(f"First pass - LED {index}: [skip]")
        if not MATRIX_ONLY and index < STRIP_ACTIVE_LEDS:
            logical_colors[index] = (0, 0, 0)
            led[index] = (0, 0, 0)
            led.write()
        return
    flight_category, raw_text = get_metar_data_with_retry(airport, quick=True)
    if flight_category is not None:
        update_data_success()
        update_wx_interest(index, raw_text)
        print(f"First pass - {airport}: {flight_category}")
        if not MATRIX_ONLY:
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
        update_wx_interest(index, raw_text)
        if not MATRIX_ONLY:
            get_weather_conditions_with_retry(raw_text, airport, led, index, MIN_BRIGHTNESS, MAX_BRIGHTNESS)
            flash_interesting_neighbors(index, led)
        line1 = f"{airport}={flight_category}"
        line2 = f" {raw_text}" if raw_text is not None else "Raw Text: N/A"
        print(f"Second pass - {line1}")
        print(line2)
        if not MATRIX_ONLY:
            set_led_color(led, flight_category, index, MIN_BRIGHTNESS, MAX_BRIGHTNESS)
        display_info(line1, line2, flight_category, airport)
        gc.collect()
    else:
        print(f"Second pass - {airport}: No data received")

def process_main_loop_batch(batch_airports, batch_start_index, poll_callback=None):
    check_ldr_and_refresh()
    any_data_received = False
    sleep_hit = False
    for i_in_batch, airport in enumerate(batch_airports):
        if sleep_applies_to_displays_now():
            sleep_hit = True
            break
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
            update_wx_interest(index, raw_text)
            if not MATRIX_ONLY:
                get_weather_conditions_with_retry(raw_text, airport, led, index, MIN_BRIGHTNESS, MAX_BRIGHTNESS)
                flash_interesting_neighbors(index, led)
            line1 = f"{airport}={flight_category}"
            line2 = f" {raw_text}" if raw_text is not None else "Raw Text: N/A"
            print(f"Main loop - {line1}")
            print(line2)
            if not MATRIX_ONLY:
                set_led_color(led, flight_category, index, MIN_BRIGHTNESS, MAX_BRIGHTNESS)
            display_info(line1, line2, flight_category, airport)
            gc.collect()
        else:
            print(f"No data for {airport}")
        if poll_callback is not None:
            poll_callback()
    return any_data_received, sleep_hit

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
            msg_color = apply_auto_brightness((255, 140, 0))
            # Brief banner then CLEAR matrix — leaving static text looked like a hang.
            if led_matrix is not None and DISPLAY_TYPE == "LED_MATRIX":
                try:
                    show_play_mode_banner("OTA", hold_s=1.5)
                except Exception as ex:
                    print("OTA matrix banner error:", ex)
                    try:
                        led_matrix.fill((0, 0, 0))
                        led_matrix.write()
                    except Exception:
                        pass
            elif DISPLAY_TYPE == "OLED" and oled is not None:
                try:
                    oled.fill(0)
                    if fonts_available:
                        wu = writer.Writer(oled, sans18)
                        wu.set_textpos(0, 0)
                        wu.printstring("UPDATE")
                        wu.set_textpos(0, 20)
                        wu.printstring("AVAILABLE")
                        wu.set_textpos(0, 40)
                        wu.printstring("BTN / :8080")
                    else:
                        oled.text("UPDATE AVAIL", 0, 0, 1)
                        oled.text("BTN or :8080", 0, 16, 1)
                    oled.show()
                    print("OTA: OLED — update available (2.5s)")
                    time.sleep(2.5)
                    oled.fill(0)
                    oled.show()
                except Exception as ex:
                    print("OTA OLED banner error:", ex)
            elif DISPLAY_TYPE == "NONE":
                # Strip-only: same amber as matrix — brief so METAR can start quickly
                try:
                    for i in range(NUM_LEDS):
                        logical_colors[i] = (255, 140, 0)
                        led[i] = msg_color
                    led.write()
                    print("OTA: strip-only — update color 2.5s (install: button or :8080)")
                    time.sleep(2.5)
                    for i in range(NUM_LEDS):
                        logical_colors[i] = (0, 0, 0)
                        led[i] = (0, 0, 0)
                    led.write()
                except Exception as ex:
                    print("OTA strip banner error:", ex)
    except SyntaxError as e:
        print("OTA check error: invalid syntax in updater.py — re-copy pico/updater.py to the Pico.")
        print(e)
    except Exception as e:
        print("OTA check error:", e)
    gc.collect()
    # OTA HTTPS + banner can leave STA idle / heap fragmented; reconnect before METARs
    try:
        if ensure_wifi_connected():
            print("OTA done — WiFi OK, starting flight-category METAR fetch…")
        else:
            print("OTA done — WiFi not connected; METAR fetch may fail until reconnect")
    except Exception as _wifi_e:
        print("OTA done — WiFi check error:", _wifi_e)
    gc.collect()

    # OTA HTTP on :8080 — bind NOW so browser/app work during long first/second passes (not only after).
    update_button = None
    if UPDATE_BUTTON_PIN >= 0:
        try:
            update_button = Pin(UPDATE_BUTTON_PIN, Pin.IN, Pin.PULL_UP)
            try:
                update_button.irq(
                    trigger=Pin.IRQ_FALLING | Pin.IRQ_RISING,
                    handler=_ota_btn_irq_handler,
                )
            except Exception as irq_e:
                print("OTA GPIO IRQ (button still polled):", irq_e)
        except Exception:
            update_button = None

    fc_hist = None
    fc_fcst = None
    history_trigger = None
    # Use module-level _history_busy only (do not assign a local here — it shadows busy checks)
    try:
        import fc_history as _fc_history_mod
        _hist_n = max(8, min(130, len(airports) if airports else 32))
        fc_hist = _fc_history_mod.FlightCategoryHistory(max_airports=_hist_n)
        fc_hist.loops = HISTORY_REPLAY_LOOPS
        print("fc_history: ready (max_airports=%d, %d bytes buf)" % (_hist_n, _hist_n * 6))
    except Exception as _fh_e:
        print("fc_history import failed:", _fh_e)
        fc_hist = None
    try:
        import fc_forecast as _fc_forecast_mod
        _fcst_n = max(8, min(130, len(airports) if airports else 32))
        fc_fcst = _fc_forecast_mod.FlightCategoryForecast(max_airports=_fcst_n)
        fc_fcst.loops = HISTORY_REPLAY_LOOPS
        print("fc_forecast: ready (max_airports=%d, %d bytes buf)" % (_fcst_n, _fcst_n * 6))
    except Exception as _ff_e:
        print("fc_forecast import failed:", _ff_e)
        fc_fcst = None
    if HISTORY_TRIGGER_PIN >= 0 and HISTORY_TRIGGER_PIN != UPDATE_BUTTON_PIN:
        try:
            # Active-high PIR (SR602): pull-down idle low; active-low switch: pull-up idle high
            if HISTORY_TRIGGER_ACTIVE_HIGH:
                history_trigger = Pin(HISTORY_TRIGGER_PIN, Pin.IN, Pin.PULL_DOWN)
                edge = "active-high"
            else:
                history_trigger = Pin(HISTORY_TRIGGER_PIN, Pin.IN, Pin.PULL_UP)
                edge = "active-low"
            print("History trigger GPIO %d (%s PIR/button -> PAST play)" % (HISTORY_TRIGGER_PIN, edge))
        except Exception as _ht_e:
            print("History trigger GPIO init failed:", _ht_e)
            history_trigger = None

    UPDATE_PAGE_HTML = """<!DOCTYPE html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>MetarMap Update</title>
<style>
body{font-family:Arial,sans-serif;max-width:420px;margin:24px auto;padding:0 16px;line-height:1.4}
h1{font-size:1.4rem;margin:0 0 12px}
h2{font-size:1.1rem;margin:28px 0 8px}
.nav{margin-bottom:16px}.nav a{margin-right:12px;color:#0066cc}
p{margin:8px 0;color:#333}
button{display:block;width:100%;padding:12px 16px;margin:8px 0;font-size:16px;border:none;border-radius:8px;cursor:pointer}
.btn-update{background:#0d6efd;color:#fff}
.btn-play{background:#198754;color:#fff}
.btn-forecast{background:#0dcaf0;color:#000}
.btn-refresh{background:#6c757d;color:#fff}
small{color:#666}
hr{border:none;border-top:1px solid #ddd;margin:24px 0}
</style></head><body>
<h1>MetarMap</h1>
<div class="nav"><a href="/">Setup</a> <a href="/page/airports">Airports</a> <a href="/page/weather">Weather</a> <a href="/page/help">Help</a> <a href="/page/update">Update</a></div>
<p>Firmware is <strong>not</strong> installed automatically; the device only checks at boot. Use this page when you want to download and install.</p>
<form method="post" action="/start-update"><button class="btn-update" type="submit">Install update</button></form>
<p><small>Device will reboot and apply after download.</small></p>
<hr>
<h2>24-hour flight categories</h2>
<p>Play the packed history on the LED strip, or download a fresh pack first.</p>
<form method="post" action="/history-play"><button class="btn-play" type="submit">Play past 24h</button></form>
<form method="post" action="/history-refresh"><button class="btn-refresh" type="submit">Refresh history</button></form>
<form method="post" action="/forecast-play"><button class="btn-forecast" type="submit">Play forecast 24h</button></form>
<form method="post" action="/forecast-refresh"><button class="btn-refresh" type="submit">Refresh forecast</button></form>
<p><small>Past = METARs. Forecast = TAFs (nearest TAF station if an airport has none). Button: tap = OTA, hold ~0.5s = past, hold ~1.5s = forecast. App sets replay count.</small></p>
</body></html>"""

    def _http_send_html(conn, html_str):
        b = html_str.encode("utf-8") if isinstance(html_str, str) else html_str
        conn.send(
            (
                "HTTP/1.1 200 OK\r\nContent-Type: text/html; charset=utf-8\r\n"
                "Connection: close\r\nContent-Length: %d\r\n\r\n" % len(b)
            ).encode("utf-8")
        )
        conn.sendall(b)

    def open_ota_listen_socket():
        try:
            s = socket.socket()
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind(("0.0.0.0", 8080))
            s.listen(1)
            # Non-blocking: accept() returns immediately if no client. A short timeout here
            # caused a visible matrix scroll hitch every ~72 frames (~5s at default scroll speed)
            # because scroll_single_text_ultra_smooth calls _maybe_service_ota() that often.
            s.settimeout(0)
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
            print(
                "MetarMap LAN IP:",
                _ip,
                "— http://%s:8080 OTA; GET/POST /config and POST /update-config (same as app Save when URL has no port)"
                % (_ip,),
            )
    except Exception:
        pass

    _ota_rebind_after = 0.0

    def _http_wifi_config_json_body():
        """Same JSON shape as wifi_manager GET /config — used on :8080 while main.py runs (no server on port 80)."""
        try:
            with open(CONFIG_FILE, "r") as f:
                cfg = json.load(f)
        except Exception:
            cfg = {}
        we = cfg.get("weather_enabled")
        if not isinstance(we, dict):
            we = {str(c): True for c in WX_TAGS}
        else:
            we = {str(k): bool(v) for k, v in we.items()}
            for code in WX_TAGS:
                if code not in we:
                    we[code] = True
        try:
            _lp = int(cfg.get("led_pin", 0))
            _lp = max(0, min(28, _lp))
        except (TypeError, ValueError):
            _lp = 0
        try:
            _lmp = int(cfg.get("led_matrix_pin", 1))
            _lmp = max(0, min(28, _lmp))
        except (TypeError, ValueError):
            _lmp = 1
        out = {
            "display_type": cfg.get("display_type", "LED_MATRIX"),
            "led_matrix_brightness": float(cfg.get("led_matrix_brightness", 0.1)),
            "led_matrix_pin": _lmp,
            "led_pin": _lp,
            "min_brightness": int(cfg.get("min_brightness", 2)),
            "max_brightness": int(cfg.get("max_brightness", 15)),
            "batch_size": int(cfg.get("batch_size", 3)),
            "matrix_only": bool(cfg.get("matrix_only", False)),
            "neighbor_wx_flash": _as_bool(cfg.get("neighbor_wx_flash", True), default=True),
            "matrix_scroll_category": _as_bool(cfg.get("matrix_scroll_category", True), default=True),
            "scroll_speed": float(cfg.get("scroll_speed", 0.08)),
            "matrix_wiring": str(cfg.get("matrix_wiring", "SNAKE_COLUMN")),
            "scroll_pause_before": float(cfg.get("scroll_pause_before", 0.75)),
            "cycle_delay": int(cfg.get("cycle_delay", 10)),
            "num_leds": int(cfg.get("num_leds", 256)),
            "physical_led_count": cfg.get("physical_led_count"),
            "weather_enabled": we,
            "sleep_enabled": bool(cfg.get("sleep_enabled", False)),
            "sleep_at_hour": int(cfg.get("sleep_at_hour", 22)),
            "sleep_at_minute": int(cfg.get("sleep_at_minute", 0)),
            "wake_at_hour": int(cfg.get("wake_at_hour", 6)),
            "wake_at_minute": int(cfg.get("wake_at_minute", 0)),
            "sleep_matrix": bool(cfg.get("sleep_matrix", True)),
            "sleep_leds": bool(cfg.get("sleep_leds", True)),
            "sleep_oled": bool(cfg.get("sleep_oled", True)),
            "timezone_offset_hours": int(cfg.get("timezone_offset_hours", 0)),
            "weekend_mode_enabled": bool(cfg.get("weekend_mode_enabled", False)),
            "weekend_off_weekday": int(cfg.get("weekend_off_weekday", 4)),
            "weekend_off_hour": int(cfg.get("weekend_off_hour", 18)),
            "weekend_off_minute": int(cfg.get("weekend_off_minute", 0)),
            "weekend_on_weekday": int(cfg.get("weekend_on_weekday", 0)),
            "weekend_on_hour": int(cfg.get("weekend_on_hour", 6)),
            "weekend_on_minute": int(cfg.get("weekend_on_minute", 0)),
            "firmware_version": FIRMWARE_VERSION,
            "update_available": bool(update_available),
        }
        return json.dumps(out)

    _LAN_VALID_MATRIX_WIRING = frozenset(
        ("ROW_MAJOR", "COLUMN_MAJOR", "SNAKE_ROW", "SNAKE_COLUMN")
    )

    def _http_apply_post_update_config(updates):
        """Merge JSON updates into wifi_config.json (same rules as wifi_manager POST /update-config)."""
        global NEIGHBOR_WX_FLASH
        cfg = {}
        try:
            with open(CONFIG_FILE, "r") as f:
                cfg = json.load(f)
        except Exception:
            pass
        if "display_type" in updates:
            cfg["display_type"] = str(updates["display_type"])
        if "led_matrix_brightness" in updates:
            cfg["led_matrix_brightness"] = float(updates["led_matrix_brightness"])
        if "led_matrix_pin" in updates:
            cfg["led_matrix_pin"] = int(updates["led_matrix_pin"])
        if "led_pin" in updates:
            try:
                cfg["led_pin"] = max(0, min(28, int(updates["led_pin"])))
            except (TypeError, ValueError):
                pass
        if "batch_size" in updates:
            cfg["batch_size"] = max(1, min(20, int(float(updates["batch_size"]))))
        if "num_leds" in updates:
            try:
                cfg["num_leds"] = max(1, min(480, int(float(updates["num_leds"]))))
            except (TypeError, ValueError):
                pass
        if "physical_led_count" in updates:
            try:
                v = updates["physical_led_count"]
                if v is None or v == "":
                    cfg.pop("physical_led_count", None)
                else:
                    cfg["physical_led_count"] = max(1, min(480, int(float(v))))
            except (TypeError, ValueError):
                pass
        if "min_brightness" in updates:
            cfg["min_brightness"] = max(0, min(255, int(updates["min_brightness"])))
        if "max_brightness" in updates:
            cfg["max_brightness"] = max(0, min(255, int(updates["max_brightness"])))
        if "matrix_only" in updates:
            mo = updates["matrix_only"]
            cfg["matrix_only"] = mo.lower() in ("true", "1", "yes") if isinstance(mo, str) else bool(mo)
        if "neighbor_wx_flash" in updates:
            nwf = updates["neighbor_wx_flash"]
            cfg["neighbor_wx_flash"] = (
                nwf.lower() in ("true", "1", "yes", "on") if isinstance(nwf, str) else bool(nwf)
            )
        if "matrix_scroll_category" in updates:
            msc = updates["matrix_scroll_category"]
            cfg["matrix_scroll_category"] = (
                msc.lower() in ("true", "1", "yes", "on") if isinstance(msc, str) else bool(msc)
            )
        if "scroll_speed" in updates:
            try:
                cfg["scroll_speed"] = max(0.03, min(0.2, float(updates["scroll_speed"])))
            except (TypeError, ValueError):
                pass
        if "matrix_wiring" in updates:
            mw = str(updates["matrix_wiring"]).upper()
            if mw in _LAN_VALID_MATRIX_WIRING:
                cfg["matrix_wiring"] = mw
        if "scroll_pause_before" in updates:
            try:
                cfg["scroll_pause_before"] = max(0, min(2, float(updates["scroll_pause_before"])))
            except (TypeError, ValueError):
                pass
        if "cycle_delay" in updates:
            try:
                cfg["cycle_delay"] = max(5, min(1800, int(float(updates["cycle_delay"]))))
            except (TypeError, ValueError):
                pass
        if "sleep_enabled" in updates:
            cfg["sleep_enabled"] = bool(updates["sleep_enabled"])
        if "sleep_at_hour" in updates:
            cfg["sleep_at_hour"] = max(0, min(23, int(updates["sleep_at_hour"])))
        if "sleep_at_minute" in updates:
            cfg["sleep_at_minute"] = max(0, min(59, int(updates["sleep_at_minute"])))
        if "wake_at_hour" in updates:
            cfg["wake_at_hour"] = max(0, min(23, int(updates["wake_at_hour"])))
        if "wake_at_minute" in updates:
            cfg["wake_at_minute"] = max(0, min(59, int(updates["wake_at_minute"])))
        if "sleep_matrix" in updates:
            cfg["sleep_matrix"] = bool(updates["sleep_matrix"])
        if "sleep_leds" in updates:
            cfg["sleep_leds"] = bool(updates["sleep_leds"])
        if "sleep_oled" in updates:
            cfg["sleep_oled"] = bool(updates["sleep_oled"])
        if "timezone_offset_hours" in updates:
            try:
                cfg["timezone_offset_hours"] = max(-12, min(14, int(updates["timezone_offset_hours"])))
            except (TypeError, ValueError):
                pass
        if "weekend_mode_enabled" in updates:
            cfg["weekend_mode_enabled"] = bool(updates["weekend_mode_enabled"])
        if "weekend_off_weekday" in updates:
            try:
                cfg["weekend_off_weekday"] = max(0, min(6, int(updates["weekend_off_weekday"])))
            except (TypeError, ValueError):
                pass
        if "weekend_off_hour" in updates:
            try:
                cfg["weekend_off_hour"] = max(0, min(23, int(updates["weekend_off_hour"])))
            except (TypeError, ValueError):
                pass
        if "weekend_off_minute" in updates:
            try:
                cfg["weekend_off_minute"] = max(0, min(59, int(updates["weekend_off_minute"])))
            except (TypeError, ValueError):
                pass
        if "weekend_on_weekday" in updates:
            try:
                cfg["weekend_on_weekday"] = max(0, min(6, int(updates["weekend_on_weekday"])))
            except (TypeError, ValueError):
                pass
        if "weekend_on_hour" in updates:
            try:
                cfg["weekend_on_hour"] = max(0, min(23, int(updates["weekend_on_hour"])))
            except (TypeError, ValueError):
                pass
        if "weekend_on_minute" in updates:
            try:
                cfg["weekend_on_minute"] = max(0, min(59, int(updates["weekend_on_minute"])))
            except (TypeError, ValueError):
                pass
        if "weather_enabled" in updates:
            we = updates["weather_enabled"]
            if isinstance(we, dict):
                cfg["weather_enabled"] = {str(k): bool(v) for k, v in we.items()}
                for code in WX_TAGS:
                    if code not in cfg["weather_enabled"]:
                        cfg["weather_enabled"][code] = True
        with open(CONFIG_FILE, "w") as f:
            json.dump(cfg, f)
        print("LAN :8080 POST /update-config saved to", CONFIG_FILE)
        # Apply neighbor flash live (no reboot required)
        if "neighbor_wx_flash" in updates:
            NEIGHBOR_WX_FLASH = bool(cfg.get("neighbor_wx_flash", True))
            print("Neighbor WX flash now:", NEIGHBOR_WX_FLASH)

    def _http_send_json_response(conn, success, message):
        body = json.dumps({"success": success, "message": message})
        b = body.encode("utf-8")
        conn.send(
            b"HTTP/1.1 200 OK\r\nContent-Type: application/json; charset=utf-8\r\nConnection: close\r\n"
        )
        conn.send(("Content-Length: %d\r\n\r\n" % len(b)).encode("ascii"))
        conn.sendall(b)

    def _save_history_replay_loops(value):
        """Set the button replay count in memory and persist it in wifi_config.json."""
        global HISTORY_REPLAY_LOOPS
        try:
            n = max(1, min(10, int(value)))
        except (TypeError, ValueError):
            return False
        HISTORY_REPLAY_LOOPS = n
        if fc_hist is not None:
            fc_hist.loops = n
        if fc_fcst is not None:
            fc_fcst.loops = n
        try:
            with open(CONFIG_FILE, "r") as f:
                cfg = json.load(f)
            try:
                old_n = int(cfg.get("history_replay_loops", 1))
            except (TypeError, ValueError):
                old_n = 1
            if old_n != n:
                cfg["history_replay_loops"] = n
                with open(CONFIG_FILE, "w") as f:
                    json.dump(cfg, f)
                print("History replay count saved:", n)
            return True
        except Exception as e:
            print("History replay count save failed:", e)
            return False

    def _history_scale(rgb):
        return _scale_color(rgb, current_ldr_brightness)

    def maybe_queue_hourly_history_refresh():
        """Queue a background 24h history re-fetch when HISTORY_REFRESH_INTERVAL_S has elapsed."""
        global _history_auto_anchor, _history_busy
        if fc_hist is None or HISTORY_REFRESH_INTERVAL_S <= 0 or _history_busy:
            return
        if fc_hist.refresh_pending() or fc_hist.play_pending():
            return
        now = int(time.time())
        last = int(getattr(fc_hist, "fetched_at", 0) or 0)
        basis = last if last > 0 else int(_history_auto_anchor or 0)
        if basis <= 0 or (now - basis) < int(HISTORY_REFRESH_INTERVAL_S):
            return
        print("fc_history: auto-refresh due (every %ds)" % HISTORY_REFRESH_INTERVAL_S)
        fc_hist.request_refresh()
        _history_auto_anchor = now

    def maybe_queue_hourly_forecast_refresh():
        """Queue a background TAF forecast re-fetch on FORECAST_REFRESH_INTERVAL_S."""
        global _forecast_auto_anchor, _history_busy
        if fc_fcst is None or FORECAST_REFRESH_INTERVAL_S <= 0 or _history_busy:
            return
        if fc_fcst.refresh_pending() or fc_fcst.play_pending():
            return
        now = int(time.time())
        last = int(getattr(fc_fcst, "fetched_at", 0) or 0)
        basis = last if last > 0 else int(_forecast_auto_anchor or 0)
        if basis <= 0 or (now - basis) < int(FORECAST_REFRESH_INTERVAL_S):
            return
        print("fc_forecast: auto-refresh due (every %ds)" % FORECAST_REFRESH_INTERVAL_S)
        fc_fcst.request_refresh()
        _forecast_auto_anchor = now

    def _airports_for_pack():
        """Airport list for history/forecast pack: active strip only, trailing blanks trimmed."""
        n = min(len(airports), STRIP_ACTIVE_LEDS, 130)
        while n > 0 and (not airports[n - 1] or not str(airports[n - 1]).strip()):
            n -= 1
        out = airports[:n] if n > 0 else []
        print("fc pack airports: %d (strip_active=%d)" % (len(out), STRIP_ACTIVE_LEDS))
        return out

    def _categories_from_strip():
        """Map current logical_colors back to VFR/MVFR/IFR/LIFR for history seed fallback."""
        cats = []
        n = min(len(airports), STRIP_ACTIVE_LEDS, len(logical_colors))
        rev = {
            (0, 255, 0): "VFR",
            (0, 0, 255): "MVFR",
            (255, 0, 0): "IFR",
            (255, 0, 128): "LIFR",
            (255, 0, 130): "LIFR",
        }
        for i in range(n):
            if not airports[i] or not str(airports[i]).strip():
                cats.append("VFR")
                continue
            rgb = logical_colors[i] if i < len(logical_colors) else (0, 0, 0)
            try:
                key = (int(rgb[0]), int(rgb[1]), int(rgb[2]))
            except Exception:
                key = (0, 0, 0)
            cats.append(rev.get(key, "VFR"))
        return cats

    def _dim_and_play_pack(pack):
        """Play a packed history/forecast buffer on the strip with matrix darkened."""
        global _history_busy, _play_banner_label
        if pack is None or not pack.ready:
            print("play pack: skipped (not ready)")
            return False
        if led is None:
            print("play pack: skipped (no strip)")
            return False
        _history_busy = True
        try:
            banner = _play_banner_label
            _play_banner_label = None
            if banner:
                show_play_mode_banner(banner, hold_s=2.0)
            dim_led_matrix()
            n = min(STRIP_ACTIVE_LEDS, len(airports), pack.n_airports, len(logical_colors))
            print("play pack: %d LEDs, frame_ms=%s loops=%s" % (n, pack.frame_ms, pack.loops))
            pack.play_on_strip(
                led,
                logical_colors,
                n,
                _history_scale,
                poll_callback=_history_play_poll,
            )
            return True
        finally:
            _history_busy = False
            _play_banner_label = None
            update_data_success()
            try:
                if led_matrix is not None and DISPLAY_TYPE == "LED_MATRIX":
                    led_matrix.fill((0, 0, 0))
                    led_matrix.write()
            except Exception:
                pass

    def service_history_pending():
        """Run queued history/forecast play first, then refresh. Play never waits behind refresh."""
        global _history_busy, _fetch_progress_last_ms
        if _history_busy:
            return

        def _do_fetch(label, pack):
            global _fetch_progress_last_ms
            _fetch_progress_last_ms = 0
            update_data_success()
            _show_fetch_banner(label)
            try:
                ok = pack.fetch_and_pack(_airports_for_pack(), _history_fetch_poll, chunk_size=5)
                # If 24h API pack failed, seed from live strip colors so PAST can still play
                if (not ok) and (pack is fc_hist) and hasattr(pack, "seed_flat"):
                    try:
                        if pack.seed_flat(_categories_from_strip()):
                            print("fc_history: using live-color fallback pack")
                            ok = True
                    except Exception as _seed_e:
                        print("fc_history seed fallback:", _seed_e)
                return ok
            finally:
                _clear_fetch_indicator()
                update_data_success()

        # --- PLAY FIRST (so PAST/FUTURE is not stuck behind hourly refresh) ---
        if fc_hist is not None and fc_hist.play_pending():
            fc_hist.clear_play_pending()
            # Prefer existing pack; only fetch when nothing playable
            if fc_hist.ready:
                print("fc_history: play (packed buffer ready)")
                _dim_and_play_pack(fc_hist)
            else:
                _history_busy = True
                try:
                    print("fc_history: not ready — fetching before play…")
                    _do_fetch("FETCHING", fc_hist)
                finally:
                    _history_busy = False
                if fc_hist.ready:
                    _dim_and_play_pack(fc_hist)
                else:
                    err = getattr(fc_hist, "last_error", "") or "unknown"
                    print("fc_history: still not ready after fetch — play skipped (%s)" % err)
                    try:
                        _show_fetch_banner("NO DATA")
                    except Exception:
                        pass
            # Do not chain into refresh/forecast in the same turn after a play request
            return

        if fc_fcst is not None and fc_fcst.play_pending():
            fc_fcst.clear_play_pending()
            if fc_fcst.ready:
                print("fc_forecast: play (packed buffer ready)")
                _dim_and_play_pack(fc_fcst)
            else:
                _history_busy = True
                try:
                    print("fc_forecast: not ready — fetching before play…")
                    _do_fetch("FETCH FCST", fc_fcst)
                finally:
                    _history_busy = False
                if fc_fcst.ready:
                    _dim_and_play_pack(fc_fcst)
                else:
                    print("fc_forecast: still not ready after fetch — play skipped (%s)" % (
                        getattr(fc_fcst, "last_error", "") or "unknown"
                    ))
                    try:
                        _show_fetch_banner("NO DATA")
                    except Exception:
                        pass
            return

        # --- Background refresh only when nothing is waiting to play ---
        if fc_hist is not None and fc_hist.refresh_pending():
            fc_hist.clear_refresh_pending()
            _history_busy = True
            try:
                print("fc_history: refresh starting…")
                _do_fetch("FETCHING", fc_hist)
            finally:
                _history_busy = False
            # Play may have been queued during refresh — honor it before more fetching
            if fc_hist.play_pending() or (fc_fcst is not None and fc_fcst.play_pending()):
                service_history_pending()
                return
        if fc_fcst is not None and fc_fcst.refresh_pending():
            fc_fcst.clear_refresh_pending()
            _history_busy = True
            try:
                print("fc_forecast: refresh starting…")
                _do_fetch("FETCH FCST", fc_fcst)
            finally:
                _history_busy = False
            if (fc_hist is not None and fc_hist.play_pending()) or (
                fc_fcst is not None and fc_fcst.play_pending()
            ):
                service_history_pending()
                return

    def _history_play_poll():
        """During strip playback: keep :8080 alive; do not start another play/fetch."""
        service_ota_http_and_button(run_pending_history=False, allow_button_actions=False)

    def service_ota_http_and_button(run_pending_history=True, allow_button_actions=True):
        global update_socket, _ota_rebind_after, _ota_button_prev
        global _ota_btn_down_ms, _ota_btn_pending_hold_ms, _ota_btn_ignore_until_ms, _ota_btn_hold_hint
        global _history_trigger_prev, _history_trigger_ignore_until_ms, _history_busy, _play_banner_label
        """OTA button + port 8080 + history trigger/pending."""
        # PIR / extra trigger: edge -> play PAST 24h (not FUTURE; use hold button for that)
        if (
            allow_button_actions
            and history_trigger is not None
            and fc_hist is not None
            and not _history_busy
        ):
            try:
                now_ms = time.ticks_ms()
                v = history_trigger.value()
                armed = not (
                    _history_trigger_ignore_until_ms
                    and time.ticks_diff(now_ms, _history_trigger_ignore_until_ms) < 0
                )
                if HISTORY_TRIGGER_ACTIVE_HIGH:
                    edged = _history_trigger_prev == 0 and v == 1
                else:
                    edged = _history_trigger_prev == 1 and v == 0
                if edged and armed:
                    print("History trigger: PAST play requested")
                    _play_banner_label = "PAST"
                    fc_hist.request_play()
                    _history_trigger_ignore_until_ms = time.ticks_add(
                        now_ms, HISTORY_TRIGGER_COOLDOWN_MS
                    )
                _history_trigger_prev = v
            except Exception:
                pass
        if update_button is not None:
            now = time.ticks_ms()
            install_update = False
            play_past = False
            play_future = False

            try:
                btn_v = update_button.value()
            except Exception:
                btn_v = 1

            # Poll backup when IRQ misses an edge (still respects bounce lockout)
            if not (
                _ota_btn_ignore_until_ms
                and time.ticks_diff(now, _ota_btn_ignore_until_ms) < 0
            ):
                if _ota_button_prev == 1 and btn_v == 0 and not _ota_btn_down_ms:
                    _ota_btn_down_ms = now
                elif _ota_button_prev == 0 and btn_v == 1 and _ota_btn_down_ms:
                    held = time.ticks_diff(now, _ota_btn_down_ms)
                    _ota_btn_down_ms = 0
                    _ota_btn_ignore_until_ms = time.ticks_add(now, UPDATE_BUTTON_BOUNCE_MS)
                    if held >= UPDATE_BUTTON_BOUNCE_MS and not _ota_btn_pending_hold_ms:
                        _ota_btn_pending_hold_ms = held
            _ota_button_prev = btn_v

            # Live hold feedback while pressed (OTA → PAST → FUTURE)
            if (
                allow_button_actions
                and not _history_busy
                and _ota_btn_down_ms
                and btn_v == 0
            ):
                show_button_hold_hint(
                    _hold_hint_for_ms(time.ticks_diff(now, _ota_btn_down_ms))
                )
            elif _ota_btn_hold_hint and (
                btn_v == 1 or not _ota_btn_down_ms or _ota_btn_pending_hold_ms or not allow_button_actions
            ):
                show_button_hold_hint(None)

            held_ms = _ota_btn_pending_hold_ms
            if held_ms:
                _ota_btn_pending_hold_ms = 0
                show_button_hold_hint(None)
                print("Button: hold %d ms" % held_ms)
                if not allow_button_actions:
                    print("Button: ignored (nested poll)")
                elif held_ms < UPDATE_BUTTON_TAP_MS:
                    if _history_busy:
                        print("Button: OTA ignored (busy fetching/playing)")
                    else:
                        install_update = True
                elif held_ms < UPDATE_BUTTON_PAST_MS:
                    # Always queue play — even during an in-progress refresh
                    play_past = True
                else:
                    play_future = True

            if play_past:
                if fc_hist is not None:
                    print(
                        "Button: hold — history play requested (ready=%s busy=%s)"
                        % (bool(fc_hist.ready), bool(_history_busy))
                    )
                    _play_banner_label = "PAST"
                    fc_hist.request_play()
                else:
                    print("Button: hold — fc_history not loaded")
            if play_future:
                if fc_fcst is not None:
                    print(
                        "Button: hold — forecast play requested (ready=%s busy=%s)"
                        % (bool(fc_fcst.ready), bool(_history_busy))
                    )
                    _play_banner_label = "FUTURE"
                    fc_fcst.request_play()
                else:
                    print("Button: hold — fc_forecast not loaded")

            if install_update:
                try:
                    import updater
                    print("Button: tap — checking / installing OTA…")
                    if update_available and update_info:
                        updater.install_pending_update(update_info)
                    else:
                        has_update, version_info = updater.check_for_new_version(FIRMWARE_VERSION)
                        if has_update and version_info:
                            updater.install_pending_update(version_info)
                        else:
                            print("OTA button: no update available (device already current or version.json unreachable)")
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
                # Finish reading POST body (update-config JSON often exceeds first 2048-byte chunk)
                try:
                    _cl = None
                    for _hline in req.split("\r\n"):
                        if _hline.lower().startswith("content-length:"):
                            _cl = int(_hline.split(":", 1)[1].strip())
                            break
                    if _cl is not None and req.lstrip().upper().startswith("POST"):
                        _bs = req.find("\r\n\r\n") + 4
                        if _bs >= 4:
                            _body = req[_bs:]
                            while len(_body) < _cl:
                                _chunk = conn.recv(min(1024, _cl - len(_body)))
                                if not _chunk:
                                    break
                                _body += _chunk.decode("utf-8", "ignore")
                            req = req[:_bs] + _body[:_cl]
                except Exception as _recv_ex:
                    print("OTA HTTP body read:", _recv_ex)
                first = req.split("\n")[0].strip() if req else ""
                if first.startswith("GET ") and "/config" in first:
                    try:
                        _cfg_body = _http_wifi_config_json_body()
                        _cfg_b = _cfg_body.encode("utf-8")
                        conn.send(
                            b"HTTP/1.1 200 OK\r\nContent-Type: application/json; charset=utf-8\r\nConnection: close\r\n"
                        )
                        conn.send(("Content-Length: %d\r\n\r\n" % len(_cfg_b)).encode("ascii"))
                        conn.sendall(_cfg_b)
                    except Exception as _cfg_ex:
                        print("OTA GET /config error:", _cfg_ex)
                        try:
                            conn.send(b"HTTP/1.1 500\r\nConnection: close\r\n\r\n")
                        except Exception:
                            pass
                    try:
                        conn.close()
                    except Exception:
                        pass
                    return
                if first.startswith("GET ") and "/history" in first:
                    try:
                        if fc_hist is None:
                            body = json.dumps({"ok": False, "ready": False, "state": "error", "error": "fc_history not loaded"})
                        else:
                            body = json.dumps(fc_hist.status_dict())
                        b = body.encode("utf-8")
                        conn.send(
                            b"HTTP/1.1 200 OK\r\nContent-Type: application/json; charset=utf-8\r\nConnection: close\r\n"
                        )
                        conn.send(("Content-Length: %d\r\n\r\n" % len(b)).encode("ascii"))
                        conn.sendall(b)
                    except Exception as _hs_ex:
                        print("GET /history error:", _hs_ex)
                        try:
                            conn.send(b"HTTP/1.1 500\r\nConnection: close\r\n\r\n")
                        except Exception:
                            pass
                    try:
                        conn.close()
                    except Exception:
                        pass
                    return
                if first.startswith("POST ") and "/history-refresh" in first:
                    try:
                        if fc_hist is None:
                            _http_send_json_response(conn, False, "fc_history not loaded")
                        else:
                            fc_hist.request_refresh()
                            _http_send_json_response(conn, True, "History refresh queued")
                        try:
                            conn.close()
                        except Exception:
                            pass
                    except Exception as _hr_ex:
                        print("POST /history-refresh error:", _hr_ex)
                        try:
                            conn.close()
                        except Exception:
                            pass
                    try:
                        service_history_pending()
                    except Exception as _shp_e:
                        print("service_history_pending:", _shp_e)
                    return
                if first.startswith("POST ") and "/history-settings" in first:
                    try:
                        bs = req.find("\r\n\r\n") + 4
                        body_raw = req[bs:].strip() if bs >= 4 else "{}"
                        settings = json.loads(body_raw) if body_raw else {}
                        loops = settings.get("loops") if isinstance(settings, dict) else None
                        if fc_hist is None and fc_fcst is None:
                            _http_send_json_response(conn, False, "history/forecast not loaded")
                        elif loops is None:
                            _http_send_json_response(conn, False, "Missing loops")
                        elif _save_history_replay_loops(loops):
                            _http_send_json_response(
                                conn, True, "Button replay count saved: %d" % HISTORY_REPLAY_LOOPS
                            )
                        else:
                            _http_send_json_response(conn, False, "Could not save replay count")
                    except Exception as _hse:
                        print("POST /history-settings error:", _hse)
                        try:
                            _http_send_json_response(conn, False, "Invalid history settings")
                        except Exception:
                            pass
                    try:
                        conn.close()
                    except Exception:
                        pass
                    return
                if first.startswith("POST ") and "/history-play" in first:
                    try:
                        frame_ms = None
                        loops = None
                        try:
                            bs = req.find("\r\n\r\n") + 4
                            body_raw = req[bs:].strip() if bs >= 4 else ""
                            if body_raw:
                                j = json.loads(body_raw)
                                if isinstance(j, dict):
                                    if "frame_ms" in j:
                                        frame_ms = j.get("frame_ms")
                                    if "loops" in j:
                                        loops = j.get("loops")
                                    elif "repeat" in j:
                                        loops = j.get("repeat")
                        except Exception:
                            pass
                        if fc_hist is None:
                            _http_send_json_response(conn, False, "fc_history not loaded")
                        else:
                            if loops is not None:
                                _save_history_replay_loops(loops)
                            fc_hist.request_play(frame_ms=frame_ms, loops=loops)
                            _http_send_json_response(conn, True, "History play queued")
                        try:
                            conn.close()
                        except Exception:
                            pass
                    except Exception as _hp_ex:
                        print("POST /history-play error:", _hp_ex)
                        try:
                            conn.close()
                        except Exception:
                            pass
                    try:
                        service_history_pending()
                    except Exception as _shp_e:
                        print("service_history_pending:", _shp_e)
                    return
                if first.startswith("GET ") and "/forecast" in first:
                    try:
                        if fc_fcst is None:
                            body = json.dumps({"ok": False, "ready": False, "state": "error", "error": "fc_forecast not loaded"})
                        else:
                            body = json.dumps(fc_fcst.status_dict())
                        b = body.encode("utf-8")
                        conn.send(
                            b"HTTP/1.1 200 OK\r\nContent-Type: application/json; charset=utf-8\r\nConnection: close\r\n"
                        )
                        conn.send(("Content-Length: %d\r\n\r\n" % len(b)).encode("ascii"))
                        conn.sendall(b)
                    except Exception as _fs_ex:
                        print("GET /forecast error:", _fs_ex)
                        try:
                            conn.send(b"HTTP/1.1 500\r\nConnection: close\r\n\r\n")
                        except Exception:
                            pass
                    try:
                        conn.close()
                    except Exception:
                        pass
                    return
                if first.startswith("POST ") and "/forecast-refresh" in first:
                    try:
                        if fc_fcst is None:
                            _http_send_json_response(conn, False, "fc_forecast not loaded")
                        else:
                            fc_fcst.request_refresh()
                            _http_send_json_response(conn, True, "Forecast refresh queued")
                        try:
                            conn.close()
                        except Exception:
                            pass
                    except Exception as _fr_ex:
                        print("POST /forecast-refresh error:", _fr_ex)
                        try:
                            conn.close()
                        except Exception:
                            pass
                    try:
                        service_history_pending()
                    except Exception as _shp_e:
                        print("service_history_pending:", _shp_e)
                    return
                if first.startswith("POST ") and "/forecast-play" in first:
                    try:
                        frame_ms = None
                        loops = None
                        try:
                            bs = req.find("\r\n\r\n") + 4
                            body_raw = req[bs:].strip() if bs >= 4 else ""
                            if body_raw:
                                j = json.loads(body_raw)
                                if isinstance(j, dict):
                                    if "frame_ms" in j:
                                        frame_ms = j.get("frame_ms")
                                    if "loops" in j:
                                        loops = j.get("loops")
                                    elif "repeat" in j:
                                        loops = j.get("repeat")
                        except Exception:
                            pass
                        if fc_fcst is None:
                            _http_send_json_response(conn, False, "fc_forecast not loaded")
                        else:
                            if loops is not None:
                                _save_history_replay_loops(loops)
                            fc_fcst.request_play(frame_ms=frame_ms, loops=loops)
                            _http_send_json_response(conn, True, "Forecast play queued")
                        try:
                            conn.close()
                        except Exception:
                            pass
                    except Exception as _fp_ex:
                        print("POST /forecast-play error:", _fp_ex)
                        try:
                            conn.close()
                        except Exception:
                            pass
                    try:
                        service_history_pending()
                    except Exception as _shp_e:
                        print("service_history_pending:", _shp_e)
                    return
                if first.startswith("POST ") and "/update-config" in first:
                    try:
                        bs = req.find("\r\n\r\n") + 4
                        body_raw = req[bs:].strip() if bs >= 4 else "{}"
                        updates = json.loads(body_raw) if body_raw else {}
                        _http_apply_post_update_config(updates)
                        do_rb = updates.get("reboot", True)
                        if isinstance(do_rb, str):
                            do_rb = do_rb.lower() in ("true", "1", "yes")
                        if do_rb:
                            _http_send_json_response(conn, True, "Settings updated, rebooting")
                            try:
                                conn.close()
                            except Exception:
                                pass
                            time.sleep(2)
                            machine.reset()
                        else:
                            _http_send_json_response(conn, True, "Settings saved (no reboot)")
                            try:
                                conn.close()
                            except Exception:
                                pass
                    except Exception as _uc_ex:
                        print("LAN POST /update-config error:", _uc_ex)
                        try:
                            _http_send_json_response(conn, False, str(_uc_ex))
                        except Exception:
                            try:
                                conn.send(b"HTTP/1.1 500\r\nConnection: close\r\n\r\n")
                            except Exception:
                                pass
                        try:
                            conn.close()
                        except Exception:
                            pass
                    return
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
                    return
                # Browser pages (same as AP wifi_manager UI) on LAN :8080
                if first.startswith("GET ") and "/page/airports" in first:
                    try:
                        import wifi_manager as _wm
                        _http_send_html(conn, _wm.get_html_airports_page())
                    except Exception as _pg_e:
                        print("GET /page/airports:", _pg_e)
                        try:
                            conn.send(b"HTTP/1.1 500\r\nConnection: close\r\n\r\n")
                        except Exception:
                            pass
                    try:
                        conn.close()
                    except Exception:
                        pass
                    return
                if first.startswith("GET ") and "/page/weather" in first:
                    try:
                        import wifi_manager as _wm
                        _http_send_html(conn, _wm.get_html_weather_page())
                    except Exception as _pg_e:
                        print("GET /page/weather:", _pg_e)
                        try:
                            conn.send(b"HTTP/1.1 500\r\nConnection: close\r\n\r\n")
                        except Exception:
                            pass
                    try:
                        conn.close()
                    except Exception:
                        pass
                    return
                if first.startswith("GET ") and "/page/help" in first:
                    try:
                        import wifi_manager as _wm
                        _http_send_html(conn, _wm.get_html_help_page())
                    except Exception as _pg_e:
                        print("GET /page/help:", _pg_e)
                        try:
                            conn.send(b"HTTP/1.1 500\r\nConnection: close\r\n\r\n")
                        except Exception:
                            pass
                    try:
                        conn.close()
                    except Exception:
                        pass
                    return
                if first.startswith("GET ") and "/page/update" in first:
                    try:
                        _http_send_html(conn, UPDATE_PAGE_HTML)
                    except Exception as _pg_e:
                        print("GET /page/update:", _pg_e)
                    try:
                        conn.close()
                    except Exception:
                        pass
                    return
                if first.startswith("GET ") and "/airports" in first:
                    try:
                        with open(AIRPORT_FILE, "r") as f:
                            content = f.read()
                        b = content.encode("utf-8")
                        conn.send(
                            b"HTTP/1.1 200 OK\r\nContent-Type: text/plain; charset=utf-8\r\nConnection: close\r\n"
                        )
                        conn.send(("Content-Length: %d\r\n\r\n" % len(b)).encode("ascii"))
                        conn.sendall(b)
                    except Exception:
                        conn.send(b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
                    try:
                        conn.close()
                    except Exception:
                        pass
                    return
                if first.startswith("POST ") and "/airports" in first:
                    try:
                        bs = req.find("\r\n\r\n") + 4
                        body = req[bs:].strip() if bs >= 4 else ""
                        lines = [line.strip().upper() for line in body.replace("\r", "").split("\n")]
                        with open(AIRPORT_FILE, "w") as f:
                            f.write("\n".join(lines))
                        print("LAN :8080 saved", len(lines), "airports")
                        _http_send_json_response(conn, True, "Saved %d airports" % len(lines))
                    except Exception as _ap_e:
                        print("POST /airports:", _ap_e)
                        try:
                            _http_send_json_response(conn, False, str(_ap_e))
                        except Exception:
                            pass
                    try:
                        conn.close()
                    except Exception:
                        pass
                    return
                if first.startswith("POST ") and "/reboot" in first:
                    try:
                        _http_send_json_response(conn, True, "Rebooting")
                    except Exception:
                        pass
                    try:
                        conn.close()
                    except Exception:
                        pass
                    time.sleep(1)
                    machine.reset()
                    return
                if first.startswith("POST ") and "/configure" in first:
                    # Browser Setup form (same as AP) — save display and/or WiFi, then reboot
                    try:
                        import wifi_manager as _wm
                        parsed = _wm.parse_request_data(req)
                        (
                            ssid, password, display_type, led_matrix_brightness, led_matrix_pin,
                            min_brightness, max_brightness, batch_size, weather_enabled, matrix_only,
                            matrix_scroll_category, scroll_speed, matrix_wiring, scroll_pause_before,
                            cycle_delay, num_leds, led_pin, sleep_schedule,
                        ) = parsed
                        _plc = _wm.optional_physical_led_count_from_request(req)
                        ok = False
                        if ssid and password:
                            ok = _wm.save_wifi_config(
                                ssid, password, display_type, led_matrix_brightness, led_matrix_pin,
                                min_brightness, max_brightness, batch_size, weather_enabled, matrix_only,
                                matrix_scroll_category, scroll_speed, matrix_wiring, scroll_pause_before,
                                cycle_delay, num_leds=num_leds, led_pin=led_pin, physical_led_count=_plc,
                                sleep_schedule=sleep_schedule,
                            )
                        else:
                            ok = _wm.update_display_config_only(
                                display_type, led_matrix_brightness, led_matrix_pin, min_brightness,
                                max_brightness, batch_size, matrix_only, matrix_scroll_category,
                                scroll_speed, matrix_wiring, scroll_pause_before, cycle_delay,
                                num_leds=num_leds, led_pin=led_pin, physical_led_count=_plc,
                                sleep_schedule=sleep_schedule,
                            )
                        if ok:
                            _http_send_html(
                                conn,
                                _wm.get_html_display_saved_page(True, "Settings saved. Rebooting..."),
                            )
                            try:
                                conn.close()
                            except Exception:
                                pass
                            time.sleep(2)
                            machine.reset()
                        else:
                            _http_send_html(
                                conn,
                                _wm.get_html_display_saved_page(False, "Failed to save settings."),
                            )
                            try:
                                conn.close()
                            except Exception:
                                pass
                    except Exception as _cfg_e:
                        print("LAN POST /configure:", _cfg_e)
                        try:
                            conn.send(b"HTTP/1.1 500\r\nConnection: close\r\n\r\n")
                            conn.close()
                        except Exception:
                            pass
                    return
                # Default GET / → Setup page (AP-style UI on LAN)
                if first.startswith("GET "):
                    try:
                        import wifi_manager as _wm
                        _http_send_html(conn, _wm.get_html_setup_page())
                    except Exception as _home_e:
                        print("GET / setup page:", _home_e)
                        try:
                            _http_send_html(conn, UPDATE_PAGE_HTML)
                        except Exception:
                            pass
                    try:
                        conn.close()
                    except Exception:
                        pass
                    return
                try:
                    _http_send_html(conn, UPDATE_PAGE_HTML)
                    conn.close()
                except Exception:
                    pass
            except OSError:
                pass
            except Exception as e:
                print("OTA server handle:", e)
        # After HTTP/button poll: hourly queue + run pending history/forecast work
        if run_pending_history:
            try:
                maybe_queue_hourly_history_refresh()
            except Exception as _hr_auto_e:
                print("history auto-refresh:", _hr_auto_e)
            try:
                maybe_queue_hourly_forecast_refresh()
            except Exception as _fr_auto_e:
                print("forecast auto-refresh:", _fr_auto_e)
            try:
                service_history_pending()
            except Exception as _shp_e:
                print("service_history_pending:", _shp_e)

    _ota_service_hook = service_ota_http_and_button

    def sleep_with_ota_poll(total_seconds, run_pending_history=True):
        remaining = float(total_seconds)
        while remaining > 0:
            service_ota_http_and_button(run_pending_history=run_pending_history)
            chunk = 0.25 if remaining >= 0.25 else remaining
            time.sleep(chunk)
            remaining -= chunk

    current_ldr_brightness = map_ldr_to_brightness(read_ldr_value(), MIN_BRIGHTNESS, MAX_BRIGHTNESS)
    last_ldr_refresh_time = time.time()
    bulk_ok = False

    def _paint_bulk_chunk(results, start, end):
        """Light LEDs as each bulk METAR chunk arrives (map fills progressively)."""
        if MATRIX_ONLY or led is None:
            return
        n = min(len(airports), len(results), STRIP_ACTIVE_LEDS)
        a = max(0, min(int(start), n))
        b = max(a, min(int(end), n))
        any_set = False
        for index in range(a, b):
            fc, raw = results[index]
            if fc and airports[index] and str(airports[index]).strip():
                update_wx_interest(index, raw)
                set_led_color(led, fc, index, MIN_BRIGHTNESS, MAX_BRIGHTNESS)
                any_set = True
        if any_set:
            led.write()
            update_data_success()
            print("Bulk METAR: lit LEDs %d–%d" % (a, b - 1))

    if not MATRIX_ONLY:
        print("Startup: bulk METAR fetch for flight categories…")
        # Do not service history during bulk — packs wait until after second pass
        bulk_results = fetch_all_metars_once(airports, on_chunk=_paint_bulk_chunk)
        if bulk_results:
            n = min(len(airports), len(bulk_results), STRIP_ACTIVE_LEDS)
            for index in range(n):
                if (bulk_results[index][0] is None or bulk_results[index][1] is None) and airports[index] and airports[index].strip():
                    fc, raw = get_metar_data_with_retry(airports[index], quick=True)
                    if fc is not None:
                        bulk_results[index] = (fc, raw or bulk_results[index][1])
                        update_wx_interest(index, raw)
                        set_led_color(led, fc, index, MIN_BRIGHTNESS, MAX_BRIGHTNESS)
                        update_data_success()
                        led.write()
                    gc.collect()
                    time.sleep(0.02)
                if (index & 7) == 0:
                    service_ota_http_and_button(run_pending_history=False)
            any_set = False
            for index in range(n):
                fc, raw = bulk_results[index]
                if fc and airports[index] and airports[index].strip():
                    update_wx_interest(index, raw)
                    set_led_color(led, fc, index, MIN_BRIGHTNESS, MAX_BRIGHTNESS)
                    any_set = True
                else:
                    logical_colors[index] = (0, 0, 0)
                    led[index] = (0, 0, 0)
            led.write()
            if any_set:
                update_data_success()
                bulk_ok = True
                print("All airport LEDs set from bulk fetch (displaying 1s)")
                sleep_with_ota_poll(1, run_pending_history=False)
            clear_unused_strip_leds(len(airports))
    startup_sleep_hit = False
    if not bulk_ok:
        if not MATRIX_ONLY:
            startup_sleep_hit = process_airports_in_batches(
                airports,
                process_first_pass,
                description="First pass",
                poll_callback=lambda: service_ota_http_and_button(run_pending_history=False),
            )
    # Always run second-pass WX tour (unless sleep), then history/forecast packs after that.
    if not startup_sleep_hit:
        startup_sleep_hit = process_airports_in_batches(
            airports,
            process_second_pass,
            description="Second pass",
            poll_callback=lambda: service_ota_http_and_button(run_pending_history=False),
        )
    if startup_sleep_hit:
        print("Startup METAR passes paused for sleep window; entering scheduler loop")
    clear_unused_strip_leds(len(airports))

    # Past/future packs ONLY after second pass (or after sleep defer) — not as soon as strip lights.
    _defer_hist_fcst = bool(startup_sleep_hit) or sleep_applies_to_displays_now()
    if fc_hist is not None:
        fc_hist.request_refresh()
        print(
            "fc_history: startup 24h pack queued; auto-refresh every %ds%s"
            % (
                HISTORY_REFRESH_INTERVAL_S,
                " (deferred until wake)" if _defer_hist_fcst else " (after second pass)",
            )
        )
        if not _defer_hist_fcst:
            try:
                service_history_pending()
            except Exception as _ih_e:
                print("fc_history initial pack:", _ih_e)
    if fc_fcst is not None:
        fc_fcst.request_refresh()
        print(
            "fc_forecast: startup TAF pack queued; auto-refresh every %ds%s"
            % (
                FORECAST_REFRESH_INTERVAL_S,
                " (deferred until wake)" if _defer_hist_fcst else " (after second pass)",
            )
        )
        if not _defer_hist_fcst:
            try:
                service_history_pending()
            except Exception as _if_e:
                print("fc_forecast initial pack:", _if_e)

    # NTP sync once for sleep schedule (local_time() = gmtime(utc + offset))
    ntptime_synced = False
    def maybe_sync_ntp():
        global ntptime_synced
        if ntptime_synced or not (SLEEP_ENABLED or WEEKEND_MODE_ENABLED):
            return
        if _try_ntp_sync():
            ntptime_synced = True
            t = local_time()
            print("NTP time synced for sleep schedule")
            print("Current time (local): %04d-%02d-%02d %02d:%02d:%02d" % (t[0], t[1], t[2], t[3], t[4], t[5]))
            _try_arm_sleep_boot_override()

    def clear_displays_for_sleep():
        try:
            if SLEEP_LEDS and led is not None:
                n = min(NUM_LEDS, len(led))
                for i in range(n):
                    logical_colors[i] = (0, 0, 0)
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
    _last_sleep_diag_minute = None

    while True:
        maybe_sync_ntp()
        ensure_wifi_connected()
        t_diag = local_time()
        in_sleep = sleep_applies_to_displays_now()
        if _last_sleep_diag_minute != t_diag[4]:
            _last_sleep_diag_minute = t_diag[4]
            print(
                "Sleep check: daily=%s weekend_mode=%s ntp_trusted=%s now=%02d:%02d sleep_at=%02d:%02d wake_at=%02d:%02d blk_off=wd%d@%02d:%02d blk_on=wd%d@%02d:%02d sched_off=%s effective_sleep=%s boot_ovr=%s"
                % (
                    SLEEP_ENABLED,
                    WEEKEND_MODE_ENABLED,
                    _sleep_clock_trusted,
                    t_diag[3],
                    t_diag[4],
                    SLEEP_AT_HOUR,
                    SLEEP_AT_MIN,
                    WAKE_AT_HOUR,
                    WAKE_AT_MIN,
                    WEEKEND_OFF_WEEKDAY,
                    WEEKEND_OFF_HOUR,
                    WEEKEND_OFF_MINUTE,
                    WEEKEND_ON_WEEKDAY,
                    WEEKEND_ON_HOUR,
                    WEEKEND_ON_MINUTE,
                    is_combined_scheduled_display_sleep_now(),
                    in_sleep,
                    _sleep_boot_override_active,
                )
            )
        _strip_dark_for_sleep = bool(in_sleep and SLEEP_LEDS)
        # Keep :8080 / button alive during sleep, but do not start history/forecast packs
        service_ota_http_and_button(run_pending_history=not in_sleep)
        if not in_sleep:
            check_ldr_and_refresh()
        if in_sleep:
            clear_displays_for_sleep()
            if not displays_sleeping:
                displays_sleeping = True
                pause_no_data_watchdog_for_sleep()
                t = local_time()
                _u = _next_local_tuple_combined_sleep_false_strictly_after_now()
                if _u is not None:
                    print(
                        "Current time (local): %04d-%02d-%02d %02d:%02d:%02d - Display sleep: next wake %04d-%02d-%02d %02d:%02d"
                        % (t[0], t[1], t[2], t[3], t[4], t[5], _u[0], _u[1], _u[2], _u[3], _u[4])
                    )
                else:
                    print(
                        "Current time (local): %04d-%02d-%02d %02d:%02d:%02d - Display sleep: on (no next wake computed)"
                        % (t[0], t[1], t[2], t[3], t[4], t[5])
                    )
            sleep_with_ota_poll(CYCLE_DELAY, run_pending_history=False)
            continue
        just_woke_from_sleep = displays_sleeping
        displays_sleeping = False
        if just_woke_from_sleep:
            # After display sleep, ensure strip updates are not blocked and LDR/strip are resynced (long sleep skipped LDR poll).
            _strip_dark_for_sleep = False
            if not MATRIX_ONLY:
                try:
                    current_ldr_brightness = map_ldr_to_brightness(read_ldr_value(), MIN_BRIGHTNESS, MAX_BRIGHTNESS)
                    last_ldr_refresh_time = time.time()
                    refresh_strip_using_ldr()
                except Exception as _wake_strip_e:
                    print("Wake strip resync:", _wake_strip_e)
            t = local_time()
            print("Display wake: %02d:%02d - running full refresh from first pass" % (t[3], t[4]))
            wake_sleep_hit = False
            if not MATRIX_ONLY:
                wake_sleep_hit = process_airports_in_batches(
                    airports,
                    process_first_pass,
                    description="Wake first pass",
                    poll_callback=service_ota_http_and_button,
                )
            if not wake_sleep_hit:
                wake_sleep_hit = process_airports_in_batches(
                    airports,
                    process_second_pass,
                    description="Wake second pass",
                    poll_callback=service_ota_http_and_button,
                )
            if wake_sleep_hit:
                print("Wake refresh paused: re-entered sleep window")
                continue
            clear_unused_strip_leds(len(airports))
            if not MATRIX_ONLY:
                try:
                    refresh_strip_using_ldr()
                except Exception as _wake_strip_e2:
                    print("Wake strip refresh after batches:", _wake_strip_e2)
            update_data_success()
            t = local_time()
            print("Wake refresh complete at %02d:%02d - next cycle in %ds" % (t[3], t[4], CYCLE_DELAY))
            sleep_with_ota_poll(CYCLE_DELAY)
            continue
        check_data_timeout()
        if no_data_warning_active and DISPLAY_TYPE == "LED_MATRIX":
            print("NO DATA warning active - displaying warning and checking connection...")
            ensure_wifi_connected()
            display_no_data_warning()
            sleep_with_ota_poll(5)
            continue
        elif no_data_warning_active and DISPLAY_TYPE != "LED_MATRIX":
            print("NO DATA warning active (no matrix); reboot pending")
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
            batch_data_received, batch_sleep_hit = process_main_loop_batch(
                batch_airports, batch_start, poll_callback=service_ota_http_and_button
            )
            any_data_received = any_data_received or batch_data_received
            if batch_sleep_hit:
                print("Main loop batch paused: entered sleep window")
                break
            print(f"Free memory after batch: {gc.mem_free()} bytes")
            if batch_num < num_batches - 1:
                print(f"Waiting {BATCH_DELAY} seconds before next batch...")
                sleep_with_ota_poll(BATCH_DELAY)
        if sleep_applies_to_displays_now():
            clear_displays_for_sleep()
            displays_sleeping = True
            t = local_time()
            _u2 = _next_local_tuple_combined_sleep_false_strictly_after_now()
            if _u2 is not None:
                print(
                    "Current time (local): %04d-%02d-%02d %02d:%02d:%02d - Display sleep: next wake %04d-%02d-%02d %02d:%02d"
                    % (t[0], t[1], t[2], t[3], t[4], t[5], _u2[0], _u2[1], _u2[2], _u2[3], _u2[4])
                )
            else:
                print(
                    "Current time (local): %04d-%02d-%02d %02d:%02d:%02d - Display sleep: on (no next wake computed)"
                    % (t[0], t[1], t[2], t[3], t[4], t[5])
                )
            sleep_with_ota_poll(CYCLE_DELAY)
            continue
        clear_unused_strip_leds(len(airports))
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

