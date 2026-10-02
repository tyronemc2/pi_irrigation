"""Phone notifications via ntfy (https://ntfy.sh): free, no account needed."""
from __future__ import annotations

import logging
import threading

import requests

log = logging.getLogger(__name__)


class Notifier:
    def __init__(self, cfg: dict, session=None, background: bool = True):
        n = cfg["notifications"]
        self.enabled = bool(n["enabled"] and n["ntfy_topic"])
        self.url = f"{n['ntfy_server'].rstrip('/')}/{n['ntfy_topic']}"
        self.session = session or requests.Session()
        self.background = background
        self.sent: list[dict] = []  # recent messages, for the dashboard and tests

    def send(self, title: str, message: str, priority: str = "default", tags: str = "") -> None:
        self.sent = (self.sent + [{"title": title, "message": message}])[-20:]
        if not self.enabled:
            return
        if self.background:
            threading.Thread(target=self._post, args=(title, message, priority, tags), daemon=True).start()
        else:
            self._post(title, message, priority, tags)

    def _post(self, title, message, priority, tags) -> None:
        try:
            headers = {"Title": title.encode("utf-8"), "Priority": priority}
            if tags:
                headers["Tags"] = tags
            self.session.post(self.url, data=message.encode("utf-8"), headers=headers, timeout=10)
        except Exception as exc:  # a failed notification must never break watering
            log.warning("Notification failed: %s", exc)
