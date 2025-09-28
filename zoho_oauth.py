import os
import sqlite3
import time
from pathlib import Path
from urllib.parse import urlencode
import logging
import requests

# ------------ Config (env-driven) ------------
# DB in **current working directory** by default
ZOHO_TOKEN_DB = os.getenv("ZOHO_TOKEN_DB", str(Path.cwd() / "zoho_oauth.sqlite"))

# hardcoded prototype user id
ZOHO_USER_ID = int(os.getenv("ZOHO_USER_ID", "1"))

# Zoho OAuth app
ZOHO_CLIENT_ID = os.getenv("ZOHO_CLIENT_ID")
ZOHO_CLIENT_SECRET = os.getenv("ZOHO_CLIENT_SECRET")
ZOHO_REDIRECT_URI = os.getenv("ZOHO_REDIRECT_URI")  # must match Zoho console
ZOHO_ACCOUNTS_URL = os.getenv("ZOHO_ACCOUNTS_URL", "accounts.zoho.com")
ZOHO_OAUTH_SCOPE = os.getenv("ZOHO_OAUTH_SCOPE", "ZohoCRM.modules.ALL")

# API domain for CRM requests (may be overridden by api_domain in token response)
ZOHO_API_DOMAIN = os.getenv("ZOHO_API_DOMAIN", "www.zohoapis.com")



# ------------ Storage ------------
def _db():
    conn = sqlite3.connect(ZOHO_TOKEN_DB)
    conn.row_factory = sqlite3.Row
    conn.execute("""
    CREATE TABLE IF NOT EXISTS Zoho_Channels (
        Id INTEGER PRIMARY KEY AUTOINCREMENT,
        Account_Id TEXT DEFAULT NULL,
        Access_Token TEXT DEFAULT NULL,
        Refresh_Token TEXT DEFAULT NULL,
        AT_Expiry INTEGER DEFAULT NULL,
        RT_Expiry INTEGER DEFAULT NULL,
        Created_Time INTEGER NOT NULL,
        Modified_Time INTEGER NOT NULL,
        Domain TEXT DEFAULT NULL,
        User_Id INTEGER DEFAULT NULL
    )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_Zoho_Channels_UserId ON Zoho_Channels(User_Id)")
    return conn

def _now() -> int:
    return int(time.time())

def get_zoho_api_base() -> str:
    """
    Returns the correct Zoho API base URL:
    - Uses Domain saved in Zoho_Channels table if available
    - Falls back to ZOHO_API_DOMAIN from env
    Always returns a full https:// URL.
    """
    row = get_latest_row_for_user(ZOHO_USER_ID)
    domain = row["Domain"] if row and row["Domain"] else os.getenv("ZOHO_API_DOMAIN", "www.zohoapis.com")
    # ensure it's a full URL
    if domain.startswith("http"):
        return domain.rstrip("/")
    return f"https://{domain.rstrip('/')}"


def get_latest_row_for_user(user_id: int):
    with _db() as conn:
        row = conn.execute(
            "SELECT * FROM Zoho_Channels WHERE User_Id=? ORDER BY Id DESC LIMIT 1",
            (user_id,)
        ).fetchone()
        return dict(row) if row else None

def _insert_row(user_id: int, access: str, refresh: str, at_exp: int, rt_exp: int|None, domain: str|None, account_id: str|None):
    now = _now()
    with _db() as conn:
        conn.execute("""
        INSERT INTO Zoho_Channels (Account_Id, Access_Token, Refresh_Token, AT_Expiry, RT_Expiry, 
                                   Created_Time, Modified_Time, Domain, User_Id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (account_id, access, refresh, at_exp, rt_exp, now, now, domain, user_id))
        conn.commit()

def _update_row(id_: int, *, access: str|None=None, refresh: str|None=None,
                at_exp: int|None=None, rt_exp: int|None=None, domain: str|None=None):
    sets, vals = [], []
    if access is not None: sets.append("Access_Token=?"); vals.append(access)
    if refresh is not None: sets.append("Refresh_Token=?"); vals.append(refresh)
    if at_exp is not None: sets.append("AT_Expiry=?"); vals.append(at_exp)
    if rt_exp is not None: sets.append("RT_Expiry=?"); vals.append(rt_exp)
    if domain is not None: sets.append("Domain=?"); vals.append(domain)
    sets.append("Modified_Time=?"); vals.append(_now())
    vals.append(id_)
    with _db() as conn:
        conn.execute(f"UPDATE Zoho_Channels SET {', '.join(sets)} WHERE Id=?", vals)
        conn.commit()

def save_tokens_for_user(user_id: int, token_json: dict):
    access = token_json.get("access_token")
    refresh = token_json.get("refresh_token")  # often absent on refresh
    expires_in = int(token_json.get("expires_in", 3600))
    at_exp = _now() + expires_in - 30  # safety buffer
    rt_exp = None  # Zoho doesn’t provide; keep NULL
    domain = token_json.get("api_domain") or os.getenv("ZOHO_API_DOMAIN", "www.zohoapis.com")
    account_id = None

    row = get_latest_row_for_user(user_id)
    if row is None:
        _insert_row(user_id, access, refresh, at_exp, rt_exp, domain, account_id)
    else:
        _update_row(
            row["Id"],
            access=access,
            refresh=refresh or row["Refresh_Token"],
            at_exp=at_exp,
            rt_exp=rt_exp,
            domain=domain or row["Domain"]
        )

def clear_tokens_for_user(user_id: int):
    row = get_latest_row_for_user(user_id)
    if not row:
        return
    with _db() as conn:
        conn.execute("DELETE FROM Zoho_Channels WHERE Id=?", (row["Id"],))
        conn.commit()

# ------------ OAuth HTTP helpers ------------
def zoho_authorize_url(state: str) -> str:
    params = {
        "response_type": "code",
        "client_id": ZOHO_CLIENT_ID,
        "scope": ZOHO_OAUTH_SCOPE,
        "redirect_uri": ZOHO_REDIRECT_URI,
        "access_type": "offline",
        "prompt": "consent",
        "state": state,
    }
    return f"https://{ZOHO_ACCOUNTS_URL}/oauth/v2/auth?" + urlencode(params)

def zoho_exchange_code_for_tokens(code: str) -> dict:
    url = f"https://{ZOHO_ACCOUNTS_URL}/oauth/v2/token"
    data = {
        "grant_type": "authorization_code",
        "client_id": ZOHO_CLIENT_ID,
        "client_secret": ZOHO_CLIENT_SECRET,
        "redirect_uri": ZOHO_REDIRECT_URI,
        "code": code,
    }
    r = requests.post(url, data=data, timeout=20)
    r.raise_for_status()
    return r.json()

def zoho_refresh_access_token(refresh_token: str) -> dict:
    url = f"https://{ZOHO_ACCOUNTS_URL}/oauth/v2/token"
    data = {
        "grant_type": "refresh_token",
        "client_id": ZOHO_CLIENT_ID,
        "client_secret": ZOHO_CLIENT_SECRET,
        "refresh_token": refresh_token,
    }
    r = requests.post(url, data=data, timeout=20)
    r.raise_for_status()
    return r.json()

def zoho_revoke_token(token: str):
    url = f"https://{ZOHO_ACCOUNTS_URL}/oauth/v2/token/revoke"
    r = requests.post(url, params={"token": token}, timeout=20)
    return r.status_code, r.text

def get_access_token_by_user(user_id: int) -> str | None:
    row = get_latest_row_for_user(user_id)
    if not row:
        return None

    # still valid?
    if row.get("Access_Token") and row.get("AT_Expiry") and _now() < int(row["AT_Expiry"]):
        return row["Access_Token"]

    # refresh
    rt = row.get("Refresh_Token")
    if not rt:
        return None

    try:
        res = zoho_refresh_access_token(rt)
        save_tokens_for_user(user_id, res)
        return res.get("access_token")
    except Exception:
        logging.exception("Failed to refresh Zoho access token for user_id=%s", user_id)
        return None
