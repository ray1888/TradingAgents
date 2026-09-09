"""HTTP client for the self-hosted TrendRadar MCP service."""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

DEFAULT_MCP_URL = "http://ubuntu.local:3333/mcp"
DEFAULT_TIMEOUT = 30.0
DEFAULT_RETRIES = 2


class TrendRadarUnavailable(RuntimeError):
    """Raised when the MCP endpoint cannot be reached after retries."""


def _env_bool(name: str, default: bool = True) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() not in {"0", "false", "no", "off"}


def _parse_sse(body: str) -> dict[str, Any]:
    if not body.strip():
        return {}
    for line in body.splitlines():
        if line.startswith("data: "):
            return json.loads(line[6:])
    return json.loads(body)


@dataclass
class TrendRadarMcpClient:
    url: str = DEFAULT_MCP_URL
    timeout: float = DEFAULT_TIMEOUT
    retries: int = DEFAULT_RETRIES

    @classmethod
    def from_env(cls) -> TrendRadarMcpClient:
        return cls(
            url=os.environ.get("TRENDRADAR_MCP_URL", DEFAULT_MCP_URL).rstrip("/"),
            timeout=float(os.environ.get("TRENDRADAR_MCP_TIMEOUT_SECONDS") or DEFAULT_TIMEOUT),
            retries=int(os.environ.get("TRENDRADAR_MCP_RETRIES") or DEFAULT_RETRIES),
        )

    def _post(self, payload: dict[str, Any], session: str | None = None) -> tuple[dict[str, Any], str | None]:
        data = json.dumps(payload).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if session:
            headers["mcp-session-id"] = session
        last_error: Exception | None = None
        attempts = max(1, self.retries + 1)
        for attempt in range(attempts):
            try:
                request = Request(self.url, data=data, headers=headers, method="POST")
                with urlopen(request, timeout=self.timeout) as response:
                    raw = response.read()
                    if not raw:
                        return {}, response.headers.get("mcp-session-id") or session
                    body = raw.decode("utf-8")
                    return _parse_sse(body), response.headers.get("mcp-session-id") or session
            except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as exc:
                last_error = exc
                if attempt + 1 < attempts:
                    time.sleep(min(2 ** attempt, 4))
        raise TrendRadarUnavailable(str(last_error) if last_error else "mcp request failed")

    def _session(self) -> str:
        _parsed, session = self._post(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "tradingagents-research", "version": "1"},
                },
            }
        )
        self._post({"jsonrpc": "2.0", "method": "notifications/initialized"}, session=session)
        if not session:
            raise TrendRadarUnavailable("mcp did not return a session id")
        return session

    def call_tool(self, name: str, arguments: dict[str, Any], rpc_id: int = 2) -> dict[str, Any]:
        session = self._session()
        parsed, _ = self._post(
            {
                "jsonrpc": "2.0",
                "id": rpc_id,
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments},
            },
            session=session,
        )
        result = parsed.get("result") or {}
        content = result.get("content") or []
        text = content[0].get("text") if content else ""
        if not text:
            raise TrendRadarUnavailable(f"empty MCP result for {name}")
        payload = json.loads(text)
        if not isinstance(payload, dict):
            raise TrendRadarUnavailable(f"non-object MCP result for {name}")
        return payload


def mcp_enabled() -> bool:
    raw = os.environ.get("TRENDRADAR_MCP_ENABLED")
    if raw is not None:
        return _env_bool("TRENDRADAR_MCP_ENABLED", True)
    return bool(os.environ.get("TRENDRADAR_MCP_URL"))
