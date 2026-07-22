_creds = {}
_initialized = False


def set(lib_id, user, password):
    _creds[lib_id] = {"user": user, "password": password}


def get(lib_id):
    return _creds.get(lib_id)


def remove(lib_id):
    _creds.pop(lib_id, None)


def has(lib_id):
    return lib_id in _creds


def list_connected():
    return {k for k in _creds}


def check_clear_on_restart(conn):
    global _initialized
    if not _initialized:
        _initialized = True
        if not _creds:
            conn.execute("DELETE FROM borrow_events")
            conn.commit()
