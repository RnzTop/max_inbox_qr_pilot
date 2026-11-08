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
state_function = next(
    node for node in tree.body
    if isinstance(node, ast.FunctionDef) and node.name == "diagnose_page_state"
)
exec(compile(ast.Module(body=[state_function], type_ignores=[]), str(MODULE), "exec"), namespace)
diagnose_state = namespace["diagnose_page_state"]
marker_function = next(
    node for node in tree.body
    if isinstance(node, ast.FunctionDef) and node.name == "diagnose_exact_marker_nodes"
)
namespace["MARKER"] = "MAX HA TEST fake-marker"
exec(compile(ast.Module(body=[marker_function], type_ignores=[]), str(MODULE), "exec"), namespace)
diagnose_markers = namespace["diagnose_exact_marker_nodes"]


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


class PageStateTests(unittest.TestCase):
    def test_page_status_only_counts(self):
        message = diagnose_state(StubPage({
            "ready": "complete", "elements": 250, "body_chars": 51,
            "chat_rows": 0, "message_bubbles": 0, "message_indices": 0,
            "chat_editors": 0, "phone_inputs": 1, "qr_canvas": 1,
            "qr_words": True, "root_empty": False,
            "private_text": "SECRET_MESSAGE",
            "private_url": "https://web.max.ru/private"
        }))
        self.assertIn("строк чатов=0", message)
        self.assertIn("телефонных полей=1", message)
        self.assertIn("текстовый признак входа=да", message)
        self.assertNotIn("SECRET", message)
        self.assertNotIn("private", message)

    def test_failure_does_not_reveal_exception(self):
        class BrokenPage:
            def evaluate(self, script):
                raise RuntimeError("SECRET")
        self.assertEqual(diagnose_state(BrokenPage()), "Страница: диагностика временно недоступна.")



class MarkerStub:
    def __init__(self, result):
        self.result = result

    def evaluate_all(self, script):
        assert "textContent" not in script
        assert "innerHTML" not in script
        assert "outerHTML" not in script
        assert "cookie" not in script
        return self.result


class MarkerPage:
    def __init__(self, result):
        self.result = result

    def get_by_text(self, marker):
        return MarkerStub(self.result)


class MarkerTests(unittest.TestCase):
    def test_reports_counts_without_values(self):
        message = diagnose_markers(MarkerPage({
            "markers": 2, "bubbles": 2, "inbound": 2,
            "indexed": 2, "deep_index": 2,
            "mid": 0, "chat": 0, "seq": 0,
            "private_text": "SECRET_NAME",
            "private_mid": "SECRET_MESSAGE_ID",
        }))
        self.assertIn("с data-index=2", message)
        self.assertIn("глубже 8 уровней=2", message)
        self.assertNotIn("SECRET", message)

    def test_empty_and_broken_results(self):
        msg = diagnose_markers(MarkerPage({}))
        self.assertIn("найдено=0", msg)
        self.assertEqual(diagnose_markers(MarkerPage(None)), "Тестовые метки: проверка недоступна.")


if __name__ == "__main__":
    unittest.main()
