"""Helpers for safely exposing job state through the local HTTP API."""
from copy import deepcopy


_INTERNAL_JOB_FIELDS = frozenset({"runner", "lock_reals"})


def public_job_snapshot(job):
    """Copy the JSON-facing job state without runtime-only Python objects.

    ``runner`` is a callable used internally to resume a paused batch. It must
    remain in the server-side job record, but Flask's JSON provider cannot
    serialize it. Output-lock paths are internal synchronization state too.
    Deep-copying the public fields gives the HTTP thread a stable snapshot of
    mutable logs/results while the worker continues updating the live record.
    """
    return deepcopy({
        key: value
        for key, value in job.items()
        if key not in _INTERNAL_JOB_FIELDS
    })
