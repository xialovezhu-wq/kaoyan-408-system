"""Invocation-local diagnostics; never part of learning evidence or idempotency."""
from contextlib import contextmanager
from time import perf_counter


@contextmanager
def measure(timings, stage):
    if timings is None:
        yield
        return
    started = perf_counter()
    try:
        yield
    finally:
        timings[stage] = round(timings.get(stage, 0) + (perf_counter() - started) * 1000, 3)


def finish(timings, started):
    timings["total_ms"] = round((perf_counter() - started) * 1000, 3)
    return timings
