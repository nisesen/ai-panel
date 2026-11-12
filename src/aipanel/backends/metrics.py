"""Prometheus's HTTP API: instant and range queries."""

from datetime import datetime
from typing import Any

import httpx

from aipanel.backends.protocols import BackendError, Sample, Series, Unreachable
from aipanel.config.site import PrometheusConfig


class Prometheus:
    def __init__(self, config: PrometheusConfig, client: httpx.AsyncClient) -> None:
        self.config = config
        self.client = client

    async def _get(self, path: str, params: dict[str, str]) -> Any:
        url = self.config.url.rstrip("/") + path
        try:
            response = await self.client.get(
                url, params=params, timeout=self.config.timeout_seconds
            )
        except httpx.HTTPError as e:
            raise Unreachable(f"Prometheus: {type(e).__name__}") from None
        try:
            body = response.json()
        except ValueError:
            raise BackendError(f"Prometheus: HTTP {response.status_code}") from None
        if body.get("status") != "success":
            raise BackendError(f"Prometheus: {body.get('error', response.status_code)}")
        return body["data"]

    async def query(self, promql: str) -> list[Sample]:
        data = await self._get("/api/v1/query", {"query": promql})
        if data.get("resultType") == "scalar":
            return [Sample({}, float(data["result"][1]))]
        return [Sample(r.get("metric", {}), float(r["value"][1])) for r in data.get("result", [])]

    async def query_range(
        self, promql: str, start: datetime, end: datetime, step: int
    ) -> list[Series]:
        params = {
            "query": promql,
            "start": str(start.timestamp()),
            "end": str(end.timestamp()),
            "step": str(step),
        }
        data = await self._get("/api/v1/query_range", params)
        return [
            Series(r.get("metric", {}), [(float(t), float(v)) for t, v in r.get("values", [])])
            for r in data.get("result", [])
        ]
