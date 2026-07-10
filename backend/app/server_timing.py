"""Per-step Server-Timing instrumentation for FastAPI handlers.

Provides an async context manager ``async_timed`` that records elapsed
wall-clock time onto ``request.state._server_timing_entries`` for later
inclusion in the ``Server-Timing`` response header by the
``RequestDurationMiddleware``.

Usage::

    async with async_timed(request, "my_step"):
        result = await some_expensive_call(...)
"""

import time
from contextlib import asynccontextmanager
from fastapi import Request


@asynccontextmanager
async def async_timed(request: Request, name: str):
    """Record elapsed milliseconds for the wrapped block onto *request.state*.

    The measured duration is appended (or updated, if *name* already exists)
    to ``request.state._server_timing_entries``, a list of ``(name, ms)``
    tuples that the ``RequestDurationMiddleware`` reads when building the
    ``Server-Timing`` response header.

    If ``request.state`` is not available the context manager is a no-op
    (no side effects).
    """
    if not hasattr(request, "state"):
        yield
        return

    start = time.monotonic()
    try:
        yield
    finally:
        elapsed_ms = (time.monotonic() - start) * 1000

        if not hasattr(request.state, "_server_timing_entries"):
            request.state._server_timing_entries = []

        # Deduplicate by name — replace if exists, otherwise append.
        entries = request.state._server_timing_entries
        for i, (existing_name, _) in enumerate(entries):
            if existing_name == name:
                entries[i] = (name, elapsed_ms)
                break
        else:
            entries.append((name, elapsed_ms))
