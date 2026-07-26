import threading
import time


class RefreshCache:
    def __init__(self, refresh_func, refresh_hours=4, failure_retry_minutes=5, name=""):
        self._refresh_func = refresh_func
        self._default_refresh_interval = refresh_hours * 3600
        self._failure_retry = failure_retry_minutes * 60
        self._name = name
        self._cache = {}
        self._lock = threading.Lock()
        self._concurrency = threading.Semaphore(10)

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
                    self._cache[key] = {
                        "value": value,
                        "last_refreshed": time.time(),
                        "refresh_interval": self._default_refresh_interval,
                    }
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
