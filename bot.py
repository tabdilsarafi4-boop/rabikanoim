"""
دستیار هوشمند روبیکا (روی اکانت خودت) با OpenRouter
- تنظیمات را از config.json (ساخته‌شده توسط پنل وب) می‌خواند
- همه پیام‌ها را در history.db ذخیره می‌کند و سبک نوشتن تو را از آن‌ها یاد می‌گیرد
- mode=draft : پیش‌نویس جواب به Saved Messages خودت فرستاده می‌شود
- mode=auto  : مستقیم به طرف مقابل جواب می‌دهد
توجه: نام فیلدهای update در rubpy بین نسخه‌ها کمی فرق می‌کند؛
در صورت نیاز تابع extract() را با نسخه‌ی خودت تطبیق بده.
"""
import asyncio
import inspect
import json
import logging
import os
import sqlite3
import time
from collections import defaultdict, deque

from openai import AsyncOpenAI
from rubpy import Client, handlers

DATA_DIR = os.getenv("DATA_DIR", ".")
os.chdir(DATA_DIR)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


class _HideSecrets(logging.Filter):
    """rubpy اطلاعات نشست (کلید خصوصی) را لاگ می‌کند؛ اینجا حذفش می‌کنیم."""

    def filter(self, record):
        msg = str(record.getMessage())
        return not ("session information" in msg or "PRIVATE KEY" in msg)


for _h in logging.getLogger().handlers:
    _h.addFilter(_HideSecrets())
logging.getLogger("rubpy").setLevel(logging.WARNING)
log = logging.getLogger("assistant")

with open("config.json", encoding="utf-8") as f:
    cfg = json.load(f)

MODE = cfg.get("mode", "draft")
REPLY_IN_GROUPS = bool(cfg.get("reply_in_groups", False))
MAX_REPLIES_PER_HOUR = int(cfg.get("max_replies_per_hour", 20))
DISCLOSE = bool(cfg.get("disclose", False))
BLOCKLIST = set(cfg.get("blocklist", []))
MODEL = cfg.get("model", "dots-studio/dots-3-note-preview:free")

ai = AsyncOpenAI(
    api_key=cfg["openrouter_key"],
    base_url="https://openrouter.ai/api/v1",
    default_headers={"X-Title": "Rubika Assistant"},
    timeout=60,
)

db = sqlite3.connect("history.db", check_same_thread=False)
db.execute(
    "CREATE TABLE IF NOT EXISTS msgs (id INTEGER PRIMARY KEY, chat TEXT, sender TEXT,"
    " is_me INTEGER, text TEXT, ts REAL)"
)
db.commit()

sent_log = deque()
chat_locks = defaultdict(asyncio.Lock)


def save(chat, sender, is_me, text):
    db.execute(
        "INSERT INTO msgs (chat, sender, is_me, text, ts) VALUES (?,?,?,?,?)",
        (chat, sender, int(is_me), text, time.time()),
    )
    db.commit()


def my_style_samples(n=150):
    rows = db.execute(
        "SELECT text FROM msgs WHERE is_me=1 AND length(text) > 1 ORDER BY id DESC LIMIT ?", (n,)
    ).fetchall()
    return [r[0] for r in reversed(rows)]


def chat_context(chat, n=15):
    rows = db.execute(
        "SELECT is_me, text FROM msgs WHERE chat=? ORDER BY id DESC LIMIT ?", (chat, n)
    ).fetchall()
    return [("من" if r[0] else "طرف مقابل", r[1]) for r in reversed(rows)]


async def generate_reply(chat):
    samples = "\n".join(f"- {s}" for s in my_style_samples())
    convo = "\n".join(f"{who}: {t}" for who, t in chat_context(chat))
    system = (
        "تو دستیار شخصی صاحب این اکانت هستی و به جای او به پیام‌ها جواب می‌دهی. "
        "لحن، طول جمله، شکلک‌ها و سبک نوشتن او را از نمونه‌ها تقلید کن. "
        "کوتاه و طبیعی جواب بده. هیچ‌وقت اطلاعات حساس (پول، رمز، آدرس، تصمیم مهم) نده، "
        "قرار یا تعهد جدید نپذیر و اگر مطمئن نیستی بنویس: «بعداً خودم جواب می‌دم». "
        "اگر پیام نیاز به جواب ندارد (مثلاً فقط تشکر یا ایموجی)، فقط کلمه SKIP را بنویس. "
        "فقط متن پیام را بنویس، بدون توضیح اضافه.\n\n"
        f"نمونه پیام‌های واقعی صاحب اکانت:\n{samples or '(هنوز نمونه‌ای نیست)'}"
    )
    resp = await ai.chat.completions.create(
        model=MODEL,
        max_tokens=300,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": f"مکالمه تا اینجا:\n{convo}\n\nجواب بعدی من چیست؟"},
        ],
    )
    return (resp.choices[0].message.content or "").strip()


def rate_ok():
    now = time.time()
    while sent_log and now - sent_log[0] > 3600:
        sent_log.popleft()
    return len(sent_log) < MAX_REPLIES_PER_HOUR


_debug_left = 5


def extract(update):
    global _debug_left
    chat = getattr(update, "object_guid", None)
    sender = getattr(update, "author_guid", None) or getattr(update, "author_object_guid", None)
    text = getattr(update, "text", None) or getattr(update, "raw_text", None)
    chat_type = getattr(update, "type", None) or getattr(update, "chat_type", None)
    if _debug_left > 0:  # برای عیب‌یابی: چند آپدیت اول را خلاصه لاگ می‌کنیم
        _debug_left -= 1
        log.info("update fields: chat=%s sender=%s type=%s text=%r", chat, sender, chat_type, (text or "")[:40])
    return chat, sender, text, chat_type


def register(client, func):
    """ثبت هندلر پیام؛ با نسخه‌های مختلف rubpy کار می‌کند."""
    if hasattr(client, "on_message_updates"):  # rubpy 7.x
        client.on_message_updates()(func)
        log.info("handler registered via on_message_updates")
    elif hasattr(client, "on"):  # نسخه‌های قدیمی‌تر
        client.on(handlers.MessageUpdates())(func)
        log.info("handler registered via on()")
    elif hasattr(client, "add_handler"):
        client.add_handler(func, handlers.MessageUpdates())
        log.info("handler registered via add_handler")
    else:
        raise RuntimeError("این نسخه از rubpy روش شناخته‌شده‌ای برای ثبت هندلر ندارد")


async def wait_forever(client):
    """منتظر ماندن تا قطع اتصال؛ با نسخه‌های مختلف rubpy سازگار است."""
    for name in ("run_until_disconnected", "run_forever", "wait_until_disconnected", "idle"):
        fn = getattr(client, name, None)
        if callable(fn):
            log.info("waiting via client.%s()", name)
            res = fn()
            if inspect.isawaitable(res):
                await res
            return
    run = getattr(client, "run", None)
    if inspect.iscoroutinefunction(run):
        log.info("waiting via await client.run()")
        await run()
        return
    for name in ("connection", "_connection", "updates_task", "_updates_task"):
        t = getattr(client, name, None)
        if t is not None and asyncio.isfuture(t):
            log.info("waiting on client.%s task", name)
            await t
            return
    log.info("no wait method found; using keep-alive loop")
    while True:
        await asyncio.sleep(3600)


def log_client_info(client):
    try:
        from importlib.metadata import version
        log.info("rubpy version: %s", version("rubpy"))
    except Exception:
        pass
    keys = ("run", "start", "connect", "update", "disconnect", "handler", "on_")
    names = sorted(n for n in dir(client) if not n.startswith("__") and any(k in n for k in keys))
    log.info("client methods: %s", ", ".join(names))


async def main():
    async with Client(name="session") as client:
        me = await client.get_me()
        my_guid = getattr(getattr(me, "user", me), "user_guid", None)
        log.info("logged in as %s, mode=%s, model=%s", my_guid, MODE, MODEL)

        async def on_message(update):
            chat, sender, text, chat_type = extract(update)
            if not text or not chat:
                return
            is_me = sender == my_guid
            save(chat, sender, is_me, text)
            if is_me or chat in BLOCKLIST or chat == my_guid:
                return
            if chat_type in ("Group", "Channel") and not REPLY_IN_GROUPS:
                return
            async with chat_locks[chat]:
                if MODE == "auto" and not rate_ok():
                    log.warning("rate limit reached, skipping %s", chat)
                    return
                try:
                    reply = await generate_reply(chat)
                except Exception:
                    log.exception("AI call failed")
                    return
                if not reply or reply.upper().startswith("SKIP"):
                    return
                await asyncio.sleep(min(8, 1 + len(reply) / 20))
                if MODE == "auto":
                    if DISCLOSE:
                        reply += "\n(پاسخ خودکار)"
                    try:
                        await update.reply(reply)
                    except Exception:
                        log.exception("update.reply failed, using send_message")
                        await client.send_message(chat, reply)
                    save(chat, my_guid, True, reply)
                    sent_log.append(time.time())
                else:
                    await client.send_message(
                        my_guid, f"پیش‌نویس برای {chat}:\nپیام: {text}\n\nپیشنهاد: {reply}"
                    )

        log_client_info(client)
        register(client, on_message)
        await wait_forever(client)


if __name__ == "__main__":
    asyncio.run(main())
