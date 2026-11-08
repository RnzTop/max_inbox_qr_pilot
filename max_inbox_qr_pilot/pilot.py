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
            picture = '<img src="./qr.png" alt="Страница браузера MAX">' if image else ""
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



def show_login_if_needed(page):
    """Detect MAX login without relying on one heading or exposing chat screenshots."""
    global qr, status

    # MAX can display a choice between phone number and QR sign-in.
    switch = page.get_by_text(re.compile(r"^Войти\s+по\s+QR[-\s‑–]?коду$", re.I))
    if switch.count() == 1 and switch.first.is_visible():
        # Never click a chat message by mistake.
        if page.locator("button.cell").count() == 0:
            switch.first.click(timeout=5000)
            page.wait_for_timeout(1500)

    markers = (
        page.get_by_role("heading", name=re.compile(r"(Войдите|Вход).*(MAX|QR)", re.I)),
        page.get_by_text(re.compile(r"^(?:Отсканируйте|Наведите камеру).*QR[-\s‑–]?код", re.I)),
        page.get_by_text(re.compile(r"^Войти\s+по\s+QR[-\s‑–]?коду$", re.I)),
    )
    is_login = any(locator.count() and locator.first.is_visible() for locator in markers)
    if is_login:
        status = "Открыта страница входа MAX. Отсканируйте QR-код в панели дополнения."
        qr = page.screenshot()
        return True

    # A phone-login form is not a chat list, even if the specific QR title changed.
    phone = page.locator('input[type="tel"]')
    if phone.count() and phone.first.is_visible():
        qr = b""
        status = "Встроенный браузер MAX показывает вход по телефону, а не чаты. Нужен вход по QR-коду."
        return True

    qr = b""
    return False



def diagnose_message_ids(page):
    """Check DOM attribute PRESENCE only; never read message or ID values."""
    try:
        probe = page.evaluate("""() => {
            const bubbles = Array.from(document.querySelectorAll('.bubbleContent')).slice(0, 100);
            const keys = ['mid', 'chat', 'seq', 'index', 'html_id'];
            const found = {mid: 0, chat: 0, seq: 0, index: 0, html_id: 0};
            for (const bubble of bubbles) {
                const seen = new Set();
                function inspect(node) {
                    for (const attr of node.attributes || []) {
                        if (!attr.value) continue;
                        const name = attr.name.toLowerCase().replace(/[^a-z0-9]/g, '');
                        if (['mid', 'datamid', 'messageid', 'datamessageid',
                             'msgid', 'datamsgid'].includes(name)) seen.add('mid');
                        if (['chatid', 'datachatid', 'peerid', 'datapeerid',
                             'dialogid', 'datadialogid'].includes(name)) seen.add('chat');
                        if (['seq', 'dataseq', 'sequence', 'datasequence'].includes(name)) seen.add('seq');
                        if (name === 'dataindex') seen.add('index');
                        if (name === 'id') seen.add('html_id');
                    }
                }
                let parent = bubble;
                for (let level = 0; level < 8 && parent; level++, parent = parent.parentElement) {
                    inspect(parent);
                }
                const inside = bubble.querySelectorAll('*');
                for (let i = 0; i < Math.min(inside.length, 50); i++) inspect(inside[i]);
                for (const name of keys) if (seen.has(name)) found[name]++;
            }
            const hasChatAttributeElsewhere = !!document.querySelector(
                '[data-chat-id], [data-chatid], [chat-id], [data-peer-id], [data-dialog-id]'
            );
            const route = location.pathname !== '/' || !!location.search || !!location.hash;
            return {bubbles: bubbles.length, ...found, hasChatAttributeElsewhere, route};
        }""")
        if not isinstance(probe, dict):
            return "DOM: данные диагностики недоступны."
        return (
            "DOM (только наличие атрибутов, не ID): "
            "сообщений={bubbles}; mid={mid}; chat={chat}; seq={seq}; "
            "data-index={index}; обычный HTML id={html_id}; "
            "chat-атрибут вне сообщения={outside}; маршрут в URL={route}."
        ).format(
            bubbles=int(probe.get("bubbles", 0)),
            mid=int(probe.get("mid", 0)),
            chat=int(probe.get("chat", 0)),
            seq=int(probe.get("seq", 0)),
            index=int(probe.get("index", 0)),
            html_id=int(probe.get("html_id", 0)),
            outside="да" if probe.get("hasChatAttributeElsewhere") else "нет",
            route="да" if probe.get("route") else "нет",
        )
    except Exception:
        # The app must keep running even if the DOM changes while probing.
        return "DOM: диагностика временно недоступна."


def diagnose_page_state(page):
    """Only return bounded counters and booleans, never URLs or page text."""
    try:
        info = page.evaluate("""() => {
            const body = document.body;
            const text = (body?.innerText || '');
            return {
                ready: document.readyState,
                elements: document.querySelectorAll('*').length,
                body_chars: text.length,
                chat_rows: document.querySelectorAll('button.cell').length,
                message_bubbles: document.querySelectorAll('.bubbleContent').length,
                message_indices: document.querySelectorAll('[data-index]').length,
                chat_editors: document.querySelectorAll('[contenteditable="true"]').length,
                phone_inputs: document.querySelectorAll('input[type="tel"]').length,
                qr_canvas: document.querySelectorAll('canvas').length,
                qr_words: /QR[\s\-‑–]*код|войти\s+в\s+MAX|вход\s+в\s+MAX|сканируйте/i.test(text),
                root_empty: !body || body.children.length === 0
            };
        }""")
        if not isinstance(info, dict):
            return "Страница: диагностика недоступна."
        ready = info.get("ready") if info.get("ready") in ("loading", "interactive", "complete") else "unknown"
        return (
            "Страница MAX: загрузка={ready}; DOM-элементов={elements}; "
            "символов на странице={body_chars}; строк чатов={chat_rows}; "
            "сообщений по CSS={message_bubbles}; элементов data-index={message_indices}; "
            "полей ввода чата={chat_editors}; телефонных полей={phone_inputs}; "
            "canvas={qr_canvas}; текстовый признак входа={qr_words}; "
            "пустая страница={root_empty}."
        ).format(
            ready=ready,
            elements=min(int(info.get("elements", 0)), 99999),
            body_chars=min(int(info.get("body_chars", 0)), 99999),
            chat_rows=min(int(info.get("chat_rows", 0)), 99999),
            message_bubbles=min(int(info.get("message_bubbles", 0)), 99999),
            message_indices=min(int(info.get("message_indices", 0)), 99999),
            chat_editors=min(int(info.get("chat_editors", 0)), 99999),
            phone_inputs=min(int(info.get("phone_inputs", 0)), 99999),
            qr_canvas=min(int(info.get("qr_canvas", 0)), 99999),
            qr_words="да" if info.get("qr_words") else "нет",
            root_empty="да" if info.get("root_empty") else "нет",
        )
    except Exception:
        return "Страница: диагностика временно недоступна."


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
            if show_login_if_needed(page):
                opened = False
                time.sleep(5)
                continue
            if not opened:
                target, matches = find_test_dialog(page, dialog)
                if target is None:
                    # Keep the screen visible only to the local Home Assistant
                    # ingress panel. Never log, save, or upload the image.
                    qr = page.screenshot()
                    status = (f"Тестовый чат не найден: точных совпадений {matches}. "
                              "Под диагностикой показан экран встроенного браузера MAX. "
                              "Не присылайте снимок с личными сообщениями. "
                              + diagnose_page_state(page) + " "
                              + diagnose_message_ids(page))
                    time.sleep(3)
                    continue
                target.click(timeout=5000)
                opened = True
                time.sleep(2)
            events = snapshot(page)
            diagnostics = diagnose_message_ids(page)
            if not events:
                status = ("В тестовом диалоге ожидаю начальную метку MAX HA TEST и четыре цифры. "
                          + diagnostics)
                time.sleep(2)
                continue
            cursor.observe(events)
            status = ("Пилот слушает тестовый диалог. Отправьте новую метку MAX HA TEST с цифрами. "
                      + diagnostics)
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
