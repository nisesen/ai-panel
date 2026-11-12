"""ai-panel: a small web control panel for a self-hosted GPU/LLM server."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("ai-panel")
except PackageNotFoundError:  # running from a source tree that is not installed
    __version__ = "0.0.0"
