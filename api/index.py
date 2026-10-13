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
import re
import secrets
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

def _load_store():
    try:
        with open(STORE_PATH, "r", encoding="utf-8") as fh:
            data = json.load(fh)
            return data if isinstance(data, dict) else {}
    except (FileNotFoundError, ValueError):
        return {}


def _save_store(store):
    tmp = STORE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(store, fh, indent=2)
    os.replace(tmp, STORE_PATH)


def get_lead(reg):
    with _store_lock:
        return _load_store().get(reg)


def save_lead(lead):
    with _store_lock:
        store = _load_store()
        store[lead["car_registration_number"]] = lead
        _save_store(store)


def _load_tokens():
    try:
        with open(TOKEN_PATH, "r", encoding="utf-8") as fh:
            data = json.load(fh)
            return data if isinstance(data, dict) else {}
    except (FileNotFoundError, ValueError):
        return {}


def _save_tokens(tokens):
    tmp = TOKEN_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(tokens, fh, indent=2)
    os.replace(tmp, TOKEN_PATH)


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


# ------------------------------------------------------------------ routes ---

@app.route("/")
def home():
    return jsonify({
        "status": "running",
        "message": "Vehicle lead API — Spinny insurance-platform shape",
        "endpoints": {
            "login": "POST /auth/login {username, password}",
            "refresh": "POST /auth/refresh {refresh_token}",
            "logout": "POST /auth/logout {refresh_token}",
            "get_lead": f"GET {BASE}/get-lead-by-registration-or-proposal/?car_registration_number=JH05CK2458  (Bearer auth)",
            "get_or_create": f"POST {BASE}/get-or-create-lead/  (Bearer auth)",
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
@require_auth
def get_lead_by_registration_or_proposal():
    reg = normalize_reg(request.args.get("car_registration_number", ""))
    if not reg:
        return jsonify({"status": "failed", "message": "Missing car_registration_number"}), 400
    if not valid_reg(reg):
        return jsonify({"status": "failed", "message": f"Invalid registration number: {reg}"}), 400

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

    return jsonify({
        "status": "success",
        "car_registration_number": reg,
        "lead": lead,
        "proposal": (lead or {}).get("proposal"),
        "vehicle": vehicle,
    })


@app.route(f"{BASE}/get-or-create-lead/", methods=["POST"])
@limiter.limit("10 per minute")
@require_auth
def get_or_create_lead():
    body = request.get_json(silent=True) or {}
    reg = normalize_reg(body.get("car_registration_number", ""))
    if not reg:
        return jsonify({"status": "failed", "message": "car_registration_number is required"}), 400
    if not valid_reg(reg):
        return jsonify({"status": "failed", "message": f"Invalid registration number: {reg}"}), 400

    existing = get_lead(reg)
    if existing:
        return jsonify({"status": "success", "created": False, "lead": existing})

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
    return jsonify({"status": "success", "created": True, "lead": lead})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5001))
    app.run(host="0.0.0.0", port=port, debug=False)
