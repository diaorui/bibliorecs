import json
import os
import threading
import time


class RefreshCache:
    def __init__(self, refresh_func, refresh_hours=4, failure_retry_minutes=5,
                 name="", persist_path=None, scanner_interval=0):
        self._refresh_func = refresh_func
        self._default_refresh_interval = refresh_hours * 3600
        self._failure_retry = failure_retry_minutes * 60
        self._name = name
        self._persist_path = persist_path
        self._cache = {}
        self._lock = threading.Lock()
        self._concurrency = threading.Semaphore(10)

        if persist_path:
            self._load_from_disk()
        if scanner_interval > 0:
            threading.Thread(target=self._scanner_loop, args=(scanner_interval,),
                             daemon=True).start()

    def set(self, key, value):
        with self._lock:
            self._cache[key] = {
                "value": value,
                "last_refreshed": time.time(),
                "refresh_interval": self._default_refresh_interval,
            }

    def get(self, key):
        with self._lock:
            entry = self._cache.get(key)
            if entry and entry["value"] is not None:
                return entry["value"]
        return None

    def ensure(self, key, meta=None, wait=False):
        if wait:
            self._do_ensure(key, meta)
        else:
            threading.Thread(target=self._do_ensure, args=(key, meta), daemon=True).start()

    def _load_from_disk(self):
        try:
            with open(self._persist_path) as f:
                data = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return
        with self._lock:
            for key_str, entry in data.items():
                if isinstance(entry, dict) and "value" in entry:
                    entry["refresh_interval"] = self._default_refresh_interval
                    self._cache[key_str] = entry

    def _save_to_disk(self):
        with self._lock:
            data = dict(self._cache)
        dirpath = os.path.dirname(self._persist_path)
        if dirpath:
            os.makedirs(dirpath, exist_ok=True)
        tmp = self._persist_path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f)
        os.replace(tmp, self._persist_path)

    def _scanner_loop(self, interval):
        while True:
            time.sleep(interval)
            with self._lock:
                keys = list(self._cache.keys())
            for key in keys:
                self._do_ensure(key)

    def _do_ensure(self, key, meta=None):
        with self._concurrency:
            with self._lock:
                now = time.time()
                entry = self._cache.get(key)
                if entry and now - entry["last_refreshed"] < entry["refresh_interval"]:
                    return
            try:
                value = self._refresh_func(key, meta)
                with self._lock:
                    old_entry = self._cache.get(key)
                    changed = old_entry is None or old_entry["value"] != value
                    self._cache[key] = {
                        "value": value,
                        "last_refreshed": time.time(),
                        "refresh_interval": self._default_refresh_interval,
                    }
                if changed and self._persist_path:
                    try:
                        self._save_to_disk()
                    except OSError:
                        pass
            except Exception:
                with self._lock:
                    entry = self._cache.get(key)
                    if entry and entry["value"] is not None:
                        entry["refresh_interval"] = self._failure_retry
                    else:
                        self._cache[key] = {
                            "value": None,
                            "last_refreshed": 0,
                            "refresh_interval": self._failure_retry,
                        }
