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


def _newest_obs_time(raw_block):
    """Newest (day, mins) found in a raw METAR block, or (0, 0)."""
    best_day, best_mins = 0, -1
    if not raw_block:
        return (0, 0)
    for line in raw_block.split("\n"):
        line = line.strip()
        if not line:
            continue
        d, m = _metar_obs_time(line)
        if d <= 0:
            continue
        if d > best_day or (d == best_day and m >= best_mins):
            best_day, best_mins = d, m
    if best_mins < 0:
        return (0, 0)
    return (best_day, best_mins)


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
    if obs_day <= 0 or now_day <= 0:
        return None
    if obs_day == now_day:
        ago = now_mins - obs_mins
    else:
        # Previous calendar day (or month wrap: treat as yesterday within 24h window)
        ago = (now_mins + 24 * 60) - obs_mins
        day_ok = (
            obs_day == now_day - 1
            or (now_day == 1 and obs_day >= 28)
            or abs(obs_day - now_day) >= 27  # month boundary either direction
        )
        if not day_ok:
            # Still accept if age looks like < ~30h (common when RTC day is wrong)
            if ago < 0 or ago > 30 * 60:
                return None
    if ago < -30:
        return None
    if ago < 0:
        ago = 0
    return ago / 60.0


def _response_looks_like_metar(text):
    if not text or not isinstance(text, str):
        return False
    t = text.lstrip()
    if not t or t[:1] == "<":
        return False
    u = t.upper()
    return ("METAR " in u) or ("SPECI " in u) or (len(t) > 10 and t[0].isalpha())


class FlightCategoryHistory:
    """Packed 24h category timeline for the strip."""

    def __init__(self, max_airports=130):
        self.max_airports = max_airports
        self.buf = bytearray(max_airports * BYTES_PER_AIRPORT)
        self.n_airports = 0
        self.ready = False
        self.state = "idle"  # idle | fetching | playing | error
        self.fetched_at = 0
        self.last_error = ""
        self.frame_ms = 1000  # default ~24s for 24 hourly frames
        self.loops = 1  # how many times to replay the 24h animation
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
            "loops": int(self.loops),
        }

    def request_play(self, frame_ms=None, loops=None):
        if frame_ms is not None:
            try:
                self.frame_ms = max(50, min(5000, int(frame_ms)))
            except (TypeError, ValueError):
                pass
        if loops is not None:
            try:
                self.loops = max(1, min(20, int(loops)))
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

    def seed_flat(self, categories):
        """
        Fill every hour from current category strings (VFR/MVFR/IFR/LIFR).
        Used when the 24h API pack fails so PAST can still animate.
        """
        n = min(len(categories), self.max_airports)
        if n <= 0:
            self.last_error = "No airports to seed"
            return False
        self.n_airports = n
        for i in range(n):
            cat = categories[i] if i < len(categories) else "VFR"
            if not cat:
                cat = "VFR"
            code = FC_TO_BITS.get(str(cat).upper(), 0)
            for h in range(HOURS):
                _set_bits(self.buf, i, h, code)
        self.ready = True
        self.state = "idle"
        try:
            self.fetched_at = int(time.time())
        except Exception:
            self.fetched_at = 0
        self.last_error = "seeded from live colors"
        print("fc_history: seeded flat pack for %d airports (API history unavailable)" % n)
        return True

    def _fill_airport_from_raw_block(self, airport_idx, raw_block, now_day, now_mins):
        """Parse multi-line raw METARs into 24 hour buckets; carry-forward gaps."""
        slots = [-1] * HOURS  # -1 = empty
        filled = 0
        if not raw_block:
            for h in range(HOURS):
                _set_bits(self.buf, airport_idx, h, 0)
            return 0
        for line in raw_block.split("\n"):
            line = line.strip()
            if not line:
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
            slots[hour] = code
            filled += 1
        # Carry forward from oldest
        last = 0
        for h in range(HOURS):
            if slots[h] < 0:
                slots[h] = last
            else:
                last = slots[h]
            _set_bits(self.buf, airport_idx, h, slots[h])
        return filled

    def _fetch_ids(self, ids_csv, hours, timeout_s):
        """Return raw METAR text for one or more comma-separated ICAOs, or ''."""
        url = (
            "https://aviationweather.gov/api/data/metar?ids={}&hours={}&format=raw"
            .format(ids_csv, int(hours))
        )
        last_err = ""
        for attempt in range(3):
            gc.collect()
            resp = None
            try:
                print("fc_history: %s hours=%d try %d" % (ids_csv, hours, attempt + 1))
                resp = urequests.get(url, timeout=timeout_s)
                raw = resp.text or ""
                try:
                    resp.close()
                except Exception:
                    pass
                resp = None
                if not _response_looks_like_metar(raw):
                    last_err = "bad body (%d bytes)" % len(raw)
                    print("fc_history: %s %s" % (ids_csv, last_err))
                    gc.collect()
                    time.sleep_ms(150)
                    continue
                return raw
            except Exception as e:
                last_err = str(e)
                print("fc_history fetch %s: %s" % (ids_csv, e))
                if resp is not None:
                    try:
                        resp.close()
                    except Exception:
                        pass
                gc.collect()
                time.sleep_ms(250)
        self.last_error = last_err or "fetch failed"
        return ""

    def _fetch_one_airport(self, apu, hours, timeout_s):
        """Return raw text for one ICAO, or '' on failure."""
        return self._fetch_ids(apu, hours, timeout_s)

    def _station_from_line(self, line):
        if not line:
            return None
        parts = line.split()
        up = line.upper()
        if up.startswith("METAR ") or up.startswith("SPECI "):
            if len(parts) > 1:
                return parts[1].upper()
        elif len(parts) >= 1 and len(parts[0]) in (3, 4):
            return parts[0].upper()
        return None

    def _split_raw_by_station(self, raw, wanted):
        """Map uppercase ICAO -> raw block for stations in wanted (set/list)."""
        want = {}
        for apu in wanted:
            want[apu] = []
        if not raw:
            return {}
        for line in raw.split("\n"):
            line = line.strip()
            if not line:
                continue
            st = self._station_from_line(line)
            if st and st in want:
                want[st].append(line)
        out = {}
        for apu, lines in want.items():
            if lines:
                out[apu] = "\n".join(lines)
        return out

    def _pack_idx_from_block(self, idx, block, rtc_day, rtc_mins):
        """Fill one airport slot from a raw block. Returns True if useful data packed."""
        nd, nm = _newest_obs_time(block)
        if nd <= 0:
            nd, nm = rtc_day, rtc_mins
        if nd <= 0:
            nd, nm = 1, 12 * 60
        filled = self._fill_airport_from_raw_block(idx, block, nd, nm)
        return bool(block) and (filled > 0 or len(block) > 20)

    def _fetch_and_pack_one(self, idx, apu, rtc_day, rtc_mins):
        """Single-airport fetch with 24h then 12h fallback. Returns True on success."""
        raw_block = self._fetch_one_airport(apu, 24, 15)
        if not raw_block:
            raw_block = self._fetch_one_airport(apu, 12, 12)
        if not raw_block:
            for h in range(HOURS):
                _set_bits(self.buf, idx, h, 0)
            return False
        lines = []
        for line in raw_block.split("\n"):
            line = line.strip()
            if not line:
                continue
            st = self._station_from_line(line)
            if st is None or st == apu:
                lines.append(line)
        block = "\n".join(lines)
        del raw_block
        del lines
        gc.collect()
        ok = self._pack_idx_from_block(idx, block, rtc_day, rtc_mins)
        del block
        gc.collect()
        return ok

    def fetch_and_pack(self, airports, poll_callback=None, limit=None, chunk_size=3):
        """
        Download hours=24 METARs in small multi-id chunks (default 3); pack into self.buf.
        Falls back to one airport per call if a chunk fails (Pico RAM-safe).
        airports: list of ICAO strings (blank = skip / all VFR zeros).
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
        if n <= 0:
            self.last_error = "No airports in list"
            self.state = "error" if not self.ready else "idle"
            print("fc_history: no airports to fetch")
            return False
        # Do not wipe an existing ready pack until a new fetch succeeds
        if not self.ready:
            for i in range(n * BYTES_PER_AIRPORT):
                self.buf[i] = 0

        try:
            now = time.gmtime()
            rtc_day = now[2]
            rtc_mins = now[3] * 60 + now[4]
        except Exception:
            rtc_day = 0
            rtc_mins = 0

        try:
            cs = int(chunk_size) if chunk_size is not None else 3
        except (TypeError, ValueError):
            cs = 3
        cs = max(1, min(5, cs))

        # Build work list of (idx, ICAO); blank slots -> VFR zeros
        work = []
        for idx in range(n):
            ap = airports[idx]
            if not ap or not str(ap).strip():
                for h in range(HOURS):
                    _set_bits(self.buf, idx, h, 0)
                continue
            work.append((idx, str(ap).strip().upper()))

        ok_count = 0
        fail_count = 0
        print("fc_history: fetching %d airports chunk_size=%d" % (len(work), cs))
        try:
            i = 0
            while i < len(work):
                if poll_callback:
                    try:
                        poll_callback()
                    except Exception:
                        pass
                chunk = work[i : i + cs]
                ids_csv = ",".join(apu for _, apu in chunk)
                raw = self._fetch_ids(ids_csv, 24, 18 if len(chunk) > 1 else 15)
                if not raw:
                    raw = self._fetch_ids(ids_csv, 12, 14 if len(chunk) > 1 else 12)

                if raw and len(chunk) > 1:
                    wanted = [apu for _, apu in chunk]
                    by_st = self._split_raw_by_station(raw, wanted)
                    del raw
                    gc.collect()
                    missing = []
                    for idx, apu in chunk:
                        block = by_st.get(apu, "")
                        if block and self._pack_idx_from_block(idx, block, rtc_day, rtc_mins):
                            ok_count += 1
                        else:
                            missing.append((idx, apu))
                        if block:
                            del block
                    del by_st
                    gc.collect()
                    for idx, apu in missing:
                        if poll_callback:
                            try:
                                poll_callback()
                            except Exception:
                                pass
                        if self._fetch_and_pack_one(idx, apu, rtc_day, rtc_mins):
                            ok_count += 1
                        else:
                            fail_count += 1
                        time.sleep_ms(50)
                elif raw and len(chunk) == 1:
                    idx, apu = chunk[0]
                    by_st = self._split_raw_by_station(raw, [apu])
                    del raw
                    gc.collect()
                    block = by_st.get(apu, "")
                    if block and self._pack_idx_from_block(idx, block, rtc_day, rtc_mins):
                        ok_count += 1
                    else:
                        fail_count += 1
                        for h in range(HOURS):
                            _set_bits(self.buf, idx, h, 0)
                    del by_st
                    gc.collect()
                else:
                    # Chunk failed entirely — fall back one-at-a-time
                    if len(chunk) > 1:
                        print("fc_history: chunk failed, fallback singles: %s" % ids_csv)
                    for idx, apu in chunk:
                        if poll_callback:
                            try:
                                poll_callback()
                            except Exception:
                                pass
                        if self._fetch_and_pack_one(idx, apu, rtc_day, rtc_mins):
                            ok_count += 1
                        else:
                            fail_count += 1
                        time.sleep_ms(50)

                i += cs
                time.sleep_ms(80)

            try:
                self.fetched_at = int(time.time())
            except Exception:
                self.fetched_at = 0
            if ok_count > 0:
                self.ready = True
                self.state = "idle"
                self.last_error = ""
            else:
                self.last_error = "No history fetched (ok=0 fail=%d n=%d)" % (fail_count, n)
                self.state = "idle" if self.ready else "error"
            print(
                "fc_history: packed ok=%d fail=%d / %d airports ready=%s"
                % (ok_count, fail_count, n, self.ready)
            )
            return self.ready
        except Exception as e:
            self.state = "error" if not self.ready else "idle"
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
        loops=None,
    ):
        """
        Animate packed history on the strip, then restore previous logical_colors.
        scale_color_fn(rgb_tuple) -> brightness-scaled tuple for NeoPixel write.
        loops: how many times to replay the 24-hour sequence (default self.loops).
        """
        if not self.ready or self.n_airports <= 0:
            print("fc_history play: not ready")
            return False
        ms = self.frame_ms if frame_ms is None else max(50, min(5000, int(frame_ms)))
        try:
            n_loops = int(self.loops if loops is None else loops)
        except (TypeError, ValueError):
            n_loops = 1
        n_loops = max(1, min(20, n_loops))
        n = min(self.n_airports, n_leds, len(logical_colors))
        # Snapshot current strip colors
        saved = [logical_colors[i] for i in range(n)]
        self.state = "playing"
        try:
            for loop_i in range(n_loops):
                if poll_callback:
                    try:
                        poll_callback()
                    except Exception:
                        pass
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
            print(
                "fc_history: playback done (%d×%d frames @ %dms)"
                % (n_loops, HOURS, ms)
            )
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
