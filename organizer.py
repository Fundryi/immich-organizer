"""
Rule-driven organizer for Immich: tags, albums and visibility from a YAML rule file, plus repair
of album and tag names that were stored as Shift_JIS bytes read as Windows-1252.

Called from immich_auto_album.py. It never imports that script (importing it starts a run), so the
API client, the configuration and the album models are duck-typed.
"""
from __future__ import annotations

import logging
import re
import time
import unicodedata
from collections import Counter, defaultdict
from typing import Any, Callable

import yaml
from immichpy.client.generated import AssetVisibility

BATCH_SIZE = 50
WATCHED_QUEUES = ("sidecar", "metadataExtraction", "storageTemplateMigration")
TOP_LEVEL_KEYS = {"keep_empty_albums", "repair_names", "rename_tags", "rename_albums", "rules"}
# path_like, name_like, make_like and model_like are substrings and extension is an ending, all ignoring case;
# type and account are exact, ignoring case; name_regex is a case-sensitive regex on the file name.
TEXT_MATCH_KEYS = {"path_like", "path_not_like", "name_like", "name_regex", "extension", "type", "account", "make_like", "model_like"}
NUMBER_MATCH_KEYS = {"min_size_mb", "max_size_mb", "min_long_side", "max_long_side", "min_duration_s", "max_duration_s"}
TAG_GROUP = re.compile(r"\{(\d)\}")
ACTION_KEYS = {"tag", "folder_tag", "folder_album", "album", "visibility"}
FOLDER_ACTIONS = ("folder_tag", "folder_album")
ALBUM_SEPARATOR = " / "
VISIBILITIES = {"archive", "hidden", "timeline"}
ASSET_TYPES = {"IMAGE", "VIDEO"}
CJK_CHARS = re.compile("[぀-ヿ一-鿿！-｠]")


def nfc(text: str) -> str:
    """Normalizes a name the way upstream compares album names."""
    return unicodedata.normalize("NFC", text)


def as_list(value: Any) -> list[Any]:
    """Wraps a single value in a list, leaves a list alone."""
    return value if isinstance(value, list) else [value]


def fix_mojibake(text: str) -> str | None:
    """
    Repairs a name whose Shift_JIS bytes were decoded as Windows-1252 back to the Japanese name.

    :returns: The repaired name, or None when the name does not look broken
    """
    if sum(1 for char in text if ord(char) > 127) < 3:
        return None
    raw = bytearray()
    for char in text:
        try:
            raw += char.encode("cp1252")
        except UnicodeEncodeError:
            if ord(char) > 255:
                return None
            # The five bytes cp1252 leaves undefined (0x81, 0x8D, 0x8F, 0x90, 0x9D) were decoded one to one.
            raw.append(ord(char))
    # Every Shift_JIS kanji or kana has a lead byte; most leads sit in 0x81..0x9F, where no German umlaut falls.
    if sum(1 for byte in raw if 0x80 <= byte <= 0x9F) < 2:
        return None
    try:
        fixed = raw.decode("cp932")
    except UnicodeDecodeError:
        return None
    return fixed if len(CJK_CHARS.findall(fixed)) >= 2 else None


def _fail(where: str, message: str) -> None:
    raise ValueError(f"Rule file, {where}: {message}")


def _value(data: dict[str, Any], key: str, default: Any) -> Any:
    # Only an absent key or an explicit null gets the default; false, 0 or [] stay and fail the type check.
    value = data.get(key)
    return default if value is None else value


def _check_keys(where: str, data: Any, allowed: set[str]) -> None:
    if not isinstance(data, dict):
        _fail(where, "must be a mapping")
    for key in data:
        if key not in allowed:
            _fail(where, f"unknown key '{key}', allowed: {', '.join(sorted(allowed))}")


def _check_texts(where: str, value: Any) -> list[str]:
    values = as_list(value)
    if not values or not all(isinstance(v, str) and v for v in values):
        _fail(where, "must be a non-empty string or a list of non-empty strings")
    return values


def _check_tags(where: str, match: dict[str, Any], actions: dict[str, Any]) -> bool:
    """Checks the tag action and returns True when a tag uses regex groups like {1}."""
    if "tag" not in actions:
        return False
    captures = any(TAG_GROUP.search(tag) for tag in _check_texts(f"{where}.actions.tag", actions["tag"]))
    if captures and "name_regex" not in match:
        _fail(f"{where}.actions.tag", "a tag with {1}..{9} needs a name_regex with capture groups")
    return captures


def _check_rule(where: str, rule: Any) -> dict[str, Any]:
    _check_keys(where, rule, {"match", "actions"})
    if not rule.get("actions"):
        _fail(where, "needs 'actions'")
    match = _value(rule, "match", {})
    _check_keys(f"{where}.match", match, TEXT_MATCH_KEYS | NUMBER_MATCH_KEYS)
    for key, value in match.items():
        if key in NUMBER_MATCH_KEYS:
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                _fail(f"{where}.match.{key}", "must be a number")
            continue
        values = _check_texts(f"{where}.match.{key}", value)
        if key == "type" and not set(values) <= ASSET_TYPES:
            _fail(f"{where}.match.type", f"must be one of {', '.join(sorted(ASSET_TYPES))}")
        if key == "extension" and not all(v.startswith(".") for v in values):
            _fail(f"{where}.match.extension", "must start with a dot, e.g. '.gif'")
    if "name_regex" in match:
        match = {**match, "name_regex": [_compile(f"{where}.match.name_regex", pattern) for pattern in as_list(match["name_regex"])]}
    actions = rule["actions"]
    _check_keys(f"{where}.actions", actions, ACTION_KEYS)
    captures = _check_tags(where, match, actions)
    if "album" in actions and (not isinstance(actions["album"], str) or not actions["album"]):
        _fail(f"{where}.actions.album", "must be a non-empty string")
    if "visibility" in actions and actions["visibility"] not in VISIBILITIES:
        _fail(f"{where}.actions.visibility", f"must be one of {', '.join(sorted(VISIBILITIES))}")
    for key in FOLDER_ACTIONS:
        if key in actions:
            actions = {**actions, key: _check_folder_spec(f"{where}.actions.{key}", actions[key])}
    return {"match": match, "actions": actions, "captures": captures}


def _compile(where: str, pattern: str) -> re.Pattern[str]:
    try:
        return re.compile(pattern)
    except (re.error, OverflowError, RecursionError) as e:
        raise ValueError(f"Rule file, {where}: bad regex '{pattern}': {e}") from e


def _check_folder_spec(where: str, spec: Any) -> dict[str, Any]:
    _check_keys(where, spec, {"strip_prefix", "levels", "prefix"})
    levels = spec.get("levels")
    if not isinstance(spec.get("strip_prefix"), str) or isinstance(levels, bool) or not isinstance(levels, int) or levels < 1:
        _fail(where, "needs strip_prefix (string) and levels (whole number from 1)")
    if not spec["strip_prefix"].endswith("/"):
        # Else /mnt/Photos also matches /mnt/Photosx/.
        _fail(f"{where}.strip_prefix", "must end with '/'")
    if not isinstance(spec.get("prefix", ""), str):
        _fail(f"{where}.prefix", "must be a string")
    return {"prefix": "", **spec}


def parse_rule_file(data: Any) -> dict[str, Any]:
    """
    Validates the loaded YAML of a rule file and returns it with defaults filled in.

    :raises ValueError: On unknown keys, wrong types or values, or a rule without actions
    """
    data = {} if data is None else data
    _check_keys("top level", data, TOP_LEVEL_KEYS)
    keep = _value(data, "keep_empty_albums", [])
    if not isinstance(keep, list) or not all(isinstance(name, str) for name in keep):
        _fail("keep_empty_albums", "must be a list of strings")
    repair = _value(data, "repair_names", False)
    if not isinstance(repair, bool):
        _fail("repair_names", "must be true or false")
    renames = {}
    for key in ("rename_tags", "rename_albums"):
        mapping = _value(data, key, {})
        if not isinstance(mapping, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in mapping.items()):
            _fail(key, "must map old name to new name, both strings")
        renames[key] = {nfc(old): new for old, new in mapping.items()}
    rules = _value(data, "rules", [])
    if not isinstance(rules, list):
        _fail("rules", "must be a list")
    return {
        "keep_empty_albums": {nfc(name) for name in keep},
        "repair_names": repair,
        "rename_tags": renames["rename_tags"],
        "rename_albums": renames["rename_albums"],
        "rules": [_check_rule(f"rules[{index}]", rule) for index, rule in enumerate(rules)],
    }


def _matches_number(key: str, value: float, asset: Any) -> bool:
    if key in ("min_size_mb", "max_size_mb"):
        size = asset.exif_info.file_size_in_byte if asset.exif_info else None
        if size is None:
            return False
        return size >= value * 1024 * 1024 if key == "min_size_mb" else size <= value * 1024 * 1024
    if key == "min_long_side":
        return max(asset.width or 0, asset.height or 0) >= value
    if key == "max_long_side":
        # An unknown width or height could be the long side, which is no match.
        return (asset.width or 0) > 0 and (asset.height or 0) > 0 and max(asset.width, asset.height) <= value
    if asset.duration is None:
        return False
    seconds = asset.duration / 1000
    return seconds >= value if key == "min_duration_s" else seconds <= value


def _matches_text(key: str, wanted: list[str], asset: Any) -> bool:
    if key in ("make_like", "model_like"):
        # No EXIF or no value is no match.
        text = getattr(asset.exif_info, key.removesuffix("_like")) if asset.exif_info else None
        return bool(text) and any(w in text.lower() for w in wanted)
    if key in ("path_like", "path_not_like"):
        path = (asset.original_path or "").lower()
        return any(w in path for w in wanted) == (key == "path_like")
    name = (asset.original_file_name or "").lower()
    if key == "name_like":
        return any(w in name for w in wanted)
    return name.endswith(tuple(wanted))


def _matches_key(key: str, value: Any, asset: Any, me: Any) -> bool:
    if key in NUMBER_MATCH_KEYS:
        return _matches_number(key, value, asset)
    if key == "name_regex":
        # Case-sensitive on the original name; re.search takes the compiled patterns of a parsed rule and plain strings alike.
        return any(re.search(pattern, asset.original_file_name or "") for pattern in as_list(value))
    wanted = [v.lower() for v in as_list(value)]
    if key == "type":
        return asset.type.value.lower() in wanted
    if key == "account":
        return (me.name or "").lower() in wanted or (me.email or "").lower() in wanted
    return _matches_text(key, wanted, asset)


def regex_groups(match: dict[str, Any], asset: Any) -> tuple[str | None, ...]:
    """The groups of the first name_regex pattern of match that matches the asset's file name, else an empty tuple."""
    for pattern in as_list(match.get("name_regex", [])):
        found = re.search(pattern, asset.original_file_name or "")
        if found:
            return found.groups()
    return ()


def fill_tag(tag: str, groups: tuple[str | None, ...]) -> str | None:
    """Replaces {1}..{9} in a tag with the regex groups. An empty group, or one with a slash, gives no tag."""
    def group(found: re.Match[str]) -> str:
        index = int(found.group(1)) - 1
        value = groups[index] if index < len(groups) else None
        if not value or "/" in value:
            raise LookupError
        return value
    try:
        return TAG_GROUP.sub(group, tag)
    except LookupError:
        return None


def matches(match: dict[str, Any], asset: Any, me: Any) -> bool:
    """
    True when the asset passes every key of match: keys are AND, list values are OR, an empty match passes everything.

    :param me: The owner of the API key (has .name and .email), for the account key
    """
    return all(_matches_key(key, value, asset, me) for key, value in match.items())


def folder_name_for(asset: Any, spec: dict[str, Any], separator: str = "/", repair: bool = False) -> str | None:
    """
    Builds a tag path or album name from the first spec['levels'] folders after spec['strip_prefix'] in the asset path,
    joined by separator, with the optional spec['prefix'] in front. With repair each folder goes through fix_mojibake
    on its own, so neither the prefix nor a correct Japanese sibling folder blocks the repair.

    :returns: The name, or None when the path does not start with strip_prefix or has too few folders
    """
    path = asset.original_path or ""
    if not path.startswith(spec["strip_prefix"]):
        return None
    folders = [part for part in path[len(spec["strip_prefix"]):].split("/")[:-1] if part]
    if len(folders) < spec["levels"]:
        return None
    folders = folders[:spec["levels"]]
    if repair:
        folders = [fix_mojibake(folder) or folder for folder in folders]
    return spec.get("prefix", "") + separator.join(folders)


def _first_by_name(albums: list[Any]) -> dict[str, Any]:
    # Upstream's lookup takes the first album of a name, so duplicates resolve the same way here.
    by_name: dict[str, Any] = {}
    for album in albums:
        by_name.setdefault(nfc(album.album_name), album)
    return by_name


def _drop_unsafe_albums(album_plan: dict[str, set[Any]], raw_names: dict[str, str], fetched: list[Any]) -> dict[str, Any]:
    """Drops planned albums whose name is taken twice or whose broken name still exists, and returns the albums by name."""
    counts = Counter(nfc(album.album_name) for album in fetched)
    albums = _first_by_name(fetched)
    for name in list(album_plan):
        if counts[name] > 1:
            logging.warning("Rules: %d albums are named '%s', skipping it (no merge)", counts[name], name)
        elif raw_names.get(name) in albums:
            logging.warning("Rules: album '%s' still exists, rename it first; skipping '%s'", raw_names[name], name)
        else:
            continue
        del album_plan[name]
    return albums


def _log_plan(repairs: list[tuple[Any, str]], tag_plan: dict[str, set[Any]], album_plan: dict[str, set[Any]], visibility_plan: dict[str, set[Any]]) -> tuple[int, ...]:
    for tag, fixed in repairs:
        logging.info("Rules: rename tag '%s' to '%s'", tag.value, fixed)
    for label, plan in (("tag", tag_plan), ("album", album_plan), ("visibility", visibility_plan)):
        for name, ids in sorted(plan.items()):
            logging.info("Rules: %s '%s': %d assets to change", label, name, len(ids))
    totals = (len(repairs), *(sum(len(ids) for ids in plan.values()) for plan in (tag_plan, album_plan, visibility_plan)))
    logging.info("Rules plan: %d tag renames, %d tag assignments, %d album additions, %d visibility changes", *totals)
    return totals


class Organizer:
    """Applies a rule file to the assets of one upstream run and repairs broken album and tag names."""

    def __init__(self, config: Any, api_client: Any):
        """
        :param config: Has rules_file, rules_dry_run, queue_api_key, find_assets_in_albums and find_archived_assets
        :param api_client: The upstream ApiClient of this run
        :raises ValueError: When the rule file is not valid
        """
        self.config = config
        self.api_client = api_client
        try:
            with open(config.rules_file, encoding="utf-8") as file:
                data = yaml.safe_load(file)
        except (OSError, yaml.YAMLError) as e:
            raise ValueError(f"Rule file {config.rules_file} cannot be read: {e}") from e
        self.rules = parse_rule_file(data)
        # Tests replace these so they never sleep.
        self.poll_interval = 5.0
        self.idle_timeout = 30 * 60.0
        self.sleep: Callable[[float], Any] = time.sleep

    def keeps_empty_album(self, name: str) -> bool:
        """True when SYNC_MODE must not delete this album even if it is empty."""
        return nfc(name) in self.rules["keep_empty_albums"]

    def _fixed_name(self, name: str, renames: dict[str, str]) -> str | None:
        fixed = renames.get(nfc(name))
        if fixed is None and self.rules["repair_names"]:
            fixed = fix_mojibake(name)
        return fixed if fixed and nfc(fixed) != nfc(name) else None

    def repair_album_names(self, albums: list[Any], planned: list[Any]) -> None:
        """
        Points planned albums with a broken name at the fixed name and renames the existing album in Immich,
        so upstream's name lookup finds the fixed album instead of creating a new broken one. rename_albums
        applies only to planned albums.

        :param albums: The existing albums upstream fetched; renamed entries get their album_name updated
        :param planned: The AlbumModel objects of this run; repaired ones get override_name set
        """
        if not self.rules["repair_names"] and not self.rules["rename_albums"]:
            return
        by_name = _first_by_name(albums)
        for model in planned:
            name = model.get_final_name()
            fixed = self._fixed_name(name, self.rules["rename_albums"])
            if fixed:
                self._repair_album(by_name, name, fixed, model)

    def _repair_album(self, by_name: dict[str, Any], name: str, fixed: str, model: Any) -> None:
        old, new = by_name.get(nfc(name)), by_name.get(nfc(fixed))
        if old is not None and new is not None:
            logging.warning("Albums '%s' and '%s' both exist, not renaming (no merge)", name, fixed)
            return
        if old is None:
            # Memory only, so also in dry run: else upstream creates the broken name again next to the repaired album.
            logging.info("Rules: using album name '%s' instead of '%s'", fixed, name)
            model.override_name = fixed
            return
        if self.config.rules_dry_run:
            logging.info("Rules dry run: would rename album '%s' to '%s'", name, fixed)
            return
        model.override_name = fixed
        logging.info("Renaming album '%s' to '%s'", name, fixed)
        self.api_client.rename_album(old.id, fixed)
        old.album_name = fixed
        by_name[nfc(fixed)] = by_name.pop(nfc(name))

    async def run_rules(self, assets: list[Any]) -> None:
        """
        Plans tags, albums and visibility for the assets, skips what is already done, and unless dry run
        writes in batches of 50 with a wait for idle Immich queues after each batch. Tag renames run first.
        Problems that stop the writes are logged, not raised, so the rest of the upstream run still goes on.
        """
        if not (self.config.find_assets_in_albums and self.config.find_archived_assets):
            logging.warning("Rules see only part of the library: turn on FIND_ASSETS_IN_ALBUMS and FIND_ARCHIVED_ASSETS to cover assets in albums and archived assets")
        tags = {nfc(tag.value): tag for tag in self.api_client.fetch_tags()}
        tag_plan, album_plan, visibility_plan, raw_album_names = self._plan(assets)
        repairs = self._plan_tag_repairs(list(tags.values()))
        # Checked before the skip: else a rule whose assets all carry the broken tag re-tags them after every repair.
        conflicts = sorted(tag.value for tag, _ in repairs if nfc(tag.value) in tag_plan)
        albums = _drop_unsafe_albums(album_plan, raw_album_names, self.api_client.fetch_albums()) if album_plan else {}
        tag_plan, album_plan = self._skip_done(tag_plan, album_plan, tags, albums)
        totals = _log_plan(repairs, tag_plan, album_plan, visibility_plan)
        if conflicts:
            logging.error("Rules both assign and repair the tags %s, rules write nothing. Fix the rule file.", ", ".join(conflicts))
            return
        if self.config.rules_dry_run:
            logging.info("Rules dry run: nothing was written")
            return
        if not any(totals):
            return

        queue_client = await self._open_queue_client()
        if queue_client is None:
            return
        try:
            queue_client.fetch_queues()
        except Exception as e:  # pylint: disable=broad-exception-caught
            # Without the queues the writes cannot be paced, so write nothing at all.
            logging.error("Cannot read the Immich queues (QUEUE_API_KEY must be an admin key), rules write nothing: %s", e)
            await self._close_queue_client(queue_client)
            return
        try:
            self._wait_idle(queue_client)
            for tag, fixed in repairs:
                self._rename_tag(tag, fixed, queue_client)
            self._write_tags(tag_plan, tags, queue_client)
            self._write_albums(album_plan, albums, queue_client)
            self._write_visibility(visibility_plan, queue_client)
            logging.info("Rules applied: %d tag renames, %d tag assignments, %d album additions, %d visibility changes", *totals)
        except RuntimeError as e:
            # A paused queue or a timeout stops the rules only; upstream's remaining steps and the next API key still run.
            logging.error("%s", e)
        finally:
            await self._close_queue_client(queue_client)

    async def _open_queue_client(self) -> Any:
        # Reading the queues needs an admin key; without QUEUE_API_KEY the run's own client is used.
        if not self.config.queue_api_key:
            return self.api_client
        client = type(self.api_client)(self.api_client.api_url, self.config.queue_api_key, self.api_client.config)
        try:
            await client.__aenter__()  # pylint: disable=unnecessary-dunder-call
        except Exception as e:  # pylint: disable=broad-exception-caught
            # The upstream client starts a non-daemon thread on enter; without the exit a failed enter hangs the process.
            await client.__aexit__(None, None, None)
            logging.error("Rules: cannot connect with QUEUE_API_KEY, rules write nothing: %s", e)
            return None
        return client

    async def _close_queue_client(self, queue_client: Any) -> None:
        if queue_client is not self.api_client:
            await queue_client.__aexit__(None, None, None)

    def _plan(self, assets: list[Any]) -> tuple[dict[str, set[Any]], dict[str, set[Any]], dict[str, set[Any]], dict[str, str]]:
        me = self.api_client.fetch_my_user()
        tag_plan: dict[str, set[Any]] = defaultdict(set)
        album_plan: dict[str, set[Any]] = defaultdict(set)
        visibility_plan: dict[str, set[Any]] = defaultdict(set)
        raw_album_names: dict[str, str] = {}
        for asset in assets:
            visibilities = []
            for rule in self.rules["rules"]:
                if not matches(rule["match"], asset, me):
                    continue
                actions = rule["actions"]
                paths = [fill_tag(path, regex_groups(rule["match"], asset) if rule["captures"] else ()) for path in as_list(actions.get("tag", []))]
                if "folder_tag" in actions:
                    paths = paths + [folder_name_for(asset, actions["folder_tag"], repair=self.rules["repair_names"])]
                for path in filter(None, paths):
                    tag_plan[nfc(path)].add(asset.id)
                names = [actions["album"]] if "album" in actions else []
                if "folder_album" in actions:
                    names.append(self._folder_album(asset, actions["folder_album"], raw_album_names))
                for name in filter(None, names):
                    album_plan[nfc(name)].add(asset.id)
                if "visibility" in actions:
                    visibilities.append(actions["visibility"])
            if len(set(visibilities)) > 1:
                logging.warning("Rules give %s more than one visibility, the last matching rule wins", asset.original_path)
            if visibilities and asset.visibility.value != visibilities[-1]:
                visibility_plan[visibilities[-1]].add(asset.id)
        for path in sorted(tag_plan):
            logging.info("Rules: tag '%s' matches %d assets", path, len(tag_plan[path]))
        return tag_plan, album_plan, visibility_plan, raw_album_names

    def _folder_album(self, asset: Any, spec: dict[str, Any], raw_names: dict[str, str]) -> str | None:
        # Repaired, else a broken folder name creates the broken album again on every run.
        name = folder_name_for(asset, spec, ALBUM_SEPARATOR, self.rules["repair_names"])
        raw = folder_name_for(asset, spec, ALBUM_SEPARATOR)
        if name and raw and name != raw:
            raw_names[nfc(name)] = nfc(raw)
        return name

    def _skip_done(self, tag_plan: dict[str, set[Any]], album_plan: dict[str, set[Any]], tags: dict[str, Any], albums: dict[str, Any]) -> tuple[dict[str, set[Any]], dict[str, set[Any]]]:
        for path, ids in tag_plan.items():
            if path in tags:
                ids -= self.api_client.fetch_asset_ids(tag_ids=[tags[path].id])
        for name, ids in album_plan.items():
            if name in albums:
                ids -= self.api_client.fetch_asset_ids(album_ids=[albums[name].id])
        return {path: ids for path, ids in tag_plan.items() if ids}, {name: ids for name, ids in album_plan.items() if ids}

    def _plan_tag_repairs(self, tags: list[Any]) -> list[tuple[Any, str]]:
        if not self.rules["repair_names"] and not self.rules["rename_tags"]:
            return []
        values = {nfc(tag.value) for tag in tags}
        # parent_id is a str in the DTO while id is a UUID.
        parents = {tag.parent_id for tag in tags if tag.parent_id}
        repairs = []
        for tag in tags:
            fixed = self._fixed_name(tag.value, self.rules["rename_tags"])
            if not fixed:
                continue
            if str(tag.id) in parents:
                logging.warning("Tag '%s' not renamed to '%s': it is the parent of other tags", tag.value, fixed)
                continue
            if nfc(fixed) in values:
                # A rerun after a stopped repair, or one name broken twice: finish the move into the existing tag.
                logging.info("Tag '%s': '%s' already exists, merging into existing tag", tag.value, fixed)
            repairs.append((tag, fixed))
        return repairs

    def _rename_tag(self, tag: Any, fixed: str, queue_client: Any) -> None:
        # Immich cannot rename a tag: copy the assets to the new tag, then remove the old one once it is empty.
        logging.info("Renaming tag '%s' to '%s'", tag.value, fixed)
        new_id = self._upsert_tags([fixed])[nfc(fixed)].id
        old_ids = self.api_client.fetch_asset_ids(tag_ids=[tag.id])
        self._in_batches(old_ids, lambda batch: self.api_client.tag_assets(new_id, batch), queue_client)
        if not self.api_client.fetch_asset_ids(tag_ids=[new_id]) >= old_ids:
            logging.error("Tag '%s': not every asset reached '%s', keeping the old tag", tag.value, fixed)
            return
        self._in_batches(old_ids, lambda batch: self.api_client.untag_assets(tag.id, batch), queue_client)
        if self.api_client.fetch_asset_ids(tag_ids=[tag.id]):
            logging.warning("Tag '%s' still has assets, not deleted", tag.value)
            return
        self.api_client.delete_tag(tag.id)

    def _write_tags(self, tag_plan: dict[str, set[Any]], tags: dict[str, Any], queue_client: Any) -> None:
        missing = [path for path in tag_plan if path not in tags]
        if missing:
            tags = {**tags, **self._upsert_tags(missing)}
        for path, ids in tag_plan.items():
            tag_id = tags[path].id
            self._in_batches(ids, lambda batch, tag_id=tag_id: self.api_client.tag_assets(tag_id, batch), queue_client)

    def _write_albums(self, album_plan: dict[str, set[Any]], albums: dict[str, Any], queue_client: Any) -> None:
        for name, ids in album_plan.items():
            if name in albums:
                album_id = albums[name].id
            else:
                album_id = self.api_client.create_album(name)
                logging.info("Rules: album '%s' created", name)
            self._in_batches(ids, lambda batch, album_id=album_id: self.api_client.add_assets_to_album(album_id, batch), queue_client)

    def _write_visibility(self, visibility_plan: dict[str, set[Any]], queue_client: Any) -> None:
        for value, ids in visibility_plan.items():
            self._in_batches(ids, lambda batch, v=AssetVisibility(value): self.api_client.set_assets_visibility(batch, v), queue_client)

    def _upsert_tags(self, paths: list[str]) -> dict[str, Any]:
        # Picked by value, not by position: the answer may also list parent tags.
        return {nfc(tag.value): tag for tag in self.api_client.upsert_tags(paths)}

    def _in_batches(self, ids: set[Any], write: Callable[[list[Any]], Any], queue_client: Any) -> None:
        ordered = sorted(ids, key=str)
        for start in range(0, len(ordered), BATCH_SIZE):
            write(ordered[start:start + BATCH_SIZE])
            self._wait_idle(queue_client)

    def _wait_idle(self, queue_client: Any) -> None:
        deadline = time.monotonic() + self.idle_timeout
        while True:
            busy = []
            seen = set()
            for queue in queue_client.fetch_queues():
                name = queue.name.value
                if name not in WATCHED_QUEUES:
                    continue
                seen.add(name)
                if queue.is_paused:
                    raise RuntimeError(f"Immich queue {name} is paused, rules stop writing. Resume it and run again.")
                stats = queue.statistics
                if stats.active + stats.waiting + stats.delayed:
                    busy.append(name)
            # A queue missing from the answer is not known to be idle.
            busy += [f"{name} (missing)" for name in WATCHED_QUEUES if name not in seen]
            if not busy:
                return
            if time.monotonic() >= deadline:
                raise RuntimeError(f"Immich queues {', '.join(busy)} not idle after {self.idle_timeout:.0f} s, rules stop writing")
            logging.debug("Waiting for Immich queues %s", busy)
            self.sleep(self.poll_interval)
