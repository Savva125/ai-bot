"""Selected-window assistant. UIA supplies visible screen text; the LLM has no tools."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import sys
from threading import Event, local
import time
from urllib.parse import urlparse

from sqlalchemy import select

from .errors import AiError
from .models import Account, Chat, Contact, Draft, Message, ConversationContext
from .security import age_concern
from .personalization import Personalization
from .conversation import merge_turns, incoming_anchor, parse_message_time
from .reply_tracking import reconcile_replies, source_receipt, short_key


class ScreenError(RuntimeError):
    pass


class InputNotStarted(ScreenError):
    """Focus failed before the first write; retrying cannot duplicate inserted text."""


class ContextChanged(ScreenError):
    """An observed conversation changed before a targeted write."""


class ComposerCleared(ScreenError):
    """The operator cleared a draft before the assistant invoked Send."""


_uia_thread = local()


def initialize_uia_com():
    """Initialize this worker before importing libraries that implicitly initialize COM.

    comtypes defaults to STA on first import. Importing it and then requesting
    MTA causes RPC_E_CHANGED_MODE. Keep an existing apartment if Qt/another
    library already initialized this thread, and cache initialization per thread.
    """
    if os.name != "nt":
        raise ScreenError("Чтение окон доступно только в Windows.")
    if hasattr(_uia_thread, "apartment"):
        return _uia_thread.apartment
    import ctypes
    ole = ctypes.WinDLL("ole32", use_last_error=True)
    initialize = ole.CoInitializeEx
    initialize.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
    initialize.restype = ctypes.c_long
    result = initialize(None, 0)
    if result in (0, 1):  # S_OK / S_FALSE: MTA was initialized or already active.
        apartment = 0
    elif result & 0xFFFFFFFF == 0x80010106:  # RPC_E_CHANGED_MODE: preserve existing STA.
        apartment = 2
    else:
        raise ScreenError(f"Не удалось включить чтение окон Windows (COM 0x{result & 0xFFFFFFFF:08X}).")
    # Both comtypes and pywinauto consult this before their automatic initialization.
    sys.coinit_flags = apartment
    _uia_thread.apartment = apartment
    return apartment


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def review_status(reason):
    reason = (reason or "").strip()
    if not reason or reason == "model_returned_no_reply":
        return "Текст ответа не создан. Нажмите «Продолжить диалог», чтобы попробовать снова."
    return "Нужна ручная проверка. " + reason


@dataclass
class Frame:
    nodes: list[dict]
    revision: str
    source: str = "selected-window"
    session_key: str = ""
    tail_visible: bool | None = None


@dataclass
class ChatView:
    account: str
    contact: str
    history: list[str]
    incoming: bool
    composer: int
    send: int | None
    revision: str
    turns: list[dict] | None = None
    last_message_at: float | None = None
    tail_visible: bool | None = None


MESSAGE_FIELD = re.compile(r"message|сообщен|ответ|reply|написа|write|send a|mensaje|nachricht|messaggio|mensagem|повідомлен|رسالة|消息|訊息|メッセージ|메시지", re.I)
NON_MESSAGE_FIELD = re.compile(r"password|login|search|email|username|address|amount|price|payment|card|поиск|парол|логин|почт|адрес|сумм|цен[аы]|плат[её]ж|карт|buscar|contraseña|suchen|passwort|recherche|mot de passe|pesquisar|senha|пошук|كلمة المرور|بحث|搜索|搜尋|パスワード|検索", re.I)
SEND_BUTTON = re.compile(r"\s*(?:send(?: message)?|отправить(?: сообщение)?|enviar(?: mensaje| mensagem)?|senden|envoyer|invia|надіслати|إرسال|发送|傳送|送信|보내기)\s*", re.I)
IDENTITY_LABEL = re.compile(r"(?:account|creator|аккаунт|собеседник|chat with|conversation with)\s*[:@]|^(?:OF|Fansly)\s+\S+", re.I)


def history_node(node):
    return not node["editable"] and (node["kind"] == "Text" or
                                     (node["kind"] == "ListItem" and node.get("message_item") is True))


def native_message_item(info):
    """Observed Qt Telegram history list, excluding Dialogs::InnerWidget previews."""
    if info.control_type != "ListItem":
        return False
    identifier = str(getattr(info, "automation_id", "")).strip()
    return (identifier.endswith(("class HistoryInner", "class HistoryView::ListWidget"))
            or str(getattr(info, "class_name", "")) in {"HistoryInner", "HistoryView::ListWidget"})


def message_metadata(node):
    """Read Qt's separate delivery/text fields, not words inside message content."""
    result = {}
    try:
        children = node.children()
    except Exception:
        return result
    for child in children:
        try:
            label = (child.element_info.name or "").strip().casefold()
        except Exception:
            continue
        if label not in {"получение", "delivery", "сообщение", "message", "тип медиафайла", "media type", "время", "time"}:
            continue
        try:
            value = child.iface_value.CurrentValue or ""
        except Exception:
            value = ""
        if not value:
            try:
                value = child.legacy_properties().get("Value", "") or ""
            except Exception:
                continue
        if label in {"получение", "delivery"}:
            if value.strip().casefold() in {"получено", "received"}:
                result["direction"] = "incoming"
            elif value.strip().casefold() in {"отправлено", "sent"}:
                result["direction"] = "outgoing"
        elif label in {"сообщение", "message"} and value.strip():
            result["message_text"] = value
        elif label in {"тип медиафайла", "media type"} and value.strip():
            result["media_type"] = value
        elif label in {"время", "time"} and value.strip():
            result["message_time"] = value
    return result


def structured_chat_view(frame):
    """Use observed Telegram message metadata before asking an LLM to guess roles."""
    messages = [n for n in frame.nodes if n.get("message_item")]
    headings = [n for n in frame.nodes if n.get("role") == "contact_heading"]
    composers = [n for n in frame.nodes if n["kind"] in {"Edit", "Document"}
                 and n["editable"] and MESSAGE_FIELD.search(n["text"])
                 and not NON_MESSAGE_FIELD.search(n["text"])]
    if len(composers) > 1:
        # Qt exposes both InputField and its actual inner editor as Edit.
        # The observed inner editor is the writable surface, not a guessed ID.
        inner = [n for n in composers if n.get("automation_id", "").endswith("class Ui::InputField::Inner")]
        if len(inner) == 1:
            composers = inner
    if (not messages or len(headings) != 1 or len(composers) != 1
            or messages[-1].get("direction") not in {"incoming", "outgoing"}):
        return None
    sends = [n for n in frame.nodes if n["kind"] == "Button" and SEND_BUTTON.fullmatch(n["text"])]
    turns = []
    for node in messages[-50:]:
        text = node.get("message_text") or node["text"]
        if node.get("media_type"):
            text = f"[Вложение: {node['media_type']}. Содержимое не просмотрено.]" + (" " + node["message_text"] if node.get("message_text") else "")
        turns.append({"key": node.get("message_key") or digest(node["text"]),
                      "role": node.get("direction", "context"), "text": text})
    history = [t["text"] for t in turns]
    return ChatView("window:" + digest(frame.session_key or frame.source), headings[0]["text"], history,
                    messages[-1]["direction"] == "incoming", composers[0]["id"],
                    sends[0]["id"] if len(sends) == 1 else None, frame.revision, turns,
                    parse_message_time(messages[-1].get("message_time")), frame.tail_visible)


def chat_region(nodes):
    """Use the observed message field bounds to prefer the central conversation.

    Coordinates only rank readable text; they never authorize clicks or typing.
    Disabled message fields still identify the chat region while a platform is locked.
    """
    candidates = []
    for node in nodes:
        rect = node.get("rect")
        if node["kind"] not in {"Edit", "Document"} or not rect or len(rect) != 4:
            continue
        if NON_MESSAGE_FIELD.search(node["text"]) or not MESSAGE_FIELD.search(node["text"]):
            continue
        left, top, right, bottom = rect
        if right - left < 120 or bottom <= top:
            continue
        candidates.append((bool(node["editable"]), right - left, top, rect))
    return max(candidates, key=lambda item: item[:3])[3] if candidates else None


def in_chat_region(node, region):
    rect = node.get("rect")
    if not region or not rect:
        return None
    left, top, right, bottom = rect
    center = (left + right) / 2
    return region[0] - 45 <= center <= region[2] + 45 and top <= region[3] + 90


def compact_screen_context(frame, budget=8500):
    """Bound the *whole* model payload, preserve native IDs and omit peripheral UI.

    Keep controls, identity/header labels and recent conversation text before
    menu items, chat-list previews, emojis and sidebar notes. Full native nodes
    remain available for validation and for saving the selected message text.
    """
    region = chat_region(frame.nodes)
    ranked = []
    text_nodes = [n for n in frame.nodes if history_node(n) and in_chat_region(n, region) is not False]
    header_ids = {n["id"] for n in text_nodes[:16]}
    recent_ids = {n["id"] for n in text_nodes[-48:]}
    for order, node in enumerate(frame.nodes):
        text = node["text"].strip()
        inside = in_chat_region(node, region)
        message_field = node["kind"] in {"Edit", "Document"} and not NON_MESSAGE_FIELD.search(text)
        send_button = node["kind"] == "Button" and SEND_BUTTON.fullmatch(text)
        identity = node.get("role") == "contact_heading" or bool(IDENTITY_LABEL.search(text))
        if inside is False and not identity:
            continue
        if message_field and (node["editable"] or MESSAGE_FIELD.search(text)):
            priority = 1000
        elif send_button:
            priority = 950
        elif identity:
            priority = 900
        elif node["id"] in header_ids:
            priority = 800
        elif node["id"] in recent_ids:
            priority = 700
        elif history_node(node):
            priority = 400
        else:
            priority = 100
        if not re.search(r"[\w]", text, re.UNICODE) and not message_field:
            continue
        ranked.append((priority, order, node))
    selected = []
    for _, order, node in sorted(ranked, key=lambda item: (-item[0], -item[1] if item[0] == 700 else item[1])):
        row = [node["id"], "Message" if node.get("message_item") else node["kind"], node["text"][:180], bool(node["editable"])]
        trial = sorted(selected + [(order, node, row)], key=lambda item: item[0])
        payload = {"format": "[id,kind,text,editable]", "elements": [item[2] for item in trial]}
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        if len(encoded) <= budget:
            selected = trial
    payload = {"format": "[id,kind,text,editable]", "elements": [item[2] for item in selected]}
    return [item[1] for item in selected], json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def screen_revision(title, nodes, stable):
    """Changes to unrelated chat-list previews/sidebar notes must not cancel a reply."""
    region = chat_region(nodes)
    if region:
        ids = {node["id"] for node in nodes if in_chat_region(node, region) is not False or IDENTITY_LABEL.search(node["text"])}
        values = [value for index, value in stable.items() if index in ids]
    else:
        values = list(stable.values())
    return digest(json.dumps([title, values], ensure_ascii=False))


class NativeScreen:
    """Only the user-selected window. No clipboard, guessed coordinates or global typing."""
    def __init__(self):
        self.handle = self.pid = self.process_stamp = None
        self.root = None
        self.wrappers = []
        self.scope = None
        self.diagnostics_path = None
        self.last_capture_seconds = 0.0

    @staticmethod
    def desktop():
        if os.name != "nt":
            raise ScreenError("Выбор окна доступен в Windows.")
        try:
            initialize_uia_com()
            from pywinauto import Desktop
            return Desktop(backend="uia")
        except ImportError:
            raise ScreenError("Не установлены компоненты чтения экрана. Запустите setup-screen.ps1.") from None
        except OSError as error:
            code = getattr(error, "winerror", None)
            detail = f" (код {code})" if code is not None else ""
            raise ScreenError("Windows не смогла запустить чтение окон" + detail + ". Перезапустите помощника.") from None

    @staticmethod
    def process(pid):
        import psutil
        process = psutil.Process(pid)
        return process.name(), process.create_time()

    def windows(self):
        denied = {"codex.exe", "chatgpt.exe", "cmd.exe", "powershell.exe", "pwsh.exe", "windowsterminal.exe", "1password.exe", "keepass.exe", "bitwarden.exe"}
        result = []
        for window in self.desktop().windows(visible_only=True):
            try:
                pid = window.process_id()
                name, _ = self.process(pid)
                title = window.window_text()
                if pid != os.getpid() and title and name.casefold() not in denied:
                    result.append({"handle": window.handle, "title": title, "app": name})
            except Exception:
                continue
        return result

    def choose(self, handle):
        self.root = self.desktop().window(handle=handle).wrapper_object()
        self.handle, self.pid = handle, self.root.process_id()
        _, self.process_stamp = self.process(self.pid)
        self.scope = None

    def _telegram_descendants(self):
        kinds = ("ListItem", "Edit", "Button", "Text")
        if getattr(getattr(self.root, "element_info", None), "element", None) is not None:
            from pywinauto.uia_defines import IUIA
            from pywinauto.controls.uiawrapper import UIAWrapper
            uia = IUIA()
            condition = uia.iuia.CreateOrConditionFromArray([uia.build_condition(control_type=k) for k in kinds])
            # One scoped traversal with an OR condition, not four full walks.
            infos = self.root.element_info._get_elements(uia.tree_scope["descendants"], condition, cache_enable=True)
            return [UIAWrapper(info) for info in infos]
        return [n for kind in kinds for n in self.root.descendants(control_type=kind)]

    def _valid_window(self):
        if not self.root:
            raise ScreenError("Сначала выберите окно или вкладку.")
        try:
            if self.root.process_id() != self.pid or self.process(self.pid)[1] != self.process_stamp:
                raise ScreenError("Выбранное приложение перезапущено. Выберите окно заново.")
        except ScreenError:
            raise
        except Exception:
            raise ScreenError("Выбранное окно закрыто или недоступно.") from None

    def _title(self):
        element = getattr(getattr(self.root, "element_info", None), "element", None)
        return (element.CurrentName or "") if element is not None else self.root.window_text()

    def can_interact(self):
        """Reading is background-safe; writing must not steal focus from another app."""
        import ctypes
        from ctypes import wintypes
        user = self._window_api()
        active = user.GetForegroundWindow()
        if active == self.handle:
            return True
        owner = wintypes.DWORD()
        user.GetWindowThreadProcessId(active, ctypes.byref(owner))
        return owner.value == os.getpid()

    def capture(self):
        started = time.perf_counter()
        self._valid_window()
        nodes, wrappers, stable, origins = [], [], {}, []
        diagnostic_items = []
        viewports = {}
        last_history_info = None
        try:
            app_name = self.process(self.pid)[0]
            if app_name.casefold() == "telegram.exe":
                # Ask UIA for needed roles directly, skipping thousands of Custom
                # delivery/time fields; read those only for visible messages.
                descendants = self._telegram_descendants()
            else:
                descendants = self.root.descendants()
            if self.scope and self.scope[0] != "window":
                if not any(n.element_info.control_type == "Document" and n.element_info.visible
                           and tuple(n.element_info.runtime_id) == self.scope for n in descendants):
                    raise ScreenError("Подключённая вкладка закрыта или скрыта. Вернитесь к ней либо выберите другую.")
            for node in descendants:
                info = node.element_info
                kind = info.control_type
                if kind not in {"Text", "Edit", "Document", "Button", "Hyperlink", "ListItem"}:
                    continue
                message_item = kind == "ListItem" and native_message_item(info)
                if message_item:
                    last_history_info = info
                diagnostic = None
                if self.diagnostics_path and kind == "ListItem":
                    diagnostic = {"is_message": message_item, "result": "filtered_type_or_visibility"}
                    diagnostic_items.append(diagnostic)
                rectangle = info.rectangle
                if message_item:
                    # Qt exposes cached offscreen messages as visible. Only retain
                    # items intersecting the observed history list's viewport.
                    history_key = getattr(info, "automation_id", "") or getattr(info, "class_name", "")
                    if history_key not in viewports:
                        parent = node.parent()
                        parent_rect = parent.element_info.rectangle
                        try:
                            viewport = parent.parent()
                            if viewport.element_info.automation_id.endswith("class Ui::ElasticScroll"):
                                parent_rect = viewport.element_info.rectangle
                        except Exception:
                            pass
                        viewports[history_key] = parent_rect
                    parent_rect = viewports[history_key]
                    if diagnostic is not None:
                        diagnostic.update(rect=[rectangle.left, rectangle.top, rectangle.right, rectangle.bottom],
                                          parent_rect=[parent_rect.left, parent_rect.top, parent_rect.right, parent_rect.bottom],
                                          result="outside_parent")
                    if (parent_rect.right > parent_rect.left and parent_rect.bottom > parent_rect.top
                            and (rectangle.right <= parent_rect.left or rectangle.left >= parent_rect.right
                                 or rectangle.bottom <= parent_rect.top or rectangle.top >= parent_rect.bottom)):
                        continue
                if not info.visible:
                    continue
                try:
                    if bool(info.element.CurrentIsPassword):
                        continue
                except Exception:
                    continue
                name = info.name or ""
                editable, value = False, ""
                if kind in {"Edit", "Document"}:
                    try:
                        editable = info.enabled and not node.iface_value.CurrentIsReadOnly
                        value = node.iface_value.CurrentValue or ""
                    except Exception:
                        pass
                if kind == "Document" and value.startswith(("https://", "http://")):
                    parsed = urlparse(value)
                    origins.append(parsed.scheme + "://" + parsed.netloc)
                if not name and not editable:
                    continue
                if len(name) > 3000:
                    continue
                if len(nodes) >= 12000:
                    raise ScreenError("Windows вернула слишком большое дерево интерфейса. Перезагрузите выбранную вкладку.")
                entry = {"id": len(nodes), "kind": kind, "text": name[:1800],
                         "editable": editable, "value": value[:4000],
                         "rect": [rectangle.left, rectangle.top, rectangle.right, rectangle.bottom]}
                if kind in {"Edit", "Document"}:
                    entry["automation_id"] = getattr(info, "automation_id", "")
                if message_item:
                    entry["message_item"] = True
                    entry["message_key"] = digest(json.dumps(tuple(info.runtime_id)))
                    entry.update(message_metadata(node))
                if diagnostic is not None:
                    diagnostic.update(result="accepted", direction=entry.get("direction"), name=name[-150:])
                nodes.append(entry)
                wrappers.append(node)
                if message_item:
                    stable[entry["id"]] = (tuple(info.runtime_id), name, entry.get("direction"), entry.get("message_text"))
                elif kind == "Text":
                    parent = node.parent()
                    if parent and parent.element_info.control_type not in {"Edit"}:
                        stable[entry["id"]] = (tuple(info.runtime_id), name)
            if not nodes:
                raise ScreenError("Приложение не предоставляет читаемый текст экрана. Для него нужен отдельный коннектор.")
            self.wrappers = wrappers
            app_name = self.process(self.pid)[0]
            if app_name.casefold() == "telegram.exe":
                # Telegram's active correspondent is exposed in the window title;
                # its painted top bar does not provide a separate Text heading.
                title = re.sub(r"\s*[–—-]\s*\(\d+\)\s*$", "", self._title()).strip("\u200e\u200f ")
                title = re.sub(r"^\(\d+\)\s*", "", title).strip("\u200e\u200f ")
                if title and any(n.get("message_item") for n in nodes):
                    nodes.append({"id": len(nodes), "kind": "Text", "text": title,
                                  "editable": False, "role": "contact_heading"})
                    wrappers.append(self.root)
            revision = screen_revision(title if app_name.casefold() == "telegram.exe" else self._title(), nodes, stable)
            session_key = digest(f"{app_name}:{self.pid}:{self.process_stamp}:{self.handle}")
            source = app_name + ":" + ("|".join(sorted(set(origins))) or session_key)
            self.last_capture_seconds = time.perf_counter() - started
            if self.diagnostics_path:
                Path(self.diagnostics_path).write_text(json.dumps({"items": diagnostic_items, "nodes": nodes,
                    "capture_seconds": self.last_capture_seconds}, ensure_ascii=False), encoding="utf-8")
            visible_messages = [n for n in nodes if n.get("message_item")]
            tail_visible = (visible_messages[-1]["message_key"] == digest(json.dumps(tuple(last_history_info.runtime_id)))) if visible_messages and last_history_info else None
            return Frame(nodes, revision, source, session_key, tail_visible)
        except ScreenError:
            raise
        except Exception:
            raise ScreenError("Не удалось прочитать выбранный экран. Откройте окно и попробуйте снова.") from None

    def bind_view(self, frame, view):
        composer = self.wrappers[view.composer]
        # A selected browser tab is bounded by its nearest accessible Document.
        current = composer
        document = None
        for _ in range(40):
            current = current.parent()
            if current is None:
                break
            if current.element_info.control_type == "Document":
                document = tuple(current.element_info.runtime_id)
                break
        scope = document or ("window", self.handle)
        if self.scope is None:
            self.scope = scope
        elif self.scope != scope:
            raise ScreenError("Открыта другая вкладка. Вернитесь к подключённой или выберите новую.")
        return {"composer": composer, "send": self.wrappers[view.send] if view.send is not None else None}

    @staticmethod
    def _window_api():
        import ctypes
        from ctypes import wintypes
        user = ctypes.WinDLL("user32", use_last_error=True)
        user.GetForegroundWindow.restype = wintypes.HWND
        user.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        user.SetForegroundWindow.argtypes = [wintypes.HWND]
        user.SetForegroundWindow.restype = wintypes.BOOL
        user.IsIconic.argtypes = [wintypes.HWND]
        user.ShowWindowAsync.argtypes = [wintypes.HWND, ctypes.c_int]
        return user

    def _activate(self, stop=None):
        """UIA SetFocus alone does not activate a native top-level Qt window."""
        import ctypes
        from ctypes import wintypes
        self._valid_window()
        stop = stop if stop is not None else Event()
        user = self._window_api()
        def allowed(active):
            if active == self.handle:
                return True
            owner = wintypes.DWORD()
            user.GetWindowThreadProcessId(active, ctypes.byref(owner))
            return owner.value == os.getpid()
        if stop.is_set():
            raise ScreenError("Остановлено.")
        active = user.GetForegroundWindow()
        if active == self.handle:
            return
        if not allowed(active):
            raise ScreenError("Вы открыли другое приложение. Вернитесь к выбранному окну или помощнику.")
        if user.IsIconic(self.handle):
            user.ShowWindowAsync(self.handle, 9)  # SW_RESTORE, only the selected window.
        user.SetForegroundWindow(self.handle)
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            if stop.is_set():
                raise ScreenError("Остановлено.")
            active = user.GetForegroundWindow()
            if active == self.handle:
                return
            if active and not allowed(active):
                raise ScreenError("Фокус переключён в другое приложение. Ввод отменён.")
            stop.wait(0.025)
        raise ScreenError("Windows не активировала выбранное окно. Нажмите на его заголовок и затем «Продолжить».")

    def _focus_composer(self, field, stop):
        self._activate(stop)
        field.set_focus()
        deadline = time.monotonic() + 0.75
        user = self._window_api()
        while time.monotonic() < deadline:
            if stop.is_set():
                raise ScreenError("Остановлено.")
            if user.GetForegroundWindow() != self.handle:
                raise ScreenError("Фокус переключён. Ввод отменён.")
            if field.has_keyboard_focus():
                return
            stop.wait(0.025)
        raise ScreenError("Поле сообщения не получило фокус. Ввод отменён.")

    def _guard(self, frame, stop):
        if stop.is_set():
            raise ScreenError("Остановлено.")
        current = self.capture()
        if current.revision != frame.revision:
            raise ContextChanged("Экран или сообщения изменились. Устаревший ответ отменён.")
        return current

    def insert(self, frame, binding, text, stop):
        field = binding["composer"]
        self._guard(frame, stop)
        if field.iface_value.CurrentValue:
            raise ScreenError("В поле уже есть текст. Он не будет перезаписан.")
        try:
            self._focus_composer(field, stop)
        except ScreenError as error:
            if not field.iface_value.CurrentValue:
                raise InputNotStarted(str(error)) from error
            raise
        self._guard(frame, stop)
        if stop.is_set() or self._window_api().GetForegroundWindow() != self.handle:
            raise InputNotStarted("Ввод отменён: фокус изменился или работа остановлена.")
        if field.iface_value.CurrentValue:
            raise ScreenError("Текст изменён вручную. Ввод отменён.")
        # One targeted write after validating context/focus. Never inject keys.
        field.iface_value.SetValue(text)
        if (field.iface_value.CurrentValue or "") != text:
            raise ScreenError("Приложение не подтвердило введённый текст.")

    def send(self, frame, binding, text, stop):
        current = self._guard(frame, stop)
        self._activate(stop)
        value = binding["composer"].iface_value.CurrentValue or ""
        if not value:
            raise ComposerCleared("Поле очищено. Автоматическая отправка отменена.")
        if value != text:
            raise ScreenError("Текст изменился. Отправка отменена.")
        # Empty Telegram composers expose a microphone; Send appears only after
        # insertion. Resolve the observed labelled button from the fresh snapshot.
        region = chat_region(current.nodes)
        candidates = [node for node in current.nodes if node["kind"] == "Button"
                      and SEND_BUTTON.fullmatch(node["text"]) and in_chat_region(node, region) is not False]
        if len(candidates) != 1:
            raise ScreenError("Не удалось однозначно найти кнопку отправки. Текст оставлен в поле — отправьте вручную.")
        if stop.is_set():
            raise ScreenError("Остановлено. Сообщение не отправлено.")
        self.wrappers[candidates[0]["id"]].iface_invoke.Invoke()
        # Invocation alone is not a receipt. Check source history, without retrying
        # the click; a late source update may instead be reconciled on the next poll.
        before_view = structured_chat_view(current)
        heading = before_view.contact if before_view else None
        for _ in range(2):
            if stop.wait(0.1):
                return None
            try:
                after = self.capture()
                view = structured_chat_view(after)
            except ScreenError:
                return None
            if view and view.contact == heading and after.source == current.source:
                old_keys = {t["key"] for t in before_view.turns}
                matches = [i for i, t in enumerate(view.turns) if t["role"] == "outgoing"
                           and t["text"].strip() == text.strip() and t["key"] not in old_keys]
                if len(matches) == 1:
                    return source_receipt(view.turns, matches[0], before_view.turns[-1]["key"] if before_view.turns else "")
        return None




class ScreenInterpreter:
    def __init__(self, provider=None):
        self.provider = provider

    def inspect(self, frame):
        structured = structured_chat_view(frame)
        if structured is not None:
            return structured
        supplied, compact = compact_screen_context(frame)
        if not supplied:
            return None
        if any(node["kind"] in {"Edit", "Document"} and not node["editable"]
               and re.search(r"начните смену (?:для|чтобы).*сообщен", node["text"], re.I) for node in supplied):
            raise ScreenError("Платформа блокирует поле сообщения: начните смену в самой платформе, затем нажмите «Продолжить».")
        if self.provider is None or not hasattr(self.provider, "inspect_screen"):
            raise ScreenError("Для распознавания этого окна нужен подключённый сервер AI.")
        raw = self.provider.inspect_screen(compact)
        if raw.get("is_chat") is not True:
            return None
        lookup = {node["id"]: node for node in supplied}
        def get(index):
            if type(index) is not int or index not in lookup:
                raise ScreenError("AI не смог надёжно распознать элементы диалога.")
            return lookup[index]
        account = get(raw["account"]) if raw.get("account") is not None else None
        contact, composer = get(raw.get("contact")), get(raw.get("composer"))
        if not composer["editable"] or composer["kind"] not in {"Edit", "Document"}:
            raise ScreenError("Поле сообщения не поддерживает безопасный ввод.")
        if NON_MESSAGE_FIELD.search(composer["text"]):
            raise ScreenError("Распознанное поле не является полем сообщения. Ввод запрещён.")
        if not contact["text"] or (account is not None and (not account["text"] or account["id"] == contact["id"])):
            raise ScreenError("Не удалось разделить аккаунт и собеседника.")
        history_ids = raw.get("history")
        if not isinstance(history_ids, list) or not history_ids or len(history_ids) > 50 or len(set(history_ids)) != len(history_ids):
            raise ScreenError("Не удалось распознать историю диалога.")
        history = [get(index)["text"] for index in history_ids]
        if any(not history_node(get(index)) for index in history_ids):
            raise ScreenError("Вместо сообщений распознаны другие элементы.")
        send = raw.get("send")
        if send is not None and get(send)["kind"] != "Button":
            raise ScreenError("Кнопка отправки не распознана.")
        if send is not None and not SEND_BUTTON.fullmatch(get(send)["text"]):
            send = None  # Ambiguous/icon-only buttons require manual sending in the source app.
        owner = account["text"] if account else "window:" + digest(frame.session_key or frame.source)
        return ChatView(owner, contact["text"], history, raw.get("incoming") is True,
                        composer["id"], send, frame.revision)


class ScreenAssistant:
    def __init__(self, sessions, provider, native=None, interpreter=None):
        self.sessions, self.provider = sessions, provider
        self.personalization = Personalization(sessions)
        self.active_chat_id = None
        self.native = native or NativeScreen()
        self.interpreter = interpreter or ScreenInterpreter(provider)
        self.stop = Event()
        self.handled = set()
        self.pending = None
        self.last_frame = None
        self.last_view = None
        self.last_attempt = 0.0
        self.last_status = None
        self.inspect_only = False
        self.inspect_focus = False
        self.inspection_result = None
        self.automatic_followup = False
        self.auto_send = False
        self.autonomous_dialogue = False
        self.followup_delay = 120
        self.outgoing_seen = {}
        self.outgoing_times = {}
        self.last_metrics = {}
        self.awaiting_focus = False
        self.tracking_pending = False
        self.rejected_anchors = set()
        if hasattr(provider, "stop_event"):
            provider.stop_event = self.stop

    def recover(self):
        with self.sessions.begin() as s:
            for draft in s.scalars(select(Draft).where(Draft.status.in_(["screen_generating", "screen_inserting", "screen_inserted", "screen_sending"]))):
                draft.status, draft.reason = "needs_review", "restart_requires_review"

    def choose(self, handle):
        self.stop.clear()
        self.pending = self.last_frame = self.last_view = None
        self.last_status = None
        self.active_chat_id = None
        self.awaiting_focus = False
        self.tracking_pending = False
        self.rejected_anchors = set()
        self.inspection_result = None
        self.native.choose(handle)
        self.outgoing_seen.clear()
        self.outgoing_times.clear()

    def _save(self, frame, view):
        self.tracking_pending = False
        self.rejected_anchors = set()
        account_key = "screen:" + digest(frame.source + ":" + view.account)
        contact_key = digest(view.contact)
        with self.sessions.begin() as s:
            account = s.scalar(select(Account).where(Account.external_id == account_key))
            if account is None:
                account = Account(external_id=account_key, name=view.account[:200], adapter="selected-window")
                s.add(account); s.flush()
            contact = s.scalar(select(Contact).where(Contact.account_id == account.id, Contact.external_id == contact_key))
            if contact is None:
                contact = Contact(account_id=account.id, external_id=contact_key, name=view.contact[:200])
                s.add(contact); s.flush()
            chat = s.scalar(select(Chat).where(Chat.account_id == account.id, Chat.contact_id == contact.id))
            if chat is None:
                chat = Chat(account_id=account.id, contact_id=contact.id, external_id=contact_key)
                s.add(chat); s.flush()
            transcript = "\n".join(view.history)
            revision = digest(json.dumps(view.turns, ensure_ascii=False)) if view.turns else digest(transcript)
            context = s.get(ConversationContext, chat.id)
            if context is None:
                context = ConversationContext(chat_id=chat.id, turns=[], last_incoming_key="", followup_anchor="")
                s.add(context)
            if view.turns:
                removed, self.rejected_anchors, self.tracking_pending = reconcile_replies(
                    s, chat, context.turns, view.turns, view.tail_visible)
                merged = merge_turns([t for t in context.turns if t["key"] not in removed], view.turns)
                if merged != context.turns:
                    context.turns = merged
                context.last_incoming_key = incoming_anchor(merged)
            elif view.incoming:
                context.last_incoming_key = digest("incoming:" + revision)
            external = "capture:" + revision
            if not s.scalar(select(Message.id).where(Message.chat_id == chat.id, Message.external_id == external)):
                s.add(Message(chat_id=chat.id, external_id=external, role="screen_context", text=transcript))
            chat.revision = revision
            if age_concern(transcript):
                contact.blocked = True
            self.active_chat_id = chat.id
            return chat.id, revision, contact.blocked

    def personalization_state(self):
        # Bind settings to a fresh read, so a recent external chat switch cannot
        # silently attach notes to the previously displayed correspondent.
        self.active_chat_id = None
        if self.last_view is not None or getattr(self.native, "root", None) is not None:
            try:
                frame = self.native.capture()
                view = self.interpreter.inspect(frame)
                if view:
                    self.native.bind_view(frame, view)
                    self._save(frame, view)
            except ScreenError:
                pass
        return self.personalization.load(self.active_chat_id)

    def step(self, continue_dialogue=False):
        started = time.perf_counter()
        if self.stop.is_set():
            return {"status": "Пауза", "ready": False}
        if self.inspect_only and self.inspection_result is not None:
            return self.inspection_result
        frame = self.native.capture()
        captured = time.perf_counter()
        if self.inspect_only:
            status = "Проверка чтения завершена. Ввод отключён."
            if self.inspect_focus:
                view = structured_chat_view(frame)
                if view is None:
                    raise ScreenError("Для проверки фокуса нужен распознанный диалог Telegram.")
                binding = self.native.bind_view(frame, view)
                self.native._focus_composer(binding["composer"], self.stop)
                status = "Окно активировано, поле получило фокус. Ввод отключён."
                if self.native.diagnostics_path:
                    path = Path(self.native.diagnostics_path)
                    data = json.loads(path.read_text(encoding="utf-8"))
                    data["focus_check"] = {"activated": True, "composer": view.composer}
                    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            self.inspection_result = {"status": status, "ready": False}
            return self.inspection_result
        # No model call when visible screen text did not change.
        unchanged = (self.last_frame and self.last_frame.revision == frame.revision
                     and self.last_frame.tail_visible == frame.tail_visible)
        if unchanged and not self.tracking_pending and not self.awaiting_focus and not continue_dialogue and not (self.automatic_followup and self.last_view and not self.last_view.incoming):
            return {"status": "Ответ в поле — отправьте его в выбранном приложении." if self.pending else
                    (self.last_status or "Ожидаю новое входящее сообщение…"), "ready": bool(self.pending)}
        # Structured parsing is cheap and refreshes current native control IDs.
        # Sidebar insertions can shift IDs even if the chat text is unchanged.
        view = (structured_chat_view(frame) if unchanged and self.last_view and self.last_view.turns else None)
        if view is None:
            view = self.last_view if unchanged and self.last_view else self.interpreter.inspect(frame)
        self.last_frame = frame
        self.last_view = view
        if view is None:
            self.pending = None
            self.last_status = "Диалог не распознан. Откройте чат в выбранном окне."
            return {"status": "Откройте диалог в выбранном окне — начну автоматически.", "ready": False}
        binding = self.native.bind_view(frame, view)
        chat_id, revision, blocked = self._save(frame, view)
        self.last_view = view
        key = chat_id, revision
        if self.pending and self.pending[0] != key:
            self.pending = None
        if blocked:
            raise ScreenError("Признаки несовершеннолетия. Автоматическая работа остановлена.")
        with self.sessions() as s:
            cancelled = s.scalar(select(Draft.id).where(Draft.chat_id == chat_id, Draft.revision == revision,
                Draft.status.in_(["cancelled", "discarded"])).limit(1))
        if cancelled:
            self.pending = None
            self.last_status = "Предыдущий ответ отменён. Не отправляю его повторно; жду новое сообщение."
            return {"status": self.last_status, "ready": False}
        if self.tracking_pending:
            self.last_status = "Сверяю исчезнувший ответ с перепиской…"
            return {"status": self.last_status, "ready": False}
        if view.turns and short_key(view.turns[-1]["key"]) in self.rejected_anchors:
            self.pending = None
            self.last_status = "Предыдущий ответ удалён. Не отправляю его повторно; жду новое сообщение."
            return {"status": self.last_status, "ready": False}
        if view.tail_visible is False:
            self.last_status = "Читаю историю. Для ответа прокрутите чат к последним сообщениям."
            return {"status": self.last_status, "ready": False}
        if hasattr(self.native, "can_interact") and not self.native.can_interact():
            self.awaiting_focus = True
            self.last_status = "Диалог читается. Вернитесь к выбранному окну для ответа."
            return {"status": self.last_status, "ready": False}
        self.awaiting_focus = False
        followup = not view.incoming
        if followup:
            observed = time.monotonic()
            if key not in self.outgoing_seen or self.outgoing_times.get(key) != view.last_message_at:
                age = max(0.0, time.time() - view.last_message_at) if view.last_message_at is not None else 0.0
                self.outgoing_seen[key] = observed - age
                self.outgoing_times[key] = view.last_message_at
            started = self.outgoing_seen[key]
            elapsed = max(0.0, observed - started)
            due = self.automatic_followup and elapsed >= self.followup_delay
            if not continue_dialogue and not due:
                remaining = max(0, int(self.followup_delay - elapsed + 0.999))
                self.last_status = f"Жду ответ. До проверки напоминания — {remaining // 60}:{remaining % 60:02d}." if self.automatic_followup else "Последнее сообщение исходящее. Ожидаю ответ."
                return {"status": self.last_status, "ready": bool(self.pending)}
            # Explicit requests may continue a NEW outgoing context. Automatic
            # continuations remain limited until a real incoming message is observed.
            with self.sessions() as s:
                previous = s.scalar(select(Draft).where(Draft.chat_id == chat_id,
                    Draft.status != "screen_not_inserted",
                    Draft.config_hash == digest("screen-followup-v1"), Draft.revision == revision)
                    .order_by(Draft.created_at.desc()).limit(1))
                context = s.get(ConversationContext, chat_id)
                exhausted = context.followup_anchor == (context.last_incoming_key or "no-incoming")
                if previous and previous.status == "handoff":
                    if not continue_dialogue:
                        self.last_status = review_status(previous.reason)
                        return {"status": self.last_status, "ready": False}
                    previous = None  # An explicit retry of an empty result is not a duplicate write.
                elif previous and previous.status == "superseded":
                    previous = None
                elif previous and previous.status == "waiting":
                    self.last_status = "Сейчас продолжение не требуется. Ожидаю ответ."
                    return {"status": self.last_status, "ready": False}
                elif previous and previous.status in {"discarded", "cancelled", "needs_review", "source_missing"}:
                    self.last_status = "Предыдущий ответ отменён или его отправка не подтверждена. Автоповтор отключён."
                    return {"status": self.last_status, "ready": False}
                if previous or (not continue_dialogue and exhausted):
                    self.last_status = "Для этого сообщения продолжение уже подготовлено." if previous else "Ожидаю ответ собеседника. Повторное напоминание не нужно."
                    return {"status": self.last_status, "ready": bool(self.pending)}
        if key in self.handled:
            with self.sessions() as s:
                previous = s.scalar(select(Draft).where(Draft.chat_id == chat_id, Draft.revision == revision)
                                    .order_by(Draft.created_at.desc()).limit(1))
            if previous and previous.status == "handoff":
                if continue_dialogue:
                    self.handled.discard(key)
                else:
                    self.last_status = review_status(previous.reason)
                    return {"status": self.last_status, "ready": False}
            elif previous and previous.status == "needs_review":
                self.last_status = "Появление предыдущего ответа в чате не подтверждено. Автоповтор отключён."
                return {"status": self.last_status, "ready": False}
            else:
                self.last_status = "Этот диалог уже обработан. Ожидаю новое сообщение."
                return {"status": self.last_status, "ready": bool(self.pending)}
        if not continue_dialogue and time.monotonic() - self.last_attempt < 1:
            self.last_frame = None
            return {"status": "Ожидаю перед следующим ответом…", "ready": False}
        self.last_attempt = time.monotonic()
        composer = frame.nodes[view.composer]
        if composer.get("value"):
            self.last_frame = None  # Recheck after the operator clears their text.
            return {"status": "В поле уже есть текст — оставляю его вам.", "ready": False}
        self.handled.add(key)
        previous_anchor = None
        with self.sessions.begin() as s:
            context = s.get(ConversationContext, chat_id)
            if followup:
                previous_anchor = context.followup_anchor
                context.followup_anchor = context.last_incoming_key or "no-incoming"
            draft = Draft(chat_id=chat_id, revision=revision, config_hash=digest("screen-followup-v1" if followup else "screen-v1"), original="", text="",
                          provider=self.provider.name, status="screen_generating",
                          receipt="uia::" + short_key(view.turns[-1]["key"]) + ":" if view.turns else None)
            s.add(draft); s.flush(); draft_id = draft.id
        try:
            guidance = self.personalization.generation_context(chat_id, view.history[-1])
            style = guidance["style"]
            bump = followup and self.autonomous_dialogue
            if bump:
                style += " Decide whether one gentle contextual reminder is useful after a long silence. It is fine to wait. Never guilt, nag, repeat an unanswered question or restart a closed conversation."
            elif followup:
                style += " The last message is YOUR outgoing message, not a new reply from the correspondent. Write one short, natural continuation of the conversation. Do not answer yourself, repeat your last message, invent a reply, or pressure the person for not responding. A relevant gentle question is enough."
            with self.sessions() as s:
                context = s.get(ConversationContext, chat_id)
                history = [{"role": t["role"], "text": t["text"]} for t in context.turns[-16:]] if view.turns else (
                    [{"role": "context", "text": text} for text in view.history[-16:-1]] +
                    [{"role": "outgoing" if followup else "incoming", "text": view.history[-1]}])
            generation_started = time.perf_counter()
            result = self.provider.generate({"account_profile": {"style": style, "mode": "bump" if bump else "followup" if followup else "reply"},
                "knowledge": "", "contact_memory": guidance["memory"], "style_examples": guidance["examples"],
                "rejected_replies": guidance["rejected_replies"], "catalog": [],
                "history": history})
            generated = time.perf_counter()
            if result.action == "wait" and not result.handoff:
                with self.sessions.begin() as s:
                    draft = s.get(Draft, draft_id)
                    draft.status, draft.reason = "waiting", (result.reason or "no_reply_needed")[:120]
                self.last_status = "Сейчас сообщение не требуется. Ожидаю продолжение разговора."
                return {"status": self.last_status, "ready": False}
            if result.handoff or not result.text:
                reason = (result.reason or "").strip()[:120]
                with self.sessions.begin() as s:
                    draft = s.get(Draft, draft_id)
                    draft.status, draft.reason = "handoff", reason or "model_returned_no_reply"
                self.last_status = review_status(reason)
                return {"status": self.last_status, "ready": False}
            if self.stop.is_set():
                raise ScreenError("Остановлено. Ответ не введён.")
            if self.native.capture().revision != frame.revision:
                with self.sessions.begin() as s:
                    draft = s.get(Draft, draft_id)
                    draft.status, draft.reason = "superseded", "new_message_before_input"
                    if followup:
                        s.get(ConversationContext, chat_id).followup_anchor = previous_anchor
                self.handled.discard(key)
                self.last_frame = self.last_view = None
                self.last_status = "Появилось новое сообщение — обновляю ответ…"
                return {"status": self.last_status, "ready": False}
            with self.sessions.begin() as s:
                draft = s.get(Draft, draft_id)
                draft.original = draft.text = result.text
                draft.status = "screen_inserting"
            self.native.insert(frame, binding, result.text, self.stop)
            with self.sessions.begin() as s:
                s.get(Draft, draft_id).status = "screen_inserted"
            self.pending = key, draft_id, frame, binding, result.text
            self.last_metrics = {"capture_seconds": captured - started,
                "generation_seconds": generated - generation_started,
                "insertion_seconds": time.perf_counter() - generated,
                "total_seconds": time.perf_counter() - started}
            self.last_status = f"Ответ введён за {self.last_metrics['total_seconds']:.1f} с. Диалог сохранён."
            if self.auto_send:
                state = self.confirm_send()
                self.last_status = state["status"]
                return {**state, "status": self.last_status}
            return {"status": self.last_status, "ready": binding.get("send") is not None}
        except ContextChanged:
            with self.sessions.begin() as s:
                draft = s.get(Draft, draft_id)
                draft.status, draft.reason = "superseded", "new_message_before_input"
                if followup:
                    s.get(ConversationContext, chat_id).followup_anchor = previous_anchor
            self.handled.discard(key)
            self.last_frame = self.last_view = None
            self.last_status = "Появилось новое сообщение — обновляю ответ…"
            return {"status": self.last_status, "ready": False}
        except AiError:
            with self.sessions.begin() as s:
                draft = s.get(Draft, draft_id)
                draft.status, draft.reason = "screen_not_inserted", "provider_failed_before_input"
                if followup:
                    s.get(ConversationContext, chat_id).followup_anchor = previous_anchor
            self.handled.discard(key)
            self.last_frame = None
            raise
        except InputNotStarted:
            with self.sessions.begin() as s:
                draft = s.get(Draft, draft_id)
                draft.status, draft.reason = "screen_not_inserted", "focus_failed_before_input"
                if followup:
                    s.get(ConversationContext, chat_id).followup_anchor = previous_anchor
            self.handled.discard(key)
            self.last_frame = None
            raise
        except Exception:
            with self.sessions.begin() as s:
                draft = s.get(Draft, draft_id)
                draft.status, draft.reason = "needs_review", "screen_action_requires_review"
            raise

    def confirm_send(self):
        if not self.pending or self.stop.is_set():
            raise ScreenError("Нет актуального текста для подтверждения.")
        _, draft_id, frame, binding, text = self.pending
        self.pending = None
        with self.sessions.begin() as s:
            draft = s.get(Draft, draft_id)
            if draft.status != "screen_inserted":
                raise ScreenError("Эта отправка уже обработана.")
            draft.status = "screen_sending"
        try:
            receipt = self.native.send(frame, binding, text, self.stop)
        except ComposerCleared:
            with self.sessions.begin() as s:
                draft = s.get(Draft, draft_id)
                draft.status, draft.reason = "cancelled", "composer_cleared_before_send"
            self.last_frame = None
            return {"status": "Поле очищено. Отправка отменена; не восстанавливаю удалённый текст.", "ready": False}
        except Exception:
            with self.sessions.begin() as s:
                draft = s.get(Draft, draft_id)
                draft.status, draft.reason = "needs_review", "verify_delivery_in_source"
            raise
        with self.sessions.begin() as s:
            draft = s.get(Draft, draft_id)
            if isinstance(receipt, str) and receipt.startswith("uia:"):
                draft.status, draft.reason, draft.receipt = "sent_observed", "source_message_observed", receipt
            else:
                draft.status, draft.reason = "needs_review", "verify_delivery_in_source"
        self.last_frame = None
        confirmed = isinstance(receipt, str) and receipt.startswith("uia:")
        return {"status": "Ответ появился в переписке." if confirmed else "Отправка вызвана, но появление ответа в чате ещё не подтверждено.", "ready": False}
