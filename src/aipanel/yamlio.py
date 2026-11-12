"""YAML with YAML 1.2's plain scalars.

PyYAML follows YAML 1.1, where `on` and `off` are booleans and `23:00` is the number 1380.
The schedule's rules have keys named `on` and `off` and times like `23:00`, so both files are
read with only true/false as booleans and no base-60 numbers.
"""

import re
from typing import Any

import yaml

BOOL = "tag:yaml.org,2002:bool"
INT = "tag:yaml.org,2002:int"


class Loader(yaml.SafeLoader):
    pass


Loader.yaml_implicit_resolvers = {
    first: [(tag, regexp) for tag, regexp in resolvers if tag not in (BOOL, INT)]
    for first, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
}
Loader.add_implicit_resolver(
    BOOL, re.compile(r"^(?:true|True|TRUE|false|False|FALSE)$"), list("tTfF")
)
Loader.add_implicit_resolver(
    INT,
    re.compile(r"^(?:[-+]?(?:0|[1-9][0-9_]*)|0o[0-7]+|0x[0-9a-fA-F]+)$"),
    list("-+0123456789"),
)


def load(text: str) -> Any:
    """Raises ValueError for text that is not YAML."""
    try:
        return yaml.load(text, Loader=Loader)  # noqa: S506 - a SafeLoader subclass
    except yaml.YAMLError as e:
        raise ValueError(f"not valid YAML: {e}") from None


def dump(data: Any) -> str:
    return yaml.safe_dump(data, sort_keys=False, allow_unicode=True)
