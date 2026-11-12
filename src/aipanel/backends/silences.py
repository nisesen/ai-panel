"""Alertmanager silences (API v2), marked as the panel's by their createdBy.

Alertmanager ANDs the matchers inside one silence, so unrelated groups of alerts (a host's
alerts by instance, an alert about its guests by node, an alert with neither) cannot share one.
The site config lists matcher sets, and a planned stop creates one silence per set.
"""

from datetime import UTC, datetime

import httpx

from aipanel.backends.protocols import BackendError, Silence, Unreachable
from aipanel.config.site import AlertmanagerConfig


class Alertmanager:
    def __init__(self, config: AlertmanagerConfig, client: httpx.AsyncClient) -> None:
        self.config = config
        self.client = client

    async def _request(self, method: str, path: str, body: object = None) -> httpx.Response:
        url = self.config.url.rstrip("/") + path
        try:
            response = await self.client.request(
                method, url, json=body, timeout=self.config.timeout_seconds
            )
        except httpx.HTTPError as e:
            raise Unreachable(f"Alertmanager: {type(e).__name__}") from None
        if response.status_code >= 400:
            raise BackendError(f"Alertmanager: HTTP {response.status_code} {response.text[:200]}")
        return response

    async def create(self, until: datetime, comment: str) -> list[str]:
        starts, ends = datetime.now(UTC).isoformat(), until.astimezone(UTC).isoformat()
        ids = []
        for silence in self.config.silences:
            body = {
                "matchers": [
                    {"name": m.name, "value": m.value, "isRegex": m.is_regex, "isEqual": m.is_equal}
                    for m in silence.matchers
                ],
                "startsAt": starts,
                "endsAt": ends,
                "createdBy": self.config.created_by,
                "comment": comment,
            }
            # A failure stops the action before anything is stopped; silences made already end
            # at `until`, or with the next power on, which ends all of the panel's.
            response = await self._request("POST", "/api/v2/silences", body)
            ids.append(str(response.json()["silenceID"]))
        return ids

    async def active(self) -> list[Silence]:
        response = await self._request("GET", "/api/v2/silences")
        return [
            Silence(s["id"], datetime.fromisoformat(s["endsAt"]), s.get("comment", ""))
            for s in response.json()
            if s.get("createdBy") == self.config.created_by
            and s.get("status", {}).get("state") == "active"
        ]

    async def expire(self, silence_id: str) -> None:
        await self._request("DELETE", f"/api/v2/silence/{silence_id}")
