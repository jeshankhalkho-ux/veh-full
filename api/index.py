# vehicle-api — Spinny-style vehicle lead API (Flask, Vercel-ready)
#
# Mirrors the two Spinny insurance-platform calls:
#   GET  /sp-insurance-platform/api/v1/leads/get-lead-by-registration-or-proposal/?car_registration_number=JH05CK2458
#   POST /sp-insurance-platform/api/v1/leads/get-or-create-lead/   (JSON body)
#
# Vehicle data comes from the same live sources as vehicle-src
# (SMC Insurance, ACKO VehicleInfo, VahanX, vahandetails), merged.
# Leads are kept in leads.json next to this file (dev store — ephemeral
# on serverless; plug in a real DB for production).
import base64
import concurrent.futures
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
from html import unescape

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
MOBILE_UA = "Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Mobile Safari/537.36"

# ---------------------------------------------------------------- sources ---

SMC_URL = "https://www.smcinsurance.com/central/centralcall/CallReqWithHeader"
SMC_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Linux; Android 16; CPH2729 Build/BP2A.250605.015) AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/150.0.7871.181 Mobile Safari/537.36",
    "Accept-Encoding": "gzip, deflate, br, zstd",
    "Content-Type": "application/json",
    "origin": "https://www.smcinsurance.com",
    "referer": "https://www.smcinsurance.com/",
    "accept-language": "en-IN,en-US;q=0.9,en;q=0.8",
}
SMC_SESSION = requests.Session()
SMC_SESSION.headers.update(SMC_HEADERS)
try:
    SMC_SESSION.get("https://www.smcinsurance.com/", timeout=15)
except Exception:
    pass

VD_HEADERS = {
    "User-Agent": MOBILE_UA,
    "Accept": "application/json, text/plain, */*",
    "Origin": "https://vahandetails.com",
    "Referer": "https://vahandetails.com/",
}

SESSION_POOL = {}


def get_session(key, headers=None):
    if key not in SESSION_POOL:
        sess = requests.Session()
        if headers:
            sess.headers.update(headers)
        SESSION_POOL[key] = sess
    return SESSION_POOL[key]


def iso(d):
    return d.replace("T00:00:00.000Z", "") if d else None


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def query_smc(vnum):
    payload = {"URL": "GetVaahanDetailsByVehicleNo", "Props": [vnum], "Token": ""}
    try:
        resp = SMC_SESSION.post(SMC_URL, json=payload, timeout=30)
        resp.raise_for_status()
        rc = resp.json().get("response", {})
        if not rc:
            return {}
        return {
            "source": "smc",
            "owner_name": rc.get("owner"),
            "father_name": rc.get("ownerFatherName"),
            "manufacturer": rc.get("manufacturer"),
            "model": rc.get("vehicle"),
            "variant": rc.get("variant"),
            "vehicle_class": rc.get("vehicleClass"),
            "fuel_type": rc.get("fuelType"),
            "cc": rc.get("cubicCapacity"),
            "seating_capacity": rc.get("seatCapacity"),
            "engine_number": rc.get("engine"),
            "chassis_number": rc.get("chassis"),
            "registration_date": rc.get("regDate"),
            "registration_valid_upto": rc.get("eDate"),
            "insurance_company": rc.get("insuranceCompanyName"),
            "insurance_policy_no": rc.get("insurancePolicyNumber"),
            "insurance_valid_upto": rc.get("insuranceUpto"),
            "rto_code": rc.get("rtoCode"),
            "rto_name": rc.get("regAuthority"),
            "address": rc.get("presentAddress"),
            "financer": rc.get("financerName"),
            "is_commercial": rc.get("isCommercial"),
            "puc_no": rc.get("puccNumber"),
            "puc_valid_upto": rc.get("puccValidUpto"),
        }
    except Exception as e:
        logger.warning("SMC failed for %s: %s", vnum, e)
        return {}


VAHANX_FIELDS = {
    "fuel_norms": "Fuel Norms",
    "vehicle_age": "Vehicle Age",
    "fitness_upto": "Fitness Upto",
    "tax_upto": "Tax Upto",
    "puc_expiry_in": "PUC Expiry In",
    "insurance_expiry_in": "Insurance Expiry In",
}
VAHANX_RTO_FIELDS = {
    "rto_city": "City Name",
    "rto_address": "Address",
    "rto_phone": "Phone",
    "rto_website": "Website",
}
VAHANX_DETAIL_RE = re.compile(
    r"<span[^>]*>\s*([^<]{1,40}?)\s*</span>\s*<p[^>]*>\s*(?:<a[^>]*>)?\s*([^<]{0,160}?)\s*(?:</a>)?\s*</p>",
    re.IGNORECASE | re.DOTALL,
)
VAHANX_RTO_RE = re.compile(
    r"<p[^>]*>\s*(?:<a[^>]*>)?\s*([^<]{0,160}?)\s*(?:</a>)?\s*</p>\s*<span[^>]*>\s*([^<]{1,40}?)\s*</span>",
    re.IGNORECASE | re.DOTALL,
)


def scrape_vahanx(vnum):
    reg = re.sub(r"[^A-Z0-9]", "", str(vnum).upper())
    if not reg:
        return {}
    sess = get_session("vahanx", {"User-Agent": USER_AGENT})
    try:
        r = sess.get("https://vahanx.in/rc-search", timeout=20)
        if r.status_code != 200:
            return {}
        block = re.search(r"window\.VX_ST\s*=\s*(\{.*?\})", r.text, re.DOTALL)
        param = re.search(r"window\.VX_ST_PARAM\s*=\s*[\"']([^\"']+)[\"']", r.text)
        if not block or not param:
            return {}
        tokens = dict(re.findall(r"([A-Za-z_$][\w$]*)\s*:\s*\"([^\"]+)\"", block.group(1)))
        token = tokens.get("rc")
        if not token:
            return {}
        r = sess.get(f"https://vahanx.in/rc-search/{reg}", params={param.group(1): token}, timeout=20)
        if r.status_code != 200 or "Registration Number" not in r.text:
            return {}
        details = {label.strip().lower(): unescape(v).strip() for label, v in VAHANX_DETAIL_RE.findall(r.text)}
        rto = {label.strip().lower(): unescape(v).strip() for v, label in VAHANX_RTO_RE.findall(r.text)}
        wanted = dict(VAHANX_FIELDS)
        wanted.update(VAHANX_RTO_FIELDS)
        data = {"source": "vahanx"}
        for key, label in wanted.items():
            value = details.get(label.lower()) or rto.get(label.lower())
            if not value or value.upper() == "NA":
                continue
            data[key] = value.rstrip("/") if key == "rto_website" else value
        return data if len(data) > 1 else {}
    except Exception as e:
        logger.warning("VahanX failed for %s: %s", vnum, e)
        return {}


def query_vahandetails(vnum):
    sess = get_session("vahandetails", VD_HEADERS)
    try:
        r = sess.get(f"https://restapi.vahandetails.com/api/vehicles/search?rc_regn_no={vnum}", timeout=30)
        if r.status_code != 200:
            return {}
        raw = r.json().get("data", {})
        rc = raw.get("rc_details", {})
        ins = raw.get("insurance", [{}])
        puc = raw.get("puc", [{}])
        if not rc:
            return {}
        return {
            "source": "vahandetails",
            "owner_name": rc.get("rc_owner_name"),
            "color": rc.get("rc_color"),
            "maker_model": rc.get("rc_maker_model"),
            "vehicle_class": rc.get("rc_vh_class_desc"),
            "fuel_type": rc.get("rc_fuel_desc"),
            "cubic_capacity": rc.get("rc_cubic_cap"),
            "seating_capacity": rc.get("rc_seat_cap"),
            "engine_number": rc.get("rc_eng_no"),
            "chassis_number": rc.get("rc_chasi_no"),
            "manufacturing_month_year": rc.get("rc_manu_month_yr"),
            "registration_date": iso(rc.get("rc_regn_dt")),
            "registration_valid_upto": iso(rc.get("rc_regn_upto")),
            "fitness_upto": iso(rc.get("rc_fit_upto")),
            "tax_upto": iso(rc.get("rc_tax_upto")),
            "rto": rc.get("rc_registered_at"),
            "rc_status": rc.get("rc_status"),
            "financer": rc.get("rc_financer"),
            "insurance_company": ins[0].get("insurance_company") if ins else None,
            "insurance_policy_no": ins[0].get("insurance_policy_no") if ins else None,
            "insurance_valid_upto": iso(ins[0].get("insurance_valid_upto")) if ins else None,
            "puc_no": puc[0].get("pucc_no") if puc else None,
            "puc_valid_upto": iso(puc[0].get("pucc_valid_upto")) if puc else None,
            "is_rc_expired": raw.get("is_rc_expired"),
            "is_insurance_expired": raw.get("is_insurance_expired"),
            "is_puc_expired": raw.get("is_puc_expired"),
            "has_pending_challan": raw.get("has_pending_challan"),
            "pending_challan_count": raw.get("pending_challan_count"),
        }
    except Exception as e:
        logger.warning("vahandetails failed for %s: %s", vnum, e)
        return {}


def query_acko_vehicleinfo(vnum):
    try:
        r = requests.get(
            f"https://www.acko.com/api/app/vehicleInfo/?regNo={vnum}",
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "application/json",
                "Origin": "https://www.acko.com",
                "Referer": "https://www.acko.com/",
            },
            timeout=20,
        )
        if r.status_code == 200:
            d = r.json()
            if d.get("registration_number"):
                return {
                    "source": "acko_vehicleinfo",
                    "owner_name": d.get("owner_name"),
                    "vehicle_make": d.get("db_make_name"),
                    "vehicle_model": d.get("db_model_name"),
                    "vehicle_variant": d.get("model_name"),
                    "vehicle_type": d.get("vehicle_type_v2"),
                    "fuel_type": d.get("fuel_type"),
                    "variant_id": d.get("variant_id"),
                }
    except Exception as e:
        logger.warning("ACKO VehicleInfo failed for %s: %s", vnum, e)
    return {}


MASKED_FIELDS = {
    "chassis_number_masked", "engine_number_masked",
    "chassis_number_unmasked", "engine_number_unmasked",
    "owner_name_unmasked",
}


def merge_results(*results):
    merged = {}
    for r in results:
        if r and isinstance(r, dict):
            for k in ("source", *MASKED_FIELDS):
                r.pop(k, None)
            for k, v in r.items():
                if v not in (None, "", [], {}) and k not in merged:
                    merged[k] = v
    return merged


def lookup_vehicle(vnum):
    """Query all sources in parallel; returns (merged_dict, [source_names])."""
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as ex:
        futures = {
            "smc": ex.submit(query_smc, vnum),
            "acko_vehicleinfo": ex.submit(query_acko_vehicleinfo, vnum),
            "vahanx": ex.submit(scrape_vahanx, vnum),
            "vahandetails": ex.submit(query_vahandetails, vnum),
        }
        results = {k: f.result() for k, f in futures.items()}
    sources = [k for k, v in results.items() if v]
    if not sources:
        return None, []
    merged = merge_results(results["smc"], results["acko_vehicleinfo"], results["vahanx"], results["vahandetails"])
    merged["registration_number"] = vnum
    return merged, sources


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
