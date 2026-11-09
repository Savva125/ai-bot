"""Fresh observed Send controls only; never invokes a real app."""
from threading import Event
from types import SimpleNamespace as NS
from unittest.mock import Mock

import pytest
from creator_desk.screen_agent import NativeScreen, Frame, ScreenError


@pytest.mark.parametrize("labels,field_text,allowed", [
    (["Отправить"], "Ответ", True),
    (["Send"], "Ответ", True),
    (["Оплатить"], "Ответ", False),
    (["Отправить", "Отправить"], "Ответ", False),
    (["Отправить"], "Изменён вручную", False),
])
def test_send_button_is_resolved_after_typing(labels, field_text, allowed):
    nodes = [{"id": i, "kind": "Button", "text": label, "editable": False} for i, label in enumerate(labels)]
    native = NativeScreen()
    frame = Frame(nodes, "same-chat")
    native._guard = Mock(return_value=frame)
    native._activate = Mock()
    invokes = [Mock() for _ in nodes]
    native.wrappers = [NS(iface_invoke=NS(Invoke=invoke)) for invoke in invokes]
    # No Send existed at initial capture, before filling the composer.
    binding = {"composer": NS(iface_value=NS(CurrentValue=field_text)), "send": None}
    if allowed:
        native.send(frame, binding, "Ответ", Event())
        invokes[0].assert_called_once()
    else:
        with pytest.raises(ScreenError):
            native.send(frame, binding, "Ответ", Event())
        assert not any(invoke.called for invoke in invokes)


def test_invoke_is_confirmed_only_by_new_outgoing_source_message():
    native = NativeScreen()
    def node(i, kind, text, **extra):
        return {"id": i, "kind": kind, "text": text, "editable": kind == "Edit", **extra}
    before = Frame([node(0, "Text", "Contact", role="contact_heading"),
        node(1, "ListItem", "Question", message_item=True, message_key="before", direction="incoming"),
        node(2, "Edit", "Сообщение"), node(3, "Button", "Отправить")], "old", "source")
    after = Frame([before.nodes[0], before.nodes[1],
        node(2, "ListItem", "Ответ", message_text="Ответ", message_item=True, message_key="reply", direction="outgoing"),
        node(3, "Edit", "Сообщение"), node(4, "Button", "Отправить")], "new", "source")
    invoke = Mock()
    native._guard = Mock(return_value=before)
    native._activate = Mock()
    native.wrappers = [None, None, None, NS(iface_invoke=NS(Invoke=invoke))]
    native.capture = Mock(return_value=after)
    receipt = native.send(before, {"composer": NS(iface_value=NS(CurrentValue="Ответ")), "send": None}, "Ответ", Event())
    assert receipt == "uia:reply:before:"
    invoke.assert_called_once()
