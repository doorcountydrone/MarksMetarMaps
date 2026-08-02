# 24-hour future flight-category forecast from TAFs.
# Same 2-bit packing as fc_history (6 bytes/airport). If an airport has no TAF,
# use the nearest station that does (from aviationweather.gov stationinfo).

import gc
import json
import urequests
import utime as time

HOURS = 24
BYTES_PER_AIRPORT = 6
FC_TO_BITS = {"VFR": 0, "MVFR": 1, "IFR": 2, "LIFR": 3}
FC_FROM_BITS = ("VFR", "MVFR", "IFR", "LIFR")
FC_RGB = (
    (0, 255, 0),
    (0, 0, 255),
    (255, 0, 0),
    (255, 0, 130),
)
# Degrees of lat/lon for nearest-TAF search when the strip airport itself has no TAF.
NEAREST_BBOX_DEG = 1.25


def _set_bits(buf, airport_idx, hour, code):
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


def _dist2(lat1, lon1, lat2, lon2):
    """Squared approx distance (good enough for nearest within ~100 nm)."""
    dlat = lat1 - lat2
    # Rough lon shrink near mid-latitudes
    mid = (lat1 + lat2) * 0.5
    # cos(lat) ≈ 1 - lat^2/2 in radians; use simple scale: cos(40°)≈0.76
    # Avoid math module: approximate cos degrees with a coarse table
    a = abs(mid)
    if a < 30:
        cosf = 0.90
    elif a < 40:
        cosf = 0.80
    elif a < 50:
        cosf = 0.70
    else:
        cosf = 0.55
    dlon = (lon1 - lon2) * cosf
    return dlat * dlat + dlon * dlon


def _http_get_text(url, timeout=12):
    resp = urequests.get(url, timeout=timeout)
    try:
        return resp.text or ""
    finally:
        try:
            resp.close()
        except Exception:
            pass


def _http_get_json(url, timeout=12):
    text = _http_get_text(url, timeout=timeout)
    if not text:
        return None
    try:
        return json.loads(text)
    except Exception:
        return None


def _site_has_taf(site_types):
    if not site_types:
        return False
    for t in site_types:
        if str(t).upper() == "TAF":
            return True
    return False


def _fc_from_vis_clouds(visib, clouds, vert_vis):
    vis_m = 10.0
    if isinstance(visib, (int, float)):
        vis_m = float(visib)
    elif visib is not None:
        s = str(visib).strip().upper().replace("SM", "")
        if s.startswith("P") or s.endswith("+"):
            s = s.replace("P", "").replace("+", "")
            try:
                vis_m = max(6.0, float(s) if s else 10.0)
            except ValueError:
                vis_m = 10.0
        elif "/" in s:
            try:
                a, b = s.split("/", 1)
                vis_m = float(a.strip()) / max(1.0, float(b.strip()))
            except ValueError:
                pass
        else:
            try:
                vis_m = float(s)
            except ValueError:
                pass
    ceiling_ft = 10000
    if vert_vis is not None:
        try:
            ceiling_ft = min(ceiling_ft, int(vert_vis))
        except (TypeError, ValueError):
            pass
    if isinstance(clouds, list):
        for c in clouds:
            if not isinstance(c, dict):
                continue
            cover = str(c.get("cover", "") or "").upper()
            if cover in ("BKN", "OVC", "VV"):
                try:
                    base = c.get("base")
                    if base is not None:
                        h = int(base)
                        if h < ceiling_ft:
                            ceiling_ft = h
                except (TypeError, ValueError):
                    pass
    if ceiling_ft > 5000:
        ceiling_ft = 5000
    if ceiling_ft < 500 or vis_m < 1.0:
        return "LIFR"
    if ceiling_ft < 1000 or vis_m < 3.0:
        return "IFR"
    if ceiling_ft < 3000 or vis_m < 5.0:
        return "MVFR"
    return "VFR"


def _periods_to_hourly(fcsts, now_epoch):
    """Return list of 24 category codes (0..3) for next 24 hours from now."""
    slots = [0] * HOURS
    if not isinstance(fcsts, list) or not fcsts:
        return slots
    parsed = []
    for p in fcsts:
        if not isinstance(p, dict):
            continue
        try:
            t0 = int(p.get("timeFrom"))
            t1 = int(p.get("timeTo"))
        except (TypeError, ValueError):
            continue
        code = FC_TO_BITS.get(
            _fc_from_vis_clouds(p.get("visib"), p.get("clouds"), p.get("vertVis")),
            0,
        )
        change = str(p.get("fcstChange") or "").upper()
        # TEMPO/PROB are temporary; still consider them (more restrictive wins)
        parsed.append((t0, t1, code, change))
    for h in range(HOURS):
        t = int(now_epoch) + h * 3600 + 1800  # mid-hour sample
        best = None
        for t0, t1, code, change in parsed:
            if t0 <= t < t1:
                if best is None or code > best:
                    best = code
        if best is not None:
            slots[h] = best
        elif h > 0:
            slots[h] = slots[h - 1]
    return slots


class FlightCategoryForecast:
    """Packed next-24h TAF category timeline for the strip."""

    def __init__(self, max_airports=120):
        self.max_airports = max_airports
        self.buf = bytearray(max_airports * BYTES_PER_AIRPORT)
        self.n_airports = 0
        self.ready = False
        self.state = "idle"
        self.fetched_at = 0
        self.last_error = ""
        self.frame_ms = 1000
        self.loops = 1
        self._play_pending = False
        self._refresh_pending = False
        self.source_map = {}  # strip ICAO -> TAF ICAO used

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
            "kind": "forecast",
            "sources": self.source_map,
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

    def _fetch_stationinfo_ids(self, id_list, poll_callback=None):
        out = {}
        if not id_list:
            return out
        cs = 5
        i = 0
        while i < len(id_list):
            if poll_callback:
                try:
                    poll_callback()
                except Exception:
                    pass
            chunk = id_list[i : i + cs]
            ids = ",".join(chunk)
            url = "https://aviationweather.gov/api/data/stationinfo?ids=%s&format=json" % ids
            data = None
            for attempt in range(2):
                try:
                    print("fc_forecast: stationinfo %s try %d" % (ids, attempt + 1))
                    data = _http_get_json(url)
                    if data is not None:
                        break
                except Exception as e:
                    print("fc_forecast stationinfo:", e)
                    gc.collect()
                    time.sleep_ms(200)
            if isinstance(data, list):
                for st in data:
                    if not isinstance(st, dict):
                        continue
                    icao = str(st.get("icaoId") or st.get("id") or "").upper()
                    if not icao:
                        continue
                    try:
                        lat = float(st.get("lat"))
                        lon = float(st.get("lon"))
                    except (TypeError, ValueError):
                        continue
                    out[icao] = {
                        "lat": lat,
                        "lon": lon,
                        "has_taf": _site_has_taf(st.get("siteType")),
                    }
            del data
            gc.collect()
            i += cs
            time.sleep_ms(80)
        return out

    def _nearest_taf_station(self, lat, lon, poll_callback=None):
        d = NEAREST_BBOX_DEG
        bbox = "%.3f,%.3f,%.3f,%.3f" % (lat - d, lon - d, lat + d, lon + d)
        url = (
            "https://aviationweather.gov/api/data/stationinfo?bbox=%s&format=json"
            % bbox
        )
        data = None
        for attempt in range(2):
            if poll_callback:
                try:
                    poll_callback()
                except Exception:
                    pass
            try:
                print("fc_forecast: nearest bbox try %d" % (attempt + 1))
                data = _http_get_json(url, timeout=15)
                if data is not None:
                    break
            except Exception as e:
                print("fc_forecast nearest:", e)
                gc.collect()
                time.sleep_ms(250)
        best_id = None
        best_d = None
        if isinstance(data, list):
            for st in data:
                if not isinstance(st, dict):
                    continue
                if not _site_has_taf(st.get("siteType")):
                    continue
                icao = str(st.get("icaoId") or st.get("id") or "").upper()
                if not icao:
                    continue
                try:
                    slat = float(st.get("lat"))
                    slon = float(st.get("lon"))
                except (TypeError, ValueError):
                    continue
                dd = _dist2(lat, lon, slat, slon)
                if best_d is None or dd < best_d:
                    best_d = dd
                    best_id = icao
        del data
        gc.collect()
        return best_id

    def _fetch_tafs(self, id_list, poll_callback=None):
        """Return dict icao -> list of forecast period dicts."""
        out = {}
        if not id_list:
            return out
        cs = 3
        i = 0
        while i < len(id_list):
            if poll_callback:
                try:
                    poll_callback()
                except Exception:
                    pass
            chunk = id_list[i : i + cs]
            ids = ",".join(chunk)
            url = "https://aviationweather.gov/api/data/taf?ids=%s&format=json" % ids
            data = None
            for attempt in range(2):
                try:
                    print("fc_forecast: taf %s try %d" % (ids, attempt + 1))
                    data = _http_get_json(url, timeout=15)
                    if data is not None:
                        break
                except Exception as e:
                    print("fc_forecast taf fetch:", e)
                    gc.collect()
                    time.sleep_ms(200)
            if isinstance(data, list):
                for taf in data:
                    if not isinstance(taf, dict):
                        continue
                    icao = str(taf.get("icaoId") or "").upper()
                    if not icao:
                        continue
                    fcsts = taf.get("fcsts")
                    if isinstance(fcsts, list):
                        out[icao] = fcsts
            elif isinstance(data, dict):
                icao = str(data.get("icaoId") or "").upper()
                fcsts = data.get("fcsts")
                if icao and isinstance(fcsts, list):
                    out[icao] = fcsts
            del data
            gc.collect()
            i += cs
            time.sleep_ms(100)
        return out

    def fetch_and_pack(self, airports, poll_callback=None, limit=None):
        self.state = "fetching"
        self.last_error = ""
        self.source_map = {}
        n = min(len(airports), self.max_airports)
        if limit is not None:
            try:
                n = min(n, max(0, int(limit)))
            except (TypeError, ValueError):
                pass
        self.n_airports = n
        for i in range(n * BYTES_PER_AIRPORT):
            self.buf[i] = 0
        try:
            now_epoch = int(time.time())
        except Exception:
            now_epoch = 0

        # 1) Collect strip ICAOs + station metadata
        strip_ids = []
        idx_for = {}
        for idx in range(n):
            ap = airports[idx]
            if not ap or not str(ap).strip():
                continue
            apu = str(ap).strip().upper()
            if apu not in idx_for:
                idx_for[apu] = []
                strip_ids.append(apu)
            idx_for[apu].append(idx)

        if not strip_ids:
            self.ready = False
            self.state = "error"
            self.last_error = "No airports"
            return False

        stations = self._fetch_stationinfo_ids(strip_ids, poll_callback)
        # 2) Resolve TAF source per strip airport
        source_for = {}  # strip icao -> taf icao
        need_nearest = []
        for apu in strip_ids:
            st = stations.get(apu)
            if st and st.get("has_taf"):
                source_for[apu] = apu
            else:
                need_nearest.append(apu)

        for apu in need_nearest:
            st = stations.get(apu)
            if not st:
                print("fc_forecast: no coords for %s — skipping nearest" % apu)
                continue
            near = self._nearest_taf_station(st["lat"], st["lon"], poll_callback)
            if near:
                source_for[apu] = near
                print("fc_forecast: %s has no TAF → nearest %s" % (apu, near))
            else:
                print("fc_forecast: no nearby TAF for %s" % apu)

        self.source_map = dict(source_for)
        taf_ids = []
        seen_taf = {}
        for apu, src in source_for.items():
            if src not in seen_taf:
                seen_taf[src] = True
                taf_ids.append(src)

        # 3) Fetch TAF periods
        taf_data = self._fetch_tafs(taf_ids, poll_callback)

        # 4) Pack hourly slots per strip index
        ok_count = 0
        for apu, idxs in idx_for.items():
            src = source_for.get(apu)
            fcsts = taf_data.get(src) if src else None
            slots = _periods_to_hourly(fcsts, now_epoch) if fcsts else [0] * HOURS
            for idx in idxs:
                for h in range(HOURS):
                    _set_bits(self.buf, idx, h, slots[h])
                if fcsts:
                    ok_count += 1
        del taf_data
        del stations
        gc.collect()

        self.fetched_at = int(time.time())
        self.ready = ok_count > 0
        self.state = "idle" if self.ready else "error"
        if not self.ready:
            self.last_error = "No TAFs fetched"
        print(
            "fc_forecast: packed %d slot-fills / %d airports (%d bytes); sources=%s"
            % (ok_count, n, n * BYTES_PER_AIRPORT, self.source_map)
        )
        return self.ready

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
        if not self.ready or self.n_airports <= 0:
            print("fc_forecast play: not ready")
            return False
        ms = self.frame_ms if frame_ms is None else max(50, min(5000, int(frame_ms)))
        try:
            n_loops = int(self.loops if loops is None else loops)
        except (TypeError, ValueError):
            n_loops = 1
        n_loops = max(1, min(20, n_loops))
        n = min(self.n_airports, n_leds, len(logical_colors))
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
            for i in range(n):
                logical_colors[i] = saved[i]
                led[i] = scale_color_fn(saved[i])
            led.write()
            self.state = "idle"
            print(
                "fc_forecast: playback done (%d×%d frames @ %dms)"
                % (n_loops, HOURS, ms)
            )
            return True
        except Exception as e:
            self.state = "error"
            self.last_error = str(e)
            print("fc_forecast play error:", e)
            try:
                for i in range(n):
                    logical_colors[i] = saved[i]
                    led[i] = scale_color_fn(saved[i])
                led.write()
            except Exception:
                pass
            return False
