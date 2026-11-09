"""Explicit server migrations; never touch the desktop conversation database."""
import argparse
from pathlib import Path
from alembic import command
from alembic.config import Config
from creator_desk.db import connect
from .config import Settings


def migrate(engine):
    config = Config()
    config.set_main_option("script_location", str(Path(__file__).parent / "migrations"))
    with engine.begin() as connection:
        config.attributes["connection"] = connection
        command.upgrade(config, "head")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["migrate"])
    parser.parse_args()
    settings = Settings.from_env()
    engine, _ = connect(settings.database_url)
    try:
        migrate(engine)
        print("Server database upgraded.")
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
