from copy import deepcopy
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from sqlalchemy import select
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QListWidget

from creator_desk.screen_agent import ScreenAssistant, Frame, ChatView, ScreenError, ScreenInterpreter, compact_screen_context, NativeScreen, screen_revision
from creator_desk.models import Base, Account, Contact, Message, Draft
from creator_desk.db import connect
from creator_desk.providers import Result
from creator_desk.overlay import OverlayWindow


class FakeScreen:
    def __init__(self):
        self.frame = Frame([
            {"id": 0, "kind": "Text", "text": "creator123", "editable": False},
            {"id": 1, "kind": "Text", "text": "subscriber456", "editable": False},
            {"id": 2, "kind": "Text", "text": "Привет, как дела?", "editable": False},
            {"id": 3, "kind": "Edit", "text": "Сообщение", "editable": True, "value": ""},
            {"id": 4, "kind": "Button", "text": "Отправить", "editable": False},
        ], "revision-1", "https://chat.example")
        self.insertions, self.sends = [], []

    def choose(self, handle):
        self.selected = handle

    def capture(self):
        return deepcopy(self.frame)

    def bind_view(self, frame, view):
        return {"composer": object(), "send": object()}

    def insert(self, frame, binding, text, stop):
        if stop.is_set() or frame.revision != self.frame.revision:
            raise ScreenError("context_changed")
        self.insertions.append(text)
        self.frame.nodes[3]["value"] = text

    def send(self, frame, binding, text, stop):
        if stop.is_set() or frame.revision != self.frame.revision:
            raise ScreenError("context_changed")
        self.sends.append(text)


class InspectionProvider:
    def __init__(self, callback):
        self.callback = callback

    def inspect_screen(self, compact):
        return self.callback(compact)


class FakeInterpreter:
    def inspect(self, frame):
        return ChatView(frame.nodes[0]["text"], frame.nodes[1]["text"], [frame.nodes[2]["text"]], True, 3, 4, frame.revision)


class FakeProvider:
    name = "unit-test-model"
    def __init__(self):
        self.calls = 0
        self.callback = lambda: None

    def generate(self, context):
        self.calls += 1
        self.last_context = context
        self.callback()
        return Result("Привет! Какие планы на день?")


@pytest.fixture
def assistant(tmp_path):
    engine, sessions = connect("sqlite:///" + str(tmp_path / "screen.db"))
    Base.metadata.create_all(engine)
    native, provider = FakeScreen(), FakeProvider()
    instance = ScreenAssistant(sessions, provider, native, FakeInterpreter())
    instance.choose(123)
    yield instance
    engine.dispose()


def test_detection_immediately_saves_and_inserts_without_sending(assistant):
    state = assistant.step()
    assert state["ready"]
    assert len(assistant.native.insertions) == 1
    assert not assistant.native.sends
    with assistant.sessions() as session:
        assert session.scalar(select(Message)).text == "Привет, как дела?"
        assert session.scalar(select(Draft)).status == "screen_inserted"
    assistant.step()
    assert assistant.provider.calls == 1


def test_user_text_is_not_overwritten(assistant):
    assistant.native.frame.nodes[3]["value"] = "Уже пишу сам"
    assistant.step()
    assert not assistant.native.insertions
    assert assistant.provider.calls == 0


def test_new_message_during_generation_cancels_input(assistant):
    assistant.provider.callback = lambda: setattr(assistant.native.frame, "revision", "new-message")
    state = assistant.step()
    assert "обновляю ответ" in state["status"]
    assert not assistant.native.insertions
    with assistant.sessions() as session:
        assert session.scalar(select(Draft)).status == "superseded"
    assistant.provider.callback = lambda: None
    assistant.native.frame.nodes[2]["text"] = "Новое сообщение"
    assistant.last_attempt = 0
    assistant.step()
    assert len(assistant.native.insertions) == 1 and not assistant.stop.is_set()


def test_stop_during_generation_cancels_input(assistant):
    assistant.provider.callback = assistant.stop.set
    with pytest.raises(ScreenError):
        assistant.step()
    assert not assistant.native.insertions


def test_send_requires_separate_confirmation_and_never_repeats(assistant):
    assistant.step()
    assert not assistant.native.sends
    assistant.confirm_send()
    assert len(assistant.native.sends) == 1
    with pytest.raises(ScreenError):
        assistant.confirm_send()


def test_changed_screen_blocks_confirmed_send(assistant):
    assistant.step()
    assistant.native.frame.revision = "other-chat"
    with pytest.raises(ScreenError):
        assistant.confirm_send()
    assert not assistant.native.sends
    assert assistant.pending is None


def test_capture_isolated_across_sites_and_minor_blocks(assistant):
    assistant.step()
    assistant.native.frame.source = "https://different.example"
    assistant.native.frame.revision = "other-site"
    assistant.native.frame.nodes[2]["text"] = "Мне 16 лет"
    assistant.native.frame.nodes[3]["value"] = ""
    with pytest.raises(ScreenError, match="несовершеннолетия"):
        assistant.step()
    with assistant.sessions() as session:
        assert len(session.scalars(select(Account)).all()) == 2
        assert len(session.scalars(select(Contact)).all()) == 2


def test_recovery_does_not_repeat_window_input(assistant):
    assistant.step()
    assistant.recover()
    with assistant.sessions() as session:
        assert session.scalar(select(Draft)).status == "needs_review"


def test_overlay_is_topmost_and_has_no_chat_or_sections(assistant, tmp_path):
    app = QApplication.instance() or QApplication([])
    window = OverlayWindow(assistant, "SQLite test")
    window.show()
    try:
        assert window.windowFlags() & Qt.WindowType.WindowStaysOnTopHint
        assert not window.findChildren(QListWidget)
        assert window.width() == 320
        from PySide6.QtWidgets import QPushButton, QComboBox, QCheckBox
        assert [b.text() for b in window.findChildren(QPushButton)] == ["Начать работу"]
        assert not window.findChildren(QComboBox) and not window.findChildren(QCheckBox)
        app.processEvents()
        until = __import__('time').monotonic() + 5
        while window.jobs and __import__('time').monotonic() < until:
            app.processEvents()
            __import__('time').sleep(.01)
        window.grab().save(str(tmp_path / 'overlay.png'))
    finally:
        window.timer.stop()
        assistant.stop.set()
        window.close()


def test_window_picker_selects_target_and_starts_autonomous_work(assistant):
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QDialog, QPushButton
    from time import monotonic
    app = QApplication.instance() or QApplication([])
    window = OverlayWindow(assistant, "SQLite test")
    window.show()
    assistant.native.windows = lambda: [{"handle": 456, "title": "Omni — тестовое окно", "app": "chrome.exe"}]
    observed = []
    def choose_in_dialog():
        dialog = app.activeModalWidget()
        if not isinstance(dialog, QDialog):
            QTimer.singleShot(10, choose_in_dialog)
            return
        choices = dialog.findChild(QListWidget)
        observed.append(choices.count())
        choices.setCurrentRow(0)
        next(b for b in dialog.findChildren(QPushButton) if b.text() == "Работать здесь").click()
    QTimer.singleShot(10, choose_in_dialog)
    try:
        window.start_button.click()
        deadline = monotonic() + 12
        while (not window.connected or not assistant.native.sends) and monotonic() < deadline:
            app.processEvents()
            __import__('time').sleep(.01)
        assert observed == [1]
        assert window.connected
        assert assistant.native.selected == 456
        assert "Omni — тестовое окно" in window.windowTitle()
        assert window.running and window.start_button.text() == "Остановить"
        assert assistant.auto_send and assistant.automatic_followup and assistant.followup_delay == 300
        assert len(assistant.native.sends) == 1
        window.start_button.click()
        assert not window.running and assistant.stop.is_set()
    finally:
        window.timer.stop()
        assistant.stop.set()
        deadline = monotonic() + 3
        while window.jobs and monotonic() < deadline:
            app.processEvents()
            __import__('time').sleep(.01)
        window.close()


@pytest.mark.parametrize("change", ["wrong_index", "search_field", "send_is_edit"])
def test_model_cannot_choose_unknown_or_sensitive_controls(change):
    frame = FakeScreen().frame
    raw = {"is_chat": True, "account": 0, "contact": 1, "history": [2], "incoming": True, "composer": 3, "send": 4}
    if change == "wrong_index":
        raw["composer"] = 999
    elif change == "search_field":
        frame.nodes[3]["text"] = "Search"
    else:
        raw["send"] = 3
    def inspect(compact):
        return raw
    with pytest.raises(ScreenError):
        ScreenInterpreter(InspectionProvider(inspect)).inspect(frame)


def large_chat_frame():
    nodes = []
    for index in range(900):
        nodes.append({"id": index, "kind": "Text", "text": "Боковая заметка " + "нейтральный текст " * 30,
                      "editable": False, "rect": [1650, 120, 1850, 700]})
    nodes += [
        {"id": 900, "kind": "Text", "text": "Account: @owner", "editable": False, "rect": [650, 80, 820, 105]},
        {"id": 901, "kind": "Text", "text": "Chat with @reader", "editable": False, "rect": [450, 80, 640, 105]},
    ]
    for index in range(30):
        nodes.append({"id": 902 + index, "kind": "Text", "text": "@reader: " + "Сообщение о путешествии. " * 30,
                      "editable": False, "rect": [500, 180 + index * 20, 1300, 200 + index * 20]})
    nodes += [
        {"id": 932, "kind": "Edit", "text": "Write a message", "editable": True, "value": "", "rect": [450, 880, 1580, 920]},
        {"id": 933, "kind": "Button", "text": "Send", "editable": False, "rect": [1450, 940, 1580, 970]},
    ]
    return Frame(nodes, "large-screen", "https://test.example")


def test_large_screen_is_bounded_and_preserves_chat_and_native_ids():
    import json
    frame = large_chat_frame()
    supplied, payload = compact_screen_context(frame)
    ids = {node["id"] for node in supplied}
    assert len(payload) <= 8500
    assert {900, 901, 931, 932, 933} <= ids
    assert not ids.intersection(range(900))
    assert all(row[0] in ids for row in json.loads(payload)["elements"])
    assert frame.nodes[902]["text"].endswith("путешествии. " )  # Full text remains unmodified for storage.


def test_large_screen_reaches_model_instead_of_size_error():
    seen = []
    def inspect(compact):
        content = compact
        assert len(content) <= 8500
        seen.append(content)
        raw = {"is_chat": True, "account": 900, "contact": 901, "history": [930, 931], "incoming": True, "composer": 932, "send": 933}
        return raw
    view = ScreenInterpreter(InspectionProvider(inspect)).inspect(large_chat_frame())
    assert seen and view.composer == 932
    assert len(view.history) == 2


def test_model_cannot_reference_omitted_sidebar_element():
    def inspect(compact):
        raw = {"is_chat": True, "account": 0, "contact": 901, "history": [931], "incoming": True, "composer": 932, "send": 933}
        return raw
    with pytest.raises(ScreenError):
        ScreenInterpreter(InspectionProvider(inspect)).inspect(large_chat_frame())


def test_native_capture_does_not_stop_at_old_450_element_limit(monkeypatch):
    import ctypes
    from types import SimpleNamespace as NS
    class Node:
        def __init__(self, index):
            self.element_info = NS(visible=True, control_type="Text", name=f"Message {index}",
                                   element=NS(CurrentIsPassword=False), runtime_id=(index,),
                                   rectangle=NS(left=400, top=100, right=1000, bottom=120))
        def parent(self):
            return NS(element_info=NS(control_type="Group"))
    class Root:
        def descendants(self): return [Node(i) for i in range(600)]
        def window_text(self): return "Test window"
    native = NativeScreen()
    native.root, native.handle, native.pid = Root(), 123, 456
    native._valid_window = lambda: None
    native.process = lambda pid: ("test.exe", 1)
    foreground = lambda: 123
    thread = lambda handle, pid: 1
    monkeypatch.setattr(ctypes, "WinDLL", lambda *args, **kwargs: NS(GetForegroundWindow=foreground, GetWindowThreadProcessId=thread), raising=False)
    assert len(native.capture().nodes) == 600


def test_sidebar_change_does_not_invalidate_chat_but_new_message_does():
    frame = large_chat_frame()
    def revision():
        stable = {n["id"]: ((n["id"],), n["text"]) for n in frame.nodes if n["kind"] == "Text"}
        return screen_revision("Chat", frame.nodes, stable)
    original = revision()
    frame.nodes[0]["text"] = "Обновление другой боковой карточки"
    assert revision() == original
    frame.nodes[931]["text"] = "@reader: Новое входящее сообщение"
    assert revision() != original


def test_disabled_composer_explains_platform_shift_without_model_call(monkeypatch):
    import httpx
    frame = large_chat_frame()
    frame.nodes[932]["text"] = "Начните смену для отправки сообщений…"
    frame.nodes[932]["editable"] = False
    def unexpected(*args, **kwargs):
        raise AssertionError("A locked composer does not need model inference")
    monkeypatch.setattr(httpx.Client, "post", unexpected)
    with pytest.raises(ScreenError, match="начните смену"):
        ScreenInterpreter().inspect(frame)


@pytest.mark.parametrize("label,send,message", [
    ("Сообщение…", "Отправить", "Привет! Какие планы на выходные?"),
    ("Message", "Send", "Hello! How was your day?"),
    ("Escribe un mensaje", "Enviar", "¡Hola! ¿Cómo estás?"),
    ("Nachricht", "Senden", "Hallo! Wie geht es dir?"),
    ("رسالة", "إرسال", "مرحبا كيف حالك؟"),
    ("消息", "发送", "你好，今天怎么样？"),
])
def test_personal_chat_without_visible_owner_preserves_languages(label, send, message):
    frame = FakeScreen().frame
    frame.session_key = "selected-window-one"
    frame.nodes[2]["text"] = message
    frame.nodes[3]["text"] = label
    frame.nodes[4]["text"] = send
    def inspect(compact):
        assert message in compact
        raw = {"is_chat": True, "account": None, "contact": 1, "history": [2],
               "incoming": True, "composer": 3, "send": 4}
        return raw
    first = ScreenInterpreter(InspectionProvider(inspect)).inspect(frame)
    assert first.history == [message] and first.send == 4
    assert first.account.startswith("window:") and first.account != first.contact
    frame.session_key = "selected-window-two"
    assert ScreenInterpreter(InspectionProvider(inspect)).inspect(frame).account != first.account


@pytest.mark.parametrize("field,value", [("contact", None), ("contact", 999), ("composer", "3")])
def test_missing_owner_does_not_allow_invalid_contact_or_composer(field, value):
    raw = {"is_chat": True, "account": None, "contact": 1, "history": [2],
           "incoming": True, "composer": 3, "send": 4, field: value}
    def inspect(compact):
        return raw
    with pytest.raises(ScreenError):
        ScreenInterpreter(InspectionProvider(inspect)).inspect(FakeScreen().frame)


@pytest.mark.parametrize("label", ["Сообщение", "Message", "Escribe un mensaje", "Nachricht", "رسالة", "消息"])
def test_multilingual_composer_keeps_large_chat_region(label):
    frame = large_chat_frame()
    frame.nodes[932]["text"] = label
    supplied, _ = compact_screen_context(frame)
    ids = {node["id"] for node in supplied}
    assert {900, 901, 931, 932, 933} <= ids
    assert not ids.intersection(range(900))


def test_telegram_message_items_reach_model_and_history():
    import json
    frame = FakeScreen().frame
    frame.nodes[2].update(kind="ListItem", message_item=True,
                          text="Просмотрено Меня Привет! Отправлено в 10:33")
    def inspect(compact):
        assert [2, "Message", frame.nodes[2]["text"], False] in json.loads(compact)["elements"]
        raw = {"is_chat": True, "account": None, "contact": 1, "history": [2],
               "incoming": False, "composer": 3, "send": 4}
        return raw
    view = ScreenInterpreter(InspectionProvider(inspect)).inspect(frame)
    assert view.history == [frame.nodes[2]["text"]] and not view.incoming
    stable = {2: ((123,), frame.nodes[2]["text"])}
    original = screen_revision("Chat", frame.nodes, stable)
    stable[2] = ((123,), "Новый текст")
    assert original != screen_revision("Chat", frame.nodes, stable)


def test_telegram_history_is_distinguished_from_chat_list():
    from types import SimpleNamespace as NS
    from creator_desk.screen_agent import native_message_item, history_node
    assert native_message_item(NS(control_type="ListItem", automation_id="class HistoryWidget.class HistoryInner"))
    assert not native_message_item(NS(control_type="ListItem", automation_id="class Dialogs::InnerWidget"))
    assert not history_node({"kind": "ListItem", "text": "Preview", "editable": False})


def test_native_telegram_capture_keeps_visible_history_and_title(monkeypatch):
    import ctypes
    from types import SimpleNamespace as NS
    def rect(top, bottom):
        return NS(left=400, top=top, right=1000, bottom=bottom)
    class Item:
        def __init__(self, top, text):
            self.element_info = NS(visible=True, control_type="ListItem", name=text,
                automation_id="class HistoryWidget.class HistoryInner", class_name="",
                element=NS(CurrentIsPassword=False), runtime_id=(top,), rectangle=rect(top, top + 25))
        def parent(self):
            return NS(element_info=NS(control_type="List", rectangle=rect(100, 600)))
    class Root:
        def descendants(self, **kwargs):
            return [Item(-300, "Cached old message"), Item(500, "Анна: Привет!")] if kwargs.get("control_type") == "ListItem" else []
        def window_text(self): return "\u200eАнна – (180)"
    native = NativeScreen()
    native.root, native.handle, native.pid = Root(), 123, 456
    native._valid_window = lambda: None
    native.process = lambda pid: ("Telegram.exe", 1)
    monkeypatch.setattr(ctypes, "WinDLL", lambda *args, **kwargs: NS(
        GetForegroundWindow=lambda: 123, GetWindowThreadProcessId=lambda *args: 1), raising=False)
    frame = native.capture()
    assert len(frame.nodes) == 2
    assert frame.nodes[0]["message_item"] and frame.nodes[0]["text"] == "Анна: Привет!"
    assert frame.nodes[1]["text"] == "Анна" and frame.nodes[1]["role"] == "contact_heading"


def test_telegram_new_incoming_triggers_reply_without_role_guessing(assistant, monkeypatch):
    import httpx
    def no_model_inspection(*args, **kwargs):
        raise AssertionError("Structured Telegram metadata needs no role classification")
    monkeypatch.setattr(httpx.Client, "post", no_model_inspection)
    frame = assistant.native.frame
    frame.nodes[1]["role"] = "contact_heading"
    frame.nodes[2].update(kind="ListItem", message_item=True, direction="outgoing", message_text="Привет!")
    assistant.interpreter = ScreenInterpreter()
    first = assistant.step()
    assert "исходящее" in first["status"] and assistant.provider.calls == 0
    assert assistant.step()["status"] == first["status"]
    frame.nodes[2].update(text="Анна: Как дела? Получено в 11:01", direction="incoming", message_text="Как дела?")
    frame.revision = "new-incoming"
    assistant.step()
    assert assistant.provider.calls == 1
    assert len(assistant.native.insertions) == 1 and not assistant.native.sends
    assistant.step()
    assert assistant.provider.calls == 1


def test_telegram_delivery_is_not_inferred_from_message_words():
    from types import SimpleNamespace as NS
    from creator_desk.screen_agent import message_metadata
    def field(label, value):
        return NS(element_info=NS(name=label), iface_value=NS(CurrentValue=value))
    node = NS(children=lambda: [field("Сообщение", "Меня Отправлено Sent"), field("Получение", "Получено")])
    assert message_metadata(node) == {"direction": "incoming", "message_text": "Меня Отправлено Sent"}
    node = NS(children=lambda: [field("Сообщение", "Получено Received"), field("Получение", "Отправлено")])
    assert message_metadata(node)["direction"] == "outgoing"


def test_telegram_delivery_supports_legacy_accessibility_values():
    from types import SimpleNamespace as NS
    from creator_desk.screen_agent import message_metadata
    child = NS(element_info=NS(name="Получение"), legacy_properties=lambda: {"Value": "Получено"})
    assert message_metadata(NS(children=lambda: [child])) == {"direction": "incoming"}


def test_telegram_duplicate_editors_choose_observed_inner_surface():
    from creator_desk.screen_agent import structured_chat_view
    frame = FakeScreen().frame
    frame.nodes[1]["role"] = "contact_heading"
    frame.nodes[2].update(kind="ListItem", message_item=True, direction="incoming", message_text="Как дела?")
    frame.nodes[3]["automation_id"] = "class HistoryWidget.class Ui::InputField"
    frame.nodes.append({**frame.nodes[3], "id": 5,
                        "automation_id": "class HistoryWidget.class Ui::InputField.class Ui::InputField::Inner"})
    view = structured_chat_view(frame)
    assert view and view.incoming and view.composer == 5 and view.history[-1] == "Как дела?"


def test_telegram_uses_scroll_viewport_not_entire_history_content(monkeypatch):
    import ctypes
    from types import SimpleNamespace as NS
    def rect(top, bottom): return NS(left=400, top=top, right=1000, bottom=bottom)
    class Item:
        def __init__(self, top, direction):
            self.element_info = NS(visible=True, control_type="ListItem", name="Neutral message",
                automation_id="class HistoryWidget.class HistoryInner", class_name="",
                element=NS(CurrentIsPassword=False), runtime_id=(top,), rectangle=rect(top, top + 25))
            self.direction = direction
        def children(self):
            return [NS(element_info=NS(name="Получение"), iface_value=NS(CurrentValue=self.direction))]
        def parent(self):
            return NS(element_info=NS(control_type="List", rectangle=rect(-22000, 600)),
                      parent=lambda: NS(element_info=NS(automation_id="class HistoryWidget.class Ui::ElasticScroll", rectangle=rect(100,600))))
    class Root:
        def descendants(self, **kwargs):
            return [Item(-300, "Отправлено"), Item(500, "Получено")] if kwargs.get("control_type") == "ListItem" else []
        def window_text(self): return "\u200eАнна – (180)"
    native = NativeScreen()
    native.root, native.handle, native.pid = Root(), 123, 456
    native._valid_window = lambda: None
    native.process = lambda pid: ("Telegram.exe", 1)
    monkeypatch.setattr(ctypes, "WinDLL", lambda *args, **kwargs: NS(
        GetForegroundWindow=lambda: 123, GetWindowThreadProcessId=lambda *args: 1), raising=False)
    frame = native.capture()
    messages = [n for n in frame.nodes if n.get("message_item")]
    assert len(messages) == 1 and messages[0]["direction"] == "incoming"


def test_inspection_mode_never_calls_model_or_inserts(assistant):
    assistant.inspect_only = True
    state = assistant.step()
    assert "Ввод отключён" in state["status"]
    assert assistant.provider.calls == 0
    assert not assistant.native.insertions and not assistant.native.sends


class OutgoingInterpreter(FakeInterpreter):
    def inspect(self, frame):
        view = super().inspect(frame)
        view.incoming = False
        return view


def test_manual_continuation_of_outgoing_chat_uses_correct_role(assistant):
    assistant.interpreter = OutgoingInterpreter()
    assistant.step()
    assert assistant.provider.calls == 0
    assistant.step(continue_dialogue=True)
    assert len(assistant.native.insertions) == 1 and not assistant.native.sends
    assert assistant.provider.last_context["history"][-1]["role"] == "outgoing"
    assert "continuation" in assistant.provider.last_context["account_profile"]["style"]
    assistant.step(continue_dialogue=True)
    assert assistant.provider.calls == 1


def test_automatic_continuation_waits_then_works_on_unchanged_screen(assistant, monkeypatch):
    import creator_desk.screen_agent as screen_agent
    clock = [1000.0]
    monkeypatch.setattr(screen_agent.time, "monotonic", lambda: clock[0])
    assistant.interpreter = OutgoingInterpreter()
    assistant.automatic_followup = True
    assistant.step()
    clock[0] += 119
    assistant.step()
    assert assistant.provider.calls == 0
    clock[0] += 2
    assistant.step()
    assert assistant.provider.calls == 1 and len(assistant.native.insertions) == 1
    clock[0] += 300
    assistant.step()
    assert assistant.provider.calls == 1


def test_followup_limit_survives_sent_text_and_restart(assistant):
    assistant.interpreter = OutgoingInterpreter()
    assistant.step(continue_dialogue=True)
    assistant.native.frame.nodes[3]["value"] = ""
    assistant.native.frame.nodes[2]["text"] = assistant.native.insertions[0]
    assistant.native.frame.revision = "followup-sent"
    restarted = ScreenAssistant(assistant.sessions, assistant.provider, assistant.native, OutgoingInterpreter())
    restarted.choose(123)
    restarted.automatic_followup = True
    restarted.followup_delay = 0
    state = restarted.step()
    assert "Повторное напоминание не нужно" in state["status"]
    assert assistant.provider.calls == 1


def test_continuation_preserves_operator_text_and_respects_pause(assistant):
    assistant.interpreter = OutgoingInterpreter()
    assistant.native.frame.nodes[3]["value"] = "Мой текст"
    assistant.step(continue_dialogue=True)
    assert assistant.provider.calls == 0
    assistant.native.frame.nodes[3]["value"] = ""
    assistant.stop.set()
    assistant.step(continue_dialogue=True)
    assert assistant.provider.calls == 0


def test_new_incoming_allows_another_continuation(assistant):
    assistant.interpreter = OutgoingInterpreter()
    assistant.step(continue_dialogue=True)
    assistant.native.frame.nodes[3]["value"] = ""
    assistant.native.frame.nodes[2]["text"] = "Новый ответ собеседника"
    assistant.native.frame.revision = "incoming-again"
    assistant.interpreter = FakeInterpreter()
    assistant.last_attempt = 0
    assistant.step()
    assistant.native.frame.nodes[3]["value"] = ""
    assistant.native.frame.nodes[2]["text"] = "Мой новый ответ"
    assistant.native.frame.revision = "outgoing-again"
    assistant.interpreter = OutgoingInterpreter()
    assistant.step(continue_dialogue=True)
    assert assistant.provider.calls == 3


def test_manual_continuation_does_not_reuse_view_after_unrecognized_screen(assistant):
    assistant.interpreter = OutgoingInterpreter()
    assistant.step()
    assistant.native.frame.revision = "unrecognized"
    assistant.interpreter = type("NoChat", (), {"inspect": lambda self, frame: None})()
    assistant.step()
    assistant.step(continue_dialogue=True)
    assert assistant.provider.calls == 0 and assistant.last_view is None


def test_focus_failure_before_input_allows_manual_continuation_retry(assistant):
    from creator_desk.screen_agent import InputNotStarted
    assistant.interpreter = OutgoingInterpreter()
    original = assistant.native.insert
    def fail_before_input(*args): raise InputNotStarted("Focus failed")
    assistant.native.insert = fail_before_input
    with pytest.raises(InputNotStarted):
        assistant.step(continue_dialogue=True)
    with assistant.sessions() as session:
        assert session.scalar(select(Draft)).status == "screen_not_inserted"
    assistant.native.insert = original
    assistant.step(continue_dialogue=True)
    assert len(assistant.native.insertions) == 1 and not assistant.native.sends


def test_focus_diagnostics_never_generate_or_write_and_run_once(assistant):
    assistant.inspect_only = assistant.inspect_focus = True
    assistant.native.diagnostics_path = None
    frame = assistant.native.frame
    frame.nodes[1]["role"] = "contact_heading"
    frame.nodes[2].update(kind="ListItem", message_item=True, direction="incoming")
    calls = []
    assistant.native._focus_composer = lambda field, stop: calls.append(field)
    state = assistant.step()
    assert "получило фокус" in state["status"]
    assert assistant.step() == state and len(calls) == 1
    assert assistant.provider.calls == 0 and not assistant.native.insertions and not assistant.native.sends


def test_generation_uses_saved_style_examples_and_contact_memory(assistant):
    frame = assistant.native.capture()
    chat_id, _, _ = assistant._save(frame, assistant.interpreter.inspect(frame))
    assistant.personalization.save({"style": "Говори тепло, без канцелярита", "chat_id": chat_id,
        "memory": "Любит прогулки", "example": {"context": "Привет, как дела?", "reply": "Рада тебя видеть!"}})
    assistant.step()
    context = assistant.provider.last_context
    assert context["account_profile"]["style"] == "Говори тепло, без канцелярита"
    assert context["contact_memory"] == "Любит прогулки"
    assert context["style_examples"][0]["reply"] == "Рада тебя видеть!"


def test_personalization_refreshes_contact_after_external_chat_switch(assistant):
    frame = assistant.native.capture()
    assistant.last_view = assistant.interpreter.inspect(frame)
    old_chat, _, _ = assistant._save(frame, assistant.last_view)
    assistant.personalization.save({"style": "Тепло", "chat_id": old_chat, "memory": "Старая заметка"})
    assistant.native.frame.nodes[1]["text"] = "Другой собеседник"
    assistant.native.frame.revision = "other-chat"
    state = assistant.personalization_state()
    assert state["contact"] == "Другой собеседник" and state["memory"] == ""
    assert state["chat_id"] != old_chat


def test_personalization_panel_saves_without_generating_or_sending(assistant):
    from PySide6.QtCore import QTimer
    from creator_desk.personalization_ui import PersonalizationDialog
    from time import monotonic
    app = QApplication.instance() or QApplication([])
    assistant.last_view = assistant.interpreter.inspect(assistant.native.capture())
    assistant._save(assistant.native.frame, assistant.last_view)
    window = OverlayWindow(assistant, "Test")
    window.show()
    window.timer.stop()
    def edit_dialog():
        dialog = app.activeModalWidget()
        if not isinstance(dialog, PersonalizationDialog):
            QTimer.singleShot(10, edit_dialog)
            return
        dialog.style.setPlainText("Тёплый разговорный стиль")
        dialog.memory.setPlainText("Любит джаз")
        dialog.context.setPlainText("Сегодня устал")
        dialog.reply.setPlainText("Уже дома отдыхаешь?")
        dialog.save()
    QTimer.singleShot(10, edit_dialog)
    try:
        window.open_personalization()
        deadline = monotonic() + 15
        while window.jobs and monotonic() < deadline:
            app.processEvents()
            __import__('time').sleep(.01)
        state = assistant.personalization.load(assistant.active_chat_id)
        assert state["style"] == "Тёплый разговорный стиль" and state["memory"] == "Любит джаз"
        assert state["examples"][0]["reply"] == "Уже дома отдыхаешь?"
        assert assistant.provider.calls == 0 and not assistant.native.insertions and not assistant.native.sends
        assert assistant.stop.is_set()
    finally:
        window.close()


def test_speaker_context_persists_and_is_used_after_viewport_change(assistant):
    from creator_desk.models import ConversationContext
    first = [{"key": "a", "role": "incoming", "text": "Давай на ты"},
             {"key": "b", "role": "outgoing", "text": "Конечно"}]
    frame = assistant.native.frame
    view = ChatView("creator123", "subscriber456", [t["text"] for t in first], False, 3, 4, frame.revision, first)
    chat_id, _, _ = assistant._save(frame, view)
    later = [first[1], {"key": "c", "role": "incoming", "text": "dfskdfsjiklo"}]
    class Interpreter:
        def inspect(self, frame):
            return ChatView("creator123", "subscriber456", [t["text"] for t in later], True, 3, 4, frame.revision, later)
    assistant.interpreter = Interpreter()
    frame.revision = "later"
    assistant.step()
    history = assistant.provider.last_context["history"]
    assert [t["role"] for t in history] == ["incoming", "outgoing", "incoming"]
    assert history[0]["text"] == "Давай на ты" and history[-1]["text"] == "dfskdfsjiklo"
    with assistant.sessions() as s:
        assert len(s.get(ConversationContext, chat_id).turns) == 3


def test_manual_continuation_can_continue_a_new_outgoing_context(assistant):
    assistant.interpreter = OutgoingInterpreter()
    assistant.step(continue_dialogue=True)
    assistant.native.frame.nodes[3]["value"] = ""
    assistant.native.frame.nodes[2]["text"] = assistant.native.insertions[0]
    assistant.native.frame.revision = "followup-sent"
    assistant.step(continue_dialogue=True)
    assert assistant.provider.calls == 2 and len(assistant.native.insertions) == 2
    assistant.step(continue_dialogue=True)
    assert assistant.provider.calls == 2


def test_new_incoming_observation_reopens_auto_followup_without_generated_reply(assistant):
    assistant.interpreter = OutgoingInterpreter()
    assistant.step(continue_dialogue=True)
    assistant.native.frame.nodes[3]["value"] = ""
    frame = assistant.native.frame
    frame.nodes[2]["text"] = "Новый ответ собеседника"
    frame.revision = "received"
    assistant._save(frame, FakeInterpreter().inspect(frame))
    frame.nodes[2]["text"] = "Мой ручной ответ"
    frame.revision = "manual-reply"
    assistant.automatic_followup = True
    assistant.followup_delay = 0
    assistant.last_attempt = 0
    assistant.step()
    assert assistant.provider.calls == 2


def test_context_change_immediately_before_write_is_retried_without_pausing(assistant):
    from creator_desk.screen_agent import ContextChanged
    def changed(*args): raise ContextChanged("New incoming")
    assistant.native.insert = changed
    state = assistant.step()
    assert "обновляю ответ" in state["status"] and not assistant.stop.is_set()
    assert not assistant.native.insertions


def test_attachment_is_not_presented_as_seen_video():
    frame = FakeScreen().frame
    frame.nodes[1]["role"] = "contact_heading"
    frame.nodes[2].update(kind="ListItem", message_item=True, direction="incoming", media_type="Видео")
    from creator_desk.screen_agent import structured_chat_view
    view = structured_chat_view(frame)
    assert view.turns[-1]["role"] == "incoming"
    assert "Содержимое не просмотрено" in view.turns[-1]["text"]


def test_handoff_reason_is_saved_and_stays_visible(assistant):
    assistant.provider.generate = lambda context: Result("", True, "Нужно уточнение у оператора")
    state = assistant.step()
    assert "Нужно уточнение у оператора" in state["status"]
    with assistant.sessions() as s:
        assert s.scalar(select(Draft)).reason == "Нужно уточнение у оператора"
    assert assistant.step()["status"] == state["status"]
    assert not assistant.native.insertions


@pytest.mark.parametrize("outgoing", [False, True])
def test_explicit_retry_after_handoff_creates_and_inserts_reply(assistant, outgoing):
    if outgoing:
        assistant.interpreter = OutgoingInterpreter()
    calls = []
    def generate(context):
        calls.append(context)
        return Result("", True, "Нужно уточнение") if len(calls) == 1 else Result("Давай поговорим спокойно.")
    assistant.provider.generate = generate
    assistant.step(continue_dialogue=True)
    assert not assistant.native.insertions
    assistant.step()  # Never turn a handoff into an automatic retry.
    assert len(calls) == 1
    state = assistant.step(continue_dialogue=True)
    assert assistant.native.insertions == ["Давай поговорим спокойно."]
    assert len(calls) == 2 and state["ready"]
    assistant.step(continue_dialogue=True)
    assert len(calls) == 2 and not assistant.native.sends


def test_legacy_empty_handoff_is_not_reported_as_prepared_after_restart(assistant):
    assistant.interpreter = OutgoingInterpreter()
    assistant.provider.generate = lambda context: Result("", True, "")
    state = assistant.step(continue_dialogue=True)
    assert "Текст ответа не создан" in state["status"]
    with assistant.sessions.begin() as s:
        s.scalar(select(Draft)).reason = ""  # Old records discarded the reason.
    restarted = ScreenAssistant(assistant.sessions, assistant.provider, assistant.native, OutgoingInterpreter())
    restarted.choose(123)
    restarted.automatic_followup = True
    restarted.followup_delay = 0
    state = restarted.step()
    assert "Текст ответа не создан" in state["status"]
    assert "уже подготовлено" not in state["status"]
    assert not assistant.native.insertions


def test_auto_send_inserts_then_sends_once_without_confirmation(assistant):
    assistant.auto_send = True
    state = assistant.step()
    assert assistant.native.sends == assistant.native.insertions
    assert len(assistant.native.sends) == 1
    assert assistant.pending is None and not state["ready"]
    assistant.step()
    assert len(assistant.native.sends) == 1


@pytest.mark.parametrize("case", ["handoff", "context"])
def test_auto_send_does_not_send_handoff_or_changed_context(assistant, case):
    assistant.auto_send = True
    if case == "handoff":
        assistant.provider.generate = lambda context: Result("", True, "Нужна проверка")
    else:
        assistant.provider.callback = lambda: setattr(assistant.native.frame, "revision", "new-incoming")
    assistant.step()
    assert not assistant.native.sends and not assistant.native.insertions


def test_single_start_stop_button_uses_autonomous_defaults(assistant):
    app = QApplication.instance() or QApplication([])
    window = OverlayWindow(assistant)
    window.timer.stop()
    try:
        window.connected = True
        window.start()
        assert assistant.auto_send and assistant.automatic_followup
        assert assistant.autonomous_dialogue and assistant.followup_delay == 300
        assert window.start_button.text() == "Остановить"
        window.start_button.click()
        assert assistant.stop.is_set() and not window.running
        assert window.start_button.text() == "Начать работу"
        assert not assistant.native.sends
    finally:
        window.close()


def test_autonomous_bump_waits_five_minutes_and_sends_only_once(assistant, monkeypatch):
    import creator_desk.screen_agent as module
    clock = [1000.0]
    monkeypatch.setattr(module.time, "monotonic", lambda: clock[0])
    assistant.interpreter = OutgoingInterpreter()
    assistant.auto_send = assistant.automatic_followup = assistant.autonomous_dialogue = True
    assistant.followup_delay = 300
    assistant.step()
    clock[0] += 299
    assistant.step()
    assert assistant.provider.calls == 0
    clock[0] += 2
    assistant.step()
    assert assistant.provider.calls == 1 and len(assistant.native.sends) == 1
    assert assistant.provider.last_context["account_profile"]["mode"] == "bump"
    assistant.native.frame.nodes[3]["value"] = ""
    assistant.native.frame.nodes[2]["text"] = assistant.native.sends[0]
    assistant.native.frame.revision = "bump-sent"
    clock[0] += 3600
    assistant.step()
    assert assistant.provider.calls == 1 and len(assistant.native.sends) == 1


def test_autonomous_wait_is_not_handoff_or_empty_message_and_survives_restart(assistant):
    assistant.interpreter = OutgoingInterpreter()
    assistant.auto_send = assistant.automatic_followup = assistant.autonomous_dialogue = True
    assistant.followup_delay = 0
    assistant.provider.generate = lambda context: Result("", False, "Conversation closed", "wait")
    state = assistant.step()
    assert "не требуется" in state["status"]
    with assistant.sessions() as s:
        assert s.scalar(select(Draft)).status == "waiting"
    assert not assistant.native.insertions and not assistant.native.sends
    assistant.step()
    restarted = ScreenAssistant(assistant.sessions, assistant.provider, assistant.native, OutgoingInterpreter())
    restarted.choose(123)
    restarted.auto_send = restarted.automatic_followup = restarted.autonomous_dialogue = True
    restarted.followup_delay = 0
    restarted.provider.generate = lambda context: pytest.fail("Wait must not trigger another generation")
    assert "не требуется" in restarted.step()["status"]


def test_waiting_for_incoming_does_not_type_empty_reply(assistant):
    assistant.auto_send = True
    assistant.provider.generate = lambda context: Result("", False, "Nothing to add", "wait")
    state = assistant.step()
    assert "Ожидаю" in state["status"]
    assert not assistant.native.insertions and not assistant.native.sends


def test_old_outgoing_message_is_due_immediately_after_start(assistant, monkeypatch):
    import creator_desk.screen_agent as module
    monkeypatch.setattr(module.time, "time", lambda: 10000.0)
    class Timed(OutgoingInterpreter):
        def inspect(self, frame):
            view = super().inspect(frame)
            view.last_message_at = 9400.0
            return view
    assistant.interpreter = Timed()
    assistant.automatic_followup = assistant.auto_send = assistant.autonomous_dialogue = True
    assistant.followup_delay = 300
    assistant.step()
    assert assistant.provider.calls == 1 and len(assistant.native.sends) == 1
    assistant.step()
    assert assistant.provider.calls == 1


def test_recent_message_waits_only_remaining_time_and_shows_countdown(assistant, monkeypatch):
    import creator_desk.screen_agent as module
    clock = [1000.0]
    monkeypatch.setattr(module.time, "time", lambda: 10000.0 + clock[0] - 1000)
    monkeypatch.setattr(module.time, "monotonic", lambda: clock[0])
    class Timed(OutgoingInterpreter):
        def inspect(self, frame):
            view = super().inspect(frame)
            view.last_message_at = 9880.0
            return view
    assistant.interpreter = Timed()
    assistant.automatic_followup = assistant.autonomous_dialogue = True
    assistant.followup_delay = 300
    state = assistant.step()
    assert "3:00" in state["status"] and assistant.provider.calls == 0
    clock[0] += 179
    assistant.step()
    assert assistant.provider.calls == 0
    clock[0] += 1
    assistant.step()
    assert assistant.provider.calls == 1


def test_source_timestamp_is_used_by_structured_view():
    from creator_desk.screen_agent import structured_chat_view
    frame = FakeScreen().frame
    frame.nodes[1]["role"] = "contact_heading"
    frame.nodes[2].update(kind="ListItem", message_item=True, direction="outgoing", message_time="04.11.2026 в 23:04")
    view = structured_chat_view(frame)
    assert view.last_message_at is not None and not view.incoming


def test_newer_source_timestamp_resets_remaining_time_without_new_body(assistant, monkeypatch):
    import creator_desk.screen_agent as module
    clock, stamp = [1000.0], [9880.0]
    monkeypatch.setattr(module.time, "time", lambda: 10000.0 + clock[0] - 1000)
    monkeypatch.setattr(module.time, "monotonic", lambda: clock[0])
    class Timed(OutgoingInterpreter):
        def inspect(self, frame):
            view = super().inspect(frame)
            view.last_message_at = stamp[0]
            return view
    assistant.interpreter = Timed()
    assistant.automatic_followup = True
    assistant.followup_delay = 300
    assert "3:00" in assistant.step()["status"]
    clock[0] += 100
    stamp[0] = 10040.0
    assistant.native.frame.revision = "time-edited"
    assert "4:00" in assistant.step()["status"]
    assert assistant.provider.calls == 0


def test_current_telegram_history_view_is_recognized_but_sidebar_is_not():
    from types import SimpleNamespace as NS
    from creator_desk.screen_agent import native_message_item
    prefix = "class MainWidget.class HistoryView::ChatWidget.class Ui::ElasticScroll.class "
    assert native_message_item(NS(control_type="ListItem", automation_id=prefix + "HistoryView::ListWidget"))
    assert not native_message_item(NS(control_type="ListItem", automation_id="class Dialogs::InnerWidget"))


def test_current_telegram_layout_produces_messages_and_composer(assistant):
    from types import SimpleNamespace as NS
    from creator_desk.screen_agent import native_message_item, structured_chat_view
    frame = assistant.native.frame
    frame.nodes[1]["role"] = "contact_heading"
    frame.nodes[2].update(kind="ListItem", message_item=native_message_item(NS(control_type="ListItem",
        automation_id="class HistoryView::ChatWidget.class Ui::ElasticScroll.class HistoryView::ListWidget")),
        direction="incoming", message_text="Привет, как дела?")
    frame.nodes[3]["automation_id"] = "class HistoryView::ChatWidget.class Ui::InputField.class Ui::InputField::Inner"
    view = structured_chat_view(frame)
    assert view and view.incoming and view.composer == 3 and view.history[-1] == "Привет, как дела?"


def test_background_read_waits_for_focus_and_resumes_unchanged_chat(assistant):
    interactive = [False]
    assistant.native.can_interact = lambda: interactive[0]
    first = assistant.step()
    assert "Диалог читается" in first["status"]
    assert assistant.provider.calls == 0 and not assistant.stop.is_set()
    assistant.step()
    assert assistant.provider.calls == 0
    interactive[0] = True
    assistant.step()
    assert assistant.provider.calls == 1 and len(assistant.native.insertions) == 1


def test_window_title_is_read_fresh_instead_of_cached_wrapper_text():
    from types import SimpleNamespace as NS
    native = NativeScreen()
    native.root = NS(element_info=NS(element=NS(CurrentName="Новый диалог")), window_text=lambda: "Старый диалог")
    assert native._title() == "Новый диалог"


def test_clearing_composer_cancels_auto_send_without_reporting_sent(assistant):
    from creator_desk.screen_agent import ComposerCleared
    assistant.auto_send = True
    def cleared(*args):
        assistant.native.frame.nodes[3]["value"] = ""
        raise ComposerCleared("Cleared")
    assistant.native.send = cleared
    state = assistant.step()
    assert "Отправка отменена" in state["status"] and not assistant.native.sends
    with assistant.sessions() as s:
        assert s.scalar(select(Draft)).status == "cancelled"
    assistant.step()
    assert assistant.provider.calls == 1


def test_unverified_invoke_is_not_reported_as_message_in_chat(assistant):
    assistant.auto_send = True
    state = assistant.step()  # Fake invoke returns no observed receipt.
    assert "ещё не подтверждено" in state["status"]
    with assistant.sessions() as s:
        assert s.scalar(select(Draft)).status == "needs_review"


def test_source_deleted_reply_is_not_regenerated_after_restart(assistant):
    from creator_desk.models import ConversationContext
    original = [{"key": "before", "role": "incoming", "text": "Question"}]
    class Interpreter:
        def inspect(self, frame):
            return ChatView("creator123", "subscriber456", ["Question"], True, 3, 4, frame.revision,
                            original, tail_visible=True)
    assistant.interpreter = Interpreter()
    view = assistant.interpreter.inspect(assistant.native.frame)
    chat_id, revision, _ = assistant._save(assistant.native.frame, view)
    with assistant.sessions.begin() as s:
        s.get(ConversationContext, chat_id).turns = original + [{"key": "reply", "role": "outgoing", "text": "Bad reply"}]
        s.add(Draft(chat_id=chat_id, revision=revision, config_hash="test", original="Bad reply", text="Bad reply",
                    provider="test", status="sent_observed", receipt="uia:reply:before:"))
    assistant.native.frame.revision = "reply-deleted"
    assert "Сверяю" in assistant.step()["status"]
    assistant.step()
    with assistant.sessions() as s:
        assert s.scalar(select(Draft)).status == "discarded"
        assert s.get(ConversationContext, chat_id).turns == original
    restarted = ScreenAssistant(assistant.sessions, assistant.provider, assistant.native, Interpreter())
    restarted.choose(123)
    assert "Не отправляю его повторно" in restarted.step()["status"]
    assert assistant.provider.calls == 0 and not assistant.native.insertions


def test_hosted_access_failure_before_input_allows_restart(assistant):
    from creator_desk.errors import AiError
    def fail():
        raise AiError("license_expired")
    assistant.provider.callback = fail
    with pytest.raises(AiError):
        assistant.step()
    assert not assistant.native.insertions and not assistant.native.sends
    with assistant.sessions() as s:
        assert s.scalar(select(Draft)).status == "screen_not_inserted"
    assistant.provider.callback = lambda: None
    assistant.choose(123)
    assistant.last_attempt = 0
    assert assistant.step()["ready"]
    assert len(assistant.native.insertions) == 1


def test_hosted_screen_interpreter_never_calls_local_model(monkeypatch):
    class Hosted:
        calls = 0
        def inspect_screen(self, compact):
            self.calls += 1
            assert len(compact) <= 8500
            return {"is_chat": True, "account": 0, "contact": 1, "history": [2],
                    "incoming": True, "composer": 3, "send": 4}
    def no_local(*args, **kwargs):
        raise AssertionError("Local AI must not be used in hosted mode")
    monkeypatch.setattr("httpx.Client.post", no_local)
    provider = Hosted()
    view = ScreenInterpreter(provider).inspect(FakeScreen().frame)
    assert view.composer == 3 and provider.calls == 1


def test_read_only_diagnostics_do_not_require_active_ai(assistant):
    from creator_desk.providers import DemoProvider
    app = QApplication.instance() or QApplication([])
    assistant.provider = DemoProvider()
    assistant.inspect_only = True
    window = OverlayWindow(assistant)
    assert window.start_button.isEnabled()
    window.close()

