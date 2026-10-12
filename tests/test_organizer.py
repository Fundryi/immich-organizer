# pylint: disable=missing-docstring,too-many-arguments,too-many-positional-arguments,too-many-instance-attributes,too-few-public-methods
from __future__ import annotations
import asyncio
import logging
from pathlib import Path
from types import SimpleNamespace as NS
from uuid import uuid4

import pytest

from organizer import Organizer, fix_mojibake, folder_tag_for, matches

BROKEN = "ŠÛ“Û˜b-ŽQ-"
FIXED = "丸呑話-参-"
ME = NS(name="alice", email="alice@example.com")
QUEUE_NAMES = ["sidecar", "metadataExtraction", "storageTemplateMigration", "thumbnailGeneration"]


def asset(path="/lib/a.jpg", kind="IMAGE", size=None, width=None, height=None, duration=None, vis="timeline"):
    return NS(id=uuid4(), original_path=path, original_file_name=path.rsplit("/", 1)[-1], type=NS(value=kind),
              visibility=NS(value=vis), width=width, height=height, duration=duration, exif_info=NS(file_size_in_byte=size))


class FakeApi:
    """Records every call and serves canned tags, albums and queues."""

    def __init__(self, api_url="http://immich", api_key="key", config=None):
        self.api_url, self.api_key, self.config = api_url, api_key, config
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

    def add_album(self, name, members=()):
        album = NS(id=uuid4(), album_name=name, asset_count=len(members), owner=None)
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

    def fetch_queues(self):
        self.calls.append(("fetch_queues",))
        if self.queue_error:
            raise self.queue_error
        busy = self.busy_polls > 0
        self.busy_polls -= 1
        return [NS(name=NS(value=n), is_paused=self.paused, statistics=NS(active=int(busy), waiting=0, delayed=0)) for n in QUEUE_NAMES]

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


def make(tmp_path, text, api=None, dry_run=False, queue_api_key=None):
    rules_file = tmp_path / "rules.yaml"
    rules_file.write_text(text, encoding="utf-8")
    config = NS(rules_file=str(rules_file), rules_dry_run=dry_run, queue_api_key=queue_api_key, find_assets_in_albums=True, find_archived_assets=True)
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


def test_folder_tag():
    spec = {"strip_prefix": "/mnt/Pictures/", "levels": 2}
    assert folder_tag_for(asset("/mnt/Pictures/Vore/artist/x/a.jpg"), spec) == "Vore/artist"
    assert folder_tag_for(asset("/mnt/Pictures/Vore/a.jpg"), spec) is None
    assert folder_tag_for(asset("/other/Vore/artist/a.jpg"), spec) is None


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
    a = asset("/mnt/Pictures/Vore/artist/a.jpg")
    text = "rules:\n  - actions: {folder_tag: {strip_prefix: /mnt/Pictures/, levels: 2}}\n"
    run(make(tmp_path, text, api), [a])
    assert ("upsert_tags", ["Vore/artist"]) in api.calls
    assert api.writes() == ["upsert_tags", "tag_assets"]


def test_album_skips_assets_already_in_it(tmp_path):
    api = FakeApi()
    a1, a2 = asset("/Twitter/a.jpg"), asset("/Twitter/b.jpg")
    album = api.add_album("Twitter", members={a1.id})
    run(make(tmp_path, "rules:\n  - match: {path_like: /Twitter/}\n    actions: {album: Twitter}\n", api), [a1, a2])
    assert api.writes() == ["add_assets_to_album"]
    assert ("add_assets_to_album", album.id, [a2.id]) in api.calls


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
    assert "idle" in caplog.text


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
    ("New / RJ270617 / ŠÛ“Û˜b4 JPG", "New / RJ270617 / 丸呑話4 JPG"),
    ("Gallery / 2024 / You’re Under Arrest!", None),
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
    text = "repair_names: true\nrules:\n  - actions: {folder_tag: {strip_prefix: /mnt/Pictures/, levels: 1}}\n"
    run(make(tmp_path, text, api), [asset(f"/mnt/Pictures/{BROKEN}/a.jpg")])
    assert ("upsert_tags", [FIXED]) in api.calls


def test_tag_repair_keeps_old_tag_when_copy_failed(tmp_path):
    api = FakeApi()
    api.add_tag("Old", members={uuid4()})
    api.tag_assets = lambda tag_id, ids: None
    run(make(tmp_path, "rename_tags: {Old: New}\n", api), [])
    assert api.writes() == ["upsert_tags"]
