"""llama.cpp's server: /health (public), /props and /slots (with the API key when it has one)."""

from typing import Any

import httpx

from aipanel.backends.protocols import BackendError, Unreachable
from aipanel.config.site import LlmConfig


class LlamaCpp:
    def __init__(self, config: LlmConfig, client: httpx.AsyncClient) -> None:
        self.config = config
        self.client = client

    def _headers(self) -> dict[str, str]:
        if self.config.api_key is None:
            return {}
        try:
            return {"Authorization": f"Bearer {self.config.api_key.read()}"}
        except (OSError, LookupError) as e:
            raise BackendError(f"LLM API key: {e}") from None

    async def _get(self, path: str) -> httpx.Response:
        url = self.config.url.rstrip("/") + path
        try:
            return await self.client.get(
                url, headers=self._headers(), timeout=self.config.timeout_seconds
            )
        except httpx.HTTPError as e:
            raise Unreachable(f"LLM: {type(e).__name__}") from None

    async def health(self) -> bool:
        return (await self._get("/health")).status_code == 200

    async def props(self) -> dict[str, Any]:
        response = await self._get("/props")
        if response.status_code != 200:
            raise BackendError(f"LLM /props: HTTP {response.status_code}")
        data = response.json()
        return data if isinstance(data, dict) else {}

    async def slots(self) -> tuple[int, int] | None:
        response = await self._get("/slots")
        if response.status_code in (404, 501):  # started with --no-slots
            return None
        if response.status_code != 200:
            raise BackendError(f"LLM /slots: HTTP {response.status_code}")
        slots = response.json()
        if not isinstance(slots, list):
            return None
        busy = sum(1 for s in slots if isinstance(s, dict) and s.get("is_processing"))
        return busy, len(slots)
