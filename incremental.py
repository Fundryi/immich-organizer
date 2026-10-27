"""
Incremental runs: ask Immich only for assets changed since the last run.

The state file holds, per account, the start time of the last completed run. A run with the flag fetches
assets with updatedAfter = that time minus an overlap, so a change during the last run is seen again.
No entry (first run, or an unreadable file) means a full run.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta, timezone

OVERLAP = timedelta(minutes=10)


def _load(path: str) -> dict[str, str]:
    try:
        with open(path, encoding="utf-8") as file:
            data = json.load(file)
        if not isinstance(data, dict):
            raise ValueError("not a JSON object")
        return data
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as e:
        logging.warning("State file %s unreadable, running a full run: %s", path, e)
        return {}


def read_state(path: str, account: str) -> datetime | None:
    """The start time of the last completed run of this account, or None when there is none."""
    try:
        return datetime.fromisoformat(_load(path)[account])
    except KeyError:
        return None
    except (ValueError, TypeError) as e:
        logging.warning("State file %s has a bad time for %s, running a full run: %s", path, account, e)
        return None


def write_state(path: str, account: str, started_at: datetime) -> None:
    """Records the start time of a run of this account that completed. The file is replaced in one step."""
    data = {**_load(path), account: started_at.isoformat()}
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as file:
        json.dump(data, file)
    os.replace(tmp, path)


def window(last_start: datetime | None, overlap: timedelta = OVERLAP) -> datetime | None:
    """The updatedAfter value for a run: the last start minus the overlap, or None for a full run."""
    if last_start is None:
        return None
    if last_start.tzinfo is None:
        last_start = last_start.replace(tzinfo=timezone.utc)
    return last_start - overlap


def now() -> datetime:
    """The current time in UTC, the clock Immich compares updatedAt against."""
    return datetime.now(timezone.utc)
