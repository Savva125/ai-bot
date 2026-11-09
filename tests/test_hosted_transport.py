"""Real loopback HTTP and a real aiogram dispatcher, without contacting Telegram."""
import asyncio
from datetime import datetime, timezone
import json
import socket
from threading import Thread
import time
from uuid import uuid4

from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.methods import SendMessage
from aiogram.types import Chat, Message, Update, User
import httpx
import uvicorn

from creator_desk.db import connect
from creator_desk.remote_ai import RemoteProvider
from creator_desk_server.api import create_app
from creator_desk_server.bot import build_dispatcher
from creator_desk_server.config import Settings
from creator_desk_server.manage import migrate
from creator_desk_server.schemas import CreateLicense


def test_real_http_activation_generation_and_block(tmp_path, monkeypatch):
    settings = Settings("sqlite:///" + str(tmp_path / "transport.db"), "a" * 48, allow_sqlite=True)
    engine, _ = connect(settings.database_url)
    migrate(engine)
    engine.dispose()

    class Model:
        async def generate(self, context):
            assert context["history"][-1]["text"] == "Привет"
            return {"text": "Рада тебя видеть!", "handoff": False, "reason": "", "action": "reply"}

    app = create_app(settings, Model())
    item = app.state.licensing.create(CreateLicense(), "test")
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    url = f"http://127.0.0.1:{sock.getsockname()[1]}"
    server = uvicorn.Server(uvicorn.Config(app, log_level="critical", access_log=False))
    thread = Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 10
        while not server.started and time.monotonic() < deadline:
            time.sleep(0.02)
        assert server.started
        saved = {}
        monkeypatch.setattr("creator_desk.remote_ai.save_secret", lambda name, value: saved.update({name: value}))
        provider = RemoteProvider(url)
        activation = provider.activate_sync(item["key"])
        assert activation["license_id"] == item["id"] and saved
        assert provider.generate({"history": [{"role": "incoming", "text": "Привет"}], "catalog": []}).text == "Рада тебя видеть!"
        app.state.licensing.admin_change(item["id"], "block", "test")
        import pytest
        from creator_desk.errors import AiError
        with pytest.raises(AiError, match="заблокирован"):
            provider.generate({"history": []})
    finally:
        server.should_exit = True
        thread.join(10)
        sock.close()
    assert not thread.is_alive()


def test_bot_dispatches_create_to_api_and_ignores_outsiders(monkeypatch):
    real_client = httpx.AsyncClient
    calls, answers = [], []

    def handler(request):
        calls.append(request)
        assert request.url.path == "/admin/licenses"
        assert request.headers["Authorization"] == "Bearer " + "a" * 48
        body = json.loads(request.content)
        assert body == {"days": 30, "daily_limit": 500, "max_devices": 1, "label": "Иван"}
        return httpx.Response(200, json={"id": str(uuid4()), "key": "test-key", **body})

    monkeypatch.setattr("creator_desk_server.bot.httpx.AsyncClient", lambda **kwargs: real_client(transport=httpx.MockTransport(handler), **kwargs))

    class FakeSession(BaseSession):
        async def close(self):
            pass

        async def make_request(self, bot, method, timeout=None):
            assert isinstance(method, SendMessage)
            answers.append(method)
            return True

        async def stream_content(self, *args, **kwargs):
            yield b""

    async def run():
        bot = Bot("123456:" + "x" * 35, session=FakeSession())
        dp = build_dispatcher({42}, "http://127.0.0.1:8000", "a" * 48)
        for index, (user_id, kind) in enumerate([(7, "private"), (42, "group"), (42, "private")]):
            message = Message(message_id=index + 1, date=datetime.now(timezone.utc),
                chat=Chat(id=user_id, type=kind), from_user=User(id=user_id, is_bot=False, first_name="Test"),
                text="/new 30 500 1 Иван")
            await dp.feed_update(bot, Update(update_id=index + 1, message=message))
        await bot.session.close()

    asyncio.run(run())
    assert len(calls) == len(answers) == 1
    assert "test-key" in answers[0].text and answers[0].protect_content is True
