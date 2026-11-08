"""Limited QR pilot. Reads only marked incoming text in an explicitly named test dialog."""
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import re
import threading
import time
from urllib.parse import urlsplit

from playwright.sync_api import sync_playwright

from cursor import TestCursor
from inbox import Inbox
from run_group_test import MaxAPI
from test_group import TestGroup


DATA = Path("/data")
MARKER = re.compile(r"^MAX HA TEST [0-9]{4,16}$")
status = "Запуск пилота"
qr = b""


class Panel(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.client_address[0] != "172.30.32.2":
            self.send_error(403)
            return
        from html import escape
        image = qr
        if self.path.split("?")[0].endswith("/qr.png"):
            if not image:
                self.send_error(404)
                return
            payload, content_type = image, "image/png"
        else:
            text = escape(status)
            picture = '<img src="./qr.png" alt="QR входа MAX">' if image else ""
            payload = (f'<!doctype html><meta charset="utf-8"><meta http-equiv="refresh" content="5">'
                       f'<h1>MAX — QR-пилот</h1><p>{text}</p>{picture}').encode()
            content_type = "text/html; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):
        pass


def snapshot(page):
    return page.get_by_text(MARKER).evaluate_all("""nodes => nodes.flatMap(el => {
        const bubble = el.closest('.bubbleContent');
        const item = bubble?.closest('[data-index]');
        if (!item || !el.closest('[class*="--left"]')) return [];
        return [[item.getAttribute('data-index'), el.textContent.trim()]];
    })""")



def find_test_dialog(page, name):
    """Only select one *visible* exact-name match, never a fuzzy chat match."""
    labels = page.get_by_text(name, exact=True)
    total = labels.count()
    if total > 50:
        return None, total
    visible = [labels.nth(i) for i in range(total) if labels.nth(i).is_visible()]
    if len(visible) != 1:
        return None, len(visible)

    # Prefer the known chat-row markup, but do not depend on it.
    rows = page.locator("button.cell").filter(has=visible[0])
    if rows.count() == 1 and rows.first.is_visible():
        return rows.first, 1
    if rows.count() > 1:
        return None, len(visible)
    # Clicking the only visible exact-name label also works when MAX uses
    # a div, link, or a differently named chat-row element.
    return visible[0], 1


def run(options):
    global status, qr
    if not options.get("test_confirmed"):
        status = "Включите test_confirmed и заполните параметры тестовых аккаунтов."
        return
    dialog = options["test_dialog"].strip()
    account = options["test_account_label"].strip()
    if not dialog or not account or not options["test_bot_token"].strip():
        raise ValueError("Заполните имя тестового диалога, метку аккаунта и токен")
    queue = Inbox(DATA / "pilot.sqlite3")
    api = MaxAPI(options["test_bot_token"].strip())
    bot = api.call("GET", "/me")
    if (bot.get("username") or "").lstrip("@") != options["test_bot_username"].strip().lstrip("@"):
        raise ValueError("Токен не соответствует указанному тестовому боту")
    group = TestGroup(queue, api, options["test_chat_id"])
    group.check_destination()
    binding = DATA / "binding.json"
    identity = [account, dialog, bot["user_id"], group.chat_id]
    if binding.exists() and json.loads(binding.read_text()) != identity:
        raise ValueError("Параметры уже созданного теста изменились")
    binding.write_text(json.dumps(identity, ensure_ascii=False))
    cursor = TestCursor(queue, account, dialog)
    params = {"types": "message_callback", "timeout": 0}
    marker = api.call("GET", "/updates", params).get("marker")
    with sync_playwright() as playwright:
        context = playwright.chromium.launch_persistent_context(
            str(DATA / "browser"), executable_path="/usr/bin/chromium",
            headless=True, chromium_sandbox=True, accept_downloads=False,
        )
        page = context.pages[0] if context.pages else context.new_page()
        page.goto("https://web.max.ru/", wait_until="domcontentloaded")
        opened = False
        while True:
            if urlsplit(page.url).hostname != "web.max.ru":
                raise ValueError("Браузер покинул веб-версию MAX")
            login = page.get_by_role("heading", name=re.compile("Войдите в MAX по QR-коду"))
            if login.is_visible():
                status = "Отсканируйте QR только телефоном тестового аккаунта MAX."
                qr = page.screenshot()
                opened = False
                time.sleep(5)
                continue
            qr = b""
            if not opened:
                target, matches = find_test_dialog(page, dialog)
                if target is None:
                    status = (f"Чат не найден в браузере дополнения: точных видимых совпадений "
                              f"имени {matches}; нужно ровно одно. Проверьте вход через QR "
                              f"и видимость чата в этом аккаунте MAX.")
                    time.sleep(3)
                    continue
                target.click(timeout=5000)
                opened = True
                time.sleep(2)
            events = snapshot(page)
            if not events:
                status = "В тестовом диалоге ожидаю начальную метку MAX HA TEST и четыре цифры."
                time.sleep(2)
                continue
            cursor.observe(events)
            status = "Пилот слушает тестовый диалог. Отправьте новую метку MAX HA TEST с цифрами."
            if group.send_one():
                time.sleep(0.6)
            if marker is not None:
                params["marker"] = marker
            batch = api.call("GET", "/updates", params)
            for event in batch.get("updates", []):
                group.accept_callback(event)
            marker = batch.get("marker", marker)
            if group.edit_one():
                time.sleep(0.6)
            time.sleep(2)


if __name__ == "__main__":
    os.umask(0o077)
    panel = ThreadingHTTPServer(("0.0.0.0", 8099), Panel)
    threading.Thread(target=panel.serve_forever, daemon=True).start()
    try:
        run(json.loads((DATA / "options.json").read_text()))
    except Exception as exc:
        qr = b""
        status = ("Пилот остановлен: " + str(exc)) if type(exc) in (ValueError, RuntimeError) else (
            "Пилот остановлен на этапе браузера или API. Проверьте настройки и поддержку Chromium sandbox.")
        print(status, flush=True)
    threading.Event().wait()
