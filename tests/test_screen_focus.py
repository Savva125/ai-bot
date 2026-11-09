"""Focus sequencing with mocked Windows API; no desktop reads or real input."""
import ctypes
import os
from threading import Event
from types import SimpleNamespace as NS
from unittest.mock import Mock

import pytest

from creator_desk.screen_agent import NativeScreen, ScreenError, InputNotStarted


@pytest.fixture
def focus(monkeypatch):
    import creator_desk.screen_agent as module
    clock = [0.0]
    monkeypatch.setattr(module.time, "monotonic", lambda: clock[0])
    stop = NS(is_set=lambda: False, wait=lambda delay: clock.__setitem__(0, clock[0] + delay))
    api = NS(GetForegroundWindow=Mock(return_value=999), IsIconic=Mock(return_value=False),
             SetForegroundWindow=Mock(), ShowWindowAsync=Mock())
    def owner(handle, pointer):
        ctypes.cast(pointer, ctypes.POINTER(ctypes.c_ulong)).contents.value = os.getpid()
    api.GetWindowThreadProcessId = Mock(side_effect=owner)
    native = NativeScreen()
    native.handle = 123
    native._valid_window = lambda: None
    native._window_api = lambda: api
    native.root = NS(set_focus=Mock(side_effect=AssertionError("UIA root focus is insufficient")))
    return native, api, stop, clock


def test_activation_waits_for_selected_window(focus):
    native, api, stop, clock = focus
    api.GetForegroundWindow.side_effect = [999, 999, 123]
    native._activate(stop)
    api.SetForegroundWindow.assert_called_once_with(123)
    assert clock[0] > 0
    native.root.set_focus.assert_not_called()


def test_selected_window_needs_no_reactivation(focus):
    native, api, stop, _ = focus
    api.GetForegroundWindow.return_value = 123
    native._activate(stop)
    api.SetForegroundWindow.assert_not_called()


def test_activation_does_not_steal_focus_from_other_app(focus):
    native, api, stop, _ = focus
    def other_owner(handle, pointer):
        ctypes.cast(pointer, ctypes.POINTER(ctypes.c_ulong)).contents.value = os.getpid() + 1
    api.GetWindowThreadProcessId.side_effect = other_owner
    with pytest.raises(ScreenError, match="другое приложение"):
        native._activate(stop)
    api.SetForegroundWindow.assert_not_called()


def test_windows_activation_refusal_times_out_without_typing(focus):
    native, api, stop, clock = focus
    with pytest.raises(ScreenError, match="Windows не активировала"):
        native._activate(stop)
    assert clock[0] >= 1


def test_composer_focus_can_arrive_after_foreground_activation(focus):
    native, api, stop, clock = focus
    api.GetForegroundWindow.return_value = 123
    field = NS(set_focus=Mock(), has_keyboard_focus=Mock(side_effect=[False, False, True]))
    native._focus_composer(field, stop)
    assert clock[0] >= .05
    assert field.has_keyboard_focus.call_count == 3


def test_focus_failure_before_atomic_write_is_retryable(focus):
    native, api, stop, _ = focus
    native._guard = lambda *args: None
    value = NS(CurrentValue="", writes=[])
    def write(text):
        value.CurrentValue = text
        value.writes.append(text)
    value.SetValue = write
    field = NS(iface_value=value)
    def focus_field(*args):
        raise ScreenError("Focus failed")
    native._focus_composer = focus_field
    with pytest.raises(ScreenError) as error:
        native.insert(NS(), {"composer": field}, "abcdefghijklmnop", stop)
    assert isinstance(error.value, InputNotStarted)
    assert value.writes == []


def test_long_reply_is_written_once_without_per_chunk_capture(focus):
    native, api, stop, _ = focus
    api.GetForegroundWindow.return_value = 123
    native._guard = Mock()
    native._focus_composer = Mock()
    value = NS(CurrentValue="", writes=[])
    def write(text):
        value.CurrentValue = text
        value.writes.append(text)
    value.SetValue = write
    text = "Длинный ответ " * 100
    native.insert(NS(), {"composer": NS(iface_value=value)}, text, stop)
    assert value.writes == [text]
    assert native._guard.call_count == 2


def test_unconfirmed_atomic_write_cannot_be_retried_as_unstarted(focus):
    native, api, stop, _ = focus
    api.GetForegroundWindow.return_value = 123
    native._guard = native._focus_composer = Mock()
    value = NS(CurrentValue="", SetValue=Mock())
    with pytest.raises(ScreenError) as error:
        native.insert(NS(), {"composer": NS(iface_value=value)}, "Reply", stop)
    assert not isinstance(error.value, InputNotStarted)
    value.SetValue.assert_called_once()


def test_activation_respects_stop(focus):
    native, api, _, _ = focus
    stopped = Event()
    stopped.set()
    with pytest.raises(ScreenError, match="Остановлено"):
        native._activate(stopped)
    api.SetForegroundWindow.assert_not_called()
