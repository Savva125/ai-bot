from dataclasses import dataclass
import os
from urllib.parse import urlsplit


@dataclass(frozen=True)
class Settings:
    database_url: str
    admin_token: str
    model_url: str = "http://127.0.0.1:8081/v1"
    model_name: str = "creator-desk"
    model_api_key: str = ""
    model_parallel: int = 1
    model_timeout: float = 180
    allow_sqlite: bool = False

    def validate(self):
        if len(self.admin_token) < 32:
            raise ValueError("ADMIN_API_TOKEN must contain at least 32 characters")
        if not self.database_url.startswith("postgresql+psycopg://") and not (
                self.allow_sqlite and self.database_url.startswith("sqlite")):
            raise ValueError("SERVER_DATABASE_URL must use postgresql+psycopg")
        parsed = urlsplit(self.model_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("Invalid MODEL_URL")
        if parsed.scheme == "http" and parsed.hostname not in {"127.0.0.1", "localhost", "::1", "model"}:
            raise ValueError("Remote model endpoints require HTTPS")
        if not 1 <= self.model_parallel <= 32 or not 1 <= self.model_timeout <= 300:
            raise ValueError("Invalid model concurrency or timeout")

    @classmethod
    def from_env(cls):
        settings = cls(
            database_url=os.environ["SERVER_DATABASE_URL"],
            admin_token=os.environ["ADMIN_API_TOKEN"],
            model_url=os.getenv("MODEL_URL", "http://127.0.0.1:8081/v1").rstrip("/"),
            model_name=os.getenv("MODEL_NAME", "creator-desk"),
            model_api_key=os.getenv("MODEL_API_KEY", ""),
            model_parallel=int(os.getenv("MODEL_PARALLEL", "1")),
            model_timeout=float(os.getenv("MODEL_TIMEOUT", "180")),
        )
        settings.validate()
        return settings
