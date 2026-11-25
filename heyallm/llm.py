"""OpenAI-compatible chat client.

Retry and SSE handling adapted from Bubble's daemon
(`_post_with_retry`, `_stream_post_with_retry`, `_consume_sse`) so the
failure behaviour is already known-good against llama.cpp / Ollama / vLLM.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

import requests

from .config import Endpoint

log = logging.getLogger("heyallm.llm")

RETRY_ATTEMPTS = 3
RETRY_BASE_DELAY = 1.0

SILENT_TOOL = {
    "type": "function",
    "function": {
        "name": "stay_silent",
        "description": (
            "Do not take a turn. Nothing is posted, nothing is said, no action "
            "is performed. Use this when the scene has reached a natural stop "
            "or you have nothing to do or say."
        ),
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
}


class LLMError(Exception):
    """Raised when a completion cannot be obtained."""


@dataclass
class LLMResult:
    text: str = ""
    tool_calls: list[dict] = field(default_factory=list)
    finish_reason: str | None = None
    model: str = ""

    @property
    def called_stay_silent(self) -> bool:
        for call in self.tool_calls:
            name = (call.get("function") or {}).get("name") or call.get("name") or ""
            if name == "stay_silent":
                return True
        return False


def _retry_delay(attempt: int, base: float = RETRY_BASE_DELAY) -> float:
    return base * (2 ** attempt)


class LLMClient:
    """Thin wrapper over a requests.Session with bounded retry."""

    def __init__(self, session: requests.Session | None = None) -> None:
        self._session = session
        self._local = threading.local()

    def _get_session(self) -> requests.Session:
        if self._session is not None:
            return self._session
        session = getattr(self._local, "session", None)
        if session is None:
            session = requests.Session()
            self._local.session = session
        return session

    # ------------------------------------------------------------------ http
    def _headers(self, endpoint: Endpoint) -> dict:
        headers = {"Content-Type": "application/json"}
        if endpoint.api_key:
            headers["Authorization"] = f"Bearer {endpoint.api_key}"
        return headers

    def post_with_retry(
        self,
        endpoint: Endpoint,
        payload: dict,
        *,
        timeout: float | tuple[float, float] | None = None,
        attempts: int = RETRY_ATTEMPTS,
    ) -> requests.Response:
        """POST with bounded exponential backoff on transient failures."""
        url = endpoint.chat_url()
        headers = self._headers(endpoint)
        limit = timeout if timeout is not None else (10, endpoint.timeout)
        for attempt in range(attempts):
            try:
                resp = self._get_session().post(url, json=payload, headers=headers, timeout=limit)
                if resp.status_code >= 500:
                    resp.close()
                    raise requests.exceptions.HTTPError(f"HTTP {resp.status_code}")
                return resp
            except Exception as exc:  # noqa: BLE001 - bounded by attempts
                if attempt + 1 >= attempts:
                    raise LLMError(f"{url} failed after {attempts} attempts: {exc}") from exc
                delay = _retry_delay(attempt)
                log.warning("Request to %s failed (%s); retrying in %.1fs", url, exc, delay)
                time.sleep(delay)
        raise LLMError(f"{url} unreachable")  # pragma: no cover - unreachable

    # ------------------------------------------------------------ completion
    def chat(
        self,
        endpoint: Endpoint,
        messages: list[dict],
        *,
        temperature: float = 0.9,
        max_tokens: int = 420,
        tools: list[dict] | None = None,
        timeout: float | tuple[float, float] | None = None,
    ) -> LLMResult:
        payload: dict[str, Any] = {
            "model": endpoint.model or "local",
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": False,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"

        resp = self.post_with_retry(endpoint, payload, timeout=timeout)
        try:
            try:
                resp.raise_for_status()
            except requests.HTTPError as exc:
                detail = ""
                try:
                    detail = resp.text[:500]
                except Exception:  # noqa: BLE001
                    pass
                raise LLMError(f"HTTP error from {endpoint.chat_url()}: {exc} {detail}") from exc

            try:
                data = resp.json()
            except (json.JSONDecodeError, requests.exceptions.JSONDecodeError) as exc:
                raise LLMError(f"Non-JSON response from {endpoint.chat_url()}") from exc
            if not isinstance(data, dict):
                raise LLMError(f"Invalid response from {endpoint.chat_url()}: expected an object")

            choices = data.get("choices")
            choice = choices[0] if isinstance(choices, list) and choices else {}
            if not isinstance(choice, dict):
                choice = {}
            message = choice.get("message")
            if not isinstance(message, dict):
                message = {}
            tool_calls = message.get("tool_calls")
            return LLMResult(
                text=str(message.get("content") or ""),
                tool_calls=tool_calls if isinstance(tool_calls, list) else [],
                finish_reason=choice.get("finish_reason"),
                model=str(data.get("model") or endpoint.model or ""),
            )
        finally:
            resp.close()

    def chat_stream(
        self,
        endpoint: Endpoint,
        messages: list[dict],
        *,
        temperature: float = 0.9,
        max_tokens: int = 420,
        on_token: Callable[[str], None] | None = None,
        timeout: float | tuple[float, float] | None = None,
    ) -> LLMResult:
        """Stream a completion; `on_token` receives each content delta."""
        payload: dict[str, Any] = {
            "model": endpoint.model or "local",
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": True,
        }
        headers = self._headers(endpoint)
        url = endpoint.chat_url()
        limit = timeout if timeout is not None else (10, None)

        resp: requests.Response | None = None
        for attempt in range(RETRY_ATTEMPTS):
            try:
                resp = self._get_session().post(
                    url, json=payload, headers=headers, stream=True, timeout=limit
                )
                resp.raise_for_status()
                break
            except Exception as exc:  # noqa: BLE001
                if resp is not None:
                    resp.close()
                resp = None
                if attempt + 1 >= RETRY_ATTEMPTS:
                    raise LLMError(f"Stream to {url} failed: {exc}") from exc
                delay = _retry_delay(attempt)
                log.warning("Stream to %s failed (%s); retrying in %.1fs", url, exc, delay)
                time.sleep(delay)

        assert resp is not None
        pieces: list[str] = []
        try:
            for raw_line in resp.iter_lines():
                if not raw_line:
                    continue
                line = raw_line.decode("utf-8", errors="replace")
                if not line.startswith("data: "):
                    continue
                data = line[6:]
                if data == "[DONE]":
                    break
                try:
                    chunk = json.loads(data)
                except json.JSONDecodeError:
                    continue
                delta = ((chunk.get("choices") or [{}])[0]) .get("delta") or {}
                piece = delta.get("content") or ""
                if piece:
                    pieces.append(piece)
                    if on_token:
                        on_token(piece)
        finally:
            resp.close()
        return LLMResult(text="".join(pieces))

    # -------------------------------------------------------------- metadata
    def list_models(self, endpoint: Endpoint, timeout: float = 3.0) -> list[str]:
        resp = None
        try:
            resp = self._get_session().get(
                endpoint.models_url(), headers=self._headers(endpoint), timeout=timeout
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception:  # noqa: BLE001 - probing is best-effort
            return []
        finally:
            if resp is not None:
                resp.close()
        if not isinstance(data, dict):
            return []
        names = []
        for item in data.get("data") or []:
            if isinstance(item, dict) and item.get("id"):
                names.append(str(item["id"]))
        return names

    def ping(self, endpoint: Endpoint, timeout: float = 3.0) -> bool:
        return bool(self.list_models(endpoint, timeout=timeout))
