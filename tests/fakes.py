"""A fake LLM client so tests never touch the network."""

from __future__ import annotations

from heyallm.llm import LLMClient, LLMResult


class FakeLLM(LLMClient):
    """Returns scripted responses in order and records every call."""

    def __init__(self, responses=None, default: str = '*looks up* "Hello."') -> None:
        super().__init__()
        self.responses = list(responses) if responses else None
        self.default = default
        self.calls: list[dict] = []

    def chat(self, endpoint, messages, **kwargs):
        self.calls.append({"endpoint": endpoint, "messages": messages, **kwargs})
        if self.responses is None:
            text = self.default
        elif not self.responses:
            text = self.default
        else:
            text = self.responses.pop(0)
        if callable(text):
            text = text(endpoint, messages, **kwargs)
        if isinstance(text, LLMResult):
            return text
        return LLMResult(text=text)

    def list_models(self, endpoint, timeout=3.0):  # never probe the network
        return ["fake-model"]
