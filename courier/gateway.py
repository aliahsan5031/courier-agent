"""Telegram gateway: talk to Courier from your phone. Only chat ids you list in config are served.

Unattended by design: approvals are denied, so shell commands, overwrites and outbound actions can't be
triggered remotely. Read-only and answer-style tasks work.
"""
from __future__ import annotations

import json
import time


class TelegramGateway:
    def __init__(self, token, http, agent_factory, allowed_chats, sleep=time.sleep):
        self.token, self.http, self.factory = token, http, agent_factory
        self.allowed = {int(c) for c in allowed_chats}
        self.agents, self.offset, self.sleep = {}, 0, sleep

    def _api(self, method, payload):
        st, tx, _ = self.http("POST", f"https://api.telegram.org/bot{self.token}/{method}", {"Content-Type": "application/json"},
                              json.dumps(payload).encode())
        if st >= 400:
            raise RuntimeError(f"telegram {method}: HTTP {st}")
        return json.loads(tx)

    def reply(self, chat, text):
        for i in range(0, max(len(text), 1), 3800):
            self._api("sendMessage", {"chat_id": chat, "text": text[i:i + 3800] or "(empty)"})

    def handle(self, update):
        msg = update.get("message") or {}
        chat, text = (msg.get("chat") or {}).get("id"), (msg.get("text") or "").strip()
        if chat is None or not text:
            return "ignored"
        if chat not in self.allowed:
            self.reply(chat, f"Not authorised. Add {chat} to telegram_allowed_chats in config.json to allow this chat.")
            return "rejected"
        if text == "/new":
            self.agents.pop(chat, None)
            self.reply(chat, "New conversation started.")
            return "reset"
        agent = self.agents.get(chat) or self.agents.setdefault(chat, self.factory())
        try:
            out = agent.run(text, reflect=False)
        except Exception as e:  # never let one bad message kill the gateway
            out = f"error: {e}"
        self.reply(chat, f"[{agent.last_status}] {out}" if agent.last_status != "completed" else out)
        return "answered"

    def poll_once(self, timeout=25):
        d = self._api("getUpdates", {"offset": self.offset, "timeout": timeout, "allowed_updates": ["message"]})
        results = []
        for u in d.get("result", []):
            self.offset = max(self.offset, u["update_id"] + 1)
            results.append(self.handle(u))
        return results

    def run_forever(self):
        while True:
            try:
                self.poll_once()
            except Exception as e:
                print("gateway error:", e)
                self.sleep(5)
