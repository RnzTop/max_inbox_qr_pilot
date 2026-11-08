"""No-account security and dedupe tests for the PyMax scaffold."""
import ast
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from id_store import SeenIds

source = (ROOT / "app.py").read_text(encoding="utf-8")
tree = ast.parse(source)
validate_fn = next(
    fn for fn in tree.body
    if isinstance(fn, ast.FunctionDef) and fn.name == "validate_test_options"
)
scope = {}
exec(compile(ast.Module(body=[validate_fn], type_ignores=[]), str(ROOT / "app.py"), "exec"), scope)
validate = scope["validate_test_options"]


class TestOnlyOptions(unittest.TestCase):
    def test_requires_explicit_confirmation_and_two_test_fields(self):
        self.assertIsNone(validate({"test_confirmed": False, "test_dialog": "Test", "test_account_label": "test"}))
        self.assertIsNone(validate({"test_confirmed": True, "test_dialog": " ", "test_account_label": "test"}))
        self.assertIsNone(validate({"test_confirmed": True, "test_dialog": "Test", "test_account_label": ""}))
        self.assertEqual(validate({
            "test_confirmed": True, "test_dialog": " Test ", "test_account_label": "test"
        }), "Test")

    def test_prototype_does_not_import_bot_api_or_send_messages(self):
        self.assertNotIn("MaxAPI(", source)
        self.assertNotIn("test_bot_token", source)
        self.assertNotIn("/updates", source)
        self.assertNotIn("send_message(", source)
        self.assertNotIn(".answer(", source)
        self.assertNotIn("print(qr_url)", source)


class SeenIdsTests(unittest.TestCase):
    def setUp(self):
        self.folder = Path(tempfile.mkdtemp(prefix="readonly-test-"))

    def tearDown(self):
        shutil.rmtree(self.folder)

    def test_stores_only_keyed_fingerprints_and_survives_restart(self):
        store = SeenIds(self.folder)
        self.assertTrue(store.observe(111111111111, 222222222222))
        self.assertFalse(store.observe("111111111111", "222222222222"))
        self.assertEqual(store.count(), 1)
        store.conn.close()
        store2 = SeenIds(self.folder)
        self.assertEqual(store2.count(), 1)
        self.assertFalse(store2.observe(111111111111, 222222222222))
        self.assertTrue(store2.observe(111111111111, 222222222223))
        contents = (self.folder / "seen.sqlite3").read_bytes()
        self.assertNotIn(b"111111111111", contents)
        self.assertNotIn(b"222222222222", contents)
        store2.conn.close()

    def test_refuses_missing_ids(self):
        store = SeenIds(self.folder)
        self.assertFalse(store.observe(None, 1))
        self.assertFalse(store.observe(1, None))
        self.assertEqual(store.count(), 0)
        store.conn.close()


if __name__ == "__main__":
    unittest.main()
