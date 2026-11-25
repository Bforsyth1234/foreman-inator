"""A fake Anthropic Messages upstream (``POST .../messages``), for the proxy's #52 route.

Reached through an injected ``httpx`` transport, never the network:

    fake = FakeAnthropic()
    fake.queue_reply("hi", usage={"input_tokens": 10, "output_tokens": 3})
    proxy = ModelProxy(models, transport=fake.transport)

The event shapes follow https://platform.claude.com/docs/en/build-with-claude/streaming
(read 2026-11-25): ``message_start`` (``message.usage`` with the input and cache
counts), ``content_block_start`` / ``content_block_delta`` (``text_delta``,
``input_json_delta``) / ``content_block_stop``, ``message_delta`` (``delta.stop_reason``
and the cumulative ``usage``), ``message_stop``; ``ping`` events are mixed in.

Controls:
    queue_reply(text, tool_calls=, usage=, stop_reason=, delta_usage=)
                         the next reply. ``usage``: the ``message_start`` usage (and the
                         body's, non-streaming); ``delta_usage``: the final
                         ``message_delta`` usage (default ``{"output_tokens": ...}``).
    fail_next(status, body)  the next request gets this HTTP error (JSON body)
Inspection:
    requests             every request: AnthropicRequest(method, url, headers, content)
    sent                 the exact response bytes of each answered request
"""

from __future__ import annotations

import json
from collections import deque
from dataclasses import dataclass, field
from typing import Any

import httpx


@dataclass
class AnthropicRequest:
    method: str
    url: str
    headers: dict[str, str]
    content: bytes

    @property
    def json(self) -> Any:
        return json.loads(self.content)


@dataclass
class AnthropicReply:
    text: str
    tool_calls: list[dict[str, Any]] = field(default_factory=list)  # {id, name, input}
    usage: dict[str, Any] = field(default_factory=lambda: {"input_tokens": 11})
    stop_reason: str | None = None
    delta_usage: dict[str, Any] | None = None
    output_tokens: int = 7


class FakeAnthropic:
    def __init__(self) -> None:
        self.requests: list[AnthropicRequest] = []
        self.sent: list[bytes] = []
        self.replies: deque[AnthropicReply] = deque()
        self.failures: deque[tuple[int, Any]] = deque()
        self.on_request: Any = None  # optional fn(AnthropicRequest), called first
        self.transport = httpx.MockTransport(self.handle)

    def queue_reply(self, text: str = "hello from claude", **kw: Any) -> None:
        self.replies.append(AnthropicReply(text, **kw))

    def fail_next(self, status: int, body: Any) -> None:
        self.failures.append((status, body))

    # ------------------------------------------------------------------ the server

    def handle(self, request: httpx.Request) -> httpx.Response:
        req = AnthropicRequest(
            request.method, str(request.url), dict(request.headers), request.read()
        )
        self.requests.append(req)
        if self.on_request is not None:
            self.on_request(req)
        if self.failures:
            status, body = self.failures.popleft()
            data = json.dumps(body).encode()
            self.sent.append(data)
            return httpx.Response(status, content=data, headers=_JSON)
        if not str(request.url).endswith("/messages"):
            return httpx.Response(404, content=b'{"type":"error"}', headers=_JSON)
        body = req.json
        reply = self.replies.popleft() if self.replies else AnthropicReply("hello from claude")
        if body.get("stream"):
            data = self._stream(body, reply)
            self.sent.append(data)
            return httpx.Response(200, content=data, headers={"content-type": "text/event-stream"})
        data = json.dumps(self._message(body, reply)).encode()
        self.sent.append(data)
        return httpx.Response(200, content=data, headers=_JSON)

    @staticmethod
    def _stop(reply: AnthropicReply) -> str:
        return reply.stop_reason or ("tool_use" if reply.tool_calls else "end_turn")

    def _message(self, body: dict[str, Any], reply: AnthropicReply) -> dict[str, Any]:
        content: list[dict[str, Any]] = [{"type": "text", "text": reply.text}]
        content += [{"type": "tool_use", **tc} for tc in reply.tool_calls]
        usage = {**reply.usage, "output_tokens": reply.output_tokens}
        if reply.delta_usage:
            usage.update(reply.delta_usage)
        return {
            "id": "msg_fake",
            "type": "message",
            "role": "assistant",
            "model": body.get("model"),
            "content": content,
            "stop_reason": self._stop(reply),
            "stop_sequence": None,
            "usage": usage,
        }

    def _stream(self, body: dict[str, Any], reply: AnthropicReply) -> bytes:
        events: list[tuple[str, dict[str, Any]]] = []
        start_usage = {**reply.usage, "output_tokens": 1}
        events.append(
            (
                "message_start",
                {
                    "type": "message_start",
                    "message": {
                        "id": "msg_fake",
                        "type": "message",
                        "role": "assistant",
                        "content": [],
                        "model": body.get("model"),
                        "stop_reason": None,
                        "stop_sequence": None,
                        "usage": start_usage,
                    },
                },
            )
        )
        events.append(("ping", {"type": "ping"}))
        events.append(
            (
                "content_block_start",
                {
                    "type": "content_block_start",
                    "index": 0,
                    "content_block": {"type": "text", "text": ""},
                },
            )
        )
        half = len(reply.text) // 2
        for piece in (reply.text[:half], reply.text[half:]):
            events.append(
                (
                    "content_block_delta",
                    {
                        "type": "content_block_delta",
                        "index": 0,
                        "delta": {"type": "text_delta", "text": piece},
                    },
                )
            )
        events.append(("content_block_stop", {"type": "content_block_stop", "index": 0}))
        for i, tc in enumerate(reply.tool_calls, start=1):
            events.append(
                (
                    "content_block_start",
                    {
                        "type": "content_block_start",
                        "index": i,
                        "content_block": {
                            "type": "tool_use",
                            "id": tc["id"],
                            "name": tc["name"],
                            "input": {},
                        },
                    },
                )
            )
            args = json.dumps(tc.get("input", {}))
            cut = len(args) // 2
            for piece in (args[:cut], args[cut:]):
                events.append(
                    (
                        "content_block_delta",
                        {
                            "type": "content_block_delta",
                            "index": i,
                            "delta": {"type": "input_json_delta", "partial_json": piece},
                        },
                    )
                )
            events.append(("content_block_stop", {"type": "content_block_stop", "index": i}))
        delta_usage = reply.delta_usage or {"output_tokens": reply.output_tokens}
        events.append(
            (
                "message_delta",
                {
                    "type": "message_delta",
                    "delta": {"stop_reason": self._stop(reply), "stop_sequence": None},
                    "usage": delta_usage,
                },
            )
        )
        events.append(("message_stop", {"type": "message_stop"}))
        return b"".join(
            f"event: {name}\ndata: {json.dumps(data)}\n\n".encode() for name, data in events
        )


_JSON = {"content-type": "application/json"}
