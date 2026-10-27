# pylint: disable=missing-docstring,too-many-arguments,too-many-positional-arguments,too-many-instance-attributes,too-few-public-methods
from __future__ import annotations
import asyncio
import logging
from pathlib import Path
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as NS
from uuid import uuid4

import pytest

from organizer import Organizer, fix_mojibake, folder_name_for, matches, natural_key, parse_rule_file

T0 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
BROKEN = "ŠÛ“Û˜b-ŽQ-"
FIXED = "丸呑話-参-"
ME = NS(name="alice", email="alice@example.com")
QUEUE_NAMES = ["sidecar", "metadataExtraction", "storageTemplateMigration", "thumbnailGeneration"]


def asset(path="/lib/a.jpg", kind="IMAGE", size=None, width=None, height=None, duration=None, vis="timeline", camera=(None, None), created=T0):
    return NS(id=uuid4(), original_path=path, original_file_name=path.rsplit("/", 1)[-1], type=NS(value=kind), visibility=NS(value=vis),
              width=width, height=height, duration=duration, file_created_at=created, exif_info=NS(file_size_in_byte=size, make=camera[0], model=camera[1]))


class FakeApi:
    """Records every call and serves canned tags, albums and queues."""

    def __init__(self, api_url="http://immich", api_key="key", config=None):
        self.api_url, self.api_key, self.config = api_url, api_key, config or NS(updated_after=None)
        self.folders = {}
        self.calls = []
        self.tags = []
        self.tag_members = {}
        self.albums = []
        self.album_members = {}
        self.busy_polls = 0
        self.paused = False
        self.queue_error = None
        self.untag_keeps_assets = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return None

    def add_tag(self, value, members=(), parent_id=None):
        tag = NS(id=uuid4(), name=value.rsplit("/", 1)[-1], value=value, parent_id=parent_id)
        self.tags.append(tag)
        self.tag_members[tag.id] = set(members)
        return tag

    def add_album(self, name, members=(), order="desc"):
        album = NS(id=uuid4(), album_name=name, asset_count=len(members), owner=None, order=NS(value=order))
        self.albums.append(album)
        self.album_members[album.id] = set(members)
        return album

    def fetch_my_user(self):
        return ME

    def fetch_tags(self):
        return list(self.tags)

    def upsert_tags(self, paths):
        self.calls.append(("upsert_tags", list(paths)))
        existing = {t.value: t for t in self.tags}
        return [existing.get(p) or self.add_tag(p) for p in paths]

    def tag_assets(self, tag_id, ids):
        self.calls.append(("tag_assets", tag_id, list(ids)))
        self.tag_members[tag_id] |= set(ids)

    def untag_assets(self, tag_id, ids):
        self.calls.append(("untag_assets", tag_id, list(ids)))
        if not self.untag_keeps_assets:
            self.tag_members[tag_id] -= set(ids)

    def delete_tag(self, tag_id):
        self.calls.append(("delete_tag", tag_id))

    def fetch_asset_ids(self, **search_options):
        self.calls.append(("fetch_asset_ids", search_options))
        if "tag_ids" in search_options:
            return set(self.tag_members.get(search_options["tag_ids"][0], ()))
        return set(self.album_members.get(search_options["album_ids"][0], ()))

    def fetch_albums(self):
        return list(self.albums)

    def fetch_assets_in_folder(self, folder):
        self.calls.append(("fetch_assets_in_folder", folder))
        return list(self.folders.get(folder, []))

    def create_album(self, name):
        self.calls.append(("create_album", name))
        return self.add_album(name).id

    def add_assets_to_album(self, album_id, ids):
        self.calls.append(("add_assets_to_album", album_id, list(ids)))
        return list(ids)

    def set_assets_visibility(self, ids, visibility):
        self.calls.append(("set_assets_visibility", list(ids), visibility.value))

    def rename_album(self, album_id, name):
        self.calls.append(("rename_album", album_id, name))

    def set_album_order(self, album_id, order):
        self.calls.append(("set_album_order", album_id, order))

    def set_asset_date(self, asset_id, date):
        self.calls.append(("set_asset_date", asset_id, date))

    def fetch_queues(self):
        self.calls.append(("fetch_queues",))
        if self.queue_error:
            raise self.queue_error
        busy = self.busy_polls > 0
        self.busy_polls -= 1
        return [NS(name=NS(value=n), is_paused=self.paused, statistics=NS(active=1000 if busy else 0, waiting=0, delayed=0)) for n in QUEUE_NAMES]

    def names(self):
        return [c[0] for c in self.calls]

    def writes(self):
        return [n for n in self.names() if not n.startswith("fetch_")]


class AlbumStub:
    def __init__(self, name):
        self.name = name
        self.override_name = None

    def get_final_name(self):
        return self.override_name or self.name


def queue_api_class(events, fail=False):
    # run_rules builds the queue client itself, so its enter and exit are seen through the class.
    class QueueApi(FakeApi):
        async def __aenter__(self):
            events.append(("enter", self.api_key))
            if fail:
                raise OSError("connection refused")
            return self

        async def __aexit__(self, *exc):
            events.append(("exit", self.api_key))

    return QueueApi


def run(org, assets):
    asyncio.run(org.run_rules(assets))


def make(tmp_path, text, api=None, dry_run=False, queue_api_key=None, post_regex=None):
    rules_file = tmp_path / "rules.yaml"
    rules_file.write_text(text, encoding="utf-8")
    config = NS(rules_file=str(rules_file), rules_dry_run=dry_run, queue_api_key=queue_api_key, find_assets_in_albums=True, find_archived_assets=True,
                album_name_post_regex=post_regex)
    org = Organizer(config, api or FakeApi())
    org.sleeps = []
    org.sleep = org.sleeps.append
    return org


# Rule file validation

def test_example_rule_file_is_valid(tmp_path):
    text = (Path(__file__).resolve().parents[1] / "rules.example.yaml").read_text(encoding="utf-8")
    org = make(tmp_path, text)
    assert org.keeps_empty_album("Screenshots")
    assert not org.keeps_empty_album("Twitter")


def test_empty_rule_file_is_valid(tmp_path):
    assert not make(tmp_path, "").keeps_empty_album("x")


@pytest.mark.parametrize("text, message", [
    ("colour: red\n", "unknown key 'colour'"),
    ("rules:\n  - match: {type: VIDEO}\n", "actions"),
    ("rules:\n  - actions: {visibility: locked}\n", "visibility"),
    ("rules:\n  - match: {type: AUDIO}\n    actions: {tag: x}\n", "type"),
    ("rules:\n  - match: {pathlike: x}\n    actions: {tag: x}\n", "unknown key 'pathlike'"),
    ("rules:\n  - match: {min_size_mb: big}\n    actions: {tag: x}\n", "min_size_mb"),
    ("repair_names: yes please\n", "repair_names"),
    ("rules:\n  - match: false\n    actions: {tag: x}\n", "match"),
    ("keep_empty_albums: false\n", "keep_empty_albums"),
    ("rules:\n  - match: {name_regex: 5}\n    actions: {tag: x}\n", "name_regex"),
    ("rules:\n  - match: {max_long_side: wide}\n    actions: {tag: x}\n", "max_long_side"),
    ("rules:\n  - actions: {folder_tag: {strip_prefix: /p/, levels: 1, prefix: 5}}\n", r"folder_tag\.prefix"),
    ("rules:\n  - actions: {folder_album: {levels: 1}}\n", "folder_album"),
    ("rules:\n  - actions: {folder_album: {strip_prefix: /p/, levels: 0}}\n", "folder_album"),
    ("rules:\n  - actions: {folder_album: {strip_prefix: /p/, levels: 1, prefix: [a]}}\n", r"folder_album\.prefix"),
    ("rules:\n  - actions: {folder_album: {strip_prefix: /p/, levels: 1, separator: ' - '}}\n", "unknown key 'separator'"),
    ("rules:\n  - actions: {folder_tag: {strip_prefix: /mnt/P, levels: 1}}\n", r"rules\[0\]\.actions\.folder_tag\.strip_prefix"),
    ("rules:\n  - actions: {album: ''}\n", r"rules\[0\]\.actions\.album"),
    ("rules:\n  - match: {name_like: [a, '']}\n    actions: {tag: x}\n", r"rules\[0\]\.match\.name_like"),
    ("rules:\n  - match: {extension: ''}\n    actions: {tag: x}\n", r"rules\[0\]\.match\.extension"),
    ("rules:\n  - actions: {folder_album: {strip_prefix: /p/, levels: 1, sep: x}}\n", "unknown key 'sep'"),
    ("rules:\n  - actions: {folder_album: /p/}\n", "folder_album"),
    ("rules:\n  - actions: {folder_album: {strip_prefix: /p/, levels: 1, order: up}}\n", r"folder_album\.order"),
    ("rules:\n  - actions: {folder_tag: {strip_prefix: /p/, levels: 1, order: asc}}\n", "unknown key 'order'"),
    ("rules:\n  - actions: {order_by_name: {strip_prefix: /p/, levels: 1, prefix: x}}\n", "unknown key 'prefix'"),
    ("rules:\n  - actions: {order_by_name: true}\n", "order_by_name"),
])
def test_rule_file_errors(tmp_path, text, message):
    with pytest.raises(ValueError, match=message):
        make(tmp_path, text)


# Matchers

def test_path_like_is_or_and_ignores_case():
    match = {"path_like": ["/twitter/", "/Reddit/"]}
    assert matches(match, asset("/x/Twitter/a.jpg"), ME)
    assert matches(match, asset("/x/reddit/a.jpg"), ME)
    assert not matches(match, asset("/x/pixiv/a.jpg"), ME)


def test_name_like_looks_at_the_file_name_only():
    assert matches({"name_like": "screenshot"}, asset("/x/Screenshot_1.png"), ME)
    assert not matches({"name_like": "screenshot"}, asset("/Screenshot/1.png"), ME)


def test_extension_ignores_case():
    assert matches({"extension": [".gif"]}, asset("/x/A.GIF"), ME)
    assert not matches({"extension": [".gif"]}, asset("/x/a.gifv"), ME)


def test_type():
    assert matches({"type": "VIDEO"}, asset(kind="VIDEO"), ME)
    assert not matches({"type": "VIDEO"}, asset(kind="IMAGE"), ME)


def test_keys_are_and():
    match = {"type": "VIDEO", "extension": ".mp4"}
    assert matches(match, asset("/a.mp4", kind="VIDEO"), ME)
    assert not matches(match, asset("/a.webm", kind="VIDEO"), ME)


def test_min_size_treats_unknown_size_as_no_match():
    big = 200 * 1024 * 1024
    assert matches({"min_size_mb": 100}, asset(size=big), ME)
    assert not matches({"min_size_mb": 100}, asset(size=1024), ME)
    assert not matches({"min_size_mb": 100}, asset(size=None), ME)
    no_exif = asset()
    no_exif.exif_info = None
    assert not matches({"min_size_mb": 100}, no_exif, ME)


def test_min_long_side():
    assert matches({"min_long_side": 3840}, asset(width=2160, height=3840), ME)
    assert not matches({"min_long_side": 3840}, asset(width=1920, height=1080), ME)
    assert not matches({"min_long_side": 3840}, asset(), ME)


def test_duration_is_in_milliseconds():
    assert matches({"max_duration_s": 10}, asset(duration=6100), ME)
    assert not matches({"min_duration_s": 600}, asset(duration=6100), ME)
    assert matches({"min_duration_s": 600}, asset(duration=700_000), ME)
    assert not matches({"max_duration_s": 10}, asset(duration=None), ME)


def test_account_matches_name_or_email():
    assert matches({"account": "Alice@Example.com"}, asset(), ME)
    assert matches({"account": ["someone", "ALICE"]}, asset(), ME)
    assert not matches({"account": "someone"}, asset(), ME)


def test_bad_name_regex_names_the_rule(tmp_path):
    with pytest.raises(ValueError) as error:
        make(tmp_path, "rules:\n  - actions: {tag: x}\n  - match: {name_regex: ['ok', '(']}\n    actions: {tag: x}\n")
    assert "rules[1].match.name_regex" in str(error.value)
    assert "missing )" in str(error.value)


def test_too_large_name_regex_names_the_rule(tmp_path):
    with pytest.raises(ValueError, match=r"rules\[0\]\.match\.name_regex"):
        make(tmp_path, "rules:\n  - match: {name_regex: 'a{999999999999999999999999}'}\n    actions: {tag: x}\n")


def test_name_regex_is_case_sensitive_on_the_original_name():
    assert matches({"name_regex": r"^IMG_\d+"}, asset("/x/IMG_0001.JPG"), ME)
    assert not matches({"name_regex": r"^IMG_\d+"}, asset("/x/img_0001.jpg"), ME)
    assert not matches({"name_regex": "IMG"}, asset("/IMG/a.jpg"), ME)


def test_name_regex_list_is_or_and_compiled_once():
    rule = parse_rule_file({"rules": [{"match": {"name_regex": [r"^\d{6}_p\d+", "^Screenshot"]}, "actions": {"tag": "x"}}]})["rules"][0]
    assert all(hasattr(pattern, "search") for pattern in rule["match"]["name_regex"])
    assert matches(rule["match"], asset("/x/123456_p0.png"), ME)
    assert matches(rule["match"], asset("/x/Screenshot_1.png"), ME)
    assert not matches(rule["match"], asset("/x/screenshot_1.png"), ME)


def test_make_and_model_like():
    phone = asset(camera=("Google", "Pixel 8 Pro"))
    assert matches({"make_like": "google", "model_like": ["iphone", "PIXEL 8"]}, phone, ME)
    assert not matches({"make_like": "apple"}, phone, ME)
    assert not matches({"make_like": "google"}, asset(), ME)
    assert not matches({"model_like": "pixel"}, asset(camera=("Google", None)), ME)
    no_exif = asset()
    no_exif.exif_info = None
    assert not matches({"make_like": "google"}, no_exif, ME)
    assert not matches({"model_like": "pixel"}, no_exif, ME)


def test_max_long_side():
    assert matches({"max_long_side": 1920}, asset(width=1080, height=1920), ME)
    assert not matches({"max_long_side": 1920}, asset(width=1921, height=1080), ME)
    assert not matches({"max_long_side": 1920}, asset(), ME)
    assert not matches({"max_long_side": 1920}, asset(width=1000), ME)
    assert not matches({"max_long_side": 1920}, asset(height=1000), ME)
    assert matches({"min_long_side": 1280, "max_long_side": 1920}, asset(width=1280, height=720), ME)


def test_folder_tag():
    spec = {"strip_prefix": "/mnt/Pictures/", "levels": 2}
    assert folder_name_for(asset("/mnt/Pictures/Comics/artist/x/a.jpg"), spec) == "Comics/artist"
    assert folder_name_for(asset("/mnt/Pictures/Comics/a.jpg"), spec) is None
    assert folder_name_for(asset("/other/Comics/artist/a.jpg"), spec) is None
    assert folder_name_for(asset("/mnt/Picturesx/Comics/artist/a.jpg"), spec) is None


def test_folder_tag_prefix():
    spec = {"strip_prefix": "/mnt/Creators/", "levels": 1, "prefix": "Source/Creators/"}
    assert folder_name_for(asset("/mnt/Creators/4242/post/a.jpg"), spec) == "Source/Creators/4242"
    assert folder_name_for(asset("/mnt/Creators/a.jpg"), spec) is None


def test_folder_album_name():
    def album(path, **extra):
        rules = parse_rule_file({"rules": [{"actions": {"folder_album": {"strip_prefix": "/mnt/Pictures/", "levels": 2, **extra}}}]})
        return folder_name_for(asset(path), rules["rules"][0]["actions"]["folder_album"], " / ")
    assert album("/mnt/Pictures/Comics/Artist/x/a.jpg") == "Comics / Artist"
    assert album("/mnt/Pictures/Comics/a.jpg") is None
    assert album("/mnt/Pictures/Comics/Artist/a.jpg", prefix="Art: ") == "Art: Comics / Artist"


# Planning, skip logic, writes

def test_tag_skips_assets_that_already_have_it(tmp_path):
    api = FakeApi()
    a1, a2 = asset("/a.gif"), asset("/b.gif")
    gif = api.add_tag("GIF", members={a1.id})
    run(make(tmp_path, "rules:\n  - match: {extension: .gif}\n    actions: {tag: GIF}\n", api), [a1, a2])
    assert api.writes() == ["tag_assets"]
    assert ("tag_assets", gif.id, [a2.id]) in api.calls


def test_folder_tag_is_created_and_applied(tmp_path):
    api = FakeApi()
    a = asset("/mnt/Pictures/Comics/artist/a.jpg")
    text = "rules:\n  - actions: {folder_tag: {strip_prefix: /mnt/Pictures/, levels: 2}}\n"
    run(make(tmp_path, text, api), [a])
    assert ("upsert_tags", ["Comics/artist"]) in api.calls
    assert api.writes() == ["upsert_tags", "tag_assets"]


def test_album_skips_assets_already_in_it(tmp_path):
    api = FakeApi()
    a1, a2 = asset("/Twitter/a.jpg"), asset("/Twitter/b.jpg")
    album = api.add_album("Twitter", members={a1.id})
    run(make(tmp_path, "rules:\n  - match: {path_like: /Twitter/}\n    actions: {album: Twitter}\n", api), [a1, a2])
    assert api.writes() == ["add_assets_to_album"]
    assert ("add_assets_to_album", album.id, [a2.id]) in api.calls


def test_folder_album_feeds_the_album_plan_and_skips_done(tmp_path):
    api = FakeApi()
    a1, a2 = asset("/mnt/P/Creators/1/a.jpg"), asset("/mnt/P/Creators/1/b.jpg")
    a3, flat = asset("/mnt/P/Creators/2/c.jpg"), asset("/mnt/P/Creators/d.jpg")
    album = api.add_album("Src: Creators / 1", members={a1.id})
    text = "rules:\n  - actions: {folder_album: {strip_prefix: /mnt/P/, levels: 2, prefix: 'Src: '}}\n"
    run(make(tmp_path, text, api), [a1, a2, a3, flat])
    assert sorted(api.writes()) == ["add_assets_to_album", "add_assets_to_album", "create_album"]
    assert ("add_assets_to_album", album.id, [a2.id]) in api.calls
    created = next(a for a in api.albums if a.album_name == "Src: Creators / 2")
    assert ("add_assets_to_album", created.id, [a3.id]) in api.calls


def test_missing_album_is_created(tmp_path):
    api = FakeApi()
    run(make(tmp_path, "rules:\n  - actions: {album: New}\n", api), [asset()])
    assert api.writes() == ["create_album", "add_assets_to_album"]


def test_visibility_skips_assets_already_set(tmp_path):
    api = FakeApi()
    a1, a2 = asset(vis="archive"), asset(vis="timeline")
    run(make(tmp_path, "rules:\n  - actions: {visibility: archive}\n", api), [a1, a2])
    assert api.writes() == ["set_assets_visibility"]
    assert ("set_assets_visibility", [a2.id], "archive") in api.calls


def test_last_visibility_wins_and_conflict_warns(tmp_path, caplog):
    api = FakeApi()
    a = asset("/x/a.jpg")
    text = "rules:\n  - actions: {visibility: archive}\n  - actions: {visibility: hidden}\n"
    with caplog.at_level(logging.WARNING):
        run(make(tmp_path, text, api), [a])
    assert ("set_assets_visibility", [a.id], "hidden") in api.calls
    assert "/x/a.jpg" in caplog.text


def test_dry_run_writes_nothing(tmp_path):
    api = FakeApi()
    api.add_tag(BROKEN, members={uuid4()})
    text = "repair_names: true\nrules:\n  - actions: {tag: New, album: New, visibility: archive}\n"
    run(make(tmp_path, text, api, dry_run=True), [asset(), asset()])
    assert not api.writes()
    assert "fetch_queues" not in api.names()


def test_writes_in_batches_of_50_with_a_queue_wait_after_each(tmp_path):
    api = FakeApi()
    assets = [asset() for _ in range(120)]
    run(make(tmp_path, "rules:\n  - actions: {tag: Many}\n", api), assets)
    paced = [n for n in api.names() if n in ("fetch_queues", "upsert_tags", "tag_assets")]
    assert paced == ["fetch_queues", "fetch_queues", "upsert_tags", "tag_assets", "fetch_queues", "tag_assets", "fetch_queues", "tag_assets", "fetch_queues"]
    assert [len(c[2]) for c in api.calls if c[0] == "tag_assets"] == [50, 50, 20]


def test_queue_wait_polls_until_idle(tmp_path):
    api = FakeApi()
    api.busy_polls = 3
    org = make(tmp_path, "rules:\n  - actions: {tag: T}\n", api)
    run(org, [asset()])
    assert org.sleeps == [5.0, 5.0]
    assert "tag_assets" in api.writes()


def test_pre_flight_failure_writes_nothing(tmp_path):
    api = FakeApi()
    api.queue_error = RuntimeError("403 not an admin")
    run(make(tmp_path, "rules:\n  - actions: {tag: T, album: A}\n", api), [asset()])
    assert not api.writes()


def test_paused_queue_stops_the_rules_with_an_error(tmp_path, caplog):
    api = FakeApi()
    api.paused = True
    with caplog.at_level(logging.ERROR):
        run(make(tmp_path, "rules:\n  - actions: {tag: T}\n", api), [asset()])
    assert "tag_assets" not in api.writes()
    assert "paused" in caplog.text


def test_queue_timeout_stops_the_rules_with_an_error(tmp_path, caplog):
    api = FakeApi()
    api.busy_polls = 10**6
    org = make(tmp_path, "rules:\n  - actions: {tag: T}\n", api)
    org.idle_timeout = 0
    with caplog.at_level(logging.ERROR):
        run(org, [asset()])
    assert "tag_assets" not in api.writes()
    assert "over 300 jobs" in caplog.text


def test_missing_queue_counts_as_busy(tmp_path, caplog):
    api = FakeApi()
    api.fetch_queues = lambda: []
    org = make(tmp_path, "rules:\n  - actions: {tag: T}\n", api)
    org.idle_timeout = 0
    with caplog.at_level(logging.ERROR):
        run(org, [asset()])
    assert not api.writes()
    assert "sidecar (missing)" in caplog.text


def test_without_queue_key_the_run_client_reads_the_queues(tmp_path):
    events = []
    api = queue_api_class(events)()
    run(make(tmp_path, "rules:\n  - actions: {tag: T}\n", api), [asset()])
    assert not events
    assert "fetch_queues" in api.names() and "tag_assets" in api.writes()


def test_queue_key_client_is_entered_and_closed(tmp_path):
    events = []
    api = queue_api_class(events)()
    run(make(tmp_path, "rules:\n  - actions: {tag: T}\n", api, queue_api_key="admin"), [asset()])
    assert events == [("enter", "admin"), ("exit", "admin")]
    assert "fetch_queues" not in api.names() and "tag_assets" in api.writes()


def test_queue_key_connect_failure_writes_nothing(tmp_path, caplog):
    events = []
    api = queue_api_class(events, fail=True)()
    with caplog.at_level(logging.ERROR):
        run(make(tmp_path, "rules:\n  - actions: {tag: T}\n", api, queue_api_key="admin"), [asset()])
    assert events == [("enter", "admin"), ("exit", "admin")]
    assert not api.writes()
    assert "QUEUE_API_KEY" in caplog.text


# Name repair

@pytest.mark.parametrize("text, expected", [
    (BROKEN, FIXED),
    ("New / RJ123456 / ŠÛ“Û˜b4 JPG", "New / RJ123456 / 丸呑話4 JPG"),
    ("Albums / 2024 / You’re Welcome!", None),
    ("Café", None),
    ("plain", None),
    ("Mädchen Bären Käse", None),
    ("Säle Täler Wälder", None),
])
def test_fix_mojibake(text, expected):
    assert fix_mojibake(text) == expected


def test_album_repair_renames_existing_album(tmp_path):
    api = FakeApi()
    album = api.add_album(BROKEN)
    model = AlbumStub(BROKEN)
    make(tmp_path, "repair_names: true\n", api).repair_album_names(api.albums, [model])
    assert api.calls == [("rename_album", album.id, FIXED)]
    assert album.album_name == FIXED
    assert model.override_name == FIXED


def test_album_repair_dry_run_changes_nothing(tmp_path):
    api = FakeApi()
    album = api.add_album(BROKEN)
    model = AlbumStub(BROKEN)
    make(tmp_path, "repair_names: true\n", api, dry_run=True).repair_album_names(api.albums, [model])
    assert not api.calls
    assert album.album_name == BROKEN
    assert model.override_name is None


def test_album_repair_dry_run_uses_fixed_name_when_broken_album_is_missing(tmp_path):
    api = FakeApi()
    model = AlbumStub(BROKEN)
    make(tmp_path, "repair_names: true\n", api, dry_run=True).repair_album_names(api.albums, [model])
    assert not api.calls
    assert model.override_name == FIXED


def test_album_repair_skips_when_both_names_exist(tmp_path, caplog):
    api = FakeApi()
    api.add_album(BROKEN)
    api.add_album(FIXED)
    model = AlbumStub(BROKEN)
    with caplog.at_level(logging.WARNING):
        make(tmp_path, "repair_names: true\n", api).repair_album_names(api.albums, [model])
    assert not api.calls
    assert model.override_name is None
    assert "exist" in caplog.text


def test_album_repair_uses_existing_fixed_album(tmp_path):
    api = FakeApi()
    api.add_album(FIXED)
    model = AlbumStub(BROKEN)
    make(tmp_path, "repair_names: true\n", api).repair_album_names(api.albums, [model])
    assert not api.calls
    assert model.override_name == FIXED


def test_tag_repair_pipeline_order(tmp_path):
    api = FakeApi()
    members = {uuid4() for _ in range(60)}
    old = api.add_tag(BROKEN, members=members)
    run(make(tmp_path, "repair_names: true\n", api), [])
    steps = [n for n in api.names() if n != "fetch_queues"]
    assert steps == ["upsert_tags", "fetch_asset_ids", "tag_assets", "tag_assets",
                     "fetch_asset_ids", "untag_assets", "untag_assets", "fetch_asset_ids", "delete_tag"]
    assert ("upsert_tags", [FIXED]) in api.calls
    assert api.calls[-1] == ("delete_tag", old.id)
    new = next(t for t in api.tags if t.value == FIXED)
    assert api.tag_members[new.id] == members


def test_tag_repair_keeps_tag_that_still_has_assets(tmp_path):
    api = FakeApi()
    api.untag_keeps_assets = True
    api.add_tag("Old", members={uuid4()})
    run(make(tmp_path, "rename_tags: {Old: New}\n", api), [])
    assert "untag_assets" in api.writes()
    assert "delete_tag" not in api.writes()


def test_tag_repair_skips_parent_tags(tmp_path, caplog):
    api = FakeApi()
    parent = api.add_tag("Old", members={uuid4()})
    api.add_tag("Old/Child", parent_id=str(parent.id))
    with caplog.at_level(logging.WARNING):
        run(make(tmp_path, "rename_tags: {Old: New}\n", api), [])
    assert not api.writes()
    assert "parent" in caplog.text


def test_tag_repair_merges_into_existing_tag(tmp_path, caplog):
    api = FakeApi()
    members = {uuid4(), uuid4()}
    old = api.add_tag("Old", members=members)
    new = api.add_tag("New", members={uuid4()})
    with caplog.at_level(logging.INFO):
        run(make(tmp_path, "rename_tags: {Old: New}\n", api), [])
    assert api.tag_members[new.id] >= members
    assert api.calls[-1] == ("delete_tag", old.id)
    assert len(api.tags) == 2
    assert "merging into existing tag" in caplog.text


def test_rule_tag_that_is_also_repaired_writes_nothing(tmp_path, caplog):
    api = FakeApi()
    api.add_tag("Old", members={uuid4()})
    with caplog.at_level(logging.ERROR):
        run(make(tmp_path, "rename_tags: {Old: New}\nrules:\n  - actions: {tag: Old}\n", api), [asset()])
    assert not api.writes()
    assert "fetch_queues" not in api.names()
    assert "both assign and repair the tags Old" in caplog.text


def test_broken_folder_tag_is_repaired(tmp_path):
    api = FakeApi()
    spec = "{strip_prefix: /mnt/Pictures/, levels: 1}"
    text = f"repair_names: true\nrules:\n  - actions: {{folder_tag: {spec}, folder_album: {spec}}}\n"
    run(make(tmp_path, text, api), [asset(f"/mnt/Pictures/{BROKEN}/a.jpg")])
    assert ("upsert_tags", [FIXED]) in api.calls
    assert ("create_album", FIXED) in api.calls


def test_folder_repair_is_per_folder(tmp_path):
    # Neither a non-ASCII prefix nor a correct Japanese sibling folder may block the repair.
    api = FakeApi()
    text = ("repair_names: true\nrules:\n  - actions: {folder_tag: {strip_prefix: /mnt/P/, levels: 1, prefix: 'Künstler/'},"
            " folder_album: {strip_prefix: /mnt/Q/, levels: 2}}\n")
    run(make(tmp_path, text, api), [asset(f"/mnt/P/{BROKEN}/a.jpg"), asset(f"/mnt/Q/{FIXED}/{BROKEN}/b.jpg")])
    assert ("upsert_tags", [f"Künstler/{FIXED}"]) in api.calls
    assert ("create_album", f"{FIXED} / {FIXED}") in api.calls


def test_album_name_taken_twice_is_skipped(tmp_path, caplog):
    api = FakeApi()
    for name in ("Dup", "Dup", "Twin", "Twin"):
        api.add_album(name)
    text = "rules:\n  - actions: {album: Dup, folder_album: {strip_prefix: /mnt/P/, levels: 1}}\n"
    with caplog.at_level(logging.WARNING):
        run(make(tmp_path, text, api), [asset("/mnt/P/Twin/a.jpg")])
    assert not api.writes()
    assert "named 'Dup'" in caplog.text and "named 'Twin'" in caplog.text


def test_repaired_folder_album_waits_for_the_broken_album_rename(tmp_path, caplog):
    api = FakeApi()
    api.add_album(BROKEN)
    text = "repair_names: true\nrules:\n  - actions: {folder_album: {strip_prefix: /mnt/P/, levels: 1}}\n"
    with caplog.at_level(logging.WARNING):
        run(make(tmp_path, text, api), [asset(f"/mnt/P/{BROKEN}/a.jpg")])
    assert not api.writes()
    assert "rename it first" in caplog.text


def test_tag_repair_keeps_old_tag_when_copy_failed(tmp_path):
    api = FakeApi()
    api.add_tag("Old", members={uuid4()})
    api.tag_assets = lambda tag_id, ids: None
    run(make(tmp_path, "rename_tags: {Old: New}\n", api), [])
    assert api.writes() == ["upsert_tags"]


def test_path_not_like_excludes_folders():
    assert matches({"path_not_like": ["/Twitter/"]}, asset("/lib/Download/a.jpg"), ME)
    assert not matches({"path_not_like": ["/twitter/"]}, asset("/lib/Twitter/a.jpg"), ME)


def test_max_size_mb_boundary_and_unknown_size():
    assert matches({"max_size_mb": 100}, asset(size=100 * 1024 * 1024), ME)
    assert not matches({"max_size_mb": 100}, asset(size=100 * 1024 * 1024 + 1), ME)
    assert not matches({"max_size_mb": 100}, asset(size=None), ME)


def test_capture_group_tag(tmp_path):
    api = FakeApi()
    text = chr(10).join(["rules:", "  - match:", "      name_regex: ['" + r"^1\d{9}\.([a-z0-9~.-]+)_" + "']", "    actions:", "      tag: ['Source/Example', 'Artist/{1}']"])
    org = make(tmp_path, text, api=api, dry_run=True)
    plan = org._plan([asset("/lib/1700000000.some-artist_title.jpg"), asset("/lib/1700000000._title.jpg"), asset("/lib/other.jpg")]).tags  # pylint: disable=protected-access
    assert len(plan["Artist/some-artist"]) == 1
    assert len(plan["Source/Example"]) == 1
    assert "Artist/" not in "".join(k for k in plan if k != "Artist/some-artist")


def test_capture_tag_without_regex_is_rejected():
    with pytest.raises(ValueError, match="capture"):
        parse_rule_file({"rules": [{"match": {"path_like": ["/x/"]}, "actions": {"tag": "Artist/{1}"}}]})


def test_natural_key_reads_numbers_as_numbers():
    names = ["10.png", "2.png", "05319b.png", "05319a.png", "164 v3.jpg", "164 v10.jpg"]
    assert sorted(names, key=natural_key) == ["2.png", "10.png", "164 v3.jpg", "164 v10.jpg", "05319a.png", "05319b.png"]


def test_order_by_name_sets_dates_in_name_order(tmp_path, caplog):
    api = FakeApi()
    s = timedelta(seconds=1)
    # 02 was saved before 01, and 03 sits in a variant subfolder of the same post
    p1, p2 = asset("/mnt/G/Post/01a.png", created=T0 + 5 * s), asset("/mnt/G/Post/02a.png", created=T0)
    p3 = asset("/mnt/G/Post/Internal/03b.png", created=T0 + 9 * s)
    fine = [asset("/mnt/G/Fine/01.png", created=T0), asset("/mnt/G/Fine/02.png", created=T0 + s)]
    mixed = [asset("/mnt/G/Mixed/02.png", created=T0), asset("/mnt/G/Mixed/cover.png", created=T0)]
    text = "rules:\n  - actions: {order_by_name: {strip_prefix: /mnt/G/, levels: 1}}\n"
    with caplog.at_level(logging.INFO):
        run(make(tmp_path, text, api), [p1, p2, p3, *fine, *mixed])
    dates = {c[1]: c[2] for c in api.calls if c[0] == "set_asset_date"}
    assert dates == {p1.id: T0.isoformat(), p2.id: (T0 + s).isoformat(), p3.id: (T0 + 2 * s).isoformat()}
    assert "order '/mnt/G/Mixed' skipped" in caplog.text
    assert "3 date changes" in caplog.text


def test_order_by_name_leaves_pages_already_on_their_date(tmp_path):
    api = FakeApi()
    s = timedelta(seconds=1)
    p1, p2, p3 = asset("/mnt/G/P/1.png", created=T0), asset("/mnt/G/P/2.png", created=T0 + 3 * s), asset("/mnt/G/P/3.png", created=T0 + 2 * s)
    run(make(tmp_path, "rules:\n  - actions: {order_by_name: {strip_prefix: /mnt/G/, levels: 1}}\n", api), [p1, p2, p3])
    # Page 3 already sits on its target second, only page 2 moves
    assert ("set_asset_date", p2.id, (T0 + s).isoformat()) in api.calls
    assert api.writes() == ["set_asset_date"]


def test_order_by_name_dry_run_writes_nothing(tmp_path):
    api = FakeApi()
    pages = [asset("/mnt/G/P/1.png", created=T0 + timedelta(seconds=1)), asset("/mnt/G/P/2.png", created=T0)]
    run(make(tmp_path, "rules:\n  - actions: {order_by_name: {strip_prefix: /mnt/G/, levels: 1}}\n", api, dry_run=True), pages)
    assert api.writes() == []


def test_folder_album_order_is_set_on_new_and_existing_albums(tmp_path):
    api = FakeApi()
    a1, a2, a3 = asset("/mnt/P/A/1.jpg"), asset("/mnt/P/B/2.jpg"), asset("/mnt/P/C/3.jpg")
    old = api.add_album("A", members={a1.id}, order="desc")
    fine = api.add_album("C", members={a3.id}, order="asc")
    text = "rules:\n  - actions: {folder_album: {strip_prefix: /mnt/P/, levels: 1, order: asc}}\n"
    run(make(tmp_path, text, api), [a1, a2, a3])
    created = next(a for a in api.albums if a.album_name == "B")
    orders = [c for c in api.calls if c[0] == "set_album_order"]
    assert sorted(orders, key=lambda c: str(c[1])) == sorted([("set_album_order", old.id, "asc"), ("set_album_order", created.id, "asc")], key=lambda c: str(c[1]))
    assert fine.id not in [c[1] for c in orders]


def test_folder_album_applies_the_upstream_post_regex(tmp_path):
    api = FakeApi()
    existing = api.add_album("Comics / Da ball   3")   # two underscores and a space
    page = asset("/mnt/P/Comics/Da ball_ _3_/1.png")   # the trailing underscore becomes a space that upstream strips
    text = "rules:\n  - actions: {folder_album: {strip_prefix: /mnt/P/, levels: 2}}\n"
    run(make(tmp_path, text, api, post_regex=[["_", " "]]), [page])
    assert api.writes() == ["add_assets_to_album"]
    assert ("add_assets_to_album", existing.id, [page.id]) in api.calls


def test_order_by_name_groups_do_not_double_or_merge_across_rules(tmp_path):
    api = FakeApi()
    s = timedelta(seconds=1)
    comic = [asset("/mnt/C/Post/2.png", created=T0), asset("/mnt/C/Post/1.png", created=T0 + s)]
    scan = asset("/mnt/S/Post/1.png", created=T0 + 5 * s)   # same relative folder name under another prefix
    text = ("rules:\n"
            "  - actions: {order_by_name: {strip_prefix: /mnt/C/, levels: 1}}\n"
            "  - match: {extension: .png}\n    actions: {order_by_name: {strip_prefix: /mnt/C/, levels: 1}}\n"
            "  - actions: {order_by_name: {strip_prefix: /mnt/S/, levels: 1}}\n")
    run(make(tmp_path, text, api), [*comic, scan])
    dates = {c[1]: c[2] for c in api.calls if c[0] == "set_asset_date"}
    assert dates == {comic[1].id: T0.isoformat(), comic[0].id: (T0 + s).isoformat()}


def test_incremental_run_completes_the_post_and_checks_only_the_window(tmp_path):
    api = FakeApi(config=NS(updated_after=T0))
    s = timedelta(seconds=1)
    old = [asset("/mnt/G/P/1.png", created=T0 - 9 * s), asset("/mnt/G/P/2.png", created=T0 - 8 * s)]
    new = asset("/mnt/G/P/0.png", created=T0 + s)   # the one changed page sorts first by name
    api.folders["/mnt/G/P/"] = old + [new]
    tag = api.add_tag("New", members={old[0].id})
    text = "rules:\n  - actions: {tag: New, order_by_name: {strip_prefix: /mnt/G/, levels: 1}}\n"
    run(make(tmp_path, text, api), [new])
    dates = {c[1]: c[2] for c in api.calls if c[0] == "set_asset_date"}
    assert dates == {new.id: (T0 - 9 * s).isoformat(), old[0].id: (T0 - 8 * s).isoformat(), old[1].id: (T0 - 7 * s).isoformat()}
    assert ("fetch_asset_ids", {"tag_ids": [tag.id], "updated_after": T0}) in api.calls
    assert ("tag_assets", tag.id, [new.id]) in api.calls


def test_full_run_does_not_fetch_folders(tmp_path):
    api = FakeApi()
    run(make(tmp_path, "rules:\n  - actions: {order_by_name: {strip_prefix: /mnt/G/, levels: 1}}\n", api), [asset("/mnt/G/P/1.png")])
    assert "fetch_assets_in_folder" not in api.names()


def test_post_completion_respects_the_rule_match(tmp_path):
    api = FakeApi(config=NS(updated_after=T0))
    s = timedelta(seconds=1)
    new = asset("/mnt/G/P/2.png", created=T0 + s)
    kept, excluded = asset("/mnt/G/P/1.png", created=T0 + 2 * s), asset("/mnt/G/P/cover.jpg", created=T0)
    api.folders["/mnt/G/P/"] = [new, kept, excluded]
    text = "rules:\n  - match: {extension: .png}\n    actions: {order_by_name: {strip_prefix: /mnt/G/, levels: 1}}\n"
    run(make(tmp_path, text, api), [new])
    dates = {c[1]: c[2] for c in api.calls if c[0] == "set_asset_date"}
    assert dates == {kept.id: (T0 + s).isoformat(), new.id: (T0 + 2 * s).isoformat()}


def test_run_rules_reports_whether_the_writes_finished(tmp_path):
    api = FakeApi()
    assert asyncio.run(make(tmp_path, "rules:\n  - actions: {tag: X}\n", api).run_rules([asset()])) is True
    api.paused = True
    assert asyncio.run(make(tmp_path, "rules:\n  - actions: {tag: X}\n", api).run_rules([asset()])) is False
    assert asyncio.run(make(tmp_path, "rules:\n  - actions: {tag: X}\n", api, dry_run=True).run_rules([asset()])) is True
