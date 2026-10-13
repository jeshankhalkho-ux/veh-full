# vehicle-api — Spinny-style vehicle lead API (Flask, Vercel-ready)
#
# Mirrors the two Spinny insurance-platform calls:
#   GET  /sp-insurance-platform/api/v1/leads/get-lead-by-registration-or-proposal/?car_registration_number=JH05CK2458
#   POST /sp-insurance-platform/api/v1/leads/get-or-create-lead/   (JSON body)
#
# Vehicle data comes from the separate vehicle-src project over HTTP
# (VEHICLE_SRC_URL) — no scraper code lives in this repo.
# Leads are kept in leads.json next to this file (dev store — ephemeral
# on serverless; plug in a real DB for production).
import base64
import hashlib
import hmac
import json
import logging
import os
import random
import re
import secrets
import tempfile
import threading
import time
import uuid
from datetime import datetime, timezone
from functools import wraps

import requests
from flask import Flask, jsonify, request
from flask_cors import CORS
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

app = Flask(__name__)
CORS(app)
limiter = Limiter(get_remote_address, app=app, default_limits=["30 per minute"])

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BASE = "/sp-insurance-platform/api/v1/leads"
STORE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "leads.json")
_store_lock = threading.Lock()

# ------------------------------------------------------------------- auth ---
# Short-lived JWT access tokens (default 15 min) + opaque rotating refresh
# tokens (default 7 days). Configure via env:
#   JWT_SECRET=...  ACCESS_TTL_SECONDS=900  REFRESH_TTL_SECONDS=604800
#   API_USERS="admin:secret,staff:other"
JWT_SECRET = os.environ.get("JWT_SECRET", "dev-secret-change-me")
if JWT_SECRET == "dev-secret-change-me":
    logger.warning("JWT_SECRET not set — using insecure dev default; set JWT_SECRET in production")
if "API_USERS" not in os.environ:
    logger.warning("API_USERS not set — using dev default admin:admin")
ACCESS_TTL = int(os.environ.get("ACCESS_TTL_SECONDS", "900"))
REFRESH_TTL = int(os.environ.get("REFRESH_TTL_SECONDS", "604800"))
TOKEN_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tokens.json")

API_USERS = {}
for _pair in os.environ.get("API_USERS", "admin:admin").split(","):
    if ":" in _pair:
        _u, _p = _pair.split(":", 1)
        if _u.strip():
            API_USERS[_u.strip()] = _p

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
# ------------------------------------------------- local data (old API) ---
# RC/vehicle details come from the SEPARATE vehicle-src project over HTTP —
# no scraper code lives here. Override with VEHICLE_SRC_URL if it moves.
VEHICLE_SRC_URL = os.environ.get("VEHICLE_SRC_URL", "https://vehicle-api-tau.vercel.app").rstrip("/")


def lookup_vehicle(vnum):
    """GET the separate old vehicle API. Returns (merged_dict, [sources])."""
    r = None
    for attempt in (1, 2):
        try:
            r = requests.get(f"{VEHICLE_SRC_URL}/api/vehicle", params={"vnum": vnum},
                             headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
                             timeout=55)
            break
        except Exception as e:
            logger.warning("old vehicle-api attempt %s failed for %s: %s", attempt, vnum, e)
            time.sleep(2)
    if r is None or r.status_code != 200:
        return None, []
    try:
        data = r.json()
    except ValueError:
        return None, []
    if not isinstance(data, dict) or data.get("status") != "success":
        return None, []
    data.pop("status", None)
    sources = data.pop("data_source", []) or []
    if not data or set(data) <= {"registration_number"}:
        return None, []
    return data, sources


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")




















# ------------------------------------------------------------------ store ---

_MEM = {}


def _write_json(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, indent=2)
    os.replace(tmp, path)


def _load_json(path, mem_key):
    """disk -> /tmp -> memory. Serverless (Vercel) is read-only except /tmp."""
    for candidate in (path, os.path.join(tempfile.gettempdir(), os.path.basename(path))):
        try:
            with open(candidate, "r", encoding="utf-8") as fh:
                data = json.load(fh)
                if isinstance(data, dict):
                    return data
        except (FileNotFoundError, ValueError, OSError):
            continue
    return _MEM.get(mem_key, {})


def _save_json(path, mem_key, obj):
    """Best-effort save, never raises. Returns disk | tmp | memory."""
    try:
        _write_json(path, obj)
        return "disk"
    except OSError:
        pass
    try:
        _write_json(os.path.join(tempfile.gettempdir(), os.path.basename(path)), obj)
        return "tmp"
    except OSError:
        _MEM[mem_key] = obj
        return "memory"


def _load_store():
    return _load_json(STORE_PATH, "leads")


def _save_store(store):
    _save_json(STORE_PATH, "leads", store)


def get_lead(reg):
    with _store_lock:
        return _load_store().get(reg)


def save_lead(lead):
    with _store_lock:
        store = _load_store()
        store[lead["car_registration_number"]] = lead
        _save_store(store)


def _load_tokens():
    return _load_json(TOKEN_PATH, "tokens")


def _save_tokens(tokens):
    _save_json(TOKEN_PATH, "tokens", tokens)


def _b64url(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _b64url_dec(s):
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def jwt_encode(payload):
    head = _b64url(json.dumps({"alg": "HS256", "typ": "JWT"}, separators=(",", ":")).encode())
    body = _b64url(json.dumps(payload, separators=(",", ":")).encode())
    sig = hmac.new(JWT_SECRET.encode(), f"{head}.{body}".encode(), hashlib.sha256).digest()
    return f"{head}.{body}.{_b64url(sig)}"


def jwt_decode(token):
    try:
        head, body, sig = str(token).split(".")
        expect = hmac.new(JWT_SECRET.encode(), f"{head}.{body}".encode(), hashlib.sha256).digest()
        if not hmac.compare_digest(_b64url(expect), sig):
            return None
        payload = json.loads(_b64url_dec(body))
        if int(payload.get("exp", 0)) < int(time.time()):
            return None
        return payload
    except Exception:
        return None


def issue_pair(username):
    now = int(time.time())
    access = jwt_encode({"sub": username, "iat": now, "exp": now + ACCESS_TTL})
    refresh = secrets.token_urlsafe(32)
    with _store_lock:
        tokens = _load_tokens()
        tokens[hashlib.sha256(refresh.encode()).hexdigest()] = {
            "sub": username, "exp": now + REFRESH_TTL, "created_at": now_iso(),
        }
        tokens = {h: r for h, r in tokens.items() if r.get("exp", 0) >= now}
        _save_tokens(tokens)
    return {"access_token": access, "refresh_token": refresh,
            "token_type": "Bearer", "expires_in": ACCESS_TTL}


# ----------------------------------------------------------------- helpers ---

REG_RE = re.compile(r"^[A-Z]{2}\d{1,2}[A-Z]{1,3}\d{4}$")


def normalize_reg(raw):
    return re.sub(r"[^A-Z0-9]", "", str(raw or "").upper())


def valid_reg(reg):
    return bool(REG_RE.match(reg))


def require_auth(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            return jsonify({"status": "failed", "message": "Missing Bearer access token"}), 401
        payload = jwt_decode(auth[7:].strip())
        if not payload:
            return jsonify({"status": "failed",
                            "message": "Invalid or expired access token — POST /auth/refresh to renew"}), 401
        request.api_user = payload.get("sub")
        return fn(*args, **kwargs)
    return wrapper


@app.errorhandler(429)
def _rate_limited(e):
    msg = "Too many attempts — wait a minute and retry"
    if request.path.startswith(("/api", "/auth")):
        return jsonify({"ok": False, "status": "failed", "message": msg}), 429
    return msg, 429


@app.errorhandler(500)
def _server_error(e):
    logger.exception("unhandled error on %s", request.path)
    if request.path.startswith(("/api", "/auth")):
        return jsonify({"ok": False, "status": "failed",
                        "message": "Server error — check Render logs for the traceback"}), 500
    return "Internal server error", 500


@app.route("/auth/login", methods=["POST"])
@limiter.limit("10 per minute")
def auth_login():
    body = request.get_json(silent=True) or {}
    username = str(body.get("username", "")).strip()
    if not username or API_USERS.get(username) != str(body.get("password", "")):
        return jsonify({"status": "failed", "message": "Invalid credentials"}), 401
    return jsonify({"status": "success", "username": username, **issue_pair(username)})


@app.route("/auth/refresh", methods=["POST"])
@limiter.limit("30 per minute")
def auth_refresh():
    body = request.get_json(silent=True) or {}
    rt = str(body.get("refresh_token", ""))
    if not rt:
        return jsonify({"status": "failed", "message": "refresh_token is required"}), 401
    digest = hashlib.sha256(rt.encode()).hexdigest()
    now = int(time.time())
    with _store_lock:
        tokens = _load_tokens()
        rec = tokens.pop(digest, None)
        _save_tokens(tokens)
    if not rec or rec.get("exp", 0) < now:
        return jsonify({"status": "failed",
                        "message": "Refresh token expired, revoked or already used — POST /auth/login again"}), 401
    return jsonify({"status": "success", "username": rec.get("sub"),
                    "rotated": True, **issue_pair(rec.get("sub"))})


@app.route("/auth/logout", methods=["POST"])
def auth_logout():
    body = request.get_json(silent=True) or {}
    rt = str(body.get("refresh_token", ""))
    if rt:
        with _store_lock:
            tokens = _load_tokens()
            tokens.pop(hashlib.sha256(rt.encode()).hexdigest(), None)
            _save_tokens(tokens)
    return jsonify({"status": "success", "message": "Logged out"})


# ------------------------------------------------------- spinny vault ---
# Same shape as the reference refreshtoken service:
#   GET  /refreshtoken?key=...               -> OTP page (vault key gate)
#   POST /api/request-otp?key=...   {phone}  -> {ok, message}
#   POST /api/verify-otp?key=... {phone,otp} -> {ok, exp, token} (saves vault)
#   GET  /api/token-status?key=...           -> {ok, present, phone, exp, expired}
# Upstream is Spinny's real OTP API by default (override via env):
#   SPINNY_REQUEST_OTP_URL / SPINNY_VERIFY_OTP_URL
# otp-request: {phone_number, whatsapp_opt_in} -> {message}
# verify-otp:  {phone_number, otp, cid}       -> {access_token, token_type} (+ cookie)
VAULT_KEY = os.environ.get("REFRESH_VAULT_KEY", "abhi")
SPINNY_REQUEST_OTP_URL = os.environ.get(
    "SPINNY_REQUEST_OTP_URL",
    "https://api.spinnyinsurance.com/sp-insurance-platform/api/v1/accounts/otp-request/")
SPINNY_VERIFY_OTP_URL = os.environ.get(
    "SPINNY_VERIFY_OTP_URL",
    "https://api.spinnyinsurance.com/sp-insurance-platform/api/v1/accounts/verify-otp/")
VAULT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "spinny_token.json")

REFRESH_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>Spinny Token Refresh</title>
<style>
  :root { --bg:#0b0f14; --card:#141a22; --line:#243041; --txt:#e8eef7; --mut:#8b9bb4; --acc:#3b82f6; --ok:#22c55e; --bad:#ef4444; }
  *{box-sizing:border-box}
  body{margin:0;font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;background:var(--bg);color:var(--txt);min-height:100vh;display:flex;align-items:center;justify-content:center;padding:20px}
  .card{width:100%;max-width:420px;background:var(--card);border:1px solid var(--line);border-radius:16px;padding:28px;box-shadow:0 10px 40px rgba(0,0,0,.35)}
  h1{margin:0 0 6px;font-size:1.25rem}
  p.sub{margin:0 0 20px;color:var(--mut);font-size:.9rem}
  label{display:block;font-size:.8rem;color:var(--mut);margin:12px 0 6px}
  input{width:100%;padding:12px 14px;border-radius:10px;border:1px solid var(--line);background:#0e141c;color:var(--txt);font-size:1rem}
  input:focus{outline:none;border-color:var(--acc)}
  .row{display:flex;gap:10px;margin-top:16px}
  button{flex:1;padding:12px;border:0;border-radius:10px;font-weight:600;cursor:pointer;font-size:.95rem}
  .primary{background:var(--acc);color:#fff}
  .ghost{background:#1c2533;color:var(--txt)}
  .status{margin-top:16px;padding:12px;border-radius:10px;background:#0e141c;border:1px solid var(--line);font-size:.9rem;white-space:pre-wrap;word-break:break-all}
  .ok{border-color:rgba(34,197,94,.4);color:var(--ok)}
  .bad{border-color:rgba(239,68,68,.4);color:var(--bad)}
  .muted{color:var(--mut)}
  .step{font-size:.75rem;color:var(--mut);margin-bottom:8px}
</style>
</head>
<body>
<div class="card">
  <h1>Spinny Token Refresh</h1>
  <p class="sub">Apna number + OTP se naya access token save hoga.</p>
  <div class="step" id="cur">Current: checking...</div>

  <label>Phone (10 digit)</label>
  <input id="phone" type="tel" maxlength="10" placeholder="9xxxxxxxxx" value=""/>

  <label>OTP</label>
  <input id="otp" type="text" maxlength="8" placeholder="After request OTP"/>

  <div class="row">
    <button class="ghost" onclick="reqOtp()">1. Request OTP</button>
    <button class="primary" onclick="verify()">2. Verify &amp; Save</button>
  </div>
  <div id="status" class="status muted">Ready</div>
</div>
<script>
const KEY = "__VAULT_KEY__";
async function cur(){
  try{
    const r=await fetch('/api/token-status?key='+encodeURIComponent(KEY));
    const j=await r.json();
    document.getElementById('cur').textContent = j.present
      ? 'Current: '+(j.phone||'?')+' \\u00b7 exp '+(j.exp||'?')+' \\u00b7 '+(j.expired?'EXPIRED':'OK')
      : 'Current: \\u2014 \\u00b7 exp \\u2014 \\u00b7 MISSING';
  }catch(e){ document.getElementById('cur').textContent='Current: unknown'; }
}
async function post(path, body){
  const ctl=new AbortController();
  const t=setTimeout(()=>ctl.abort(), 60000);
  try{
    const r=await fetch(path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body),signal:ctl.signal});
    return await r.json().catch(()=>({ok:false,message:'Server HTTP '+r.status}));
  }catch(e){ return {ok:false,message:'Network/timeout: '+e.message}; }
  finally{ clearTimeout(t); }
}
async function reqOtp(){
  const phone=document.getElementById('phone').value.trim();
  set('Requesting OTP...','');
  const j=await post('/api/request-otp?key='+encodeURIComponent(KEY),{phone});
  set(j.message||JSON.stringify(j), j.ok?'ok':'bad');
}
async function verify(){
  const phone=document.getElementById('phone').value.trim();
  const otp=document.getElementById('otp').value.trim();
  set('Verifying (up to ~60s on cold start)...','');
  const j=await post('/api/verify-otp?key='+encodeURIComponent(KEY),{phone,otp});
  if(j.ok){ set('Token saved. Expiry: '+j.exp+'\\n'+(j.token||'').slice(0,40)+'...','ok'); cur(); }
  else set(j.message||JSON.stringify(j),'bad');
}
function set(t,c){const e=document.getElementById('status');e.textContent=t;e.className='status '+(c||'muted');}
cur();
</script>
</body>
</html>"""


def _load_vault():
    return _load_json(VAULT_PATH, "vault")


def _save_vault(vault):
    return _save_json(VAULT_PATH, "vault", vault)


def jwt_payload_unverified(token):
    try:
        parts = str(token).split(".")
        if len(parts) != 3:
            return None
        return json.loads(_b64url_dec(parts[1]))
    except Exception:
        return None


def vault_status():
    v = _load_vault()
    exp = v.get("exp")
    return {
        "present": bool(v.get("token")),
        "phone": v.get("phone"),
        "exp": v.get("exp_iso"),
        "expired": True if not exp else int(exp) < int(time.time()),
        "updated_at": v.get("updated_at"),
    }


def require_vault_key(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if request.args.get("key", "") != VAULT_KEY:
            if "text/html" in request.headers.get("Accept", ""):
                return "Forbidden (bad key)", 403
            return jsonify({"ok": False, "message": "Forbidden (bad key)"}), 403
        return fn(*args, **kwargs)
    return wrapper


def _call_spinny(url, payload):
    t0 = time.time()
    r = requests.post(url, json=payload, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/155.0.0.0 Safari/537.36",
        "Accept": "application/json",
        "Accept-Language": "en-US,en;q=0.9",
        "Origin": "https://www.spinnyinsurance.com",
        "Referer": "https://www.spinnyinsurance.com/",
    }, timeout=20)
    ms = int((time.time() - t0) * 1000)
    short = "/".join(url.rstrip("/").rsplit("/", 2)[-2:])
    logger.info("spinny POST %s -> HTTP %s in %sms", short, r.status_code, ms)
    if r.status_code != 200:
        logger.info("spinny POST %s error body: %.500s", short, r.text)
    try:
        data = r.json()
    except ValueError:
        data = {"raw": r.text[:500]}
    return r.status_code, data, dict(r.cookies)


def _extract_token(data):
    if not isinstance(data, dict):
        return None
    for k in ("access_token", "accessToken", "token", "id_token"):
        if data.get(k):
            return data[k]
    nested = data.get("data")
    if isinstance(nested, dict):
        for k in ("access_token", "accessToken", "token"):
            if nested.get(k):
                return nested[k]
    return None


def _spinny_error(data, code):
    """Pull a human reason out of Spinny's error body (plain or DRF-style)."""
    if isinstance(data, dict):
        for k in ("message", "error", "detail", "msg"):
            if data.get(k):
                return str(data[k])[:300]
        bits = []
        for k, v in data.items():
            if k in ("access_token", "token", "id_token"):
                continue
            bits.append(f"{k}: {', '.join(map(str, v)) if isinstance(v, list) else v}")
        if bits:
            return "; ".join(bits)[:300]
    return f"OTP verification failed (HTTP {code})"


@app.route("/refreshtoken", methods=["GET"])
@require_vault_key
def refreshtoken_page():
    return REFRESH_PAGE.replace("__VAULT_KEY__", VAULT_KEY), 200, {"Content-Type": "text/html; charset=utf-8"}


@app.route("/api/request-otp", methods=["POST"])
@limiter.limit("10 per minute")
@require_vault_key
def vault_request_otp():
    body = request.get_json(silent=True) or {}
    if not isinstance(body, dict):
        body = {}
    phone = re.sub(r"\D", "", str(body.get("phone", "")))
    if len(phone) != 10:
        return jsonify({"ok": False, "message": "Enter a 10-digit phone number"})
    if not SPINNY_REQUEST_OTP_URL:
        return jsonify({"ok": False, "message": "Spinny OTP upstream not configured — set SPINNY_REQUEST_OTP_URL"})
    try:
        code, data, _cookies = _call_spinny(
            SPINNY_REQUEST_OTP_URL, {"phone_number": phone, "whatsapp_opt_in": True})
    except Exception as e:
        return jsonify({"ok": False, "message": f"Upstream error: {e}"})
    if code != 200:
        return jsonify({"ok": False, "message": _spinny_error(data, code), "upstream_http": code})
    msg = data.get("message") if isinstance(data, dict) else None
    return jsonify({"ok": True, "message": msg or "OTP sent"})


@app.route("/api/verify-otp", methods=["POST"])
@limiter.limit("10 per minute")
@require_vault_key
def vault_verify_otp():
    body = request.get_json(silent=True) or {}
    if not isinstance(body, dict):
        body = {}
    phone = re.sub(r"\D", "", str(body.get("phone", "")))
    otp = str(body.get("otp", "")).strip()
    if len(phone) != 10 or not otp:
        return jsonify({"ok": False, "message": "Phone (10 digit) + OTP required"})
    if not SPINNY_VERIFY_OTP_URL:
        return jsonify({"ok": False, "message": "Spinny OTP upstream not configured — set SPINNY_VERIFY_OTP_URL"})
    try:
        # cid is Spinny's GA-style client id ("<9 digits>.<epoch>"); fresh one per attempt.
        cid = f"{random.randint(100000000, 999999999)}.{int(time.time())}"
        code, data, cookies = _call_spinny(
            SPINNY_VERIFY_OTP_URL, {"phone_number": phone, "otp": otp, "cid": cid})
    except Exception as e:
        return jsonify({"ok": False, "message": f"Upstream error: {e}"})
    token = _extract_token(data) or cookies.get("access_token")
    if code != 200 or not token:
        return jsonify({"ok": False, "message": _spinny_error(data, code), "upstream_http": code})
    payload = jwt_payload_unverified(token) or {}
    try:
        exp = int(payload.get("exp", 0)) or None
    except (TypeError, ValueError):
        exp = None
    exp_iso = datetime.fromtimestamp(exp, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if exp else None
    with _store_lock:
        persisted = _save_vault({"token": token, "phone": phone, "exp": exp,
                                 "exp_iso": exp_iso, "updated_at": now_iso()})
    return jsonify({"ok": True, "exp": exp_iso, "token": token, "persisted": persisted or False})


@app.route("/api/token-status", methods=["GET"])
@require_vault_key
def vault_token_status():
    return jsonify({"ok": True, **vault_status()})


def _ping_spinny():
    r = requests.get("https://api.spinnyinsurance.com/", headers={"User-Agent": USER_AGENT}, timeout=10)
    return r.status_code


@app.route("/api/debug-spinny", methods=["GET"])
@require_vault_key
def vault_debug_spinny():
    """Reachability check: proves TLS/DNS/Cloudflare from the server side."""
    t0 = time.time()
    try:
        code = _ping_spinny()
        return jsonify({"ok": True, "http_status": code,
                        "ms": int((time.time() - t0) * 1000)})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)[:200],
                        "ms": int((time.time() - t0) * 1000)})


# ------------------------------------------------------------------ routes ---

SPINNY_API = "https://api.spinnyinsurance.com"
CHROME_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/155.0.0.0 Safari/537.36"


def _spinny_lead_headers():
    v = _load_vault()
    tok = v.get("token") if isinstance(v, dict) else None
    if not tok:
        return None
    return {
        "User-Agent": CHROME_UA,
        "Accept": "application/json",
        "Accept-Language": "en-US,en;q=0.9",
        "Origin": "https://www.spinnyinsurance.com",
        "Referer": "https://www.spinnyinsurance.com/",
        "Cookie": f"access_token={tok}",
        "Authorization": f"Bearer {tok}",
    }


def _spinny_call(method, suffix, params=None, payload=None):
    """Call the real Spinny API with the vault token.

    Returns (http_status, data), or None when no Spinny token is saved
    (caller falls back to the local lead store + live RC lookup)."""
    headers = _spinny_lead_headers()
    if not headers:
        return None
    url = SPINNY_API + BASE + suffix
    if method == "GET":
        r = requests.get(url, params=params, headers=headers, timeout=25)
    else:
        r = requests.post(url, json=payload, headers=headers, timeout=25)
    try:
        return r.status_code, r.json()
    except ValueError:
        return r.status_code, {"raw": r.text[:500]}


def _spinny_gone():
    return jsonify({"status": "failed",
                    "message": "Spinny rejected the saved token — re-verify at /refreshtoken"}), 502


def require_api_access(fn):
    """Our login Bearer OR the vault ?key=. (The vault key gates the Spinny proxy.)"""
    @wraps(fn)
    def wrapper(*args, **kwargs):
        auth = request.headers.get("Authorization", "")
        if auth.startswith("Bearer ") and jwt_decode(auth[7:].strip()):
            return fn(*args, **kwargs)
        if request.args.get("key", "") == VAULT_KEY:
            return fn(*args, **kwargs)
        if request.method == "POST":
            body = request.get_json(silent=True) or {}
            if isinstance(body, dict) and body.get("key", "") == VAULT_KEY:
                return fn(*args, **kwargs)
        return jsonify({"status": "failed", "message": "Login first (POST /auth/login) or pass ?key=<vault-key>; save a Spinny token at /refreshtoken"}), 401
    return wrapper


@app.route("/")
def home():
    return jsonify({
        "status": "running",
        "message": "Vehicle lead API — Spinny insurance-platform shape",
        "endpoints": {
            "login": "POST /auth/login {username, password}",
            "refresh": "POST /auth/refresh {refresh_token}",
            "logout": "POST /auth/logout {refresh_token}",
            "get_lead": f"GET {BASE}/get-lead-by-registration-or-proposal/?car_registration_number=JH05CK2458&key=abhi  (login Bearer or ?key=; proxies real Spinny when vault token saved)",
            "get_or_create": f"POST {BASE}/get-or-create-lead/  (login Bearer or key in query/body; proxies real Spinny when vault token saved)",
            "refreshtoken": "GET /refreshtoken?key=abhi  (Spinny OTP vault page)",
            "token_status": "GET /api/token-status?key=abhi",
            "health": "GET /health",
        },
        "rate_limit": "10 requests/minute per IP on lead endpoints",
    })


@app.route("/health")
def health():
    with _store_lock:
        count = len(_load_store())
    return jsonify({"status": "ok", "leads_stored": count})


@app.route(f"{BASE}/get-lead-by-registration-or-proposal/", methods=["GET"])
@limiter.limit("10 per minute")
@require_api_access
def get_lead_by_registration_or_proposal():
    reg = normalize_reg(request.args.get("car_registration_number", ""))
    if not reg:
        return jsonify({"status": "failed", "message": "Missing car_registration_number"}), 400
    if not valid_reg(reg):
        return jsonify({"status": "failed", "message": f"Invalid registration number: {reg}"}), 400

    # Real Spinny first (vault token); fall back to local RC data when no
    # vault token is saved OR Spinny itself reports no lead.
    spinny_note = None
    try:
        proxied = _spinny_call("GET", "/get-lead-by-registration-or-proposal/",
                               params={"car_registration_number": reg})
    except Exception as e:
        return jsonify({"status": "failed", "message": f"Spinny upstream error: {e}"}), 502
    if proxied is not None:
        code, data = proxied
        if code in (401, 403):
            return _spinny_gone()
        has_lead = (code == 200 and isinstance(data, dict)
                    and (data.get("success") is True or data.get("lead_id")
                         or data.get("lead") or data.get("data")))
        if has_lead:
            return jsonify(data), code
        if isinstance(data, dict) and data.get("message"):
            spinny_note = str(data["message"])[:200]

    lead = get_lead(reg)
    vehicle, sources = lookup_vehicle(reg)
    if vehicle:
        vehicle["sources"] = sources

    if not lead and not vehicle:
        return jsonify({
            "status": "failed",
            "car_registration_number": reg,
            "message": "No lead, proposal or vehicle found for this registration number",
        }), 404

    resp = {
        "status": "success",
        "mode": "local",
        "car_registration_number": reg,
        "lead": lead,
        "proposal": (lead or {}).get("proposal"),
        "vehicle": vehicle,
    }
    if spinny_note:
        resp["spinny_note"] = spinny_note
    return jsonify(resp)


@app.route(f"{BASE}/get-or-create-lead/", methods=["POST"])
@limiter.limit("10 per minute")
@require_api_access
def get_or_create_lead():
    body = request.get_json(silent=True) or {}
    if not isinstance(body, dict):
        body = {}
    reg = normalize_reg(body.get("car_registration_number", ""))
    if not reg:
        return jsonify({"status": "failed", "message": "car_registration_number is required"}), 400
    if not valid_reg(reg):
        return jsonify({"status": "failed", "message": f"Invalid registration number: {reg}"}), 400

    # Real Spinny first (vault token), local fallback when no token saved.
    try:
        passthrough = {k: v for k, v in body.items() if k != "key"}
        proxied = _spinny_call("POST", "/get-or-create-lead/", payload=passthrough)
    except Exception as e:
        return jsonify({"status": "failed", "message": f"Spinny upstream error: {e}"}), 502
    if proxied is not None:
        code, data = proxied
        if code in (401, 403):
            return _spinny_gone()
        return jsonify(data), code

    existing = get_lead(reg)
    if existing:
        return jsonify({"status": "success", "mode": "local", "created": False, "lead": existing})

    vehicle, sources = lookup_vehicle(reg)
    if vehicle:
        vehicle["sources"] = sources
    stamp = now_iso()
    lead = {
        "id": uuid.uuid4().hex,
        "car_registration_number": reg,
        "name": body.get("name"),
        "mobile": body.get("mobile"),
        "email": body.get("email"),
        "source": body.get("source", "api"),
        "status": "new",
        "proposal": None,
        "vehicle_snapshot": vehicle,
        "created_at": stamp,
        "updated_at": stamp,
    }
    # drop unset optional fields for a clean record
    lead = {k: v for k, v in lead.items() if v is not None}
    lead.setdefault("proposal", None)
    save_lead(lead)
    return jsonify({"status": "success", "mode": "local", "created": True, "lead": lead})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5001))
    app.run(host="0.0.0.0", port=port, debug=False)
