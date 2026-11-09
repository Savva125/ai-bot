import json
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from sqlalchemy import select
from PySide6.QtWidgets import QApplication
from PySide6.QtGui import QFontDatabase, QFont

from creator_desk.db import connect
from creator_desk.models import Base, Account, Chat, Contact, Draft, Feedback
from creator_desk.personalization import Personalization, PersonalizationError, DEFAULT_STYLE
from creator_desk.personalization_ui import PersonalizationDialog


@pytest.fixture
def store(tmp_path):
    engine, sessions = connect("sqlite:///" + str(tmp_path / "learning.db"))
    Base.metadata.create_all(engine)
    ids = []
    with sessions.begin() as s:
        for i in range(2):
            account = Account(external_id=f"account-{i}", name=f"Account {i}")
            s.add(account); s.flush()
            contact = Contact(account_id=account.id, external_id="same-name", name="Анна")
            s.add(contact); s.flush()
            chat = Chat(account_id=account.id, contact_id=contact.id, external_id="chat")
            s.add(chat); s.flush(); ids.append(chat.id)
    yield Personalization(sessions, tmp_path / "export"), sessions, ids
    engine.dispose()


def save(repo, chat, **changes):
    data = {"style": DEFAULT_STYLE, "chat_id": chat, "memory": ""}
    data.update(changes)
    return repo.save(data)


def test_style_and_contact_memory_survive_repository_restart_without_cross_account_leaks(store):
    repo, sessions, chats = store
    save(repo, chats[0], style="Пиши дружелюбно и без шаблонов", memory="Любит джаз")
    restarted = Personalization(sessions)
    assert restarted.load(chats[0])["memory"] == "Любит джаз"
    assert restarted.load(chats[1])["memory"] == ""
    assert restarted.load()["style"] == "Пиши дружелюбно и без шаблонов"


def test_examples_are_retrieved_by_topic_and_isolated_by_account(store):
    repo, _, chats = store
    for text in ("Фотографии", "Работа", "Путешествия", "Музыка джаз", "Прогулка"):
        save(repo, chats[0], example={"context": text, "reply": "Живой ответ про " + text})
    context = repo.generation_context(chats[0], "Ты слушаешь джаз?")
    assert len(context["examples"]) == 3
    assert context["examples"][0]["context"] == "Музыка джаз"
    assert repo.generation_context(chats[1], "джаз")["examples"] == []


def test_correction_preserves_generated_draft_and_records_feedback(store):
    repo, sessions, chats = store
    with sessions.begin() as s:
        draft = Draft(chat_id=chats[0], revision="r", config_hash="c", original="Сухой ответ",
                      text="Сухой ответ", provider="test", status="needs_review")
        s.add(draft); s.flush(); draft_id = draft.id
    save(repo, chats[0], example={"draft_id": draft_id, "context": "Вымотался на работе",
                               "reply": "Уже удалось выдохнуть?", "note": "Конкретнее и теплее"})
    with sessions() as s:
        assert s.get(Draft, draft_id).text == "Сухой ответ"
        assert s.scalar(select(Feedback)).corrected == "Уже удалось выдохнуть?"
    assert repo.load(chats[0])["examples"][0]["proposed"] == "Сухой ответ"
    save(repo, chats[0], example={"draft_id": draft_id, "context": "Вымотался на работе",
                               "reply": "Получилось отдохнуть после работы?"})
    assert len(repo.load(chats[0])["examples"]) == 1
    assert repo.load(chats[0])["examples"][0]["reply"] == "Получилось отдохнуть после работы?"


def test_foreign_example_update_rolls_back_other_settings(store):
    repo, _, chats = store
    save(repo, chats[0], example={"context": "Hello", "reply": "Hi there"})
    identity = repo.load(chats[0])["examples"][0]["id"]
    with pytest.raises(PersonalizationError):
        save(repo, chats[1], style="Should not commit", memory="Wrong", example={"id": identity, "context": "x", "reply": "y"})
    assert repo.load()["style"] == DEFAULT_STYLE
    assert repo.load(chats[1])["memory"] == ""


def test_saved_examples_can_be_edited_deleted_and_exported(store):
    repo, _, chats = store
    save(repo, chats[0], example={"context": "Привет", "reply": "Привет! Как проходит вечер?"})
    identity = repo.load(chats[0])["examples"][0]["id"]
    save(repo, chats[0], example={"id": identity, "context": "Привет", "reply": "Рада тебя видеть 🙂"})
    exported = repo.export(chats[0])
    from pathlib import Path
    rows = [json.loads(line) for line in Path(exported["path"]).read_text(encoding="utf-8").splitlines()]
    assert exported["count"] == 1 and rows[0]["messages"][-1]["content"] == "Рада тебя видеть 🙂"
    save(repo, chats[0], delete_ids=[identity])
    assert repo.load(chats[0])["examples"] == []


def test_invalid_inputs_do_not_commit_partial_style(store):
    repo, _, chats = store
    with pytest.raises(PersonalizationError):
        save(repo, chats[0], style="New", memory="x" * 2001)
    with pytest.raises(PersonalizationError):
        save(repo, None, example={"context": "x", "reply": "y"})
    assert repo.load()["style"] == DEFAULT_STYLE


def test_rejected_reply_is_negative_guidance_for_this_chat_only(store):
    repo, sessions, chats = store
    with sessions.begin() as s:
        draft = Draft(chat_id=chats[0], revision="r", config_hash="c", text="Unwanted reply", original="Unwanted reply",
                      provider="test", status="discarded")
        s.add(draft); s.flush()
        s.add(Feedback(draft_id=draft.id, rating="rejected", proposed=draft.text, corrected="", explanation="Deleted by operator"))
    assert repo.generation_context(chats[0], "hello")["rejected_replies"] == ["Unwanted reply"]
    assert repo.generation_context(chats[1], "hello")["rejected_replies"] == []


def test_dialog_correction_payload_is_bound_to_displayed_chat(store, tmp_path):
    repo, _, chats = store
    app = QApplication.instance() or QApplication([])
    font_id = QFontDatabase.addApplicationFont("C:/Windows/Fonts/segoeui.ttf")
    families = QFontDatabase.applicationFontFamilies(font_id)
    if families:
        app.setFont(QFont(families[0], 10))
    data = repo.load(chats[0])
    data["latest_draft"] = {"id": "draft-1", "context": "Как прошёл день?", "reply": "Нормально"}
    dialog = PersonalizationDialog(data)
    from creator_desk.overlay import STYLE
    dialog.setStyleSheet(STYLE)
    payloads = []
    dialog.save_requested.connect(payloads.append)
    dialog.show()
    dialog.tabs.setCurrentIndex(1)
    dialog.correct_latest()
    dialog.reply.setPlainText("День был насыщенный, теперь отдыхаю. А твой?")
    app.processEvents()
    dialog.grab().save(str(tmp_path / "personalization.png"))
    dialog.save()
    assert payloads[0]["chat_id"] == chats[0]
    assert payloads[0]["example"]["draft_id"] == "draft-1"
    dialog.set_busy(False)
    dialog.close()
