"""Actions. Importing this package registers every action module's actions."""

from aipanel.actions import guests, llm, maintenance, power
from aipanel.actions.registry import REGISTRY, enabled, get

__all__ = ["REGISTRY", "enabled", "get", "guests", "llm", "maintenance", "power"]
