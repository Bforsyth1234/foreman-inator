"""A fake OpenAI-compatible model server (stands in for Ollama, Z.ai and the Claude API).

Reached through an injected ``httpx`` transport, never the network:

    fake = FakeModels()
    fake.queue_reply("hello", tokens=(12, 3))
    proxy = ModelProxy(config, transport=fake.transport)

Endpoints, under any base URL: ``POST .../chat/completions`` (``stream`` true or false),
``POST .../completions``, ``POST .../embeddings``, ``GET .../models``.

Controls:
    queue_reply(text, tool_calls=, tokens=, finish_reason=, usage_in_stream=)
                              the next chat reply (default: "hello from <model>")
    fail_next(status, body)   the next request gets this HTTP error
    raise_next(exc)           the next request raises (e.g. httpx.ConnectError)
    stream_delay              seconds between streamed chunks (slow streams)
    hold_stream               a threading.Event; streams pause after their first chunk
                              until it is set (for disconnect tests; any thread may set it)
Inspection:
    requests                  every request: FakeRequest(method, url, headers, json)
    sent                      the exact response bytes of each answered request
    stream_closed             count of streams the client stopped reading early
"""

from __future__ import annotations

import asyncio
import json
import threading
from collections import deque
from dataclasses import dataclass, field
from typing import Any

import httpx


@dataclass
class FakeRequest:
    method: str
    url: str
    headers: dict[str, str]
    json: Any


@dataclass
class Reply:
    text: str
    tool_calls: list[dict[str, Any]] = field(default_factory=list)  # {id, name, arguments}
    tokens: tuple[int, int] | None = (11, 7)  # None: report no usage at all
    finish_reason: str | None = None
    usage_in_stream: bool = True


class FakeModels:
    def __init__(self) -> None:
        self.requests: list[FakeRequest] = []
        self.sent: list[bytes] = []
        self.replies: deque[Reply] = deque()
        self.failures: deque[tuple[int, Any]] = deque()
        self.raises: deque[Exception] = deque()
        self.stream_delay = 0.0
        self.hold_stream: threading.Event | None = None
        self.stream_closed = 0
        self.transport = httpx.MockTransport(self.handle)

    # ------------------------------------------------------------------ controls

    def queue_reply(
        self,
        text: str,
        *,
        tool_calls: list[dict[str, Any]] | None = None,
        tokens: tuple[int, int] | None = (11, 7),
        finish_reason: str | None = None,
        usage_in_stream: bool = True,
    ) -> None:
        self.replies.append(Reply(text, tool_calls or [], tokens, finish_reason, usage_in_stream))

    def fail_next(self, status: int, body: Any = None) -> None:
        self.failures.append((status, body or {"error": {"message": "fake failure"}}))

    def raise_next(self, exc: Exception) -> None:
        self.raises.append(exc)

    # ------------------------------------------------------------------ helpers for tests

    @staticmethod
    def chat_body(reply: Reply, model: str) -> dict[str, Any]:
        msg: dict[str, Any] = {"role": "assistant", "content": reply.text}
        if reply.tool_calls:
            msg["tool_calls"] = [
                {
                    "id": c["id"],
                    "type": "function",
                    "function": {"name": c["name"], "arguments": c["arguments"]},
                }
                for c in reply.tool_calls
            ]
        out: dict[str, Any] = {
            "id": "chatcmpl-fake",
            "object": "chat.completion",
            "created": 1763856000,
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "message": msg,
                    "finish_reason": reply.finish_reason
                    or ("tool_calls" if reply.tool_calls else "stop"),
                }
            ],
        }
        if reply.tokens is not None:
            out["usage"] = _usage(reply.tokens)
        return out

    @staticmethod
    def stream_chunks(reply: Reply, model: str, include_usage: bool) -> list[bytes]:
        """The SSE events for a streamed reply, one bytes object per event."""

        def ev(obj: Any) -> bytes:
            return b"data: " + json.dumps(obj, separators=(",", ":")).encode() + b"\n\n"

        base = {"id": "chatcmpl-fake", "object": "chat.completion.chunk", "created": 1763856000}
        base["model"] = model
        chunks = [ev({**base, "choices": [{"index": 0, "delta": {"role": "assistant"}}]})]
        words = reply.text.split(" ")
        for i, w in enumerate(words):
            piece = w if i == 0 else " " + w
            chunks.append(ev({**base, "choices": [{"index": 0, "delta": {"content": piece}}]}))
        for n, c in enumerate(reply.tool_calls):
            args = c["arguments"]
            half = len(args) // 2
            first = {"index": n, "id": c["id"], "type": "function"}
            first["function"] = {"name": c["name"], "arguments": args[:half]}
            chunks.append(ev({**base, "choices": [{"index": 0, "delta": {"tool_calls": [first]}}]}))
            rest = {"index": n, "function": {"arguments": args[half:]}}
            chunks.append(ev({**base, "choices": [{"index": 0, "delta": {"tool_calls": [rest]}}]}))
        finish = reply.finish_reason or ("tool_calls" if reply.tool_calls else "stop")
        chunks.append(ev({**base, "choices": [{"index": 0, "delta": {}, "finish_reason": finish}]}))
        if include_usage and reply.usage_in_stream and reply.tokens is not None:
            chunks.append(ev({**base, "choices": [], "usage": _usage(reply.tokens)}))
        chunks.append(b"data: [DONE]\n\n")
        return chunks

    # ------------------------------------------------------------------ transport

    async def handle(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        self.requests.append(
            FakeRequest(request.method, str(request.url), dict(request.headers), body)
        )
        if self.raises:
            raise self.raises.popleft()
        if self.failures:
            status, err = self.failures.popleft()
            data = json.dumps(err).encode()
            self.sent.append(data)
            return httpx.Response(
                status, content=data, headers={"content-type": "application/json"}
            )
        path = request.url.path
        model = (body or {}).get("model", "")
        if path.endswith("/models") and request.method == "GET":
            return self._json({"object": "list", "data": [{"id": "fake", "object": "model"}]})
        if path.endswith("/embeddings"):
            inputs = body.get("input")
            inputs = inputs if isinstance(inputs, list) else [inputs]
            return self._json(
                {
                    "object": "list",
                    "model": model,
                    "data": [
                        {"object": "embedding", "index": i, "embedding": [0.1, 0.2, 0.3]}
                        for i, _ in enumerate(inputs)
                    ],
                    "usage": {"prompt_tokens": 5 * len(inputs), "total_tokens": 5 * len(inputs)},
                }
            )
        if path.endswith("/chat/completions") or path.endswith("/completions"):
            reply = self.replies.popleft() if self.replies else Reply(f"hello from {model}")
            if body.get("stream"):
                include = bool((body.get("stream_options") or {}).get("include_usage"))
                chunks = self.stream_chunks(reply, model, include)
                self.sent.append(b"".join(chunks))
                return httpx.Response(
                    200,
                    headers={"content-type": "text/event-stream"},
                    stream=_Stream(self, chunks),
                )
            return self._json(self.chat_body(reply, model))
        return self._json({"error": {"message": f"no route {path}"}}, status=404)

    def _json(self, obj: Any, status: int = 200) -> httpx.Response:
        data = json.dumps(obj, separators=(",", ":")).encode()
        self.sent.append(data)
        return httpx.Response(status, content=data, headers={"content-type": "application/json"})


class _Stream(httpx.AsyncByteStream):
    def __init__(self, fake: FakeModels, chunks: list[bytes]):
        self.fake, self.chunks = fake, chunks
        self.finished = False

    async def __aiter__(self):
        for i, c in enumerate(self.chunks):
            if i and self.fake.stream_delay:
                await asyncio.sleep(self.fake.stream_delay)
            if i == 1 and self.fake.hold_stream is not None:
                while not self.fake.hold_stream.is_set():
                    await asyncio.sleep(0.01)
            yield c
        self.finished = True

    async def aclose(self) -> None:
        if not self.finished:
            self.fake.stream_closed += 1


def _usage(tokens: tuple[int, int]) -> dict[str, int]:
    return {"prompt_tokens": tokens[0], "completion_tokens": tokens[1], "total_tokens": sum(tokens)}
