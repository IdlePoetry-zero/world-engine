"""Process-local provider endpoint admission with speed-profile fairness.

Admission is deliberately outside the gateway: it never changes a model
request, profile, temperature, or provider parameters. The implementation uses
thread-safe state plus event-loop-local futures, so a long-running command-line
runner may create a fresh asyncio loop for each call without losing admission
coordination.
"""

from __future__ import annotations

import asyncio
from collections import deque
from contextlib import asynccontextmanager
from contextlib import suppress
from dataclasses import dataclass, field
import ipaddress
import threading
import time
from typing import AsyncIterator
from urllib.parse import urlsplit

from world_engine.adapters.models.contracts import ProviderConfig, ProviderKind, SpeedProfile


CLOUD_ENDPOINT_CONCURRENCY = 4
LOCAL_ENDPOINT_CONCURRENCY = 1
MAX_RETAINED_ENDPOINTS = 256

# Weighted round-robin service gives fast requests more slots while ensuring
# balanced and patient queues continue to make progress under sustained load.
_SPEED_SERVICE_CYCLE = (
    SpeedProfile.FAST,
    SpeedProfile.BALANCED,
    SpeedProfile.FAST,
    SpeedProfile.PATIENT,
    SpeedProfile.BALANCED,
    SpeedProfile.FAST,
)


@dataclass(slots=True)
class _Waiter:
    loop: asyncio.AbstractEventLoop
    future: asyncio.Future[float]
    speed: SpeedProfile
    enqueued_at: float
    granted: bool = False


@dataclass(slots=True)
class _EndpointState:
    capacity: int
    active: int = 0
    queues: dict[SpeedProfile, deque[_Waiter]] = field(
        default_factory=lambda: {speed: deque() for speed in SpeedProfile}
    )
    cycle_index: int = 0

    @property
    def idle(self) -> bool:
        return self.active == 0 and not any(self.queues.values())


class EndpointAdmissionScheduler:
    """Bound concurrent provider calls per normalized endpoint.

    Aliases with the same origin share a queue. Local endpoints, including
    loopback Ollama aliases and non-loopback Ollama services, receive one slot;
    other endpoints receive four. State and cancellation are safe across
    concurrent asyncio event loops in the same process.
    """

    def __init__(
        self,
        *,
        local_capacity: int = LOCAL_ENDPOINT_CONCURRENCY,
        cloud_capacity: int = CLOUD_ENDPOINT_CONCURRENCY,
        max_retained_endpoints: int = MAX_RETAINED_ENDPOINTS,
    ) -> None:
        if local_capacity < 1 or cloud_capacity < 1 or max_retained_endpoints < 1:
            raise ValueError("endpoint capacities and retained-state limit must be positive")
        self._local_capacity = local_capacity
        self._cloud_capacity = cloud_capacity
        self._max_retained_endpoints = max_retained_endpoints
        self._lock = threading.Lock()
        self._endpoints: dict[tuple[str, str, int], _EndpointState] = {}
        self._lru: deque[tuple[str, str, int]] = deque()

    @staticmethod
    def endpoint_key(config: ProviderConfig) -> tuple[str, str, int]:
        parsed = urlsplit(config.base_url)
        scheme = parsed.scheme.lower()
        host = (parsed.hostname or "").rstrip(".").lower()
        try:
            address = ipaddress.ip_address(host)
            if address.is_loopback:
                host = "<loopback>"
        except ValueError:
            if host == "localhost":
                host = "<loopback>"
        port = parsed.port or (443 if scheme == "https" else 80)
        # The path is intentionally omitted: `/` and `/v1` aliases to one
        # server must not bypass its concurrency allowance.
        return scheme, host, port

    @staticmethod
    def _capacity(config: ProviderConfig, local_capacity: int, cloud_capacity: int) -> int:
        if config.kind is ProviderKind.OLLAMA or config.is_local_endpoint:
            return local_capacity
        return cloud_capacity

    @asynccontextmanager
    async def slot(
        self,
        config: ProviderConfig,
        speed: SpeedProfile,
        *,
        deadline_at: float | None = None,
    ) -> AsyncIterator[float]:
        """Admit one call and yield its measured queue time in seconds."""
        if not isinstance(speed, SpeedProfile):
            speed = SpeedProfile(speed)
        key = self.endpoint_key(config)
        capacity = self._capacity(config, self._local_capacity, self._cloud_capacity)
        queued_at = time.perf_counter()
        loop = asyncio.get_running_loop()
        future: asyncio.Future[float] | None = None
        waiter: _Waiter | None = None

        with self._lock:
            state = self._state_locked(key, capacity)
            self._touch_locked(key)
            if state.active < state.capacity and not any(state.queues.values()):
                state.active += 1
                queue_seconds = 0.0
            else:
                future = loop.create_future()
                waiter = _Waiter(loop, future, speed, queued_at)
                state.queues[speed].append(waiter)
                self._dispatch_locked(key, state)

        if future is not None and waiter is not None:
            try:
                remaining = None if deadline_at is None else deadline_at - time.perf_counter()
                if remaining is not None and remaining <= 0:
                    raise TimeoutError
                queue_seconds = await future if remaining is None else await asyncio.wait_for(future, timeout=remaining)
            except (asyncio.CancelledError, TimeoutError):
                if not future.done():
                    future.cancel()
                with self._lock:
                    state = self._endpoints.get(key)
                    if state is not None:
                        if waiter.granted:
                            state.active -= 1
                        else:
                            with suppress(ValueError):
                                state.queues[speed].remove(waiter)
                        self._dispatch_locked(key, state)
                        self._evict_idle_locked()
                raise

        try:
            yield queue_seconds
        finally:
            with self._lock:
                state = self._endpoints.get(key)
                if state is not None:
                    state.active -= 1
                    if state.active < 0:
                        state.active = 0
                        raise RuntimeError("endpoint admission slot released more than once")
                    self._dispatch_locked(key, state)
                    self._evict_idle_locked()

    def _state_locked(self, key: tuple[str, str, int], capacity: int) -> _EndpointState:
        state = self._endpoints.get(key)
        if state is None:
            state = _EndpointState(capacity)
            self._endpoints[key] = state
        elif state.capacity != capacity:
            # Endpoint locality/capacity should not vary across aliases. Use
            # the stricter setting if a future config policy makes it differ.
            state.capacity = min(state.capacity, capacity)
        return state

    def _touch_locked(self, key: tuple[str, str, int]) -> None:
        try:
            self._lru.remove(key)
        except ValueError:
            pass
        self._lru.append(key)

    def _dispatch_locked(self, key: tuple[str, str, int], state: _EndpointState) -> None:
        while state.active < state.capacity:
            waiter = self._next_waiter_locked(state)
            if waiter is None:
                return
            waiter.granted = True
            state.active += 1
            elapsed = max(0.0, time.perf_counter() - waiter.enqueued_at)
            try:
                waiter.loop.call_soon_threadsafe(_deliver_grant, waiter.future, elapsed)
            except RuntimeError:
                # A loop can close during shutdown. Return the slot and keep
                # serving other live callers rather than leaking capacity.
                state.active -= 1

    @staticmethod
    def _next_waiter_locked(state: _EndpointState) -> _Waiter | None:
        for offset in range(len(_SPEED_SERVICE_CYCLE)):
            index = (state.cycle_index + offset) % len(_SPEED_SERVICE_CYCLE)
            speed = _SPEED_SERVICE_CYCLE[index]
            queue = state.queues[speed]
            while queue and queue[0].future.cancelled():
                queue.popleft()
            if queue:
                state.cycle_index = (index + 1) % len(_SPEED_SERVICE_CYCLE)
                return queue.popleft()
        return None

    def _evict_idle_locked(self) -> None:
        if len(self._endpoints) <= self._max_retained_endpoints:
            return
        retained: deque[tuple[str, str, int]] = deque()
        while self._lru:
            key = self._lru.popleft()
            state = self._endpoints.get(key)
            if state is None:
                continue
            if len(self._endpoints) > self._max_retained_endpoints and state.idle:
                self._endpoints.pop(key, None)
                continue
            retained.append(key)
        self._lru = retained


def _deliver_grant(future: asyncio.Future[float], queue_seconds: float) -> None:
    if not future.done():
        future.set_result(queue_seconds)


DEFAULT_ADMISSION_SCHEDULER = EndpointAdmissionScheduler()
