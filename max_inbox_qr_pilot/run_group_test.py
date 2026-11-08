"""Send one synthetic card to an explicitly confirmed private MAX test group."""
import getpass
import json
from pathlib import Path
import ssl
import tempfile
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPSHandler, HTTPRedirectHandler, Request, build_opener

from inbox import Inbox, Incoming
from test_group import TestGroup


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class MaxAPI:
    def __init__(self, token):
        context = ssl.create_default_context()
        for cert in (Path(__file__).parent / "certs").glob("*.pem"):
            context.load_verify_locations(str(cert))
        self.opener = build_opener(NoRedirect(), HTTPSHandler(context=context))
        self.token = token

    def call(self, method, path, params=None, body=None):
        url = "https://platform-api2.max.ru" + path
        if params:
            url += "?" + urlencode(params)
        data = None if body is None else json.dumps(body, ensure_ascii=False).encode()
        req = Request(url, data=data, method=method, headers={
            "Authorization": self.token, "Content-Type": "application/json"
        })
        try:
            with self.opener.open(req, timeout=20) as response:
                result = json.load(response)
        except HTTPError as exc:
            raise RuntimeError(f"{method} {path.split('/')[1]}: MAX вернул HTTP {exc.code}") from None
        except (URLError, TimeoutError, ValueError):
            raise RuntimeError("Не удалось получить ответ от MAX") from None
        if result.get("success") is False:
            raise RuntimeError("MAX отклонил запрос")
        return result


def main():
    token = getpass.getpass("Токен отдельного тестового бота (ввод скрыт): ").strip()
    if not token:
        raise ValueError("Токен не введён")
    api = MaxAPI(token)
    bot = api.call("GET", "/me")
    if api.call("GET", "/subscriptions").get("subscriptions"):
        raise ValueError("У этого бота уже настроен Webhook. Используйте отдельного тестового бота.")
    print("Бот:", bot.get("username") or bot.get("first_name"))
    chat_id = input("Числовой ID закрытой тестовой группы или Enter для поиска события добавления: ").strip()
    if not chat_id:
        updates = api.call("GET", "/updates", {"types": "bot_added", "timeout": 0})
        candidates = {u["chat_id"] for u in updates.get("updates", [])
                      if u.get("update_type") == "bot_added" and not u.get("is_channel")
                      and type(u.get("chat_id")) is int}
        if len(candidates) != 1:
            raise ValueError("Событие добавления не найдено однозначно. Укажите ID тестовой группы.")
        chat_id = str(candidates.pop())
    chat_id = int(chat_id)
    with tempfile.TemporaryDirectory() as folder:
        queue = Inbox(Path(folder) / "test.db")
        group = TestGroup(queue, api, chat_id)
        group.check_destination()
        chat = api.call("GET", f"/chats/{chat_id}")
        print(f"Группа: {chat.get('title') or 'без названия'} ({chat_id})")
        if input("Для отправки одной искусственной карточки в эту группу введите ТЕСТ: ").strip() != "ТЕСТ":
            print("Отправка отменена.")
            return
        params = {"types": "message_callback", "timeout": 0}
        marker = api.call("GET", "/updates", params).get("marker")
        queue.receive(Incoming("synthetic", "dialog", "test-message", "Тестовый клиент", "Тест уведомления MAX."))
        group.send_one()
        print("Карточка отправлена. Нажмите «Взять в работу» в тестовой группе. Ожидание до 2 минут.")
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            params["timeout"] = 8
            if marker is not None:
                params["marker"] = marker
            batch = api.call("GET", "/updates", params)
            for event in batch.get("updates", []):
                if group.accept_callback(event):
                    group.edit_one()
                    print("MAX подтвердил назначение сотрудника и обновление карточки.")
                    return
            marker = batch.get("marker", marker)
            time.sleep(0.35)
        print("Время проверки истекло. Нажатие в тестовой группе не получено.")


if __name__ == "__main__":
    try:
        main()
    except (ValueError, RuntimeError, KeyError) as exc:
        print(f"Проверка остановлена: {exc}")
        raise SystemExit(1) from None
