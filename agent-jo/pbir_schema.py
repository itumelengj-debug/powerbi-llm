"""
pbir_schema.py - validate PBIR files (page.json, visual.json, ...) against the JSON schema their
`$schema` URL names, instead of relying only on hand-written rules.

Schemas are cached on disk so validation works offline once the cache is filled:

    python pbir_schema.py --fetch-from C:\\PBI\\FX              # cache every $schema the project uses (+ $refs)
    python pbir_schema.py --validate C:\\PBI\\FX\\FX.Report       # validate a report folder

In the adapter:

    from pbir_schema import SchemaValidator
    pbi = PowerBIAgent(cfg, schema_validator=SchemaValidator())   # cache-only by default

Needs the optional `jsonschema` package (pip install jsonschema). Without it, or when a schema
is not cached and fetching is off, validation is skipped (and recorded in `.skipped`), never faked.
Only https://developer.microsoft.com/json-schemas/ URLs are fetched.
"""
from __future__ import annotations

import json
import re
import sys
import urllib.request
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import urljoin, urlparse

ALLOWED_PREFIX = "https://developer.microsoft.com/json-schemas/"
DEFAULT_CACHE = Path.home() / ".powerbi-llm" / "schemas"

try:  # optional dependency
    import jsonschema
    import referencing
    from referencing.jsonschema import DRAFT7
    HAVE_JSONSCHEMA = True
except ImportError:  # pragma: no cover - exercised only without jsonschema
    HAVE_JSONSCHEMA = False


def _http_fetch(url: str, timeout: int = 20) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "powerbi-llm-schema-cache"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


class SchemaValidator:
    def __init__(self, cache_dir: Optional[str] = None, allow_fetch: bool = False,
                 fetch: Optional[Callable[[str], bytes]] = None, max_errors: int = 5,
                 allowed_prefix: str = ALLOWED_PREFIX):
        self.cache = Path(cache_dir) if cache_dir else DEFAULT_CACHE
        self.allow_fetch = allow_fetch
        self.fetch = fetch or _http_fetch
        self.max_errors = max_errors
        self.allowed_prefix = allowed_prefix
        self.skipped: list = []           # (where, reason) for validations that could not run
        self._mem: dict = {}

    @property
    def available(self) -> bool:
        return HAVE_JSONSCHEMA

    # ---- cache
    def _path(self, url: str) -> Path:
        u = urlparse(url)
        safe = re.sub(r"[^\w./-]", "_", (u.netloc + u.path).strip("/"))
        return self.cache / safe

    def load(self, url: str) -> Optional[dict]:
        url = url.split("#", 1)[0]
        if url in self._mem:
            return self._mem[url]
        if not url.startswith(self.allowed_prefix):
            return None
        p = self._path(url)
        if not p.exists():
            if not self.allow_fetch:
                return None
            data = self.fetch(url)
            json.loads(data)                 # refuse to cache anything that is not JSON
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(data)
        doc = json.loads(p.read_text(encoding="utf-8"))
        self._mem[url] = doc
        return doc

    def prefetch(self, urls) -> dict:
        """Cache schemas and everything they $ref. Returns {url: "ok" | error}."""
        fetcher = SchemaValidator(self.cache, True, self.fetch, allowed_prefix=self.allowed_prefix)
        status, todo, seen = {}, list(urls), set()
        while todo:
            url = todo.pop().split("#", 1)[0]
            if not url or url in seen:
                continue
            seen.add(url)
            try:
                doc = fetcher.load(url)
                if doc is None:
                    status[url] = "skipped (not a Microsoft json-schemas URL)"
                    continue
                status[url] = "ok"
                todo += [urljoin(url, r) for r in _refs(doc) if not r.startswith("#")]
            except Exception as e:
                status[url] = f"{type(e).__name__}: {e}"
        return status

    # ---- validation
    def __call__(self, doc: dict, where: str = "") -> list:
        url = doc.get("$schema") if isinstance(doc, dict) else None
        if not url:
            return []
        if not HAVE_JSONSCHEMA:
            self.skipped.append((where, "jsonschema not installed"))
            return []
        try:
            schema = self.load(url)
        except Exception as e:
            self.skipped.append((where, f"could not load {url}: {e}"))
            return []
        if schema is None:
            self.skipped.append((where, f"schema not cached: {url}"))
            return []

        def retrieve(uri: str):
            contents = self.load(uri)
            if contents is None:
                raise LookupError(f"schema not cached: {uri}")
            return referencing.Resource.from_contents(contents, default_specification=DRAFT7)

        registry = referencing.Registry(retrieve=retrieve).with_resource(
            url, referencing.Resource.from_contents(schema, default_specification=DRAFT7))
        cls = jsonschema.validators.validator_for(schema, default=jsonschema.Draft7Validator)
        validator = cls({"$ref": url}, registry=registry)
        try:
            errors = sorted(validator.iter_errors(doc), key=lambda e: list(e.absolute_path))
        except Exception as e:  # unresolvable $ref etc.
            self.skipped.append((where, f"validation could not run: {e}"))
            return []
        out = []
        for e in errors[: self.max_errors]:
            path = "/".join(str(x) for x in e.absolute_path) or "(root)"
            out.append(f"{where}: schema violation at {path}: {e.message[:200]}")
        if len(errors) > self.max_errors:
            out.append(f"{where}: {len(errors) - self.max_errors} more schema violation(s)")
        return out


def _refs(node) -> list:
    out = []
    if isinstance(node, dict):
        for k, v in node.items():
            if k == "$ref" and isinstance(v, str):
                out.append(v)
            else:
                out += _refs(v)
    elif isinstance(node, list):
        for v in node:
            out += _refs(v)
    return out


def schema_urls(folder: str) -> set:
    urls = set()
    for f in Path(folder).rglob("*.json"):
        try:
            u = json.loads(f.read_text(encoding="utf-8-sig")).get("$schema")
        except (json.JSONDecodeError, AttributeError, UnicodeDecodeError):
            continue
        if isinstance(u, str):
            urls.add(u)
    return urls


def validate_folder(folder: str, validator: SchemaValidator) -> list:
    issues = []
    for f in sorted(Path(folder).rglob("*.json")):
        try:
            doc = json.loads(f.read_text(encoding="utf-8-sig"))
        except json.JSONDecodeError as e:
            issues.append(f"{f}: invalid JSON ({e.msg})")
            continue
        if isinstance(doc, dict):
            issues += validator(doc, str(f.relative_to(folder)))
    return issues


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--cache", default=str(DEFAULT_CACHE))
    ap.add_argument("--fetch-from", metavar="FOLDER", help="cache every $schema used under FOLDER")
    ap.add_argument("--validate", metavar="FOLDER", help="validate every JSON file under FOLDER")
    ap.add_argument("--allow-fetch", action="store_true", help="fetch uncached schemas while validating")
    a = ap.parse_args(argv)
    v = SchemaValidator(a.cache, allow_fetch=a.allow_fetch)
    rc = 0
    if a.fetch_from:
        for url, st in sorted(v.prefetch(schema_urls(a.fetch_from)).items()):
            print(f"{st:>8}  {url}")
            rc |= st != "ok" and not st.startswith("skipped")
    if a.validate:
        if not v.available:
            print("jsonschema is not installed: pip install jsonschema", file=sys.stderr)
            return 2
        issues = validate_folder(a.validate, v)
        for i in issues:
            print(i)
        for where, why in v.skipped:
            print(f"skipped  {where}: {why}")
        print(f"{len(issues)} schema issue(s), {len(v.skipped)} file(s) skipped")
        rc |= bool(issues)
    return int(rc)


if __name__ == "__main__":
    sys.exit(main())
