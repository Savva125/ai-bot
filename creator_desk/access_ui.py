"""Activation UI runs network requests outside the Qt event thread."""
from datetime import datetime, timezone
import json
import os
from pathlib import Path

from PySide6.QtCore import QThreadPool, Qt
from PySide6.QtWidgets import (QDialog, QVBoxLayout, QLabel, QLineEdit, QPushButton)

from .remote_ai import RemoteProvider, validate_server_url
from .errors import AiError
from .overlay import Work, STYLE


def data_directory():
    folder = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / ".local" / "share"))) / "CreatorDesk"
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def configured_url():
    if os.getenv("CREATOR_DESK_SERVER_URL"):
        return os.environ["CREATOR_DESK_SERVER_URL"]
    path = data_directory() / "server.json"
    if path.is_file():
        return json.loads(path.read_text(encoding="utf-8"))["url"]
    # Bundled public endpoint, set by the owner before building the client.
    bundled = Path(__file__).with_name("client-config.json")
    return json.loads(bundled.read_text(encoding="utf-8")).get("server_url", "") if bundled.is_file() else ""


class AccessDialog(QDialog):
    def __init__(self, url="", parent=None):
        super().__init__(parent)
        self.provider = None
        self.job = None
        self.pool = QThreadPool(self)
        self.setWindowTitle("AI-помощник · Доступ по ключу")
        self.setMinimumWidth(430)
        self.setStyleSheet(STYLE)
        layout = QVBoxLayout(self)
        label = QLabel("Введите ключ, полученный у администратора.\nПереписка, нужная для ответа, обрабатывается на сервере AI.")
        label.setWordWrap(True)
        layout.addWidget(label)
        layout.addWidget(QLabel("Сервер"))
        self.url = QLineEdit(url)
        self.url.setPlaceholderText("https://ai.example.com")
        layout.addWidget(self.url)
        layout.addWidget(QLabel("Ключ доступа"))
        self.key = QLineEdit()
        self.key.setEchoMode(QLineEdit.EchoMode.Password)
        self.key.setPlaceholderText("Ключ от администратора")
        layout.addWidget(self.key)
        self.status = QLabel("Срок доступа начинается при первой активации.")
        self.status.setWordWrap(True)
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self.status)
        self.button = QPushButton("Активировать")
        self.button.setObjectName("primary")
        self.button.clicked.connect(self.activate)
        self.key.returnPressed.connect(self.activate)
        layout.addWidget(self.button)

    def activate(self):
        if self.job:
            return
        try:
            url = validate_server_url(self.url.text())
            if not self.key.text().strip():
                raise AiError("Введите ключ доступа.")
            provider = RemoteProvider.load(url)
        except (AiError, RuntimeError, ValueError):
            self.status.setText("Проверьте адрес сервера, ключ и доступ к хранилищу Windows.")
            return
        key = self.key.text()
        self.button.setEnabled(False)
        self.url.setEnabled(False)
        self.key.setEnabled(False)
        self.status.setText("Проверяю ключ…")

        def work():
            result = provider.activate_sync(key)
            path = data_directory() / "server.json"
            temporary = path.with_suffix(".tmp")
            temporary.write_text(json.dumps({"url": url}), encoding="utf-8")
            temporary.replace(path)
            return provider, result

        self.job = Work(work)
        self.job.events.result.connect(self.activated)
        self.job.events.error.connect(self.failed)
        self.job.events.finished.connect(self.finished)
        self.pool.start(self.job)

    def activated(self, value):
        self.provider, result = value
        self.key.clear()
        self.status.setText("Доступ активирован до " + datetime.fromtimestamp(result["expires_at"], timezone.utc).astimezone().strftime("%d.%m.%Y %H:%M"))

    def failed(self, message):
        self.status.setText(message)

    def finished(self):
        self.job = None
        self.button.setEnabled(True)
        self.url.setEnabled(True)
        self.key.setEnabled(True)
        if self.provider:
            self.accept()

    def reject(self):
        if not self.job:
            super().reject()

    def closeEvent(self, event):
        if self.job:
            event.ignore()
        else:
            event.accept()
