_creds = {}


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
