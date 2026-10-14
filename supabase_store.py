"""Server-side access to shared AI settings in Supabase REST API.
Only the service-role key may be used here; never expose it to browser code.
"""
import json
import os
import urllib.error
import urllib.request

DEFAULTS = {
    "openrouter_keys": [],
    "pollinations_keys": [],
    "openrouter_model": "dots-studio/dots-3-note-preview:free",
    "pollinations_model": "openai/gpt-5.4-nano",
}


def _config():
    url = os.getenv("SUPABASE_URL", "https://bklayrocexkvxfgjacoo.supabase.co").rstrip("/")
    key = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "").strip()
    if not key:
        raise RuntimeError("SUPABASE_SERVICE_ROLE_KEY تنظیم نشده است؛ کلید service_role را فقط در محیط سرور قرار بده.")
    return url, key


def _request(method, payload=None):
    url, key = _config()
    endpoint = url + ("/rest/v1/ai_settings?id=eq.1" if method == "GET" else "/rest/v1/ai_settings?on_conflict=id")
    headers = {"apikey": key, "Authorization": "Bearer " + key, "Accept": "application/json"}
    data = None
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
        headers["Prefer"] = "resolution=merge-duplicates,return=representation"
    req = urllib.request.Request(endpoint, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=15) as response:
            raw = response.read().decode("utf-8")
            return json.loads(raw) if raw else []
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "ignore")[:300]
        raise RuntimeError(f"خطای Supabase ({exc.code}): {detail}") from exc


def get_ai_settings():
    rows = _request("GET")
    result = dict(DEFAULTS)
    if rows:
        row = rows[0]
        for key in result:
            if row.get(key) is not None:
                result[key] = row[key]
    return result


def save_ai_settings(settings):
    payload = {
        "id": 1,
        "openrouter_keys": [str(k).strip() for k in settings.get("openrouter_keys", []) if str(k).strip()],
        "pollinations_keys": [str(k).strip() for k in settings.get("pollinations_keys", []) if str(k).strip()],
        "openrouter_model": str(settings.get("openrouter_model") or DEFAULTS["openrouter_model"]).strip(),
        "pollinations_model": str(settings.get("pollinations_model") or DEFAULTS["pollinations_model"]).strip(),
    }
    rows = _request("POST", payload)
    return rows[0] if rows else payload
