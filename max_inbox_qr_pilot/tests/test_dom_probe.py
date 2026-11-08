import ast
import unittest
from pathlib import Path


MODULE = Path(__file__).resolve().parents[1] / "pilot.py"
source = MODULE.read_text(encoding="utf-8")
tree = ast.parse(source)
probe_function = next(
    node for node in tree.body
    if isinstance(node, ast.FunctionDef) and node.name == "diagnose_message_ids"
)
namespace = {}
exec(compile(ast.Module(body=[probe_function], type_ignores=[]), str(MODULE), "exec"), namespace)
diagnose = namespace["diagnose_message_ids"]


class StubPage:
    def __init__(self, result):
        self.result = result

    def evaluate(self, script):
        # The probe does not extract text/identifiers or send network requests.
        assert "textContent" not in script
        assert "innerHTML" not in script
        assert "fetch(" not in script
        assert "XMLHttpRequest" not in script
        assert "localStorage" not in script
        assert "sessionStorage" not in script
        assert "cookie" not in script
        return self.result


class DOMProbeTests(unittest.TestCase):
    def test_prints_only_counts(self):
        message = diagnose(StubPage({
            "bubbles": 4, "mid": 2, "chat": 1, "seq": 0,
            "index": 4, "html_id": 3,
            "hasChatAttributeElsewhere": True, "route": False,
            "body": "SECRET_CUSTOMER_TEXT",
            "id_value": "SECRET_CHAT_ID"
        }))
        self.assertIn("mid=2", message)
        self.assertIn("chat=1", message)
        self.assertIn("data-index=4", message)
        self.assertNotIn("SECRET", message)

    def test_empty_page(self):
        message = diagnose(StubPage({
            "bubbles": 0, "mid": 0, "chat": 0,
            "seq": 0, "index": 0, "html_id": 0,
            "hasChatAttributeElsewhere": False, "route": False,
        }))
        self.assertIn("сообщений=0", message)

    def test_unexpected_dom(self):
        class BrokenPage:
            def evaluate(self, script):
                raise RuntimeError("Secret text in browser exception")
        self.assertEqual(diagnose(BrokenPage()), "DOM: диагностика временно недоступна.")


if __name__ == "__main__":
    unittest.main()
