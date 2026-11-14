"""Minimal SSE framing used by providers that expose event-stream responses."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass

import httpx


@dataclass(frozen=True, slots=True)
class ServerSentEvent:
    event: str | None
    data: str


async def parse_sse(response: httpx.Response) -> AsyncIterator[ServerSentEvent]:
    event_name: str | None = None
    data_lines: list[str] = []
    async for line in response.aiter_lines():
        if line == "":
            if data_lines:
                yield ServerSentEvent(event_name, "\n".join(data_lines))
            event_name = None
            data_lines = []
            continue
        if line.startswith(":"):
            continue
        field, separator, value = line.partition(":")
        if not separator:
            value = ""
        elif value.startswith(" "):
            value = value[1:]
        if field == "event":
            event_name = value
        elif field == "data":
            data_lines.append(value)
        # id/retry and extension fields do not affect JSON payload framing.
    if data_lines:
        yield ServerSentEvent(event_name, "\n".join(data_lines))

