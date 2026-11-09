"""Synthetic hosted-gateway check. Never reads or sends real user conversations."""
import argparse
import asyncio
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from creator_desk_server.config import Settings
from creator_desk_server.inference import ModelGateway


async def verify(gateway):
    result = await gateway.generate({"history": [{"role": "incoming", "text": "Привет! Люблю джаз на пианино."}],
        "account_profile": {"style": "Коротко, дружелюбно, по-русски", "mode": "reply"}})
    assert result["action"] == "reply" and result["text"] and not result["handoff"], result
    print("Synthetic reply:", json.dumps(result, ensure_ascii=False))
    screen = json.dumps({"format": "[id,kind,text,editable]", "elements": [
        [10, "Text", "Анна", False], [11, "Text", "Анна: Привет! Как дела?", False],
        [12, "Edit", "Сообщение…", True], [13, "Button", "Отправить", False]]}, ensure_ascii=False)
    inspected = await gateway.inspect_screen(screen)
    assert inspected["is_chat"] and inspected["contact"] == 10 and inspected["composer"] == 12, inspected
    print("Synthetic inspection:", json.dumps(inspected, ensure_ascii=False))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--model-url", default="http://127.0.0.1:8081/v1")
    parser.add_argument("--model-name", default="creator-desk")
    args = parser.parse_args()
    settings = Settings("postgresql+psycopg://unused", "test" * 12,
                        model_url=args.model_url, model_name=args.model_name)
    settings.validate()
    asyncio.run(verify(ModelGateway(settings)))


if __name__ == "__main__":
    main()
