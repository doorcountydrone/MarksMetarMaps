# 24-hour flight-category history: pack to 2 bits/hour, play on strip.
# Categories: 00=VFR, 01=MVFR, 10=IFR, 11=LIFR. 24 hours = 6 bytes/airport.

import gc
import urequests
import utime as time

HOURS = 24
BYTES_PER_AIRPORT = 6  # 24 * 2 bits / 8
FC_TO_BITS = {"VFR": 0, "MVFR": 1, "IFR": 2, "LIFR": 3}
FC_FROM_BITS = ("VFR", "MVFR", "IFR", "LIFR")
# RGB matches main.py FLIGHT_COLOR_MAP / set_led_color
FC_RGB = (
    (0, 255, 0),    # VFR
    (0, 0, 255),    # MVFR
    (255, 0, 0),    # IFR
    (255, 0, 130),  # LIFR
)


def _set_bits(buf, airport_idx, hour, code):
    """Pack 2-bit category into buffer. hour 0=oldest … 23=newest."""
    if hour < 0 or hour >= HOURS:
        return
    byte_i = airport_idx * BYTES_PER_AIRPORT + (hour >> 2)
    shift = (3 - (hour & 3)) * 2
    mask = 0x03 << shift
    buf[byte_i] = (buf[byte_i] & ~mask) | ((code & 3) << shift)


def _get_bits(buf, airport_idx, hour):
    byte_i = airport_idx * BYTES_PER_AIRPORT + (hour >> 2)
    shift = (3 - (hour & 3)) * 2
    return (buf[byte_i] >> shift) & 3


def _metar_obs_time(raw_line):
    """(day, minutes_since_midnight_utc) or (0, 0)."""
    if not raw_line or not isinstance(raw_line, str):
        return (0, 0)
    parts = raw_line.strip().upper().split()
    for tok in parts:
        if len(tok) >= 7 and tok.endswith("Z") and tok[:2].isdigit() and tok[2:6].isdigit():
            try:
                day = int(tok[:2])
                hour = int(tok[2:4])
                mins = int(tok[4:6])
                return (day, hour * 60 + mins)
            except ValueError:
                pass
    return (0, 0)


def _parse_flight_category(raw_text):
    """Minimal VFR/MVFR/IFR/LIFR from raw METAR (same rules as main.py)."""
    if not raw_text or not isinstance(raw_text, str):
        return ""
    raw = raw_text.strip().upper()
    vis_m = 10.0
    ceiling_ft = 10000
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
        break
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
        ceiling_ft = 5000
    if ceiling_ft < 500 or vis_m < 1.0:
        return "LIFR"
    if ceiling_ft < 1000 or vis_m < 3.0:
        return "IFR"
    if ceiling_ft < 3000 or vis_m < 5.0:
        return "MVFR"
    return "VFR"


def _hours_ago(obs_day, obs_mins, now_day, now_mins):
    """Approximate age in hours (0..~48). None if unusable."""
    if obs_day <= 0:
        return None
    if obs_day == now_day:
        ago = now_mins - obs_mins
    else:
        # Treat as previous day (handles month wrap loosely within 24h window)
        ago = (now_mins + 24 * 60) - obs_mins
        if obs_day != now_day - 1 and not (now_day == 1 and obs_day >= 28):
            if ago < 0 or ago > 30 * 60:
                return None
    if ago < -30:
        return None
    if ago < 0:
        ago = 0
    return ago / 60.0


class FlightCategoryHistory:
    """Packed 24h category timeline for the strip."""

    def __init__(self, max_airports=120):
        self.max_airports = max_airports
        self.buf = bytearray(max_airports * BYTES_PER_AIRPORT)
        self.n_airports = 0
        self.ready = False
        self.state = "idle"  # idle | fetching | playing | error
        self.fetched_at = 0
        self.last_error = ""
        self.frame_ms = 1000  # default ~24s for 24 hourly frames
        self._play_pending = False
        self._refresh_pending = False

    def status_dict(self):
        return {
            "ok": True,
            "ready": bool(self.ready),
            "airports": int(self.n_airports),
            "hours": HOURS,
            "bytes": int(self.n_airports * BYTES_PER_AIRPORT),
            "fetched_at": int(self.fetched_at),
            "state": self.state,
            "error": self.last_error or "",
            "frame_ms": int(self.frame_ms),
        }

    def request_play(self, frame_ms=None):
        if frame_ms is not None:
            try:
                self.frame_ms = max(50, min(5000, int(frame_ms)))
            except (TypeError, ValueError):
                pass
        self._play_pending = True

    def request_refresh(self):
        self._refresh_pending = True

    def clear_play_pending(self):
        self._play_pending = False

    def clear_refresh_pending(self):
        self._refresh_pending = False

    def play_pending(self):
        return self._play_pending

    def refresh_pending(self):
        return self._refresh_pending

    def _fill_airport_from_raw_block(self, airport_idx, raw_block, now_day, now_mins):
        """Parse multi-line raw METARs into 24 hour buckets; carry-forward gaps."""
        slots = [-1] * HOURS  # -1 = empty
        if not raw_block:
            for h in range(HOURS):
                _set_bits(self.buf, airport_idx, h, 0)
            return
        for line in raw_block.split("\n"):
            line = line.strip()
            if not line:
                continue
            if not (line.startswith("METAR ") or line.startswith("SPECI ") or (
                len(line) >= 4 and line[0].isalpha() and " " in line
            )):
                # Accept ICAO-first lines without METAR prefix
                if len(line) < 8:
                    continue
            obs_day, obs_mins = _metar_obs_time(line)
            ago = _hours_ago(obs_day, obs_mins, now_day, now_mins)
            if ago is None or ago > 24.5:
                continue
            hour = 23 - int(ago)
            if hour < 0:
                hour = 0
            if hour >= HOURS:
                hour = HOURS - 1
            fc = _parse_flight_category(line)
            code = FC_TO_BITS.get(fc, 0)
            # Last obs in a bucket wins
            slots[hour] = code
        # Carry forward from oldest
        last = 0
        for h in range(HOURS):
            if slots[h] < 0:
                slots[h] = last
            else:
                last = slots[h]
            _set_bits(self.buf, airport_idx, h, slots[h])

    def fetch_and_pack(self, airports, poll_callback=None, limit=None, chunk_size=3):
        """
        Download hours=24 METARs in small multi-airport batches; pack into self.buf.
        airports: list of ICAO strings (blank = skip / all VFR zeros).
        limit: only first N slots (e.g. STRIP_ACTIVE_LEDS) — keeps fetch fast.
        chunk_size: airports per HTTPS request (small: 24h payloads are large).
        """
        self.state = "fetching"
        self.last_error = ""
        n = min(len(airports), self.max_airports)
        if limit is not None:
            try:
                n = min(n, max(0, int(limit)))
            except (TypeError, ValueError):
                pass
        self.n_airports = n
        # Keep previous ready buffer until this fetch succeeds (play still works if refresh fails)
        for i in range(n * BYTES_PER_AIRPORT):
            self.buf[i] = 0
        try:
            now = time.gmtime()
            now_day = now[2]
            now_mins = now[3] * 60 + now[4]
        except Exception:
            now_day = 1
            now_mins = 12 * 60
        ok_count = 0
        cs = max(1, min(5, int(chunk_size) if chunk_size else 3))
        try:
            chunk_start = 0
            while chunk_start < n:
                if poll_callback:
                    try:
                        poll_callback()
                    except Exception:
                        pass
                chunk_end = min(chunk_start + cs, n)
                # Unique non-blank IDs in this chunk (API ids=)
                id_list = []
                seen = {}
                for idx in range(chunk_start, chunk_end):
                    ap = airports[idx]
                    if not ap or not str(ap).strip():
                        for h in range(HOURS):
                            _set_bits(self.buf, idx, h, 0)
                        continue
                    apu = str(ap).strip().upper()
                    if apu not in seen:
                        seen[apu] = []
                        id_list.append(apu)
                    seen[apu].append(idx)
                if not id_list:
                    chunk_start = chunk_end
                    continue
                gc.collect()
                raw_block = ""
                ids = ",".join(id_list)
                url = (
                    "https://aviationweather.gov/api/data/metar?ids={}&hours=24&format=raw"
                    .format(ids)
                )
                got = False
                for attempt in range(2):
                    try:
                        print("fc_history: batch %d–%d (%s) try %d" % (
                            chunk_start, chunk_end - 1, ids, attempt + 1
                        ))
                        resp = urequests.get(url, timeout=10)
                        try:
                            raw_block = resp.text or ""
                        finally:
                            try:
                                resp.close()
                            except Exception:
                                pass
                        got = True
                        break
                    except Exception as e:
                        print("fc_history batch fetch: %s" % e)
                        gc.collect()
                        time.sleep_ms(200)
                if not got:
                    for apu, idxs in seen.items():
                        for idx in idxs:
                            for h in range(HOURS):
                                _set_bits(self.buf, idx, h, 0)
                    chunk_start = chunk_end
                    time.sleep_ms(50)
                    continue
                # Split response by station, then pack each airport index
                by_station = {}
                for line in raw_block.split("\n"):
                    line = line.strip()
                    if not line:
                        continue
                    parts = line.split()
                    station = None
                    if line.startswith("METAR ") or line.startswith("SPECI "):
                        if len(parts) > 1:
                            station = parts[1].upper()
                    elif len(parts) >= 1 and len(parts[0]) in (3, 4) and parts[0].isalnum():
                        station = parts[0].upper()
                    if not station or station not in seen:
                        continue
                    if station not in by_station:
                        by_station[station] = []
                    by_station[station].append(line)
                del raw_block
                gc.collect()
                for apu, idxs in seen.items():
                    block = "\n".join(by_station.get(apu, []))
                    for idx in idxs:
                        self._fill_airport_from_raw_block(idx, block, now_day, now_mins)
                        if block:
                            ok_count += 1
                del by_station
                gc.collect()
                chunk_start = chunk_end
                if chunk_start < n:
                    time.sleep_ms(150)
            self.fetched_at = int(time.time())
            self.ready = ok_count > 0
            self.state = "idle" if self.ready else "error"
            if not self.ready:
                self.last_error = "No history fetched"
            print("fc_history: packed %d slot-fills / %d airports (%d bytes)" % (
                ok_count, n, n * BYTES_PER_AIRPORT
            ))
            return self.ready
        except Exception as e:
            self.state = "error"
            self.last_error = str(e)
            print("fc_history fetch_and_pack:", e)
            return False

    def play_on_strip(
        self,
        led,
        logical_colors,
        n_leds,
        scale_color_fn,
        frame_ms=None,
        poll_callback=None,
        write_every=True,
    ):
        """
        Animate packed history on the strip, then restore previous logical_colors.
        scale_color_fn(rgb_tuple) -> brightness-scaled tuple for NeoPixel write.
        """
        if not self.ready or self.n_airports <= 0:
            print("fc_history play: not ready")
            return False
        ms = self.frame_ms if frame_ms is None else max(50, min(5000, int(frame_ms)))
        n = min(self.n_airports, n_leds, len(logical_colors))
        # Snapshot current strip colors
        saved = [logical_colors[i] for i in range(n)]
        self.state = "playing"
        try:
            for hour in range(HOURS):
                if poll_callback:
                    try:
                        poll_callback()
                    except Exception:
                        pass
                for i in range(n):
                    code = _get_bits(self.buf, i, hour)
                    rgb = FC_RGB[code]
                    logical_colors[i] = rgb
                    led[i] = scale_color_fn(rgb)
                if write_every:
                    led.write()
                # Frame delay in small chunks so OTA/HTTP stay responsive
                left = ms
                while left > 0:
                    if poll_callback:
                        try:
                            poll_callback()
                        except Exception:
                            pass
                    step = 50 if left >= 50 else left
                    time.sleep_ms(step)
                    left -= step
            # Restore live colors
            for i in range(n):
                logical_colors[i] = saved[i]
                led[i] = scale_color_fn(saved[i])
            led.write()
            self.state = "idle"
            print("fc_history: playback done (%d frames @ %dms)" % (HOURS, ms))
            return True
        except Exception as e:
            self.state = "error"
            self.last_error = str(e)
            print("fc_history play error:", e)
            try:
                for i in range(n):
                    logical_colors[i] = saved[i]
                    led[i] = scale_color_fn(saved[i])
                led.write()
            except Exception:
                pass
            return False
