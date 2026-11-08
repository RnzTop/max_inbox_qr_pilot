"""Bot API must only publish to configured private group (no polling)."""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from bot_api import BotApiError, MaxBotAPI, parse_chat_id


class StubBot(MaxBotAPI):
    def __init__(self, chat=-12345, public=False, status="active"):
        self._token = "never_print_this_token"
        self.chat_id = chat
        self.public = public
        self.status = status
        self.bot_id = None
        self.calls = []

    def _request(self, method, path, params=None, body=None):
        self.calls.append((method, path, params, body))
        if path == "/me":
            return {"user_id": 345, "username": "workbot"}
        if path.startswith("/chats/"):
            return {
                "chat_id": self.chat_id, "type": "chat",
                "status": self.status, "is_public": self.public,
            }
        if path == "/messages" and method == "POST":
            return {"message": {"body": {"mid": "result-mid"}}}
        raise AssertionError("Unexpected Bot API call")


class BotApiTests(unittest.TestCase):
    def test_chat_id_accepts_signed_int64_and_rejects_wrong_input(self):
        self.assertEqual(parse_chat_id("-123456"), -123456)
        for bad in ("", "0", "not-a-number", "1;echo", "1.1",
                    "99999999999999999999999999", True, None):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                parse_chat_id(bad)

    def test_only_validated_private_group_can_receive(self):
        bot = StubBot()
        self.assertEqual(bot.preflight(), 345)
        self.assertEqual(bot.send_text("Заявка от клиента"), "result-mid")
        self.assertEqual(bot.calls[-1][0:2], ("POST", "/messages"))
        self.assertEqual(bot.calls[-1][2]["chat_id"], -12345)
        self.assertEqual(bot.calls[-1][3], {"text": "Заявка от клиента"})
        self.assertFalse(any("/updates" in x[1] for x in bot.calls))
        for bot in [StubBot(public=True), StubBot(status="removed")]:
            with self.assertRaises(BotApiError):
                bot.preflight()

    def test_send_body_has_4000_char_limit(self):
        bot = StubBot()
        bot.preflight()
        with self.assertRaises(BotApiError):
            bot.send_text("x" * 4001)

    def test_source_never_uses_bot_update_poller_or_webhook_changes(self):
        source = (ROOT / "bot_api.py").read_text()
        self.assertNotIn('"/updates"', source)
        self.assertNotIn('"/subscriptions"', source)
        self.assertNotIn("verify=False", source)


if __name__ == "__main__":
    unittest.main()
