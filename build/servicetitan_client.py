#!/usr/bin/env python3
"""
Thin client for the live ServiceTitan API, used to answer ad-hoc questions
against real-time data (as opposed to the emailed-report pipeline in
parse_reports.py, which is the source of truth for the dashboard itself).

Credentials live in secrets/servicetitan.json (git-ignored, never commit).
There can be more than one OAuth "app" (e.g. a Sierra-only connection and a
network-wide "Enterprise Hub" connection) - secrets/servicetitan.json maps
each tenant code to the app that's authorized for it via `tenant_app`.

Usage as a library:
    from servicetitan_client import st_get, TENANTS

    jobs = st_get("SIE", "/jpm/v2/tenant/{tenant}/jobs", params={"pageSize": 5})

Usage from the CLI (quick smoke test):
    py build/servicetitan_client.py SIE /crm/v2/tenant/{tenant}/customers?pageSize=1
"""
import json
import os
import time
import urllib.request
import urllib.parse
import urllib.error

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
CREDS_PATH = os.path.join(ROOT, "secrets", "servicetitan.json")
TOKEN_CACHE_PATH = os.path.join(ROOT, "secrets", ".token_cache.json")

AUTH_URL = "https://auth.servicetitan.io/connect/token"
API_BASE = "https://api.servicetitan.io"
# Wall-clock cap per request; the API occasionally stalls, so never hang forever.
REQUEST_TIMEOUT = int(os.environ.get("ST_TIMEOUT", "60"))

_creds = None


def _load_creds():
    global _creds
    if _creds is None:
        if not os.path.exists(CREDS_PATH):
            raise RuntimeError(
                f"Missing {CREDS_PATH} - ServiceTitan credentials are not set up locally."
            )
        with open(CREDS_PATH, encoding="utf-8") as f:
            _creds = json.load(f)
    return _creds


TENANTS = {k: v["id"] for k, v in _load_creds().get("tenants", {}).items()} if os.path.exists(CREDS_PATH) else {}


def _app_for_tenant_code(tenant_code):
    """Which entry in creds['apps'] is authorized for this partner code."""
    creds = _load_creds()
    apps = creds.get("apps", {})
    tenant_app = creds.get("tenant_app", {})
    app_name = tenant_app.get(tenant_code.upper())
    if app_name and app_name in apps:
        return app_name, apps[app_name]
    # Single legacy-style creds file with no apps/tenant_app mapping.
    if "client_id" in creds:
        return "default", creds
    raise RuntimeError(f"No ServiceTitan app configured for tenant '{tenant_code}'.")


def _read_token_cache():
    if os.path.exists(TOKEN_CACHE_PATH):
        try:
            with open(TOKEN_CACHE_PATH, encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def _write_token_cache(cache):
    with open(TOKEN_CACHE_PATH, "w", encoding="utf-8") as f:
        json.dump(cache, f)


def get_access_token(app_name, app_creds, tenant_id=None, force_refresh=False):
    """Client-credentials OAuth2 token for one app, cached on disk.
    Multi-tenant clients (e.g. an Enterprise Hub connection) require the
    target tenant id in the token request itself and get a token scoped to
    that tenant, so the cache key includes tenant_id when given."""
    cache_key = f"{app_name}:{tenant_id}" if tenant_id else app_name
    cache = _read_token_cache()
    if not force_refresh:
        entry = cache.get(cache_key)
        if entry and entry.get("expires_at", 0) > time.time() + 60:
            return entry["access_token"]

    form = {
        "grant_type": "client_credentials",
        "client_id": app_creds["client_id"],
        "client_secret": app_creds["client_secret"],
    }
    if tenant_id:
        form["tenant"] = str(tenant_id)
    body = urllib.parse.urlencode(form).encode("utf-8")

    req = urllib.request.Request(
        AUTH_URL,
        data=body,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": "Hyperion-Dashboard/1.0",
            "Accept": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
            payload = json.load(resp)
    except urllib.error.HTTPError as e:
        body_text = e.read().decode("utf-8", "ignore")
        if e.code == 400 and "tenant_required" in body_text and not tenant_id:
            raise RuntimeError(
                f"App '{app_name}' is a multi-tenant client but no tenant_id was supplied for this token request."
            ) from e
        raise RuntimeError(f"ServiceTitan auth failed for app '{app_name}' ({e.code}): {body_text}") from e

    token = payload["access_token"]
    expires_at = time.time() + payload.get("expires_in", 900)
    cache[cache_key] = {"access_token": token, "expires_at": expires_at}
    _write_token_cache(cache)
    return token


def resolve_tenant(tenant):
    """Accepts a partner code (SIE, BRO, ...) or a raw numeric tenant id."""
    if isinstance(tenant, int) or (isinstance(tenant, str) and tenant.isdigit()):
        return int(tenant)
    key = tenant.upper()
    if key not in TENANTS:
        raise ValueError(f"Unknown tenant '{tenant}'. Known: {', '.join(TENANTS)}")
    return TENANTS[key]


def _code_for_tenant_id(tenant_id):
    for code, tid in TENANTS.items():
        if tid == tenant_id:
            return code
    return None


def st_get(tenant, path, params=None):
    """GET against the ServiceTitan API for a given tenant (code or numeric id).
    `path` may contain a literal '{tenant}' placeholder."""
    return _st_request("GET", tenant, path, params=params)


def st_post(tenant, path, json_body=None, params=None):
    return _st_request("POST", tenant, path, params=params, json_body=json_body)


def _st_request(method, tenant, path, params=None, json_body=None, _retried=False):
    tenant_id = resolve_tenant(tenant)
    tenant_code = tenant.upper() if isinstance(tenant, str) and not tenant.isdigit() else _code_for_tenant_id(tenant_id)
    if not tenant_code:
        raise ValueError(f"Cannot determine partner code for tenant id {tenant_id}; pass a code like SIE instead.")

    app_name, app_creds = _app_for_tenant_code(tenant_code)

    formatted_path = path.format(tenant=tenant_id)
    if not formatted_path.startswith("/"):
        formatted_path = "/" + formatted_path

    url = API_BASE + formatted_path
    if params:
        url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)

    headers = {
        "Authorization": f"Bearer {get_access_token(app_name, app_creds, tenant_id=tenant_id)}",
        "ST-App-Key": app_creds["app_key"],
        "User-Agent": "Hyperion-Dashboard/1.0",
        "Accept": "application/json",
    }
    data = None
    if json_body is not None:
        data = json.dumps(json_body).encode("utf-8")
        headers["Content-Type"] = "application/json"

    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as e:
        if e.code == 401 and not _retried:
            get_access_token(app_name, app_creds, tenant_id=tenant_id, force_refresh=True)
            return _st_request(method, tenant, path, params=params, json_body=json_body, _retried=True)
        raise RuntimeError(f"ServiceTitan API error ({e.code}) for {url} [app={app_name}]: {e.read().decode('utf-8', 'ignore')}") from e


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 3:
        print("Usage: py build/servicetitan_client.py <TENANT_CODE|tenant_id> <path> [query]")
        print(f"Known tenants: {', '.join(TENANTS)}")
        sys.exit(1)
    tenant_arg = sys.argv[1]
    path_arg = sys.argv[2]
    result = st_get(tenant_arg, path_arg)
    print(json.dumps(result, indent=2)[:4000])
