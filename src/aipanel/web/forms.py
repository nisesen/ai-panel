"""The settings page's form, generated from the Settings model: one input per plain field,
with the field's description as help and its limits as min and max. Lists (the schedule's
rules) have their own page.
"""

import types
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal, Union, get_args, get_origin

from pydantic import BaseModel
from pydantic.fields import FieldInfo

from aipanel.config.settings import Settings

Kind = Literal["bool", "int", "float", "choice", "text"]


@dataclass(frozen=True)
class FormField:
    name: str  # dotted: section.field
    label: str
    kind: Kind
    value: Any
    help: str
    optional: bool = False
    minimum: float | None = None
    maximum: float | None = None
    choices: tuple[str, ...] = ()


@dataclass(frozen=True)
class Section:
    name: str
    title: str
    fields: tuple[FormField, ...]


def _kind(annotation: Any) -> tuple[Kind | None, bool, tuple[str, ...]]:
    """The input kind for a field's type, whether it may be empty, and its choices."""
    optional = False
    if get_origin(annotation) in (Union, types.UnionType):
        args = [a for a in get_args(annotation) if a is not type(None)]
        optional = len(args) < len(get_args(annotation))
        if len(args) != 1:
            return None, optional, ()
        annotation = args[0]
    if get_origin(annotation) is Literal:
        return "choice", optional, tuple(str(a) for a in get_args(annotation))
    simple: dict[Any, Kind] = {bool: "bool", int: "int", float: "float", str: "text"}
    return simple.get(annotation), optional, ()


def _limits(info: FieldInfo) -> tuple[float | None, float | None]:
    low = high = None
    for item in info.metadata:
        low = getattr(item, "ge", None) if getattr(item, "ge", None) is not None else low
        high = getattr(item, "le", None) if getattr(item, "le", None) is not None else high
    return low, high


def sections(settings: Settings) -> list[Section]:
    out = []
    for name, info in Settings.model_fields.items():
        section_model = info.annotation
        if not (isinstance(section_model, type) and issubclass(section_model, BaseModel)):
            continue
        values: BaseModel = getattr(settings, name)
        fields = []
        for field_name, field_info in section_model.model_fields.items():
            kind, optional, choices = _kind(field_info.annotation)
            if kind is None:
                continue  # a list or a nested model: not a single input
            low, high = _limits(field_info)
            fields.append(
                FormField(
                    name=f"{name}.{field_name}",
                    label=field_name.replace("_", " ").capitalize(),
                    kind=kind,
                    value=getattr(values, field_name),
                    help=field_info.description or "",
                    optional=optional,
                    minimum=low,
                    maximum=high,
                    choices=choices,
                )
            )
        title = name.capitalize()
        out.append(Section(name, title, tuple(fields)))
    return out


def changes_from_form(form: Mapping[str, Any], settings: Settings) -> dict[str, Any]:
    """Dotted changes from a submitted form. Pydantic converts the strings while validating."""
    changes: dict[str, Any] = {}
    for section in sections(settings):
        for f in section.fields:
            raw = form.get(f.name)
            if f.kind == "bool":
                changes[f.name] = raw is not None
            elif raw is None:
                continue
            elif f.optional and str(raw).strip() == "":
                changes[f.name] = None
            else:
                changes[f.name] = str(raw).strip()
    return changes
