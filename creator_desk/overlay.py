"""Compact always-on-top controller. The conversation stays in the source app."""
from __future__ import annotations

import os
from pathlib import Path

from PySide6.QtCore import QObject, QRunnable, QThreadPool, QTimer, Signal, Qt
from PySide6.QtGui import QFont, QFontDatabase, QKeySequence, QShortcut
from PySide6.QtWidgets import QApplication, QWidget, QVBoxLayout, QLabel, QPushButton, QDialog, QListWidget, QListWidgetItem

from .screen_agent import ScreenError
from .errors import AiError
from .providers import DemoProvider
from .personalization import PersonalizationError
from .personalization_ui import PersonalizationDialog


STYLE = """
QWidget { background:#17191F; color:#E9EAF1; font-family:'Segoe UI'; font-size:12px; }
QLabel#status { color:#ADB2C3; padding:5px 0; font-size:12px; }
QPushButton { background:#252832; border:1px solid #363A48; border-radius:8px; padding:10px 12px; color:#D9DBE6; }
QPushButton:hover { background:#303442; border-color:#706392; }
QPushButton#primary { background:#8068DC; border-color:#8068DC; color:white; font-weight:600; }
QPushButton#primary:hover { background:#937AEF; }
QPushButton:disabled { color:#646979; background:#22252C; border-color:#2A2D36; }
QListWidget { background:#20232B; border:1px solid #343844; border-radius:8px; padding:5px; }
QListWidget::item { padding:12px; border-radius:6px; }
QListWidget::item:selected { background:#574879; color:white; }
QPlainTextEdit { background:#20232B; border:1px solid #343844; border-radius:6px; padding:8px; }
QTabWidget::pane { border:1px solid #343844; } QTabBar::tab { padding:9px 14px; }
"""


class Events(QObject):
    result = Signal(object)
    error = Signal(str)
    finished = Signal()


class Work(QRunnable):
    def __init__(self, fn):
        super().__init__()
        self.fn, self.events = fn, Events()

    def run(self):
        try:
            self.events.result.emit(self.fn())
        except (ScreenError, AiError, PersonalizationError) as error:
            self.events.error.emit(str(error))
        except Exception as error:
            self.events.error.emit("Операция остановлена: " + type(error).__name__ + ". Проверьте выбранное окно и модель.")
        finally:
            self.events.finished.emit()


class OverlayWindow(QWidget):
    """One-button autonomous controller; source selection happens at Start."""
    def __init__(self, assistant, storage_label="БД"):
        super().__init__()
        if os.name == "nt" and not QFontDatabase.families():
            font_id = QFontDatabase.addApplicationFont(str(Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts" / "segoeui.ttf"))
            families = QFontDatabase.applicationFontFamilies(font_id)
            if families:
                QApplication.instance().setFont(QFont(families[0], 10))
        self.assistant = assistant
        self.running = self.connected = self.selecting = False
        self.jobs = set()
        self.pool = QThreadPool(self)
        self.pool.setMaxThreadCount(1)
        self.pool.setExpiryTimeout(-1)
        self.setWindowFlags(Qt.WindowType.Window | Qt.WindowType.WindowStaysOnTopHint)
        self.setWindowTitle("AI-помощник")
        self.setFixedWidth(320)
        self.setMinimumHeight(145)
        self.setStyleSheet(STYLE)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 18, 18, 14)
        layout.setSpacing(10)
        self.start_button = QPushButton("Начать работу")
        self.start_button.setObjectName("primary")
        self.start_button.setMinimumHeight(48)
        self.start_button.clicked.connect(self.toggle_work)
        layout.addWidget(self.start_button)
        self.status = QLabel("После запуска выберите окно с диалогом.")
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        self.status.setObjectName("status")
        self.status.setWordWrap(True)
        self.status.setMinimumHeight(38)
        layout.addWidget(self.status)
        self.setToolTip(storage_label)
        # Existing learning data remains editable without adding main-screen controls.
        self.settings_shortcut = QShortcut(QKeySequence("Ctrl+,"), self)
        self.settings_shortcut.activated.connect(self.open_personalization)
        self.access_shortcut = QShortcut(QKeySequence("Ctrl+Shift+L"), self)
        self.access_shortcut.activated.connect(self.open_access)
        if isinstance(assistant.provider, DemoProvider) and not assistant.inspect_only:
            self.start_button.setEnabled(False)
            self.status.setText("Тестовый режим: автоматическая работа отключена.")
        self.timer = QTimer(self)
        self.timer.setInterval(1000)
        self.timer.timeout.connect(self.tick)
        self.timer.start()

    def toggle_work(self):
        if self.running or self.selecting:
            self.pause()
        elif not self.jobs:
            self.choose_window()

    def submit(self, fn, callback, error_callback=None):
        job = Work(fn)
        self.jobs.add(job)
        job.events.result.connect(callback)
        job.events.error.connect(error_callback or self.failed)
        job.events.finished.connect(lambda: self.finished(job))
        self.pool.start(job)

    def finished(self, job):
        self.jobs.discard(job)
        self.start_button.setEnabled((not isinstance(self.assistant.provider, DemoProvider) or self.assistant.inspect_only)
                                     and (not self.jobs or self.running or self.selecting))

    def failed(self, text):
        self.pause()
        self.status.setText(text)

    def choose_window(self):
        if self.jobs or (isinstance(self.assistant.provider, DemoProvider) and not self.assistant.inspect_only):
            return
        self.pause()
        self.selecting = True
        self.start_button.setEnabled(True)
        self.start_button.setText("Остановить")
        self.status.setText("Ищу открытые окна…")
        self.submit(self.assistant.native.windows, self.show_targets)

    def show_targets(self, windows):
        if not self.selecting:
            return
        if not windows:
            self.failed("Откройте приложение с чатом и нажмите «Начать работу».")
            return
        dialog = QDialog(self)
        dialog.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)
        dialog.setWindowTitle("Где работать")
        dialog.resize(540, 390)
        dialog.setStyleSheet(STYLE)
        content = QVBoxLayout(dialog)
        hint = QLabel("Выберите окно. В браузере заранее откройте нужную вкладку с чатом.")
        hint.setWordWrap(True)
        content.addWidget(hint)
        choices = QListWidget()
        for window in windows:
            item = QListWidgetItem(window["title"] + "\n" + window["app"])
            item.setData(Qt.ItemDataRole.UserRole, window)
            choices.addItem(item)
        content.addWidget(choices)
        confirm = QPushButton("Работать здесь")
        confirm.setEnabled(False)
        choices.currentItemChanged.connect(lambda item, _: confirm.setEnabled(item is not None))
        confirm.clicked.connect(dialog.accept)
        content.addWidget(confirm)
        if dialog.exec() == QDialog.DialogCode.Accepted and choices.currentItem() and self.selecting:
            selected = choices.currentItem().data(Qt.ItemDataRole.UserRole)
            self.submit(lambda: self.assistant.choose(selected["handle"]), lambda _: self.target_chosen(selected))
        else:
            self.pause()

    def target_chosen(self, selected):
        if not self.selecting:
            self.assistant.stop.set()
            return
        self.connected, self.selecting = True, False
        self.setWindowTitle("AI-помощник · " + selected["title"][:70])
        self.start()

    def start(self):
        if not self.connected or (isinstance(self.assistant.provider, DemoProvider) and not self.assistant.inspect_only):
            return
        autonomous = not self.assistant.inspect_only
        self.assistant.auto_send = autonomous
        self.assistant.automatic_followup = autonomous
        self.assistant.autonomous_dialogue = autonomous
        self.assistant.followup_delay = 300
        self.assistant.stop.clear()
        self.assistant.outgoing_seen.clear()
        self.assistant.outgoing_times.clear()
        self.running = True
        self.start_button.setEnabled(True)
        self.start_button.setText("Остановить")
        self.status.setText("Читаю диалог…")
        QTimer.singleShot(100, self.tick)

    def pause(self):
        self.assistant.stop.set()
        self.assistant.outgoing_seen.clear()
        self.assistant.outgoing_times.clear()
        self.running = self.selecting = False
        self.start_button.setText("Начать работу")
        self.start_button.setEnabled(not self.jobs and (not isinstance(self.assistant.provider, DemoProvider) or self.assistant.inspect_only))
        self.status.setText("Остановлено.")

    def tick(self):
        if self.running and not self.jobs:
            self.submit(self.assistant.step, self.update_state)

    def update_state(self, state):
        if self.running:
            self.status.setText(state["status"])

    def open_personalization(self):
        if self.jobs:
            return
        self.pause()
        self.submit(self.assistant.personalization_state, self.show_personalization)

    def open_access(self):
        if self.jobs:
            return
        self.pause()
        from .remote_ai import RemoteProvider
        from .access_ui import AccessDialog
        if isinstance(self.assistant.provider, RemoteProvider):
            dialog = AccessDialog(self.assistant.provider.server_url, self)
            if dialog.exec() == QDialog.DialogCode.Accepted:
                self.assistant.provider = dialog.provider
                if hasattr(self.assistant.interpreter, "provider"):
                    self.assistant.interpreter.provider = dialog.provider
                dialog.provider.stop_event = self.assistant.stop
                self.status.setText("Доступ обновлён. Нажмите «Начать работу».")

    def show_personalization(self, data):
        dialog = PersonalizationDialog(data, self)
        dialog.setStyleSheet(STYLE)
        def saved(message):
            dialog.set_busy(False)
            dialog.accept()
            self.status.setText(message + " Нажмите «Начать работу».")
        dialog.save_requested.connect(lambda payload: self.submit(
            lambda: self.assistant.personalization.save(payload), saved, dialog.show_error))
        def export(chat_id):
            dialog.set_busy(True)
            self.submit(lambda: self.assistant.personalization.export(chat_id), dialog.exported, dialog.show_error)
        dialog.export_requested.connect(export)
        dialog.exec()

    def closeEvent(self, event):
        self.pause()
        if self.jobs:
            self.status.setText("Останавливаю…")
            event.ignore()
            QTimer.singleShot(300, self.close)
        else:
            self.timer.stop()
            event.accept()
