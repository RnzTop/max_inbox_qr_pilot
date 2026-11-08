"""Static privacy and production isolation gates."""
import ast
import json
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]


class SafeIntegrationTests(unittest.TestCase):
    def test_config_has_only_two_required_credentials(self):
        options = json.loads((ROOT / "config.yaml").read_text())
        self.assertEqual(options["slug"], "max_pymax_inbox")
        self.assertEqual(options["boot"], "auto")
        self.assertEqual(options["arch"], ["aarch64"])
        self.assertEqual(set(options["options"]), {"bot_token", "work_chat_id"})
        self.assertEqual(options["schema"]["bot_token"], "password")

    def test_not_using_ha_api_or_existing_service(self):
        code = (ROOT / "app.py").read_text()
        ast.parse(code)
        self.assertNotIn("http://supervisor/", code)
        self.assertNotIn("SUPERVISOR_TOKEN", code)
        self.assertNotIn("hassio_supervisor", code)
        self.assertNotIn("send_message(", code)
        self.assertNotIn(".answer(", code)
        self.assertNotIn("print(qr_url)", code)
        self.assertNotIn("print(token)", code)
        self.assertNotIn("MAX HA TEST", code)
        self.assertNotIn("urllib.request", code)

    def test_local_text_retention_is_explicit(self):
        db = (ROOT / "inbox_db.py").read_text()
        self.assertIn("body TEXT NOT NULL", db)
        self.assertIn("state='uncertain'", db)
        self.assertIn("max-production-message-v1", db)


if __name__ == "__main__":
    unittest.main()
