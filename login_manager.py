import threading

import api
import vault


_login_locks = {}
_login_guard = threading.Lock()


def _lock_for(account_id, library_id):
    key = (account_id, library_id)
    with _login_guard:
        lock = _login_locks.get(key)
        if lock is None:
            lock = _login_locks[key] = threading.Lock()
        return lock


def renew_creds(account_id, library_id, creds):
    """Serialized BC re-login: at most one concurrent login per (account, library).

    If another thread logged in while we were waiting, its fresh credentials are
    returned instead of issuing a second login.
    """
    with _lock_for(account_id, library_id):
        latest = vault.get_creds(account_id, library_id) or creds
        if latest.get("bc_token") != creds.get("bc_token"):
            return latest
        bc_token, session_id, account_id_bc = api.login(
            library_id, creds["user"], creds["password"])
        new_creds = {
            **creds,
            "bc_token": bc_token,
            "session_id": session_id,
            "account_id": account_id_bc,
        }
        vault.set_creds(account_id, library_id, new_creds)
        return new_creds
