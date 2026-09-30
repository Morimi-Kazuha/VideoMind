"""Reconnect-safe read projection over the existing durable event list.

Execution identity is the existing lifecycle request_id; attempt/stage are
the existing event envelope. No second event sequence is introduced.
"""
from __future__ import annotations

import asyncio
import time

from dovideo.domain import TaskEvent
from dovideo.application.task_lifecycle import TaskLifecycleEvent
from dovideo.application.status_projection import TaskStatusProjection


async def durable_task_events(key, lifecycle_store, status_query, events):
    lifecycle, initial = await _snapshot(key, lifecycle_store, status_query)
    request_id = initial.request_id
    projection = TaskStatusProjection()
    projection.apply(initial)
    yield initial
    if initial.terminal:
        return
    seen = set()
    heartbeat_started = time.monotonic()
    while True:
        lifecycle, snapshot = await _snapshot(key, lifecycle_store, status_query)
        if snapshot.request_id != request_id:
            return  # A new execution requires a fresh initial snapshot.
        before = projection.snapshot(key)
        projection.apply(snapshot)
        if projection.snapshot(key) != before:
            yield snapshot
        if snapshot.terminal:
            return
        values = await events.read(key)
        latest = await lifecycle_store.load_lifecycle(key)
        if latest != lifecycle:
            continue  # Retry/transition during the read: refresh before replay.
        seen.intersection_update((value.attempt, value.event.model_dump_json()) for value in values)
        for value in values:
            if value.request_id != request_id:
                continue
            signature = (value.attempt, value.event.model_dump_json())
            if signature in seen:
                continue
            seen.add(signature)
            before = projection.snapshot(key)
            projection.apply(value)
            if projection.snapshot(key) == before:
                continue
            yield value
            if value.terminal:
                return
            if await lifecycle_store.load_lifecycle(key) != lifecycle:
                break
        if time.monotonic() - heartbeat_started >= 15.0:
            heartbeat_started = time.monotonic()
            yield None
        await asyncio.sleep(0.5)


async def _snapshot(key, lifecycle_store, status_query):
    # Compare the complete lifecycle, including attempt, rather than only
    # request_id: a legitimate retry keeps the same request identity.
    while True:
        lifecycle = await lifecycle_store.load_lifecycle(key)
        status = await status_query.current(key.media_id, key.goal, key.mode)
        if await lifecycle_store.load_lifecycle(key) == lifecycle:
            return lifecycle, TaskLifecycleEvent(
                key=key, event=TaskEvent.of(status, None if lifecycle is None else lifecycle.stage),
                attempt=0 if lifecycle is None else lifecycle.attempt,
                retryable=False if lifecycle is None else lifecycle.retryable,
                request_id=None if lifecycle is None else lifecycle.request_id,
            )
