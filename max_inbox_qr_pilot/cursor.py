"""Fail-closed cursor for an append-only test conversation."""
import json
from inbox import Incoming


class TestCursor:
    def __init__(self, queue, account, dialog):
        self.queue = queue
        self.account = account
        self.dialog = dialog
        with queue.connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS test_cursor (
                account TEXT, dialog TEXT, snapshot TEXT, PRIMARY KEY(account,dialog)
            )""")

    def observe(self, snapshot):
        if not snapshot or len(snapshot) > 100:
            raise ValueError("Тестовые метки не видны целиком")
        indices = [int(index) for index, _ in snapshot]
        if indices != sorted(set(indices)):
            raise ValueError("Порядок тестовых сообщений изменился")
        snapshot = [list(item) for item in snapshot]
        key = (self.account, self.dialog)
        with self.queue.connect() as db:
            row = db.execute("SELECT snapshot FROM test_cursor WHERE account=? AND dialog=?", key).fetchone()
        previous = json.loads(row[0]) if row else []
        if snapshot[:len(previous)] != previous:
            raise ValueError("История тестовых меток изменилась. Пересылка приостановлена.")
        for index, text in snapshot[len(previous):]:
            self.queue.receive(Incoming(self.account, self.dialog, str(index), self.dialog, text),
                               baseline=row is None)
        with self.queue.connect() as db:
            db.execute("INSERT OR REPLACE INTO test_cursor VALUES(?,?,?)", (*key, json.dumps(snapshot)))
        return len(snapshot)-len(previous) if row else 0
