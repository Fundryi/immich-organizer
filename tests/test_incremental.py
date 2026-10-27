# pylint: disable=missing-docstring
from datetime import datetime, timedelta, timezone

import incremental

T = datetime(2026, 10, 27, 8, 0, tzinfo=timezone.utc)


def test_state_is_kept_per_account(tmp_path):
    path = str(tmp_path / "state" / "s.json")
    assert incremental.read_state(path, "a") is None
    assert incremental.window(None) is None
    incremental.write_state(path, "a", T)
    incremental.write_state(path, "b", T + timedelta(hours=1))
    assert incremental.read_state(path, "a") == T
    assert incremental.read_state(path, "b") == T + timedelta(hours=1)
    assert incremental.read_state(path, "c") is None
    assert incremental.window(T) == T - timedelta(minutes=10)
    assert not (tmp_path / "state" / "s.json.tmp").exists()


def test_unreadable_state_means_full_run(tmp_path, caplog):
    path = tmp_path / "s.json"
    path.write_text("{not json", encoding="utf-8")
    assert incremental.read_state(str(path), "a") is None
    assert "full run" in caplog.text
    path.write_text('{"a": "yesterday"}', encoding="utf-8")
    assert incremental.read_state(str(path), "a") is None
    incremental.write_state(str(path), "a", T)   # a bad file is replaced, not appended to
    assert incremental.read_state(str(path), "a") == T


def test_naive_state_is_read_as_utc():
    assert incremental.window(T.replace(tzinfo=None)) == T - timedelta(minutes=10)
