# Run before main.py. If an OTA update was staged, swap *_new.py -> *.py then run main.
#
# *_backup.py files are NOT from GitHub — boot saves the previous .py here before applying
# the downloaded *_new.py. Safe to delete *_backup.py if you need flash space (after a
# successful boot on the new firmware).

import os
import json

PENDING_FILE = "update_pending.json"

try:
    if PENDING_FILE in os.listdir():
        with open(PENDING_FILE, "r") as f:
            pending = json.load(f)
        files = pending.get("files", [])
        for entry in files:
            name = entry.get("name")
            temp = entry.get("temp")
            if not name or not temp:
                continue
            backup = name.replace(".py", "_backup.py")
            try:
                if backup in os.listdir():
                    os.remove(backup)
            except Exception as e:
                print("boot: remove old", backup, e)
            try:
                if name in os.listdir():
                    os.rename(name, backup)
            except Exception as e:
                print("boot: backup", name, "failed:", e)
            try:
                if temp in os.listdir():
                    os.rename(temp, name)
                    print("boot: applied", name)
            except Exception as e:
                print("boot: apply", temp, "failed:", e)
        try:
            os.remove(PENDING_FILE)
        except Exception:
            pass
except Exception as e:
    print("boot:", e)

# main.py runs automatically after boot.py finishes
