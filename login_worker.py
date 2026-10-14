"""
فرآیند لاگین روبیکا؛ توسط panel.py اجرا می‌شود.
rubpy هنگام لاگین از input() برای گرفتن شماره/کد استفاده می‌کند. اینجا input را
با تابعی عوض می‌کنیم که از پنل وب جواب بگیرد، پس به نسخه‌ی کتابخانه وابسته نیست.
پروتکل: هر خط خروجی یک JSON است؛ پاسخ‌ها خط‌به‌خط از stdin می‌آید.
"""
import asyncio
import builtins
import json
import os
import sys

os.chdir(os.getenv("DATA_DIR", "."))


_out = sys.stdout
sys.stdout = sys.stderr  # هر print دیگری (مثلاً از rubpy) به لاگ می‌رود، نه به پروتکل پنل
_calls = 0


def emit(**kw):
    _out.write(json.dumps(kw, ensure_ascii=False) + "\n")
    _out.flush()


def fake_input(prompt=""):
    global _calls
    p = str(prompt).lower()
    print(f"[login_worker] input() prompt={prompt!r}", file=sys.stderr, flush=True)
    if "code" in p or "کد" in p:
        kind = "code"
    elif "pass" in p or "رمز" in p:
        kind = "password"
    elif "phone" in p or "number" in p or "شماره" in p or "تلفن" in p:
        kind = "phone"
    else:
        # متن پرسش ناشناس است؛ از ترتیب پرسش‌ها حدس می‌زنیم: شماره، کد، رمز
        kind = ["phone", "code", "password"][min(_calls, 2)]
    _calls += 1
    emit(event="need", kind=kind)
    line = sys.stdin.readline()
    if not line:
        raise EOFError("panel closed")
    return line.strip()


builtins.input = fake_input

from rubpy import Client  # noqa: E402


async def main():
    try:
        async with Client(name="session") as c:
            me = await c.get_me()
            guid = getattr(getattr(me, "user", me), "user_guid", None)
            emit(event="done", guid=str(guid))
    except Exception as e:  # noqa: BLE001
        emit(event="error", message=f"{type(e).__name__}: {e}")


asyncio.run(main())
