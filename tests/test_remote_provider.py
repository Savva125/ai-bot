import asyncio
import json
from threading import Event, Thread

import httpx
import pytest

from creator_desk.errors import AiError
from creator_desk.remote_ai import RemoteProvider, validate_server_url


def test_remote_client_reauthenticates_once_and_keeps_request_id(monkeypatch):
    import creator_desk.remote_ai as remote
    real_client = httpx.AsyncClient
    calls = []
    saved = {}

    def handler(request):
        calls.append((request.url.path, json.loads(request.content)))
        if request.url.path == "/v1/activate":
            return httpx.Response(200, json={"token": "n" * 48})
        if request.headers["Authorization"] == "Bearer " + "o" * 48:
            return httpx.Response(401, json={"detail": "session_expired"})
        return httpx.Response(200, json={"text": "Ответ", "handoff": False, "reason": "", "action": "reply"})

    monkeypatch.setattr(remote.httpx, "AsyncClient", lambda **kwargs: real_client(transport=httpx.MockTransport(handler), **kwargs))
    monkeypatch.setattr(remote, "save_secret", lambda name, value: saved.update({name: value}))
    provider = RemoteProvider("https://ai.example.com", "o" * 48, "key" * 15, "d" * 32)
    assert provider.generate({"history": []}).text == "Ответ"
    assert [path for path, _ in calls] == ["/v1/generate", "/v1/activate", "/v1/generate"]
    assert calls[0][1]["request_id"] == calls[2][1]["request_id"]
    assert saved


def test_remote_stop_cancels_network_without_late_result(monkeypatch):
    import creator_desk.remote_ai as remote
    real_client = httpx.AsyncClient
    entered, cancelled, stop = Event(), Event(), Event()

    async def handler(request):
        entered.set()
        try:
            await asyncio.sleep(20)
        except asyncio.CancelledError:
            cancelled.set()
            raise

    monkeypatch.setattr(remote.httpx, "AsyncClient", lambda **kwargs: real_client(transport=httpx.MockTransport(handler), **kwargs))
    provider = RemoteProvider("https://ai.example.com", "t" * 48, stop_event=stop)
    errors = []

    def run():
        try:
            provider.generate({"history": []})
        except AiError as exc:
            errors.append(str(exc))

    thread = Thread(target=run)
    thread.start()
    assert entered.wait(3)
    stop.set()
    thread.join(3)
    assert not thread.is_alive() and errors and cancelled.is_set()


@pytest.mark.parametrize("error,status", [("license_blocked", 403), ("daily_limit", 429), ("model_unavailable", 502)])
def test_remote_errors_never_retry_generation(monkeypatch, error, status):
    import creator_desk.remote_ai as remote
    real_client = httpx.AsyncClient
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(status, json={"detail": error})

    monkeypatch.setattr(remote.httpx, "AsyncClient", lambda **kwargs: real_client(transport=httpx.MockTransport(handler), **kwargs))
    with pytest.raises(AiError):
        RemoteProvider("https://ai.example.com", "t" * 48).generate({"history": []})
    assert len(calls) == 1


@pytest.mark.parametrize("url", ["http://remote.example.com", "https://u:p@example.com", "https://example.com/path", "file:///x", "https://example.com?token=secret"])
def test_remote_rejects_insecure_or_ambiguous_endpoint(url):
    with pytest.raises(AiError):
        validate_server_url(url)


def test_legacy_sqlite_adoption_preserves_data_and_creates_backup(tmp_path):
    from creator_desk.app import prepare_client_database
    from creator_desk.db import connect
    from creator_desk.models import Base, AssistantSetting
    from sqlalchemy import inspect
    engine, sessions = connect("sqlite:///" + str(tmp_path / "legacy.db"))
    Base.metadata.create_all(engine)
    with sessions.begin() as s:
        s.add(AssistantSetting(key="reply_style", value="Мой стиль"))
    prepare_client_database(engine)
    assert (tmp_path / "legacy.db.before-hosted.bak").is_file()
    with sessions() as s:
        assert s.get(AssistantSetting, "reply_style").value == "Мой стиль"
    assert "alembic_version" in inspect(engine).get_table_names()
    prepare_client_database(engine)
    assert len(list(tmp_path.glob("*.bak"))) == 1
    engine.dispose()


def test_unknown_sqlite_schema_is_never_stamped(tmp_path):
    from creator_desk.app import prepare_client_database
    from creator_desk.db import connect
    from sqlalchemy import inspect, text
    engine, _ = connect("sqlite:///" + str(tmp_path / "unknown.db"))
    with engine.begin() as c:
        c.execute(text("CREATE TABLE unrelated (id INTEGER)"))
    with pytest.raises(RuntimeError):
        prepare_client_database(engine)
    assert "alembic_version" not in inspect(engine).get_table_names()
    engine.dispose()


def test_admin_bot_only_accepts_explicit_admin_in_private_chat():
    from types import SimpleNamespace as Obj
    from creator_desk_server.bot import allowed
    assert allowed(Obj(chat=Obj(type="private"), from_user=Obj(id=42)), {42})
    assert not allowed(Obj(chat=Obj(type="group"), from_user=Obj(id=42)), {42})
    assert not allowed(Obj(chat=Obj(type="private"), from_user=Obj(id=7)), {42})
    assert not allowed(Obj(chat=Obj(type="private"), from_user=None), {42})
