"""JSON Schemas of both models, for `aipanel schema` and for validating config.yaml in CI."""

import json
from pathlib import Path
from typing import Any

from aipanel.config.settings import Settings
from aipanel.config.site import SiteConfig


def schemas() -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for filename, model in (
        ("config.schema.json", SiteConfig),
        ("settings.schema.json", Settings),
    ):
        schema = model.model_json_schema(by_alias=True)
        out[filename] = {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            **schema,
        }
    return out


def render(schema: dict[str, Any]) -> str:
    return json.dumps(schema, indent=2, ensure_ascii=False) + "\n"


def write(directory: Path) -> list[Path]:
    directory.mkdir(parents=True, exist_ok=True)
    written = []
    for filename, schema in schemas().items():
        path = directory / filename
        path.write_text(render(schema), encoding="utf-8")
        written.append(path)
    return written
