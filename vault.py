import hashlib
import json
import os
import secrets
import sqlite3
import threading
import time

from cryptography.fernet import Fernet

_DB_PATH = os.path.join(os.path.dirname(__file__), "bibliorecs.db")
_KEY_PATH = os.path.join(os.path.dirname(__file__), "vault_key.bin")

_conn = None
_lock = threading.Lock()
_cipher_obj = None


def _get_conn():
    global _conn
    with _lock:
        if _conn is None:
            _conn = sqlite3.connect(_DB_PATH, check_same_thread=False)
            _conn.row_factory = sqlite3.Row
            _conn.execute("PRAGMA journal_mode=WAL")
            _init_schema(_conn)
        return _conn


def _init_schema(conn):
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS accounts (
        id TEXT PRIMARY KEY,
        created_at REAL NOT NULL
    );
    CREATE TABLE IF NOT EXISTS devices (
        id TEXT PRIMARY KEY,
        account_id TEXT NOT NULL REFERENCES accounts(id),
        token_hash TEXT NOT NULL UNIQUE,
        name TEXT NOT NULL DEFAULT '',
        last_seen REAL NOT NULL,
        created_at REAL NOT NULL
    );
    CREATE TABLE IF NOT EXISTS pair_codes (
        code TEXT PRIMARY KEY,
        account_id TEXT NOT NULL,
        expires_at REAL NOT NULL,
        attempts INTEGER NOT NULL DEFAULT 0,
        created_at REAL NOT NULL
    );
    CREATE TABLE IF NOT EXISTS vault (
        account_id TEXT NOT NULL,
        key TEXT NOT NULL,
        value TEXT NOT NULL,
        updated_at REAL NOT NULL,
        PRIMARY KEY (account_id, key)
    );
    CREATE TABLE IF NOT EXISTS catalog_cache (
        cache_key TEXT PRIMARY KEY,
        value TEXT NOT NULL,
        last_refreshed REAL NOT NULL
    );
    """)
    conn.commit()


def _cipher():
    global _cipher_obj
    if _cipher_obj is None:
        key = os.environ.get("BIBLIORECS_SECRET_KEY")
        if not key:
            if not os.path.exists(_KEY_PATH):
                _KEY_PATH_DIR = os.path.dirname(_KEY_PATH)
                if _KEY_PATH_DIR:
                    os.makedirs(_KEY_PATH_DIR, exist_ok=True)
                with open(_KEY_PATH, "wb") as f:
                    f.write(Fernet.generate_key())
                os.chmod(_KEY_PATH, 0o600)
            with open(_KEY_PATH, "rb") as f:
                key = f.read().decode()
        _cipher_obj = Fernet(key.encode() if isinstance(key, str) else key)
    return _cipher_obj


def _encrypt(text):
    return _cipher().encrypt(text.encode("utf-8")).decode("ascii")


def _decrypt(text):
    return _cipher().decrypt(text.encode("ascii")).decode("utf-8")


def _hash_token(token):
    return hashlib.sha256(token.encode()).hexdigest()


def _now():
    return time.time()


# ── accounts / devices ──

def create_device(token=None, name=""):
    conn = _get_conn()
    with _lock:
        account_id = secrets.token_hex(16)
        device_id = secrets.token_hex(16)
        now = _now()
        if token is None:
            token = secrets.token_urlsafe(32)
        conn.execute("INSERT INTO accounts (id, created_at) VALUES (?, ?)",
                     (account_id, now))
        conn.execute(
            "INSERT INTO devices (id, account_id, token_hash, name, last_seen, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (device_id, account_id, _hash_token(token), name, now, now))
        conn.commit()
    return {"account_id": account_id, "device_id": device_id, "token": token}


def account_for_token(token):
    if not token:
        return None
    conn = _get_conn()
    row = conn.execute(
        "SELECT account_id FROM devices WHERE token_hash = ?",
        (_hash_token(token),)).fetchone()
    return row["account_id"] if row else None


def touch_device(token):
    conn = _get_conn()
    with _lock:
        conn.execute(
            "UPDATE devices SET last_seen = ? WHERE token_hash = ?",
            (_now(), _hash_token(token)))
        conn.commit()


def current_device(token):
    conn = _get_conn()
    row = conn.execute(
        "SELECT id, account_id FROM devices WHERE token_hash = ?",
        (_hash_token(token),)).fetchone()
    return dict(row) if row else None


def list_devices(account_id):
    conn = _get_conn()
    rows = conn.execute(
        "SELECT id, name, last_seen FROM devices WHERE account_id = ? ORDER BY created_at",
        (account_id,)).fetchall()
    return [dict(r) for r in rows]


def revoke_device(device_id, account_id):
    conn = _get_conn()
    with _lock:
        cur = conn.execute(
            "DELETE FROM devices WHERE id = ? AND account_id = ?",
            (device_id, account_id))
        conn.commit()
        return cur.rowcount > 0


def forget_device(device_id):
    conn = _get_conn()
    with _lock:
        row = conn.execute(
            "SELECT account_id FROM devices WHERE id = ?", (device_id,)).fetchone()
        if not row:
            return None
        conn.execute("DELETE FROM devices WHERE id = ?", (device_id,))
        _gc_account(conn, row["account_id"])
        conn.commit()
        return row["account_id"]


def _gc_account(conn, account_id):
    row = conn.execute(
        "SELECT COUNT(*) AS c FROM devices WHERE account_id = ?",
        (account_id,)).fetchone()
    if row["c"] > 0:
        return
    conn.execute("DELETE FROM accounts WHERE id = ?", (account_id,))
    conn.execute("DELETE FROM vault WHERE account_id = ?", (account_id,))


# ── pair codes ──

def create_pair_code(account_id, ttl_sec=600):
    conn = _get_conn()
    with _lock:
        conn.execute("DELETE FROM pair_codes WHERE account_id = ?", (account_id,))
        code = f"{secrets.randbelow(1000000):06d}"
        conn.execute(
            "INSERT INTO pair_codes (code, account_id, expires_at, attempts, created_at) VALUES (?, ?, ?, 0, ?)",
            (code, account_id, _now() + ttl_sec, _now()))
        conn.commit()
        return code


def claim_pair_code(code, device_id):
    conn = _get_conn()
    with _lock:
        row = conn.execute(
            "SELECT account_id, expires_at, attempts FROM pair_codes WHERE code = ?",
            (code,)).fetchone()
        if not row:
            return {"error": "invalid code"}
        if _now() > row["expires_at"]:
            conn.execute("DELETE FROM pair_codes WHERE code = ?", (code,))
            conn.commit()
            return {"error": "code expired"}
        if row["attempts"] >= 5:
            conn.execute("DELETE FROM pair_codes WHERE code = ?", (code,))
            conn.commit()
            return {"error": "too many attempts"}
        dev = conn.execute(
            "SELECT account_id FROM devices WHERE id = ?", (device_id,)).fetchone()
        if dev and dev["account_id"] == row["account_id"]:
            conn.execute(
                "UPDATE pair_codes SET attempts = attempts + 1 WHERE code = ?",
                (code,))
            conn.commit()
            return {"error": "already linked to this account"}
        conn.execute(
            "UPDATE pair_codes SET attempts = attempts + 1 WHERE code = ?",
            (code,))
        conn.execute(
            "UPDATE devices SET account_id = ? WHERE id = ?",
            (row["account_id"], device_id))
        conn.execute("DELETE FROM pair_codes WHERE code = ?", (code,))
        if dev:
            _gc_account(conn, dev["account_id"])
        conn.commit()
        return {"success": True}


# ── creds (encrypted) ──

def get_creds(account_id, library_id):
    conn = _get_conn()
    row = conn.execute(
        "SELECT value FROM vault WHERE account_id = ? AND key = ?",
        (account_id, f"creds:{library_id}")).fetchone()
    if not row:
        return None
    try:
        return json.loads(_decrypt(row["value"]))
    except Exception:
        return None


def set_creds(account_id, library_id, creds):
    conn = _get_conn()
    with _lock:
        conn.execute(
            "INSERT OR REPLACE INTO vault (account_id, key, value, updated_at) VALUES (?, ?, ?, ?)",
            (account_id, f"creds:{library_id}", _encrypt(json.dumps(creds)), _now()))
        conn.commit()


def delete_creds(account_id, library_id):
    conn = _get_conn()
    with _lock:
        for k in (f"creds:{library_id}", f"holds:{library_id}",
                  f"checkouts:{library_id}", f"history:{library_id}"):
            conn.execute(
                "DELETE FROM vault WHERE account_id = ? AND key = ?",
                (account_id, k))
        conn.commit()


# ── account data (encrypted) ──

def get_account_data(account_id, key):
    conn = _get_conn()
    row = conn.execute(
        "SELECT value, updated_at FROM vault WHERE account_id = ? AND key = ?",
        (account_id, key)).fetchone()
    if not row:
        return None, 0.0
    try:
        return json.loads(_decrypt(row["value"])), row["updated_at"]
    except Exception:
        return None, row["updated_at"]


def set_account_data(account_id, key, value):
    conn = _get_conn()
    with _lock:
        conn.execute(
            "INSERT OR REPLACE INTO vault (account_id, key, value, updated_at) VALUES (?, ?, ?, ?)",
            (account_id, key, _encrypt(json.dumps(value)), _now()))
        conn.commit()


# ── catalog cache (plain, shared across accounts) ──

def catalog_cache_get(cache_key):
    conn = _get_conn()
    row = conn.execute(
        "SELECT value FROM catalog_cache WHERE cache_key = ?",
        (cache_key,)).fetchone()
    if not row:
        return None
    try:
        return json.loads(row["value"])
    except Exception:
        return None


def catalog_cache_set(cache_key, entry):
    conn = _get_conn()
    with _lock:
        conn.execute(
            "INSERT OR REPLACE INTO catalog_cache (cache_key, value) VALUES (?, ?)",
            (cache_key, json.dumps(entry)))
        conn.commit()
