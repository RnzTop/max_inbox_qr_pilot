"""No MAX credentials, network, or Playwright required."""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from auth_health import AuthWatch


class AuthWatchTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.path = Path(self.folder.name) / "auth_health.json"
        self.messages = []
        self.now = 0

    def watch(self, send=None):
        if send is None:
            send = lambda msg: self.messages.append(msg) or True
        return AuthWatch(self.path, send, lambda: self.now)

    def test_no_alert_on_initial_login_then_lost_and_restored(self):
        watch = self.watch()
        watch.observed("login")
        self.assertEqual(self.messages, [])
        watch.observed("online")
        self.assertEqual(self.messages, [])
        watch.observed("login")
        self.assertEqual(len(self.messages), 1)
        self.assertIn("вышел", self.messages[0])
        watch.observed("login")
        self.assertEqual(len(self.messages), 1)
        watch.observed("online")
        self.assertEqual(len(self.messages), 2)
        self.assertIn("восстановлен", self.messages[1])

    def test_state_survives_restart(self):
        self.watch().observed("online")
        after_restart = self.watch()
        after_restart.observed("login")
        self.assertEqual(len(self.messages), 1)
        next_restart = self.watch()
        next_restart.observed("login")
        self.assertEqual(len(self.messages), 1)
        next_restart.observed("online")
        self.assertEqual(len(self.messages), 2)

    def test_failed_send_is_retried_with_cooldown(self):
        sent = []
        def deliver(message):
            sent.append(message)
            return len(sent) >= 2
        watch = self.watch(deliver)
        watch.observed("online")
        watch.observed("login")
        self.assertEqual(len(sent), 1)
        self.now = 59
        watch.observed("login")
        self.assertEqual(len(sent), 1)
        self.now = 60
        watch.observed("login")
        self.assertEqual(len(sent), 2)
        self.assertIsNone(watch.state["pending"])

    def test_bad_file_does_not_trigger_false_alarm(self):
        self.path.write_text("{broken")
        self.watch().observed("login")
        self.assertEqual(self.messages, [])
        self.assertFalse(self.watch().state["ever_online"])

    def test_bad_state_rejected(self):
        with self.assertRaises(ValueError):
            self.watch().observed("not_sure")


if __name__ == "__main__":
    unittest.main()
