"""A tiny fake Telegram Bot API server for end-to-end tests of the real adapter."""

from __future__ import annotations

import asyncio
import threading
import time

import uvicorn
from fastapi import FastAPI, Request


class FakeTelegramServer:
    def __init__(self, port: int):
        self.port = port
        self.updates: list[dict] = []
        self.calls: list[tuple[str, dict]] = []
        self._update_id = 0
        self._message_id = 1000
        app = FastAPI()

        @app.post("/bot{token}/{method}")
        async def method(token: str, method: str, request: Request):
            content_type = request.headers.get("content-type", "")
            if "json" in content_type:
                data = await request.json()
            else:  # python-telegram-bot sends form data (multipart for uploads)
                form = await request.form()
                data = {k: (v if isinstance(v, str) else {"filename": v.filename, "size": len(await v.read())})
                        for k, v in form.items()}
            if method != "getUpdates":
                self.calls.append((method, data))
            return {"ok": True, "result": await self._result(method, data)}

        self.server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    async def _result(self, method: str, data: dict):
        if method == "getMe":
            return {"id": 1, "is_bot": True, "first_name": "Pod", "username": "PodBot"}
        if method == "getUpdates":
            if not self.updates:
                await asyncio.sleep(0.05)
            updates, self.updates = self.updates, []
            return updates
        if method in ("sendMessage", "editMessageText", "sendAudio"):
            if method != "editMessageText":
                self._message_id += 1
            message = {
                "message_id": int(data.get("message_id") or self._message_id),
                "date": int(time.time()),
                "chat": {"id": int(data["chat_id"]), "type": "private"},
                "text": data.get("text", ""),
            }
            if method == "sendAudio":
                message["audio"] = {"file_id": "AUDIO-FILE-ID", "file_unique_id": "u1", "duration": 10}
            return message
        return True

    def start(self):
        self.thread.start()
        while not self.server.started:
            time.sleep(0.02)

    def stop(self):
        self.server.should_exit = True
        self.thread.join(timeout=5)

    def push_message(self, chat_id: int, text: str):
        self._update_id += 1
        entities = [{"type": "bot_command", "offset": 0, "length": len(text.split()[0])}] if text.startswith("/") else []
        self.updates.append({"update_id": self._update_id, "message": {
            "message_id": self._update_id, "date": int(time.time()), "text": text, "entities": entities,
            "chat": {"id": chat_id, "type": "private"}, "from": {"id": chat_id, "is_bot": False, "first_name": "U"},
        }})

    def push_callback(self, chat_id: int, message_id: int, data: str):
        self._update_id += 1
        self.updates.append({"update_id": self._update_id, "callback_query": {
            "id": f"cq{self._update_id}", "chat_instance": "ci", "data": data,
            "from": {"id": chat_id, "is_bot": False, "first_name": "U"},
            "message": {"message_id": message_id, "date": int(time.time()), "text": "x",
                        "chat": {"id": chat_id, "type": "private"}},
        }})

    def wait_for(self, predicate, timeout: float = 20):
        deadline = time.time() + timeout
        while time.time() < deadline:
            for call in list(self.calls):
                if predicate(call):
                    return call
            time.sleep(0.05)
        raise AssertionError(f"timed out; calls: {self.calls}")
