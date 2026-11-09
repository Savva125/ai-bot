import json
from uuid import uuid4

from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import select

from creator_desk.db import connect
from creator_desk_server.api import create_app
from creator_desk_server.config import Settings
from creator_desk_server.manage import migrate
from creator_desk_server.models import Base, Device, Job, License

ADMIN = "a" * 48
HEADERS = {"Authorization": "Bearer " + ADMIN}


class FakeModel:
    def __init__(self):
        self.calls = []
        self.fail = False
        self.hook = None

    async def generate(self, context):
        self.calls.append(context)
        if self.hook:
            self.hook()
        if self.fail:
            raise ValueError("private prompt must not appear in error")
        return {"text": "Привет!", "handoff": False, "reason": "", "action": "reply"}

    async def inspect_screen(self, screen):
        self.calls.append(json.loads(screen))
        return {"is_chat": True, "account": None, "contact": 1, "history": [2],
                "incoming": True, "composer": 3, "send": 4}


@pytest.fixture
def hosted(tmp_path):
    settings = Settings("sqlite:///" + str(tmp_path / "hosted.db"), ADMIN, allow_sqlite=True)
    engine, _ = connect(settings.database_url)
    migrate(engine)
    with engine.connect() as c:
        assert not compare_metadata(MigrationContext.configure(c, opts={"version_table": "server_alembic_version"}), Base.metadata)
    engine.dispose()
    model = FakeModel()
    app = create_app(settings, model)
    with TestClient(app) as client:
        yield client, model, app.state.licensing


def create(client, **overrides):
    response = client.post("/admin/licenses", headers=HEADERS, json={"days": 30, "daily_limit": 5, **overrides})
    assert response.status_code == 200, response.text
    return response.json()


def activate(client, key, device="d" * 32):
    return client.post("/v1/activate", json={"key": key, "device_id": device})


def auth(client, key):
    response = activate(client, key)
    assert response.status_code == 200, response.text
    return {"Authorization": "Bearer " + response.json()["token"]}


def payload(**context):
    return {"request_id": str(uuid4()), "context": {"history": [{"role": "incoming", "text": "Привет"}], **context}}


def test_lifecycle_and_secret_hashing(hosted):
    client, model, service = hosted
    item = create(client)
    assert client.get("/admin/licenses/" + item["id"], headers=HEADERS).json()["expires_at"] is None
    headers = auth(client, item["key"])
    with service.sessions() as s:
        row = s.get(License, item["id"])
        assert item["key"] not in row.key_hash
        assert row.expires_at > row.created_at
        assert s.scalar(select(Device)).token_hash != headers["Authorization"][7:]
    assert activate(client, item["key"], "e" * 32).status_code == 403
    assert client.post("/v1/generate", headers=headers, json=payload()).status_code == 200
    assert client.get("/v1/license", headers=headers).json()["used_today"] == 1
    assert client.post(f"/admin/licenses/{item['id']}/block", headers=HEADERS).status_code == 200
    assert client.post("/v1/generate", headers=headers, json=payload()).json()["detail"] == "license_blocked"
    client.post(f"/admin/licenses/{item['id']}/unblock", headers=HEADERS)
    client.post(f"/admin/licenses/{item['id']}/reset", headers=HEADERS)
    assert client.get("/v1/license", headers=headers).status_code == 401
    assert activate(client, item["key"], "e" * 32).status_code == 200


def test_idempotency_quota_and_context_conflict(hosted):
    client, model, _ = hosted
    headers = auth(client, create(client, daily_limit=1)["key"])
    body = payload()
    first = client.post("/v1/generate", headers=headers, json=body)
    second = client.post("/v1/generate", headers=headers, json=body)
    assert first.status_code == second.status_code == 200
    assert first.json() == second.json() and len(model.calls) == 1
    body["context"]["history"][0]["text"] = "other"
    assert client.post("/v1/generate", headers=headers, json=body).status_code == 409
    assert client.post("/v1/generate", headers=headers, json=payload()).json()["detail"] == "daily_limit"


def test_failure_refunds_quota_and_does_not_echo_prompt(hosted):
    client, model, _ = hosted
    headers = auth(client, create(client, daily_limit=1)["key"])
    model.fail = True
    body = payload()
    response = client.post("/v1/generate", headers=headers, json=body)
    assert response.status_code == 502 and "private" not in response.text
    assert client.get("/v1/license", headers=headers).json()["used_today"] == 0
    model.fail = False
    assert client.post("/v1/generate", headers=headers, json=body).status_code == 409
    assert client.post("/v1/generate", headers=headers, json=payload()).status_code == 200


def test_expiry_extension_and_rotation(hosted):
    client, _, service = hosted
    item = create(client)
    headers = auth(client, item["key"])
    new_headers = auth(client, item["key"])
    assert client.get("/v1/license", headers=headers).status_code == 401
    with service.sessions.begin() as s:
        s.get(License, item["id"]).expires_at = 1
    assert client.get("/v1/license", headers=new_headers).json()["detail"] == "license_expired"
    client.post(f"/admin/licenses/{item['id']}/extend", headers=HEADERS, json={"days": 2})
    assert client.get("/v1/license", headers=new_headers).status_code == 200


def test_revocation_during_generation(hosted):
    client, model, service = hosted
    item = create(client)
    headers = auth(client, item["key"])
    model.hook = lambda: service.admin_change(item["id"], "block", "test")
    assert client.post("/v1/generate", headers=headers, json=payload()).status_code == 403


def test_authorization_limits_and_safe_validation(hosted):
    client, model, _ = hosted
    assert client.post("/admin/licenses", json={}).status_code == 401
    assert client.post("/admin/licenses", headers={"Authorization": "Bearer " + "x" * 48}, json={}).status_code == 403
    assert client.post("/v1/generate", json=payload()).status_code == 401
    response = client.post("/v1/activate", json={"key": "super-secret", "device_id": "bad"})
    assert response.status_code == 422 and "super-secret" not in response.text
    assert client.post("/v1/activate", content=b"x" * 65537).status_code == 413
    assert client.post("/v1/activate", json={"key": "x" * 40, "device_id": "d" * 32}).status_code == 401
    for _ in range(9):
        client.post("/v1/activate", json={"key": "x" * 40, "device_id": "d" * 32})
    assert client.post("/v1/activate", json={"key": "x" * 40, "device_id": "d" * 32}).status_code == 429
    assert not model.calls


def test_actual_client_context_and_tenant_isolation(hosted):
    client, model, _ = hosted
    a = auth(client, create(client)["key"])
    b = auth(client, create(client)["key"])
    body = payload(account_profile={"style": "Кратко", "mode": "bump"}, knowledge="",
                   contact_memory="Джаз", style_examples=[{"context": "Привет", "reply": "Рада!", "note": ""}],
                   rejected_replies=["Старый ответ"], catalog=[])
    assert client.post("/v1/generate", headers=a, json=body).status_code == 200
    assert client.post("/v1/generate", headers=b, json=body).status_code == 200
    assert len(model.calls) == 2
    assert model.calls[0]["contact_memory"] == "Джаз"


def test_pending_and_dead_worker_recovery(hosted):
    client, _, service = hosted
    headers = auth(client, create(client)["key"])
    token = headers["Authorization"][7:]
    job_id, _ = service.reserve(token, str(uuid4()), "hash", 180)
    assert client.post("/v1/generate", headers=headers, json=payload()).json()["detail"] == "generation_in_progress"
    with service.sessions.begin() as s:
        s.get(Job, job_id).lease_until = 1
    assert client.post("/v1/generate", headers=headers, json=payload()).status_code == 200


def test_server_settings_fail_closed():
    with pytest.raises(ValueError):
        Settings("sqlite:///production.db", ADMIN).validate()
    with pytest.raises(ValueError):
        Settings("postgresql+psycopg://user@localhost/db", "weak").validate()


def test_hosted_inspection_has_same_access_and_quota_controls(hosted):
    client, model, _ = hosted
    headers = auth(client, create(client, daily_limit=1)["key"])
    body = {"request_id": str(uuid4()), "screen": json.dumps({"elements": [
        [1, "Text", "Анна", False], [2, "Text", "Привет!", False],
        [3, "Edit", "Сообщение", True], [4, "Button", "Отправить", False]]}, ensure_ascii=False)}
    assert client.post("/v1/inspect", json=body).status_code == 401
    first = client.post("/v1/inspect", headers=headers, json=body)
    assert first.status_code == 200 and first.json()["composer"] == 3
    assert client.post("/v1/inspect", headers=headers, json=body).json() == first.json()
    assert len(model.calls) == 1
    assert client.post("/v1/generate", headers=headers, json=payload()).json()["detail"] == "daily_limit"
    bad = {**body, "screen": json.dumps({"elements": [[1, "Text", "x", False], [1, "Edit", "bad", True]]})}
    assert client.post("/v1/inspect", headers=headers, json=bad).status_code == 422
