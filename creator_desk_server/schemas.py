from typing import Literal
from uuid import UUID
from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Activation(StrictModel):
    key: str = Field(min_length=32, max_length=128)
    device_id: str = Field(min_length=32, max_length=128, pattern=r"^[a-zA-Z0-9_-]+$")


class Turn(StrictModel):
    role: Literal["incoming", "outgoing", "context"]
    text: str = Field(max_length=4000)
    key: str | None = Field(default=None, max_length=256)


class Example(StrictModel):
    context: str = Field(default="", max_length=4000)
    reply: str = Field(max_length=2000)
    note: str = Field(default="", max_length=500)


class Context(StrictModel):
    history: list[Turn] = Field(max_length=80)
    account_profile: dict = Field(default_factory=dict)
    knowledge: str = Field(default="", max_length=6000)
    contact_memory: str = Field(default="", max_length=4000)
    style_examples: list[Example] = Field(default_factory=list, max_length=10)
    rejected_replies: list[str] = Field(default_factory=list, max_length=10)
    catalog: list = Field(default_factory=list, max_length=0)


class Generation(StrictModel):
    request_id: str = Field(max_length=36)
    context: Context

    @model_validator(mode="after")
    def valid_id(self):
        UUID(self.request_id)
        return self


class Draft(StrictModel):
    text: str = Field(max_length=4000)
    handoff: bool
    reason: str = Field(default="", max_length=500)
    action: Literal["reply", "wait"] = "reply"

    @model_validator(mode="after")
    def valid_action(self):
        if self.handoff or self.action == "wait":
            self.text = ""
        elif not self.text.strip():
            raise ValueError("Empty reply")
        return self


class Inspection(StrictModel):
    request_id: str = Field(max_length=36)
    screen: str = Field(min_length=1, max_length=8500)

    @model_validator(mode="after")
    def valid_screen(self):
        import json
        UUID(self.request_id)
        value = json.loads(self.screen)
        if not isinstance(value, dict):
            raise ValueError("Invalid screen object")
        rows = value.get("elements")
        if not isinstance(rows, list) or not 1 <= len(rows) <= 300:
            raise ValueError("Invalid screen elements")
        ids = set()
        for row in rows:
            if (not isinstance(row, list) or len(row) != 4 or type(row[0]) is not int
                    or not 0 <= row[0] <= 12000 or row[0] in ids
                    or not isinstance(row[1], str)
                    or row[1] not in {"Text", "Message", "Edit", "Document", "Button", "Hyperlink", "ListItem"}
                    or not isinstance(row[2], str) or len(row[2]) > 180 or type(row[3]) is not bool):
                raise ValueError("Invalid screen element")
            ids.add(row[0])
        return self


class InspectedChat(StrictModel):
    is_chat: bool
    account: int | None
    contact: int | None
    history: list[int] = Field(max_length=50)
    incoming: bool
    composer: int | None
    send: int | None


class CreateLicense(StrictModel):
    days: int = Field(default=30, ge=1, le=3650)
    daily_limit: int = Field(default=500, ge=1, le=100000)
    max_devices: int = Field(default=1, ge=1, le=10)
    label: str = Field(default="", max_length=120)


class ExtendLicense(StrictModel):
    days: int = Field(ge=1, le=3650)
