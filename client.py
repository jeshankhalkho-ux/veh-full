"""Vehicle-API client with AUTO token refresh (stdlib only, no pip deps).

Logs in once, then every request sends the access token. If the server
answers 401 (access expired), the client transparently uses the refresh
token to get a new pair (old refresh token is rotated server-side) and
retries the request once. If refresh also fails, it logs in again.

Usage:
    from client import VehicleAPIClient

    api = VehicleAPIClient("http://localhost:5001", "admin", "admin")
    print(api.get_lead("JH05CK2458"))
    print(api.get_or_create("JH05CK2458", name="Amit", mobile="9876543210"))
"""
import json
import urllib.error
import urllib.request

BASE = "/sp-insurance-platform/api/v1/leads"


class AuthError(Exception):
    pass


class VehicleAPIClient:
    def __init__(self, base_url, username, password, timeout=60):
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.password = password
        self.timeout = timeout
        self.access_token = None
        self.refresh_token = None

    # -- low level ------------------------------------------------------
    def _call(self, method, path, body=None, token=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            self.base_url + path, data=data, method=method,
            headers={"Content-Type": "application/json"},
        )
        if token:
            req.add_header("Authorization", f"Bearer {token}")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return r.status, json.loads(r.read().decode() or "{}")
        except urllib.error.HTTPError as e:
            try:
                return e.code, json.loads(e.read().decode() or "{}")
            except ValueError:
                return e.code, {"status": "failed", "message": "non-JSON error"}

    # -- auth -----------------------------------------------------------
    def login(self):
        status, body = self._call("POST", "/auth/login",
                                  {"username": self.username, "password": self.password})
        if status != 200:
            raise AuthError(f"login failed: {body}")
        self.access_token = body["access_token"]
        self.refresh_token = body["refresh_token"]
        return body

    def refresh(self):
        status, body = self._call("POST", "/auth/refresh",
                                  {"refresh_token": self.refresh_token})
        if status != 200:
            raise AuthError(f"refresh failed: {body}")
        self.access_token = body["access_token"]
        self.refresh_token = body["refresh_token"]  # rotated
        return body

    def logout(self):
        if self.refresh_token:
            self._call("POST", "/auth/logout", {"refresh_token": self.refresh_token})
        self.access_token = self.refresh_token = None

    def _authed(self, method, path, body=None):
        """Request with auto refresh: 401 -> refresh -> retry once -> login -> retry."""
        if not self.access_token:
            self.login()
        status, resp = self._call(method, path, body, self.access_token)
        if status != 401:
            return resp
        try:
            self.refresh()
        except AuthError:
            self.login()  # refresh dead -> full re-login
        status, resp = self._call(method, path, body, self.access_token)
        if status == 401:
            raise AuthError(f"still unauthorized after refresh: {resp}")
        return resp

    # -- lead endpoints ---------------------------------------------------
    def get_lead(self, reg):
        return self._authed("GET", f"{BASE}/get-lead-by-registration-or-proposal/?car_registration_number={reg}")

    def get_or_create(self, reg, **fields):
        return self._authed("POST", f"{BASE}/get-or-create-lead/",
                            {"car_registration_number": reg, **fields})


if __name__ == "__main__":
    import sys

    host = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:5001"
    user = sys.argv[2] if len(sys.argv) > 2 else "admin"
    pw = sys.argv[3] if len(sys.argv) > 3 else "admin"
    api = VehicleAPIClient(host, user, pw)
    print(json.dumps(api.get_lead("JH05CK2458"), indent=2)[:600])
