"""Local notification queue for normalized, uniquely identified MAX events."""
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass


@dataclass(frozen=True)
class Incoming:
    account_id: str
    dialog_id: str
    message_id: str
    name: str
    text: str = ""
    attachment: str = ""
    visible_phone: str = ""

    def notification(self):
        lines = [f"Новое обращение от {self.name}"]
        if self.visible_phone:
            lines.append(f"Телефон: {self.visible_phone}")
        lines.append(self.text or f"Вложение: {self.attachment or 'неизвестный тип'}")
        return "\n".join(lines)


class Inbox:
    def __init__(self, path):
        self.path = path
        with self.connect() as db:
            db.execute("""
                CREATE TABLE IF NOT EXISTS notices (
                    id INTEGER PRIMARY KEY,
                    account TEXT NOT NULL, dialog TEXT NOT NULL, source_message TEXT NOT NULL,
                    body TEXT NOT NULL, delivery TEXT NOT NULL DEFAULT 'queued',
                    group_message TEXT, employee_id TEXT, employee_name TEXT,
                    revision INTEGER NOT NULL DEFAULT 0, published INTEGER NOT NULL DEFAULT 0,
                    UNIQUE(account, dialog, source_message)
                )
            """)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def receive(self, incoming, *, baseline=False):
        if not all((incoming.account_id, incoming.dialog_id, incoming.message_id)):
            raise ValueError("Источник должен предоставить устойчивый ключ сообщения")
        with self.connect() as db:
            result = db.execute("""
                INSERT INTO notices(account, dialog, source_message, body, delivery)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(account, dialog, source_message) DO NOTHING
            """, (incoming.account_id, incoming.dialog_id, incoming.message_id,
                  "" if baseline else incoming.notification(), "baseline" if baseline else "queued"))
            return result.lastrowid if result.rowcount else None

    def reserve_delivery(self):
        # Reserve before calling MAX. An interrupted send stays uncertain and
        # is never repeated automatically without evidence it was rejected.
        with self.connect() as db:
            return db.execute("""
                UPDATE notices SET delivery='uncertain'
                WHERE id=(SELECT id FROM notices WHERE delivery='queued' ORDER BY id LIMIT 1)
                RETURNING id, body
            """).fetchone()

    def delivered(self, notice_id, group_message):
        with self.connect() as db:
            return db.execute("""
                UPDATE notices SET delivery='sent', group_message=?
                WHERE id=? AND delivery='uncertain'
            """, (group_message, notice_id)).rowcount == 1

    def claim(self, notice_id, group_message, employee_id, employee_name):
        if not employee_id or not employee_name:
            raise ValueError("Нужны идентификатор и имя сотрудника")
        with self.connect() as db:
            return db.execute("""
                UPDATE notices SET employee_id=?, employee_name=?, revision=revision+1
                WHERE id=? AND group_message=? AND delivery='sent' AND employee_id IS NULL
            """, (employee_id, employee_name, notice_id, group_message)).rowcount == 1

    def pending_edit(self):
        with self.connect() as db:
            return db.execute("""
                SELECT id, group_message, body, employee_name, revision
                FROM notices WHERE revision>published ORDER BY id LIMIT 1
            """).fetchone()

    def edited(self, notice_id, revision):
        with self.connect() as db:
            db.execute("""
                UPDATE notices SET published=?
                WHERE id=? AND revision=? AND published<?
            """, (revision, notice_id, revision, revision))


def message_payload(notice):
    button = {"type": "callback", "text": "Взять в работу", "payload": f"inbox:{notice['id']}"}
    return {"text": notice["body"], "attachments": [
        {"type": "inline_keyboard", "payload": {"buttons": [[button]]}}
    ]}


def claimed_payload(edit):
    return {"text": f"{edit['body']}\n\nВзял в работу: {edit['employee_name']}",
            "attachments": []}
