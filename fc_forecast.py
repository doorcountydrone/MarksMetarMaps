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
# Nearest TAF: one regional stationinfo bbox + in-memory distance (not per-airport HTTP).


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
    """Fetch JSON; free the raw text as soon as parsing succeeds."""
    text = _http_get_text(url, timeout=timeout)
    if not text:
        return None
    try:
        data = json.loads(text)
    except Exception:
        data = None
    del text
    gc.collect()
    return data


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
        parsed.append((t0, t1, code))
    for h in range(HOURS):
        t = int(now_epoch) + h * 3600 + 1800  # mid-hour sample
        best = None
        for t0, t1, code in parsed:
            if t0 <= t < t1:
                if best is None or code > best:
                    best = code
        if best is not None:
            slots[h] = best
        elif h > 0:
            slots[h] = slots[h - 1]
    del parsed
    return slots


class FlightCategoryForecast:
    """Packed next-24h TAF category timeline for the strip."""

    def __init__(self, max_airports=130):
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
        cs = 3  # small multi-id stationinfo batches
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
                    gc.collect()
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
                    out[icao] = (lat, lon, _site_has_taf(st.get("siteType")))
            del data
            gc.collect()
            i += cs
            time.sleep_ms(80)
        return out

    def _nearest_from_list(self, lat, lon, sites):
        """sites: list of (icao, lat, lon). Return nearest icao or None."""
        best_id = None
        best_d = None
        for icao, slat, slon in sites:
            dd = _dist2(lat, lon, slat, slon)
            if best_d is None or dd < best_d:
                best_d = dd
                best_id = icao
        return best_id

    def _fetch_taf_sites_bbox(self, lat0, lon0, lat1, lon1, poll_callback=None):
        """One stationinfo bbox -> list of (icao, lat, lon) for TAF sites only."""
        if lat0 > lat1:
            lat0, lat1 = lat1, lat0
        if lon0 > lon1:
            lon0, lon1 = lon1, lon0
        pad = 0.75
        bbox = "%.3f,%.3f,%.3f,%.3f" % (lat0 - pad, lon0 - pad, lat1 + pad, lon1 + pad)
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
                print("fc_forecast: regional TAF sites try %d" % (attempt + 1))
                gc.collect()
                data = _http_get_json(url, timeout=18)
                if data is not None:
                    break
            except Exception as e:
                print("fc_forecast regional sites:", e)
                gc.collect()
                time.sleep_ms(250)
        out = []
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
                out.append((icao, slat, slon))
        del data
        gc.collect()
        print("fc_forecast: regional TAF sites = %d" % len(out))
        return out

    def _fetch_one_taf_fcsts(self, icao, poll_callback=None):
        """Fetch a single station TAF (JSON)."""
        by_id = self._fetch_tafs_chunk([icao], poll_callback)
        return by_id.get(icao)

    def _fetch_tafs_chunk(self, icaos, poll_callback=None):
        """Fetch up to a few TAFs in one request. Returns dict icao -> fcsts list."""
        out = {}
        if not icaos:
            return out
        ids = ",".join(icaos)
        url = "https://aviationweather.gov/api/data/taf?ids=%s&format=json" % ids
        data = None
        for attempt in range(2):
            if poll_callback:
                try:
                    poll_callback()
                except Exception:
                    pass
            try:
                print("fc_forecast: taf %s try %d" % (ids, attempt + 1))
                gc.collect()
                data = _http_get_json(url, timeout=18 if len(icaos) > 1 else 15)
                if data is not None:
                    break
            except Exception as e:
                print("fc_forecast taf fetch:", e)
                gc.collect()
                time.sleep_ms(250)
        want = {}
        for icao in icaos:
            want[icao] = True
        if isinstance(data, list):
            for taf in data:
                if not isinstance(taf, dict):
                    continue
                tid = str(taf.get("icaoId") or "").upper()
                if tid in want and isinstance(taf.get("fcsts"), list):
                    out[tid] = taf.get("fcsts")
        elif isinstance(data, dict) and len(icaos) == 1:
            if isinstance(data.get("fcsts"), list):
                out[icaos[0]] = data.get("fcsts")
        del data
        gc.collect()
        return out

    def _pack_slots_for_indices(self, idxs, slots):
        for idx in idxs:
            for h in range(HOURS):
                _set_bits(self.buf, idx, h, slots[h])

    def fetch_and_pack(self, airports, poll_callback=None, limit=None, chunk_size=3):
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
        if not self.ready:
            for i in range(n * BYTES_PER_AIRPORT):
                self.buf[i] = 0
        try:
            now_epoch = int(time.time())
        except Exception:
            now_epoch = 0

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
            if not self.ready:
                self.state = "error"
                self.last_error = "No airports"
            return False

        stations = self._fetch_stationinfo_ids(strip_ids, poll_callback)
        source_for = {}
        need_nearest = []
        strip_taf_sites = []
        min_lat = max_lat = min_lon = max_lon = None
        for apu in strip_ids:
            st = stations.get(apu)
            if not st:
                continue
            lat, lon, has_taf = st[0], st[1], st[2]
            if min_lat is None:
                min_lat = max_lat = lat
                min_lon = max_lon = lon
            else:
                if lat < min_lat:
                    min_lat = lat
                if lat > max_lat:
                    max_lat = lat
                if lon < min_lon:
                    min_lon = lon
                if lon > max_lon:
                    max_lon = lon
            if has_taf:
                source_for[apu] = apu
                strip_taf_sites.append((apu, lat, lon))
            else:
                need_nearest.append(apu)

        # Prefer strip TAF sites; optionally ONE regional lookup (not per-airport).
        taf_pool = list(strip_taf_sites)
        if need_nearest and min_lat is not None:
            try:
                regional = self._fetch_taf_sites_bbox(
                    min_lat, min_lon, max_lat, max_lon, poll_callback
                )
            except Exception as e:
                print("fc_forecast: regional lookup failed:", e)
                regional = []
            if regional:
                seen = {t[0]: True for t in taf_pool}
                for item in regional:
                    if item[0] not in seen:
                        seen[item[0]] = True
                        taf_pool.append(item)
                del seen
            del regional
            gc.collect()
            if not taf_pool:
                taf_pool = list(strip_taf_sites)

        for apu in need_nearest:
            st = stations.get(apu)
            if not st:
                print("fc_forecast: no coords for %s — skipping nearest" % apu)
                continue
            near = self._nearest_from_list(st[0], st[1], taf_pool)
            if near:
                source_for[apu] = near
                print("fc_forecast: %s has no TAF → nearest %s" % (apu, near))
            else:
                print("fc_forecast: no nearby TAF for %s" % apu)

        del stations
        del taf_pool
        del strip_taf_sites
        gc.collect()
        self.source_map = dict(source_for)

        source_idxs = {}
        for apu, src in source_for.items():
            if src not in source_idxs:
                source_idxs[src] = []
            source_idxs[src].extend(idx_for.get(apu, []))

        try:
            cs = int(chunk_size) if chunk_size is not None else 3
        except (TypeError, ValueError):
            cs = 3
        cs = max(1, min(5, cs))

        src_list = list(source_idxs.keys())
        ok_count = 0
        print("fc_forecast: fetching %d TAF sources chunk_size=%d" % (len(src_list), cs))
        si = 0
        while si < len(src_list):
            chunk_srcs = src_list[si : si + cs]
            by_id = self._fetch_tafs_chunk(chunk_srcs, poll_callback)
            missing = []
            for src in chunk_srcs:
                fcsts = by_id.get(src)
                if not fcsts:
                    missing.append(src)
                    continue
                slots = _periods_to_hourly(fcsts, now_epoch)
                idxs = source_idxs.get(src, [])
                self._pack_slots_for_indices(idxs, slots)
                ok_count += len(idxs)
                del slots
                gc.collect()
            del by_id
            gc.collect()
            # Fallback singles for any missing from the multi-id response
            for src in missing:
                if poll_callback:
                    try:
                        poll_callback()
                    except Exception:
                        pass
                fcsts = self._fetch_one_taf_fcsts(src, poll_callback)
                if not fcsts:
                    print("fc_forecast: no TAF body for %s" % src)
                    continue
                slots = _periods_to_hourly(fcsts, now_epoch)
                del fcsts
                gc.collect()
                idxs = source_idxs.get(src, [])
                self._pack_slots_for_indices(idxs, slots)
                ok_count += len(idxs)
                del slots
                gc.collect()
                time.sleep_ms(80)
            si += cs
            time.sleep_ms(80)

        del source_idxs
        del source_for
        del idx_for
        gc.collect()

        self.fetched_at = int(time.time())
        if ok_count > 0:
            self.ready = True
            self.state = "idle"
            self.last_error = ""
        else:
            # Keep a previously good pack so Play can still run after a failed refresh
            self.last_error = "No TAFs fetched"
            self.state = "idle" if self.ready else "error"
        print(
            "fc_forecast: packed %d slot-fills / %d airports (%d bytes) ready=%s; sources=%s"
            % (ok_count, n, n * BYTES_PER_AIRPORT, self.ready, self.source_map)
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
