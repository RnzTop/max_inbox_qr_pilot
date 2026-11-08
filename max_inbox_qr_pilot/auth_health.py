"""Best-effort, restart-safe MAX login alerts. Only message sending, no update polling.

The alert bot can be the existing customer-booking bot because this module
never subscribes to or consumes MAX events. No customer content is persisted.
"""
import json
import os
from pathlib import Path
import time


MESSAGES = {
    "lost": (
        "⚠️ MAX: рабочий аккаунт вышел из веб-версии в Home Assistant. "
        "Входящие обращения могут не поступать. Откройте панель и войдите по QR."
    ),
    "restored": (
        "✅ MAX: вход рабочего аккаунта в Home Assistant восстановлен. "
        "Проверьте получение новых обращений."
    ),
}


class AuthWatch:
    def __init__(self, state_path, send_message, clock=time.monotonic):
        self.path = Path(state_path)
        self.send_message = send_message
        self.clock = clock
        self.last_attempt = float("-inf")
        self.state = {
            "ever_online": False,
            "current": "unknown",
            "outage_notified": False,
            "pending": None,
        }
        if self.path.exists():
            try:
                restored = json.loads(self.path.read_text(encoding="utf-8"))
                if (isinstance(restored, dict)
                        and restored.get("current") in ("unknown", "online", "login")
                        and isinstance(restored.get("ever_online"), bool)
                        and isinstance(restored.get("outage_notified"), bool)
                        and restored.get("pending") in (None, "lost", "restored")):
                    self.state.update(restored)
            except (OSError, ValueError):
                # Corrupt state should not cause an unbounded notification loop.
                pass

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix(".tmp")
        with open(temp, "w", encoding="utf-8") as file:
            json.dump(self.state, file)
            file.flush()
            os.fsync(file.fileno())
        os.chmod(temp, 0o600)
        temp.replace(self.path)

    def observed(self, state):
        """State must be proven from MAX's own UI, not missing chat names."""
        if state not in ("online", "login"):
            raise ValueError("Unknown MAX auth state")
        previous = self.state["current"]
        if previous != state:
            if state == "online":
                self.state["ever_online"] = True
                self.state["pending"] = (
                    "restored" if self.state["outage_notified"] else None
                )
            elif self.state["ever_online"] and previous == "online":
                self.state["pending"] = "lost"
            self.state["current"] = state
            self.save()
        self.flush()

    def flush(self):
        pending = self.state["pending"]
        if pending is None or self.clock() - self.last_attempt < 60:
            return
        self.last_attempt = self.clock()
        try:
            ok = self.send_message(MESSAGES[pending])
        except Exception:
            # Never log tokens or message content; retry after cooldown.
            return
        if not ok:
            return
        self.state["outage_notified"] = pending == "lost"
        self.state["pending"] = None
        self.save()
