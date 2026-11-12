"""The registry of actions: each action module registers its classes here when imported."""

from aipanel.actions.base import Action
from aipanel.config.site import SiteConfig

REGISTRY: dict[str, Action] = {}


def register[A: type[Action]](cls: A) -> A:
    if cls.name in REGISTRY:
        raise ValueError(f"action {cls.name!r} registered twice")
    REGISTRY[cls.name] = cls()
    return cls


def get(name: str) -> Action | None:
    return REGISTRY.get(name)


def enabled(site: SiteConfig) -> list[Action]:
    return [a for a in REGISTRY.values() if site.action_enabled(a.name)]
