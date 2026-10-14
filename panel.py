"""
پنل وب دستیار روبیکا (تک‌کاربره)
- ورود با رمز ادمین (PANEL_PASSWORD)
- ذخیره کلید OpenRouter و مدل و تنظیمات
- لاگین روبیکا با شماره و کد (کد را همین‌جا وارد می‌کنی)
- روشن/خاموش کردن ربات و دیدن لاگ
"""
import glob
import hmac
import json
import os
import secrets
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

from flask import Flask, abort, jsonify, redirect, render_template_string, request, session
from supabase_store import get_ai_settings

BASE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.abspath(os.getenv("DATA_DIR", os.path.join(BASE, "data")))
os.makedirs(DATA_DIR, exist_ok=True)
CONFIG_PATH = os.path.join(DATA_DIR, "config.json")
LOG_PATH = os.path.join(DATA_DIR, "bot.log")
LOGIN_LOG_PATH = os.path.join(DATA_DIR, "login.log")
PANEL_PASSWORD = os.getenv("PANEL_PASSWORD", "")
DEFAULT_MODEL = "dots-studio/dots-3-note-preview:free"
RUBIKA_LOGOUT_PASSWORD = os.getenv("RUBIKA_LOGOUT_PASSWORD", "Saman1389ss")

if len(PANEL_PASSWORD) < 8:
    sys.exit("PANEL_PASSWORD باید تنظیم شود و حداقل ۸ کاراکتر باشد.")

DEFAULTS = {
    "openrouter_key": "",
    "openrouter_keys": [],
    "pollinations_key": "",
    "pollinations_keys": [],
    "openrouter_model": DEFAULT_MODEL,
    "pollinations_model": "openai/gpt-5.4-nano",
    "model": DEFAULT_MODEL,
    "style_examples": "",
    "disclose_text": "(پاسخ خودکار)",
    "mode": "draft",
    "reply_in_groups": False,
    "max_replies_per_hour": 100,
    "disclose": False,
    "blocklist": [],
}


def child_env():
    env = {k: v for k, v in os.environ.items() if k != "PANEL_PASSWORD"}
    env["DATA_DIR"] = DATA_DIR
    return env


# ---------- config ----------
def load_config():
    cfg = dict(DEFAULTS)
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            cfg.update(json.load(f))
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    # AI provider credentials and model names are centrally managed in Supabase.
    # Local customer settings (mode, style, blocklist, etc.) remain in config.json.
    try:
        cfg.update(get_ai_settings())
    except Exception as exc:
        # The panel can still load, but the bot cannot call AI until Supabase is configured.
        logging.getLogger("panel").warning("Could not load central AI settings: %s", str(exc)[:250])
    cfg["reply_in_groups"] = False
    cfg["max_replies_per_hour"] = int(cfg.get("max_replies_per_hour", 100) or 100)
    return cfg


def save_config(cfg):
    tmp = CONFIG_PATH + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    os.replace(tmp, CONFIG_PATH)


def secret_key():
    path = os.path.join(DATA_DIR, "secret.key")
    if not os.path.exists(path):
        fd = os.open(path, os.O_WRONLY | os.O_CREAT, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(secrets.token_hex(32))
    with open(path) as f:
        return f.read().strip()


def session_files():
    return glob.glob(os.path.join(DATA_DIR, "session*"))


# ---------- bot process ----------
bot = {"proc": None, "want": False}
bot_lock = threading.Lock()


def bot_running():
    p = bot["proc"]
    return p is not None and p.poll() is None


def start_bot():
    cfg = load_config()
    if not (cfg.get("openrouter_keys") or cfg.get("pollinations_keys") or cfg.get("openrouter_key") or cfg.get("pollinations_key")):
        return "حداقل یک کلید API از OpenRouter یا Pollinations وارد کن."
    if not session_files():
        return "ابتدا وارد روبیکا شو."
    # Materialize the latest centrally managed provider settings for the bot process.
    save_config(cfg)
    with bot_lock:
        if bot_running():
            return None
        logf = open(LOG_PATH, "ab")
        bot["proc"] = subprocess.Popen(
            [sys.executable, "-u", os.path.join(BASE, "bot.py")],
            cwd=DATA_DIR, env=child_env(), stdout=logf, stderr=subprocess.STDOUT,
        )
        bot["want"] = True
    return None


def stop_bot():
    with bot_lock:
        bot["want"] = False
        p = bot["proc"]
        if p and p.poll() is None:
            p.terminate()
            try:
                p.wait(10)
            except subprocess.TimeoutExpired:
                p.kill()


def monitor():
    while True:
        time.sleep(5)
        if bot["want"] and not bot_running():
            start_bot()


# ---------- rubika login ----------
login = {"proc": None, "state": "idle", "message": ""}
login_lock = threading.Lock()


def set_login(state, message=""):
    with login_lock:
        login["state"], login["message"] = state, message


def send_to_worker(text):
    p = login["proc"]
    if not p or p.poll() is not None:
        return False
    try:
        p.stdin.write(text + "\n")
        p.stdin.flush()
        return True
    except (BrokenPipeError, OSError):
        return False


def login_reader(proc, phone):
    for line in proc.stdout:
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        kind = ev.get("event")
        if kind == "need":
            need = ev.get("kind")
            if need == "phone":
                send_to_worker(phone)
                set_login("sending")
            elif need == "code":
                set_login("need_code")
            elif need == "password":
                set_login("need_password")
        elif kind == "done":
            set_login("done", "ورود موفق بود.")
            return
        elif kind == "error":
            set_login("error", ev.get("message", "خطای نامشخص"))
            return
    if login["state"] not in ("done", "error"):
        set_login("error", "فرآیند لاگین ناگهان بسته شد.")


def start_login(phone):
    cancel_login()
    stop_bot()
    proc = subprocess.Popen(
        [sys.executable, "-u", os.path.join(BASE, "login_worker.py")],
        cwd=DATA_DIR, env=child_env(), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=open(LOGIN_LOG_PATH, "wb"), text=True, bufsize=1,
    )
    login["proc"] = proc
    set_login("starting")
    threading.Thread(target=login_reader, args=(proc, phone), daemon=True).start()


def cancel_login():
    p = login["proc"]
    if p and p.poll() is None:
        p.terminate()
    login["proc"] = None
    set_login("idle")


# ---------- OpenRouter test ----------
def test_openrouter(cfg):
    providers = []
    or_keys = cfg.get("openrouter_keys") or ([cfg.get("openrouter_key", "").strip()] if cfg.get("openrouter_key", "").strip() else [])
    polli_keys = cfg.get("pollinations_keys") or ([cfg.get("pollinations_key", "").strip()] if cfg.get("pollinations_key", "").strip() else [])
    for key in or_keys:
        if key and key.strip():
            providers.append(("OpenRouter", key.strip(), "https://openrouter.ai/api/v1", cfg.get("openrouter_model") or cfg.get("model") or DEFAULT_MODEL))
    for key in polli_keys:
        if key and key.strip():
            providers.append(("Pollinations", key.strip(), "https://gen.pollinations.ai/v1", cfg.get("pollinations_model") or "openai/gpt-5.4-nano"))
    if not providers:
        return False, "ابتدا حداقل یک کلید API ذخیره کن."
    results, any_ok = [], False
    for name, key, base_url, model in providers:
        body = json.dumps({"model": model, "max_tokens": 30,
            "messages": [{"role": "user", "content": "یک جمله کوتاه فارسی بگو."}]}).encode()
        req = urllib.request.Request(base_url + "/chat/completions", data=body,
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=45) as r:
                data = json.load(r)
            answer = (data["choices"][0]["message"].get("content") or "").strip()
            results.append(f"{name} ({model}): موفق" + (f" — {answer[:100]}" if answer else ""))
            any_ok = True
        except urllib.error.HTTPError as e:
            results.append(f"{name} ({model}): خطای HTTP {e.code}")
        except Exception as e:
            results.append(f"{name} ({model}): {type(e).__name__}: {str(e)[:120]}")
    return any_ok, "نتیجه تست کلیدها:\n" + "\n".join(results)


# ---------- web ----------
app = Flask(__name__)
if os.getenv("BEHIND_PROXY", "0") == "1":
    from werkzeug.middleware.proxy_fix import ProxyFix
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
app.secret_key = secret_key()
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Strict",
    SESSION_COOKIE_SECURE=os.getenv("COOKIE_SECURE", "0") == "1",
    PERMANENT_SESSION_LIFETIME=60 * 60 * 12,
)
fails = {}


def authed():
    return session.get("ok") is True


def csrf_check():
    if not hmac.compare_digest(request.form.get("csrf", ""), session.get("csrf", "")):
        abort(400)


def guard():
    if not authed():
        return redirect("/login")
    if request.method == "POST":
        csrf_check()
    return None


def flash(msg, ok=True):
    session["flash"] = {"msg": msg, "ok": ok}


LOGIN_HTML = """<!doctype html><html lang="fa" dir="rtl"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>ورود</title>
<style>body{font-family:system-ui,Tahoma;background:#f4f5f7;display:grid;place-items:center;min-height:100vh;margin:0}
form{background:#fff;padding:28px;border-radius:12px;box-shadow:0 2px 12px #0001;width:min(340px,90vw)}
input,button{width:100%;padding:10px;margin-top:10px;font-size:15px;box-sizing:border-box}
button{background:#2563eb;color:#fff;border:0;border-radius:8px;cursor:pointer}.e{color:#b91c1c}</style></head>
<body><form method="post"><h3>پنل دستیار روبیکا</h3>
{% if error %}<div class="e">{{ error }}</div>{% endif %}
<input type="password" name="password" placeholder="رمز پنل" autofocus required>
<button>ورود</button></form></body></html>"""

PANEL_HTML = """<!doctype html><html lang="fa" dir="rtl"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>دستیار روبیکا</title>
<style>
body{font-family:system-ui,Tahoma;background:#f4f5f7;margin:0;color:#111}
main{max-width:720px;margin:0 auto;padding:16px}
section{background:#fff;border-radius:12px;box-shadow:0 2px 12px #0001;padding:18px;margin-bottom:16px}
h2{margin:0 0 12px;font-size:17px}label{display:block;margin-top:10px;font-size:14px}
input,select,textarea,button{padding:9px;font-size:15px;box-sizing:border-box;width:100%;margin-top:4px}
input[type=checkbox]{width:auto}button{background:#2563eb;color:#fff;border:0;border-radius:8px;cursor:pointer;margin-top:12px}
button.gray{background:#6b7280}button.red{background:#dc2626}
.row{display:flex;gap:8px}.row form{flex:1}.msg{padding:10px;border-radius:8px;margin-bottom:12px}
.ok{background:#dcfce7}.bad{background:#fee2e2}.tag{display:inline-block;padding:2px 10px;border-radius:99px;font-size:13px}
.on{background:#dcfce7}.off{background:#e5e7eb}pre{background:#111;color:#ddd;padding:10px;border-radius:8px;
overflow:auto;max-height:260px;font-size:12px;direction:ltr;text-align:left}small{color:#555}
</style></head><body><main>
<div style="display:flex;justify-content:space-between;align-items:center"><h1 style="font-size:20px">دستیار روبیکا</h1>
<form method="post" action="/logout"><input type="hidden" name="csrf" value="{{ csrf }}"><button class="gray" style="width:auto;margin:0">خروج</button></form></div>
{% if flash %}<div class="msg {{ 'ok' if flash.ok else 'bad' }}">{{ flash.msg }}</div>{% endif %}

<section><h2>۱. تنظیمات پاسخ‌گویی</h2>
<form method="post" action="/settings"><input type="hidden" name="csrf" value="{{ csrf }}">
<label>نمونه‌های سؤال و جواب برای لحن پاسخ‌ها<textarea name="style_examples" rows="7" placeholder="سؤال: سلام، خوبی؟&#10;جواب: سلام عزیزم، ممنون تو خوبی؟&#10;&#10;سؤال: کی وقت داری؟&#10;جواب: بذار برنامه‌هامو چک کنم بهت میگم">{{ cfg.style_examples or '' }}</textarea></label><small>اگر خالی باشد، ربات از تاریخچه پیام‌های واقعی استفاده می‌کند.</small>
<label>حالت پاسخ‌گویی<select name="mode"><option value="draft" {{ 'selected' if cfg.mode=='draft' }}>پیش‌نویس (ارسال پیشنهاد به پیام‌های ذخیره‌شده)</option><option value="auto" {{ 'selected' if cfg.mode=='auto' }}>خودکار (پاسخ مستقیم)</option></select></label>
<label><input type="checkbox" name="disclose" {{ 'checked' if cfg.disclose }}> افزودن متن به انتهای پاسخ</label>
<label>متن دلخواه انتهای پاسخ (برای حذف، خالی بگذار)<input name="disclose_text" value="{{ cfg.disclose_text or '' }}" placeholder="مثلاً: پاسخ‌گوی شما"></label>
<label>سقف پاسخ خودکار در ساعت<input type="number" min="1" max="500" name="max_replies_per_hour" value="{{ cfg.max_replies_per_hour or 100 }}"></label>
<label>GUID چت‌هایی که هرگز پاسخ خودکار نگیرند (هر خط یکی)<textarea name="blocklist" rows="3" dir="ltr">{{ cfg.blocklist|join('\n') }}</textarea></label>
<button>ذخیره تنظیمات</button></form>
<p><small>کلیدهای API و مدل‌ها توسط مدیر سیستم مدیریت می‌شوند و در پنل مشتری نمایش داده نمی‌شوند. پاسخ‌گویی در گروه‌ها غیرفعال است.</small></p></section>

<section><h2>۲. ورود به روبیکا</h2>
{% if has_session and login.state in ('idle','done') %}
<p><span class="tag on">وارد شده‌ای</span> <small>فایل session ذخیره شده است.</small></p>
<form method="post" action="/rubika/reset" onsubmit="return confirm('نشست روبیکا حذف شود؟')"><input type="hidden" name="csrf" value="{{ csrf }}"><label>برای خروج از روبیکا رمز خروج را وارد کن<input type="password" name="logout_password" autocomplete="current-password" required></label><button class="red">تأیید رمز و خروج از روبیکا</button></form>
{% elif login.state in ('idle','error') or (not has_session and login.state=='done') %}
{% if login.state=='error' %}<div class="msg bad">{{ login.message }}</div>{% endif %}
<form method="post" action="/rubika/phone"><input type="hidden" name="csrf" value="{{ csrf }}">
<label>شماره تلفن (مثلاً 09123456789)<input name="phone" dir="ltr" inputmode="tel" required></label>
<button>ارسال کد</button></form>
{% elif login.state in ('starting','sending','verifying') %}
<p>در حال انجام… <small>(صفحه خودکار به‌روز می‌شود)</small></p>
<form method="post" action="/rubika/cancel"><input type="hidden" name="csrf" value="{{ csrf }}"><button class="gray">لغو</button></form>
{% elif login.state in ('need_code','need_password') %}
<form method="post" action="/rubika/code"><input type="hidden" name="csrf" value="{{ csrf }}">
<label>{{ 'کدی که روبیکا فرستاد' if login.state=='need_code' else 'رمز دومرحله‌ای (Two-step) حساب' }}
<input name="value" dir="ltr" autocomplete="off" required autofocus {% if login.state=='need_password' %}type="password"{% endif %}></label>
<button>تأیید</button></form>
<form method="post" action="/rubika/cancel"><input type="hidden" name="csrf" value="{{ csrf }}"><button class="gray">لغو</button></form>
{% endif %}
</section>

<section><h2>۳. ربات <span class="tag {{ 'on' if running else 'off' }}">{{ 'روشن' if running else 'خاموش' }}</span></h2>
<div class="row">
<form method="post" action="/bot/start"><input type="hidden" name="csrf" value="{{ csrf }}"><button>روشن کردن</button></form>
<form method="post" action="/bot/stop"><input type="hidden" name="csrf" value="{{ csrf }}"><button class="red">خاموش کردن</button></form></div>
<small>اگر ربات خودش از کار بیفتد، پنل دوباره روشنش می‌کند. پنل را باید همیشه روشن نگه داری.</small>
<h2 style="margin-top:16px">لاگ</h2><pre>{{ log }}</pre></section>
</main>
<script>
const st0={{ login.state|tojson }}+"|{{ 1 if running else 0 }}";
setInterval(async()=>{try{const r=await fetch('/status');if(!r.ok)return;const j=await r.json();
if(j.login+"|"+(j.bot?1:0)!==st0)location.reload();}catch(e){}},2500);
</script></body></html>"""


@app.route("/login", methods=["GET", "POST"])
def login_page():
    if request.method == "POST":
        ip = request.remote_addr or "?"
        now = time.time()
        recent = [t for t in fails.get(ip, []) if now - t < 300]
        fails[ip] = recent
        if len(recent) >= 5:
            return render_template_string(LOGIN_HTML, error="تلاش بیش از حد. چند دقیقه صبر کن."), 429
        if hmac.compare_digest(request.form.get("password", ""), PANEL_PASSWORD):
            session.clear()
            session["ok"] = True
            session["csrf"] = secrets.token_hex(16)
            session.permanent = True
            return redirect("/")
        fails[ip].append(now)
        time.sleep(1)
        return render_template_string(LOGIN_HTML, error="رمز اشتباه است."), 401
    return render_template_string(LOGIN_HTML, error=None)


@app.post("/logout")
def logout():
    if authed():
        csrf_check()
    session.clear()
    return redirect("/login")


def tail_log(n=40, path=None):
    try:
        with open(path or LOG_PATH, "rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - 8000))
            return "\n".join(f.read().decode("utf-8", "ignore").splitlines()[-n:])
    except FileNotFoundError:
        return "(هنوز لاگی نیست)"


@app.get("/")
def index():
    r = guard()
    if r:
        return r
    cfg = load_config()
    or_keys = cfg.get("openrouter_keys") or ([cfg.get("openrouter_key")] if cfg.get("openrouter_key") else [])
    polli_keys = cfg.get("pollinations_keys") or ([cfg.get("pollinations_key")] if cfg.get("pollinations_key") else [])
    key_set = bool(or_keys)
    cfg_view = {k: v for k, v in cfg.items() if k not in ("openrouter_key", "openrouter_keys", "pollinations_key", "pollinations_keys")}

    return render_template_string(
        PANEL_HTML, cfg=type("C", (), cfg_view), key_set=key_set, or_key_count=len(or_keys),
        polli_key_count=len(polli_keys), csrf=session["csrf"],
        flash=session.pop("flash", None), login=dict(login), has_session=bool(session_files()),
        running=bot_running(),
        log=(("--- لاگ لاگین ---\n" + tail_log(25, LOGIN_LOG_PATH) + "\n\n--- لاگ ربات ---\n") if os.path.exists(LOGIN_LOG_PATH) else "") + tail_log(),
    )


@app.get("/status")
def status():
    if not authed():
        abort(401)
    return jsonify(login=login["state"], bot=bot_running())


@app.post("/settings")
def settings():
    r = guard()
    if r:
        return r
    cfg = load_config()
    cfg["style_examples"] = request.form.get("style_examples", "").strip()
    cfg["disclose_text"] = request.form.get("disclose_text", "").strip()
    cfg["mode"] = "auto" if request.form.get("mode") == "auto" else "draft"
    cfg["reply_in_groups"] = False
    cfg["disclose"] = "disclose" in request.form
    try:
        cfg["max_replies_per_hour"] = max(1, min(500, int(request.form.get("max_replies_per_hour", 100))))
    except ValueError:
        cfg["max_replies_per_hour"] = 100
    cfg["blocklist"] = [x.strip() for x in request.form.get("blocklist", "").replace(",", "\n").splitlines() if x.strip()]
    save_config(cfg)
    msg = "تنظیمات ذخیره شد."
    if bot_running():
        stop_bot()
        start_bot()
        msg += " ربات با تنظیمات جدید دوباره راه افتاد."
    flash(msg)
    return redirect("/")


@app.post("/rubika/phone")
def rubika_phone():
    r = guard()
    if r:
        return r
    phone = "".join(c for c in request.form.get("phone", "") if c.isdigit() or c == "+")
    if len(phone) < 10:
        flash("شماره معتبر نیست.", False)
        return redirect("/")
    start_login(phone)
    return redirect("/")


@app.post("/rubika/code")
def rubika_code():
    r = guard()
    if r:
        return r
    value = request.form.get("value", "").strip()
    if login["state"] in ("need_code", "need_password") and value and send_to_worker(value):
        set_login("verifying")
    else:
        flash("ارسال نشد؛ دوباره از اول شروع کن.", False)
    return redirect("/")


@app.post("/rubika/cancel")
def rubika_cancel():
    r = guard()
    if r:
        return r
    cancel_login()
    return redirect("/")


@app.post("/rubika/reset")
def rubika_reset():
    r = guard()
    if r:
        return r
    if not hmac.compare_digest(request.form.get("logout_password", ""), RUBIKA_LOGOUT_PASSWORD):
        flash("رمز خروج اشتباه است؛ نشست روبیکا حذف نشد.", False)
        return redirect("/")
    stop_bot()
    cancel_login()
    for f in session_files():
        try:
            os.remove(f)
        except OSError:
            pass
    flash("Session حذف شد.")
    return redirect("/")


@app.post("/bot/start")
def bot_start():
    r = guard()
    if r:
        return r
    err = start_bot()
    flash(err or "ربات روشن شد.", err is None)
    return redirect("/")


@app.post("/bot/stop")
def bot_stop():
    r = guard()
    if r:
        return r
    stop_bot()
    flash("ربات خاموش شد.")
    return redirect("/")


def main():
    host = os.getenv("PANEL_HOST", "127.0.0.1")
    port = int(os.getenv("PORT") or os.getenv("PANEL_PORT", "8080"))
    threading.Thread(target=monitor, daemon=True).start()
    cfg = load_config()
    if (cfg.get("openrouter_keys") or cfg.get("pollinations_keys") or cfg.get("openrouter_key") or cfg.get("pollinations_key")) and session_files():
        start_bot()
    try:
        from waitress import serve
        serve(app, host=host, port=port, threads=4)
    except ImportError:
        app.run(host=host, port=port, threaded=True)


if __name__ == "__main__":
    main()
