"""PBIR $schema validation: cache, relative $refs, fetch injection, offline skip."""
import json

import pytest

import pbir_schema as ps

pytestmark = pytest.mark.skipif(not ps.HAVE_JSONSCHEMA, reason="jsonschema not installed")

BASE = "https://developer.microsoft.com/json-schemas/fabric/item/report/definition/"
VIS_URL = BASE + "visualContainer/9.9.0/schema.json"
POS_URL = BASE + "visualContainer/9.9.0/position.json"
VIS = {"$schema": "http://json-schema.org/draft-07/schema#", "type": "object",
       "required": ["name", "position"],
       "properties": {"$schema": {"type": "string"}, "name": {"type": "string", "pattern": "^[\\w-]{1,50}$"},
                      "position": {"$ref": "position.json"}}}
POS = {"type": "object", "required": ["x", "y"], "properties": {"x": {"type": "number"}, "y": {"type": "number"}},
       "additionalProperties": False}
SERVED = {VIS_URL: VIS, POS_URL: POS}


def fetcher(log):
    def fetch(url):
        log.append(url)
        return json.dumps(SERVED[url]).encode()
    return fetch


def test_skips_when_not_cached(tmp_path):
    v = ps.SchemaValidator(tmp_path)
    assert v({"$schema": VIS_URL, "name": "x"}, "a.json") == []
    assert v.skipped and "not cached" in v.skipped[0][1]


def test_prefetch_follows_refs_then_validates_offline(tmp_path):
    log = []
    status = ps.SchemaValidator(tmp_path, fetch=fetcher(log)).prefetch([VIS_URL])
    assert status == {VIS_URL: "ok", POS_URL: "ok"} and sorted(log) == sorted([VIS_URL, POS_URL])

    v = ps.SchemaValidator(tmp_path)               # offline: cache only
    good = {"$schema": VIS_URL, "name": "abc", "position": {"x": 1, "y": 2}}
    assert v(good, "ok.json") == []
    bad = {"$schema": VIS_URL, "name": "bad name!", "position": {"x": "1", "y": 2, "z": 3}}
    issues = v(bad, "bad.json")
    assert len(issues) == 3 and all(i.startswith("bad.json: schema violation") for i in issues)
    assert any("position/x" in i for i in issues) and any("name" in i for i in issues)


def test_allow_fetch_on_demand(tmp_path):
    log = []
    v = ps.SchemaValidator(tmp_path, allow_fetch=True, fetch=fetcher(log))
    assert v({"$schema": VIS_URL, "name": "abc"}, "v.json")      # missing position
    assert VIS_URL in log


def test_refuses_foreign_urls(tmp_path):
    log = []
    v = ps.SchemaValidator(tmp_path, allow_fetch=True, fetch=fetcher(log))
    assert v({"$schema": "https://evil.example/schema.json", "name": 1}, "x") == [] and log == []


def test_validate_folder_and_cli(tmp_path, capsys):
    ps.SchemaValidator(tmp_path / "cache", fetch=fetcher([])).prefetch([VIS_URL])
    rep = tmp_path / "R"
    rep.mkdir()
    (rep / "good.json").write_text(json.dumps({"$schema": VIS_URL, "name": "a", "position": {"x": 0, "y": 0}}))
    (rep / "bad.json").write_text(json.dumps({"$schema": VIS_URL, "name": "a"}))
    assert ps.main(["--cache", str(tmp_path / "cache"), "--validate", str(rep)]) == 1
    out = capsys.readouterr().out
    assert "bad.json" in out and "good.json" not in out.replace("bad.json", "")
