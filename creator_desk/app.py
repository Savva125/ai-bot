from __future__ import annotations

import argparse
from pathlib import Path
import sys

from sqlalchemy import inspect

from .db import connect
from .models import Base
from .providers import DemoProvider
from .screen_agent import ScreenAssistant


def migrate(engine):
    from alembic import command
    from alembic.config import Config
    config = Config()
    location = Path(__file__).resolve().parent.parent / "migrations"
    if not location.is_dir():
        location = Path(sys.prefix) / "migrations"
    config.set_main_option("script_location", str(location))
    with engine.begin() as connection:
        config.attributes["connection"] = connection
        command.upgrade(config, "head")


def prepare_client_database(engine):
    """Adopt the previous unversioned SQLite schema only after exact verification."""
    tables = inspect(engine).get_table_names()
    if tables and "alembic_version" not in tables:
        from alembic.autogenerate import compare_metadata
        from alembic.migration import MigrationContext
        from alembic import command
        from alembic.config import Config
        import sqlite3
        with engine.connect() as connection:
            differences = compare_metadata(MigrationContext.configure(connection), Base.metadata)
        if differences:
            raise RuntimeError("Existing local database schema differs; preserve it and migrate explicitly")
        path = Path(engine.url.database)
        backup = path.with_name(path.name + ".before-hosted.bak")
        if backup.exists():
            # Never overwrite the first pre-transition backup.
            from uuid import uuid4
            backup = path.with_name(path.name + ".before-hosted-" + uuid4().hex + ".bak")
        with sqlite3.connect(str(path)) as source, sqlite3.connect(str(backup)) as destination:
            source.backup(destination)
        cfg = Config()
        location = Path(__file__).resolve().parent.parent / "migrations"
        cfg.set_main_option("script_location", str(location if location.is_dir() else Path(sys.prefix) / "migrations"))
        with engine.begin() as connection:
            cfg.attributes["connection"] = connection
            command.stamp(cfg, "head")
    migrate(engine)


def _main():
    parser = argparse.ArgumentParser(description="Compact assistant for a user-selected window")
    parser.add_argument("--demo", action="store_true", help="Use local SQLite storage; no simulated chat or messaging")
    parser.add_argument("--demo-db", default="screen-history.db")
    parser.add_argument("--server-url", help="Hosted AI API HTTPS origin")
    parser.add_argument("--activate", action="store_true", help="Open key activation even if already configured")
    parser.add_argument("--client-db", help="Local conversation database (existing databases are preserved)")
    parser.add_argument("--smoke-test", action="store_true", help="With --demo only: show UI briefly and exit without reading other windows")
    parser.add_argument("--inspect-screen", action="store_true", help="Read-only screen diagnostics; never generate, insert or send")
    parser.add_argument("--inspect-focus", action="store_true", help="Check selected window/editor focus without typing or sending")
    args = parser.parse_args()
    if args.smoke_test and not args.demo:
        parser.error("--smoke-test requires --demo")
    # Normal mode always stores UI history locally; server credentials never reach the client.
    if not args.demo:
        from .access_ui import data_directory
        existing = Path(__file__).resolve().parent.parent / "screen-history.db"
        path = Path(args.client_db) if args.client_db else existing if existing.exists() else data_directory() / "history.db"
        engine, sessions = connect("sqlite:///" + str(path.resolve()))
        prepare_client_database(engine)
        storage = "Локальная история · AI на сервере"
    elif args.demo:
        engine, sessions = connect("sqlite:///" + str(Path(args.demo_db).resolve()))
        Base.metadata.create_all(engine)  # Only explicit SQLite sandbox.
        storage = "SQLite sandbox"
    from PySide6.QtWidgets import QApplication, QDialog
    app = QApplication(sys.argv[:1])
    if args.demo or args.inspect_screen or args.inspect_focus:
        provider = DemoProvider()
    else:
        from .access_ui import AccessDialog, configured_url
        from .remote_ai import RemoteProvider
        url = args.server_url or configured_url()
        provider = RemoteProvider.load(url) if url else None
        if args.activate or provider is None or not provider.license_key:
            dialog = AccessDialog(url)
            if dialog.exec() != QDialog.DialogCode.Accepted:
                engine.dispose()
                return
            provider = dialog.provider
    service = ScreenAssistant(sessions, provider)
    if args.inspect_screen or args.inspect_focus:
        from .access_ui import data_directory
        service.inspect_only = True
        service.inspect_focus = args.inspect_focus
        service.native.diagnostics_path = data_directory() / "diagnostics" / "screen.json"
    if hasattr(provider, "stop_event"):
        provider.stop_event = service.stop
    try:
        service.recover()
        from .overlay import OverlayWindow
        window = OverlayWindow(service, storage)
        if args.inspect_screen or args.inspect_focus:
            window.setWindowTitle("AI-помощник — проверка чтения")
        window.show()
        if args.smoke_test:
            from PySide6.QtCore import QTimer
            QTimer.singleShot(350, window.close)
        sys.exit(app.exec())
    finally:
        service.stop.set()
        engine.dispose()


def main():
    try:
        _main()
    except Exception as exc:
        from .errors import AiError
        message = str(exc) if isinstance(exc, AiError) else "Не удалось запустить приложение (" + type(exc).__name__ + "). Проверьте настройки сервера, локальную базу и хранилище Windows."
        if sys.stderr is not None:
            print(message, file=sys.stderr)
        from PySide6.QtWidgets import QApplication, QMessageBox
        error_app = QApplication.instance()
        if error_app is None and getattr(sys, "frozen", False):
            error_app = QApplication(sys.argv[:1])
        if error_app:
            QMessageBox.critical(None, "AI-помощник", message)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
