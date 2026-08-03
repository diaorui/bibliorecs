import json
import os
import threading
import time

import vault


class RefreshCache:
    def __init__(self, refresh_func, refresh_hours=4, failure_retry_minutes=5,
                 name="", persist_path=None, scanner_interval=0, persist_table=None):
        self._refresh_func = refresh_func
        self._default_refresh_interval = refresh_hours * 3600
        self._failure_retry = failure_retry_minutes * 60
        self._name = name
        self._persist_path = persist_path
        self._persist_table = persist_table
        self._cache = {}
        self._lock = threading.Lock()
        self._concurrency = threading.Semaphore(10)
        self._inflight = set()

        if persist_path:
            self._load_from_disk()
        if scanner_interval > 0:
            threading.Thread(target=self._scanner_loop, args=(scanner_interval,),
                             daemon=True).start()

    def _key_str(self, key):
        return json.dumps(key, sort_keys=True)

    def set(self, key, value):
        with self._lock:
            self._cache[key] = {
                "value": value,
                "last_refreshed": time.time(),
                "refresh_interval": self._default_refresh_interval,
            }
        self._persist_key(key)

    def get(self, key):
        value, _stale = self._get_with_age(key)
        return value

    def get_with_age(self, key):
        """Return (value, stale). Missing keys count as stale."""
        value, age = self._get_with_age(key)
        return value, age is None or age > self._default_refresh_interval

    def _get_with_age(self, key):
        with self._lock:
            entry = self._cache.get(key)
            if entry and entry["value"] is not None:
                return entry["value"], time.time() - entry["last_refreshed"]
        if self._persist_table:
            entry = vault.catalog_cache_get(self._persist_key_str(key))
            if entry and entry.get("value") is not None:
                with self._lock:
                    self._cache[key] = {
                        "value": entry["value"],
                        "last_refreshed": entry.get("last_refreshed", 0.0),
                        "refresh_interval": self._default_refresh_interval,
                    }
                    last = self._cache[key]["last_refreshed"]
                return entry["value"], time.time() - last
        return None, None

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

    def _persist_key_str(self, key):
        return f"{self._persist_table}:{self._key_str(key)}"

    def _persist_key(self, key):
        if not self._persist_table:
            return
        with self._lock:
            entry = self._cache.get(key)
            if not entry:
                return
            payload = dict(entry)
        try:
            vault.catalog_cache_set(self._persist_key_str(key), payload)
        except OSError:
            pass

    def _scanner_loop(self, interval):
        while True:
            time.sleep(interval)
            with self._lock:
                keys = list(self._cache.keys())
            for key in keys:
                self._do_ensure(key)

    def _do_ensure(self, key, meta=None):
        key_id = self._key_str(key)
        with self._lock:
            if key_id in self._inflight:
                return
            self._inflight.add(key_id)
        try:
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
                    self._persist_key(key)
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
        finally:
            with self._lock:
                self._inflight.discard(key_id)
