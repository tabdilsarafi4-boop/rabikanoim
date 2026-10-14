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


def emit(**kw):
    print(json.dumps(kw, ensure_ascii=False), flush=True)


def fake_input(prompt=""):
    p = str(prompt).lower()
    if "code" in p or "کد" in p:
        kind = "code"
    elif "pass" in p or "رمز" in p:
        kind = "password"
    else:
        kind = "phone"
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
