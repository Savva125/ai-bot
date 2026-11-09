"""Small settings dialog; no parallel chat interface or message sending."""
from PySide6.QtCore import Signal, Qt
from PySide6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPlainTextEdit,
    QPushButton, QTabWidget, QWidget, QListWidget, QListWidgetItem)


class PersonalizationDialog(QDialog):
    save_requested = Signal(dict)
    export_requested = Signal(object)

    def __init__(self, data, parent=None):
        super().__init__(parent)
        self.data = data
        self.example_id = self.draft_id = None
        self.deleted_ids = []
        self.busy = False
        self.setWindowTitle("Стиль и память AI")
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)
        self.resize(590, 650)
        layout = QVBoxLayout(self)
        self.tabs = QTabWidget()
        layout.addWidget(self.tabs)
        style_page, examples_page, memory_page = QWidget(), QWidget(), QWidget()
        self.tabs.addTab(style_page, "Стиль")
        self.tabs.addTab(examples_page, "Примеры")
        self.tabs.addTab(memory_page, "Память")
        style_layout = QVBoxLayout(style_page)
        style_layout.addWidget(QLabel("Как AI должен общаться · общий стиль для выбранной базы"))
        self.style = QPlainTextEdit(data["style"])
        self.style.setPlaceholderText("Тон, длина ответов, эмодзи, что нравится и чего избегать…")
        style_layout.addWidget(self.style)
        hint = QLabel("Изменения учитываются при следующих генерациях. Сохранение примеров помогает подбирать ответы; веса модели автоматически не меняются.")
        hint.setWordWrap(True)
        style_layout.addWidget(hint)
        examples_layout = QVBoxLayout(examples_page)
        examples_layout.addWidget(QLabel("Примеры стиля для текущего аккаунта"))
        self.examples = QListWidget()
        self.examples.setMaximumHeight(105)
        for example in data["examples"]:
            item = QListWidgetItem(example["context"].replace("\n", " ")[:75])
            item.setData(Qt.ItemDataRole.UserRole, example)
            self.examples.addItem(item)
        self.examples.currentItemChanged.connect(self.select_example)
        examples_layout.addWidget(self.examples)
        row = QHBoxLayout()
        self.new_button = QPushButton("Новый пример")
        self.new_button.clicked.connect(self.new_example)
        row.addWidget(self.new_button)
        self.correct_button = QPushButton("Исправить последний ответ")
        self.correct_button.setEnabled(bool(data["latest_draft"]))
        self.correct_button.clicked.connect(self.correct_latest)
        row.addWidget(self.correct_button)
        self.remove_button = QPushButton("Удалить")
        self.remove_button.clicked.connect(self.remove_example)
        row.addWidget(self.remove_button)
        examples_layout.addLayout(row)
        examples_layout.addWidget(QLabel("Ситуация или несколько предыдущих реплик"))
        self.context = QPlainTextEdit()
        self.context.setMaximumHeight(90)
        examples_layout.addWidget(self.context)
        self.proposed = QPlainTextEdit()
        self.proposed.setReadOnly(True)
        self.proposed.setPlaceholderText("Здесь появится исходный ответ AI при исправлении")
        self.proposed.setMaximumHeight(55)
        examples_layout.addWidget(self.proposed)
        examples_layout.addWidget(QLabel("Как следовало ответить"))
        self.reply = QPlainTextEdit()
        self.reply.setMaximumHeight(90)
        examples_layout.addWidget(self.reply)
        self.note = QPlainTextEdit()
        self.note.setPlaceholderText("Почему этот вариант лучше · необязательно")
        self.note.setMaximumHeight(55)
        examples_layout.addWidget(self.note)
        self.export_button = QPushButton("Экспорт сохранённых примеров в JSONL на F:")
        self.export_button.clicked.connect(lambda: self.export_requested.emit(data["chat_id"]))
        examples_layout.addWidget(self.export_button)
        memory_layout = QVBoxLayout(memory_page)
        contact = data["contact"] or "Диалог пока не выбран"
        title = QLabel("Собеседник: " + contact)
        title.setTextFormat(Qt.TextFormat.PlainText)
        title.setWordWrap(True)
        memory_layout.addWidget(title)
        self.memory = QPlainTextEdit(data["memory"])
        self.memory.setPlaceholderText("Предпочтения, интересы, важные детали и границы собеседника…")
        memory_layout.addWidget(self.memory)
        memory_layout.addWidget(QLabel("Заметки используются только в диалоге этого собеседника."))
        if not data["chat_id"]:
            examples_page.setEnabled(False)
            memory_page.setEnabled(False)
            message = QLabel("Для примеров и памяти сначала выберите окно и распознайте диалог.")
            message.setWordWrap(True)
            layout.addWidget(message)
        self.notice = QLabel()
        self.notice.setWordWrap(True)
        self.notice.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self.notice)
        buttons = QHBoxLayout()
        cancel = QPushButton("Отмена")
        cancel.clicked.connect(self.reject)
        buttons.addWidget(cancel)
        self.save_button = QPushButton("Сохранить")
        self.save_button.setObjectName("primary")
        self.save_button.clicked.connect(self.save)
        buttons.addWidget(self.save_button)
        layout.addLayout(buttons)

    def new_example(self):
        self.examples.setCurrentRow(-1)
        self.example_id = self.draft_id = None
        for field in (self.context, self.proposed, self.reply, self.note):
            field.clear()

    def select_example(self, item, previous=None):
        if not item:
            return
        example = item.data(Qt.ItemDataRole.UserRole)
        self.example_id, self.draft_id = example["id"], None
        for field, key in ((self.context, "context"), (self.proposed, "proposed"), (self.reply, "reply"), (self.note, "note")):
            field.setPlainText(example[key])

    def correct_latest(self):
        self.new_example()
        draft = self.data["latest_draft"]
        self.draft_id = draft["id"]
        self.context.setPlainText(draft["context"])
        self.proposed.setPlainText(draft["reply"])
        self.reply.setPlainText(draft["reply"])

    def remove_example(self):
        item = self.examples.currentItem()
        if item:
            self.deleted_ids.append(item.data(Qt.ItemDataRole.UserRole)["id"])
            self.examples.takeItem(self.examples.row(item))
            self.new_example()

    def save(self):
        context, reply = self.context.toPlainText().strip(), self.reply.toPlainText().strip()
        if bool(context) != bool(reply):
            self.notice.setText("Для примера заполните и ситуацию, и хороший ответ.")
            return
        example = {"id": self.example_id, "draft_id": self.draft_id, "context": context,
                   "reply": reply, "note": self.note.toPlainText()} if context and reply else None
        self.set_busy(True)
        self.save_requested.emit({"style": self.style.toPlainText(), "chat_id": self.data["chat_id"],
            "memory": self.memory.toPlainText(), "example": example, "delete_ids": self.deleted_ids})

    def set_busy(self, busy):
        self.busy = busy
        self.save_button.setEnabled(not busy)
        self.tabs.setEnabled(not busy)

    def show_error(self, text):
        self.set_busy(False)
        self.notice.setText(text)

    def exported(self, result):
        self.set_busy(False)
        self.notice.setText(f"Экспортировано: {result['count']}.\n{result['path']}")

    def reject(self):
        if not self.busy:
            super().reject()
