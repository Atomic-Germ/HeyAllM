"""LLM transport behavior without network access."""

from __future__ import annotations

import json
import threading

import pytest
import requests

from heyallm.config import Endpoint
from heyallm.llm import LLMClient, LLMError


class FakeResponse:
    def __init__(self, *, status=200, data=None, text="", error=None, lines=None):
        self.status_code = status
        self._data = data
        self.text = text
        self._error = error
        self._lines = lines or []
        self.closed = False

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")

    def json(self):
        if self._error:
            raise self._error
        return self._data

    def iter_lines(self):
        yield from self._lines

    def close(self):
        self.closed = True


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.posts = []
        self.gets = []

    def post(self, url, **kwargs):
        self.posts.append((url, kwargs))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    def get(self, url, **kwargs):
        self.gets.append((url, kwargs))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def completion(text='"Hello."', tool_calls=None):
    return {
        "model": "played-model",
        "choices": [{"message": {"content": text, "tool_calls": tool_calls}, "finish_reason": "stop"}],
    }


def test_chat_decodes_content_tools_and_model():
    calls = [{"function": {"name": "stay_silent"}}]
    response = FakeResponse(data=completion(tool_calls=calls))
    session = FakeSession([response])
    client = LLMClient(session=session)

    result = client.chat(Endpoint(model="configured"), [])

    assert result.text == '"Hello."'
    assert result.called_stay_silent
    assert result.model == "played-model"
    assert result.finish_reason == "stop"
    assert response.closed
    payload = session.posts[0][1]["json"]
    assert payload["model"] == "configured"
    assert payload["stream"] is False


def test_chat_rejects_non_json_and_closes_response():
    response = FakeResponse(error=json.JSONDecodeError("bad", "x", 0))
    client = LLMClient(session=FakeSession([response]))

    with pytest.raises(LLMError, match="Non-JSON"):
        client.chat(Endpoint(), [])

    assert response.closed


def test_chat_rejects_non_object_json():
    response = FakeResponse(data=[])
    client = LLMClient(session=FakeSession([response]))

    with pytest.raises(LLMError, match="expected an object"):
        client.chat(Endpoint(), [])

    assert response.closed


def test_http_error_includes_detail_and_closes_response():
    response = FakeResponse(status=400, text="bad request details")
    session = FakeSession([response])
    client = LLMClient(session=session)

    with pytest.raises(LLMError, match="bad request details"):
        client.chat(Endpoint(), [])

    assert response.closed
    assert len(session.posts) == 1  # client errors are not retried


def test_server_errors_are_retried_and_closed(monkeypatch):
    monkeypatch.setattr("heyallm.llm.time.sleep", lambda _delay: None)
    responses = [FakeResponse(status=500), FakeResponse(status=503), FakeResponse(status=500)]
    session = FakeSession(responses)
    client = LLMClient(session=session)

    with pytest.raises(LLMError, match="after 3 attempts"):
        client.chat(Endpoint(), [])

    assert len(session.posts) == 3
    assert all(response.closed for response in responses)


def test_stream_collects_tokens_and_closes():
    lines = [
        b'data: {"choices":[{"delta":{"content":"Hello"}}]}',
        b"",
        b'data: {"choices":[{"delta":{"content":" there"}}]}',
        b"data: [DONE]",
    ]
    response = FakeResponse(lines=lines)
    client = LLMClient(session=FakeSession([response]))
    tokens = []

    result = client.chat_stream(Endpoint(), [], on_token=tokens.append)

    assert result.text == "Hello there"
    assert tokens == ["Hello", " there"]
    assert response.closed


def test_list_models_is_best_effort_and_closes():
    response = FakeResponse(data={"data": [{"id": "one"}, {"id": "two"}, {}]})
    client = LLMClient(session=FakeSession([response]))

    assert client.list_models(Endpoint()) == ["one", "two"]
    assert response.closed

    failed = FakeResponse(status=404)
    assert LLMClient(session=FakeSession([failed])).list_models(Endpoint()) == []
    assert failed.closed


def test_default_sessions_are_thread_local(monkeypatch):
    created = []

    class Session:
        pass

    def make_session():
        session = Session()
        created.append(session)
        return session

    monkeypatch.setattr("heyallm.llm.requests.Session", make_session)
    client = LLMClient()
    sessions = []
    threads = [threading.Thread(target=lambda: sessions.append(client._get_session())) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(created) == 2
    assert sessions[0] is not sessions[1]
