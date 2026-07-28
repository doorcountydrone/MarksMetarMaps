# OTA updater for MetarMap: check version from GitHub Pages, download and stage files.
# Install runs only when user confirms (button or app). boot.py applies the swap on next boot.
# Written for MicroPython (no try/finally — some firmware rejects it; explicit close only).

import urequests
import json
import gc
import machine
import os

UPDATE_BASE_URL = "https://doorcountydrone.github.io/MarksMetarMaps"
VERSION_URL = UPDATE_BASE_URL + "/version.json"
PENDING_FILE = "update_pending.json"
# Small reads keep peak RAM low (r.text allocates the whole body at once and fails on large main.py).
_DOWNLOAD_CHUNK = 512


def _staging_file_looks_like_html(path):
    try:
        with open(path, "rb") as f:
            head = f.read(256).lstrip()
        if not head:
            return True
        return head.startswith(b"<")
    except Exception:
        return True


def _download_url_to_file(url, dest_path, timeout, what):
    gc.collect()
    r = None
    ok = False
    try:
        r = urequests.get(url, timeout=timeout)
        code = getattr(r, "status_code", 200)
        if code != 200:
            print("OTA:", what, "HTTP", code, "for", url)
        else:
            body = r.raw
            with open(dest_path, "wb") as f:
                while True:
                    chunk = body.read(_DOWNLOAD_CHUNK)
                    if chunk is None or len(chunk) == 0:
                        break
                    f.write(chunk)
            if _staging_file_looks_like_html(dest_path):
                print("OTA:", what, "- got HTML not file (wrong URL or host error page)")
                try:
                    os.remove(dest_path)
                except Exception:
                    pass
            else:
                ok = True
    except Exception as ex:
        print("OTA:", what, "request error:", ex)
        try:
            os.remove(dest_path)
        except Exception:
            pass
    if r is not None:
        try:
            r.close()
        except Exception:
            pass
    gc.collect()
    return ok


def _get_url_text(url, timeout, what):
    gc.collect()
    r = None
    out_ok = False
    out_data = None
    try:
        r = urequests.get(url, timeout=timeout)
        data = r.text
        code = getattr(r, "status_code", 200)
        if code != 200:
            print("OTA:", what, "HTTP", code, "for", url)
        elif data and data.lstrip().startswith("<"):
            print("OTA:", what, "- got HTML not JSON/text (wrong URL or host error page)")
        else:
            out_ok = True
            out_data = data
    except Exception as ex:
        print("OTA:", what, "request error:", ex)
    if r is not None:
        try:
            r.close()
        except Exception:
            pass
    gc.collect()
    return out_ok, out_data


def _parse_version(s):
    try:
        parts = str(s).strip().split(".")
        out = []
        for x in parts:
            out.append(int(x))
        return tuple(out)
    except (ValueError, AttributeError):
        return (0, 0, 0)


def check_for_new_version(current_version):
    try:
        ok, data = _get_url_text(VERSION_URL, 8, "version check")
        if not ok or not data:
            return False, None
        try:
            info = json.loads(data)
        except Exception:
            print("OTA: version.json is not valid JSON (check URL / GitHub Pages)")
            return False, None
        remote_ver = info.get("version", "0.0.0")
        files = info.get("files", [])
        if not files:
            print("OTA: version.json has no 'files' list (cannot OTA)")
            return False, None
        cur = _parse_version(current_version)
        rem = _parse_version(remote_ver)
        if rem > cur:
            return True, info
        print("OTA: up to date (device", current_version, "remote", str(remote_ver) + ")")
        return False, None
    except Exception as e:
        print("OTA check failed:", e)
        gc.collect()
        return False, None


def install_pending_update(version_info):
    if not version_info or "files" not in version_info:
        print("OTA install: no version_info")
        return False
    files = version_info["files"]
    try:
        for entry in files:
            name = entry.get("name")
            url = entry.get("url")
            if not name or not url:
                continue
            if not name.endswith(".py"):
                continue
            # Never pull staged/backup names from the server (only real modules)
            if "_backup" in name or "_new" in name:
                continue
            temp_name = name[:-3] + "_new.py"
            print("OTA downloading", url, "->", temp_name)
            if not _download_url_to_file(url, temp_name, 30, "download " + str(name)):
                print("OTA install aborted (bad response for", str(name) + ")")
                return False
        pending_files = []
        for e in files:
            n = e.get("name") or ""
            if not n.endswith(".py") or "_backup" in n or "_new" in n:
                continue
            pending_files.append({"name": e.get("name"), "temp": n.replace(".py", "_new.py")})
        pending = {
            "target_version": version_info.get("version", "0.0.0"),
            "files": pending_files
        }
        with open(PENDING_FILE, "w") as f:
            json.dump(pending, f)
        print("OTA staged. Rebooting to apply...")
        machine.reset()
    except Exception as e:
        print("OTA install failed:", e)
        gc.collect()
        return False
    return True


def install_latest():
    try:
        ok, data = _get_url_text(VERSION_URL, 8, "install_latest version.json")
        if not ok or not data:
            return False
        try:
            info = json.loads(data)
        except Exception:
            print("OTA install_latest: invalid version.json")
            return False
        return install_pending_update(info)
    except Exception as e:
        print("OTA install_latest failed:", e)
        gc.collect()
        return False
