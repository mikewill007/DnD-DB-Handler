"""Import several independent open 5e sources into one SQLite file per source.

Each record retains its complete original JSON so no gameplay fields are lost.
Run `python import_dnd.py --help` for usage. Python 3.10+.
"""

from __future__ import annotations

import argparse
import hashlib
import yaml
import os
import json
import re
import sqlite3
import sys
import tempfile
import time
import zipfile
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import urljoin, urlparse

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

ROOT = Path(__file__).resolve().parent
SCHEMA_VERSION = "2"


def client() -> requests.Session:
    s = requests.Session()
    s.headers.update({"Accept": "application/json", "User-Agent": "DndDatasetImporter/1.0 (local SQLite backup)"})
    retry = Retry(total=5, connect=5, read=3, backoff_factor=0.8,
                  status_forcelist=[429, 500, 502, 503, 504],
                  allowed_methods=["GET"], respect_retry_after_header=True)
    s.mount("https://", HTTPAdapter(max_retries=retry))
    return s


def get_json(s: requests.Session, url: str) -> Any:
    resp = s.get(url, timeout=(15, 90))
    resp.raise_for_status()
    return resp.json()


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def compact(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def set_meta(db: sqlite3.Connection, key: str, value: Any) -> None:
    db.execute("INSERT OR REPLACE INTO metadata(key,value) VALUES (?,?)", (key, str(value)))


def init_db(db: sqlite3.Connection, source: dict, limit: int) -> None:
    db.executescript("""
    PRAGMA foreign_keys=ON;
    CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
    CREATE TABLE collections (
        id INTEGER PRIMARY KEY,
        name TEXT NOT NULL,
        edition TEXT NOT NULL DEFAULT '',
        source_url TEXT,
        expected_count INTEGER,
        fetched_count INTEGER NOT NULL DEFAULT 0,
        status TEXT NOT NULL DEFAULT 'pending',
        error TEXT,
        UNIQUE(name, edition)
    );
    CREATE TABLE records (
        id INTEGER PRIMARY KEY,
        collection_id INTEGER NOT NULL REFERENCES collections(id),
        external_id TEXT NOT NULL,
        name TEXT,
        description TEXT,
        document_key TEXT,
        source_url TEXT,
        source_file TEXT,
        data_json TEXT NOT NULL CHECK (json_valid(data_json)),
        UNIQUE(collection_id, external_id)
    );
    CREATE INDEX records_name_idx ON records(name COLLATE NOCASE);
    CREATE INDEX records_collection_idx ON records(collection_id);
    CREATE INDEX records_document_idx ON records(document_key);
    CREATE TABLE fetch_errors (
        id INTEGER PRIMARY KEY,
        collection_id INTEGER NOT NULL REFERENCES collections(id),
        external_id TEXT,
        source_url TEXT,
        error TEXT NOT NULL,
        created_utc TEXT NOT NULL
    );
    CREATE TABLE document_nodes (
        id INTEGER PRIMARY KEY,
        source_file TEXT NOT NULL,
        json_pointer TEXT NOT NULL,
        node_type TEXT NOT NULL,
        label TEXT,
        depth INTEGER NOT NULL,
        data_json TEXT NOT NULL CHECK(json_valid(data_json)),
        UNIQUE(source_file, json_pointer)
    );
    CREATE INDEX document_nodes_path_idx ON document_nodes(source_file, depth);
    CREATE TABLE tabular_rows (
        id INTEGER PRIMARY KEY,
        source_file TEXT NOT NULL,
        json_pointer TEXT NOT NULL,
        row_number INTEGER NOT NULL,
        name TEXT,
        data_json TEXT NOT NULL CHECK(json_valid(data_json)),
        UNIQUE(source_file, json_pointer, row_number)
    );
    CREATE INDEX tabular_rows_name_idx ON tabular_rows(name COLLATE NOCASE);
    CREATE TABLE source_files (
        path TEXT PRIMARY KEY,
        sha256 TEXT NOT NULL,
        data_json TEXT NOT NULL CHECK(json_valid(data_json))
    );
    CREATE VIEW catalog AS
       SELECT c.name AS collection, c.edition, c.source_url, c.status,
              c.expected_count, c.fetched_count, COUNT(r.id) AS records,
              c.error
       FROM collections c LEFT JOIN records r ON r.collection_id=c.id
       GROUP BY c.id;
    """)
    set_meta(db, "schema_version", SCHEMA_VERSION)
    set_meta(db, "source_id", source["id"])
    set_meta(db, "source_name", source["name"])
    set_meta(db, "source_type", source["type"])
    set_meta(db, "homepage", source.get("homepage", ""))
    set_meta(db, "started_utc", utcnow())
    set_meta(db, "max_per_collection", limit)
    set_meta(db, "status", "running")
    db.commit()


def add_collection(db: sqlite3.Connection, name: str, edition: str = "", source_url: str = "") -> int:
    db.execute("INSERT OR IGNORE INTO collections(name,edition,source_url) VALUES (?,?,?)",
               (name, edition, source_url))
    return db.execute("SELECT id FROM collections WHERE name=? AND edition=?", (name, edition)).fetchone()[0]


def fields_from_object(obj: Any) -> tuple[str | None, str | None, str | None]:
    if not isinstance(obj, dict):
        return None, None, None
    name = obj.get("name") or obj.get("title") or obj.get("index")
    if isinstance(name, dict):
        name = name.get("name") or name.get("key")
    description = obj.get("desc") or obj.get("description") or obj.get("summary")
    if isinstance(description, list):
        description = "\n".join(str(x) for x in description if isinstance(x, (str, int, float)))
    if description is not None and not isinstance(description, str):
        description = None
    document = obj.get("document")
    doc_key = document.get("key") if isinstance(document, dict) else None
    if not doc_key and isinstance(obj.get("source"), dict):
        doc_key = obj["source"].get("key") or obj["source"].get("book")
    return (str(name) if name is not None else None, description, str(doc_key) if doc_key else None)


def add_record(db: sqlite3.Connection, collection_id: int, external_id: str, obj: Any,
               source_url: str = "", source_file: str = "") -> None:
    name, description, document_key = fields_from_object(obj)
    db.execute("""INSERT INTO records(collection_id,external_id,name,description,document_key,source_url,source_file,data_json)
                  VALUES(?,?,?,?,?,?,?,?)
                  ON CONFLICT(collection_id,external_id) DO UPDATE SET
                     name=excluded.name,description=excluded.description,document_key=excluded.document_key,
                     source_url=excluded.source_url,source_file=excluded.source_file,data_json=excluded.data_json""",
               (collection_id, str(external_id), name, description, document_key, source_url, source_file, compact(obj)))


def mark_collection(db: sqlite3.Connection, cid: int, expected: int | None, status: str,
                    error: str = "") -> None:
    count = db.execute("SELECT COUNT(*) FROM records WHERE collection_id=?", (cid,)).fetchone()[0]
    db.execute("UPDATE collections SET expected_count=?, fetched_count=?, status=?, error=? WHERE id=?",
               (expected, count, status, error, cid))
    db.commit()


def check_host(url: str, hostname: str) -> None:
    if urlparse(url).hostname != hostname or urlparse(url).scheme != "https":
        raise ValueError(f"Unexpected endpoint address: {url}")


def import_five_e_bits(db: sqlite3.Connection, source: dict, s: requests.Session, limit: int,
                       pause: float) -> list[str]:
    """Import every listed collection, retaining good records even if a detail URL is broken.

    An upstream 404 must never silently count as a complete collection.
    """
    failures = []
    base = source["base_url"].rstrip("/") + "/"
    for edition in source["editions"]:
        root_url = base + edition
        print(f"  Discovering {edition} collections: {root_url}", flush=True)
        try:
            index = get_json(s, root_url)
            if not isinstance(index, dict) or not index:
                raise ValueError("Root response was not a collection map")
        except Exception as exc:
            failures.append(f"{edition} discovery: {exc}")
            continue
        for collection, endpoint in index.items():
            endpoint_url = urljoin("https://www.dnd5eapi.co", endpoint)
            cid = add_collection(db, collection, edition, endpoint_url)
            db.commit()
            expected = None
            try:
                check_host(endpoint_url, "www.dnd5eapi.co")
                listing = get_json(s, endpoint_url)
                if not isinstance(listing, dict) or not isinstance(listing.get("results"), list):
                    raise ValueError("Expected an object with a 'results' array")
                entries = listing["results"]
                expected = int(listing.get("count", len(entries)))
                if expected != len(entries):
                    raise ValueError(f"Index has count={expected} but provided {len(entries)} links")
                take = entries[:limit] if limit else entries
                detail_errors = []
                for n, item in enumerate(take, 1):
                    external_id = str(item.get("index") or item.get("url") or f"entry-{n}") if isinstance(item, dict) else f"entry-{n}"
                    detail_url = urljoin("https://www.dnd5eapi.co", item.get("url", "")) if isinstance(item, dict) else endpoint_url
                    try:
                        if not isinstance(item, dict) or not item.get("url"):
                            raise ValueError(f"Missing detail URL in index record {n}")
                        check_host(detail_url, "www.dnd5eapi.co")
                        payload = get_json(s, detail_url)
                        add_record(db, cid, external_id, payload, source_url=detail_url)
                    except Exception as exc:
                        message = f"{type(exc).__name__}: {exc}"
                        detail_errors.append(f"{external_id}: {message}")
                        db.execute("""INSERT INTO fetch_errors(collection_id,external_id,source_url,error,created_utc)
                                      VALUES(?,?,?,?,?)""", (cid, external_id, detail_url, message, utcnow()))
                        print(f"    MISSING {edition}/{collection}/{external_id}: {message}", file=sys.stderr)
                    if pause:
                        time.sleep(pause)
                actual = db.execute("SELECT COUNT(*) FROM records WHERE collection_id=?", (cid,)).fetchone()[0]
                if detail_errors:
                    failure = f"{edition}/{collection}: {len(detail_errors)} unavailable details; fetched {actual}/{len(take)} requested"
                    failures.append(failure)
                    mark_collection(db, cid, expected, "error", " | ".join(detail_errors[:5]))
                    print(f"    PARTIAL {edition}/{collection}: {actual} of {expected} records ({len(detail_errors)} failed)", flush=True)
                else:
                    mark_collection(db, cid, expected, "sample" if limit else "complete")
                    print(f"    {edition}/{collection}: {actual} of {expected} records", flush=True)
            except Exception as exc:
                failure = f"{edition}/{collection}: {exc}"
                failures.append(failure)
                mark_collection(db, cid, expected, "error", str(exc))
                print(f"    ERROR {failure}", file=sys.stderr)
    return failures


def import_open5e(db: sqlite3.Connection, source: dict, s: requests.Session, limit: int,
                   pause: float) -> list[str]:
    failures = []
    root_url = source["root_url"]
    root = get_json(s, root_url)
    if not isinstance(root, dict) or not root:
        return ["Open5e discovery: root was not a collection map"]
    for collection, url in root.items():
        cid = add_collection(db, collection, "", url)
        db.commit()
        expected = None
        seen_urls: set[str] = set()
        inserted = 0
        try:
            page_url = url
            while page_url:
                check_host(page_url, "api.open5e.com")
                if page_url in seen_urls:
                    raise ValueError(f"Pagination loop at {page_url}")
                seen_urls.add(page_url)
                payload = get_json(s, page_url)
                if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
                    raise ValueError("Expected a paginated response with 'results' array")
                if expected is None and isinstance(payload.get("count"), int):
                    expected = payload["count"]
                for item in payload["results"]:
                    if limit and inserted >= limit:
                        break
                    if not isinstance(item, dict):
                        raise ValueError("Non-object item returned from API")
                    slug = item.get("key") or item.get("slug") or item.get("id") or item.get("url")
                    doc = item.get("document")
                    doc_key = doc.get("key") if isinstance(doc, dict) else None
                    external_id = str(slug) if slug is not None else f"index-{inserted:07d}"
                    # Document scope also protects against cross-book slug collisions.
                    if doc_key:
                        external_id = f"{doc_key}::{external_id}"
                    if db.execute("SELECT 1 FROM records WHERE collection_id=? AND external_id=?",
                                  (cid, external_id)).fetchone():
                        raise ValueError(f"Duplicate API ID {external_id!r}; refusing to overwrite")
                    add_record(db, cid, external_id, item, source_url=page_url)
                    inserted += 1
                if limit and inserted >= limit:
                    break
                next_url = payload.get("next")
                page_url = urljoin(page_url, next_url) if next_url else None
                if pause and page_url:
                    time.sleep(pause)
            actual = db.execute("SELECT COUNT(*) FROM records WHERE collection_id=?", (cid,)).fetchone()[0]
            if not limit and expected is not None and actual != expected:
                raise ValueError(f"Expected {expected}, saved {actual} objects")
            mark_collection(db, cid, expected, "sample" if limit else "complete")
            print(f"    {collection}: {inserted} records" + (f" (of {expected})" if expected is not None else ""), flush=True)
        except Exception as exc:
            failure = f"open5e/{collection}: {exc}"
            failures.append(failure)
            mark_collection(db, cid, expected, "error", str(exc))
            print(f"    ERROR {failure}", file=sys.stderr)
    return failures


def json_entries(obj: Any) -> Iterator[tuple[str, int, Any]]:
    """Unpack list collections but preserve original file separately in source_files.

    A mapping of category->arrays (such as SRD JSON exports) is unpacked under each
    nested category. A single game entity document stays a single row.
    """
    if isinstance(obj, list):
        for i, value in enumerate(obj):
            yield "", i, value
    elif isinstance(obj, dict) and not any(k in obj for k in ("name", "_id", "index", "slug")):
        arrays = [(k, v) for k, v in obj.items() if isinstance(v, list)]
        if arrays and sum(len(a) for _, a in arrays) > 1 and len(arrays) >= max(1, len(obj) // 2):
            for category, array in arrays:
                for i, value in enumerate(array):
                    yield str(category), i, value
        else:
            yield "", 0, obj
    else:
        yield "", 0, obj


def category_for_path(path: str, prefix: str) -> str:
    remaining = path[len(prefix):].strip("/")
    parts = remaining.split("/")
    return parts[0] if len(parts) > 1 else Path(parts[0]).stem


def json_pointer_component(text: str) -> str:
    """Escape one component for RFC 6901 JSON pointers."""
    return str(text).replace("~", "~0").replace("/", "~1")


def extract_nested_json(db: sqlite3.Connection, path: str, obj: Any) -> tuple[int, int]:
    """Index all structured JSON nodes and transpose column-oriented tables.

    Original source JSON is still kept intact in source_files. This *adds* views
    to browse hierarchical SRD documents; it does not guess where a rule ends.
    """
    nodes = 0
    rows = 0
    def visit(value: Any, pointer: str = "", label: str = "", depth: int = 0) -> None:
        nonlocal nodes, rows
        if not isinstance(value, (dict, list)):
            return
        db.execute("""INSERT INTO document_nodes
                   (source_file,json_pointer,node_type,label,depth,data_json)
                   VALUES(?,?,?,?,?,?)""",
                   (path,pointer,"object" if isinstance(value, dict) else "array",
                    label or ("root" if not pointer else None),depth,compact(value)))
        nodes += 1
        if isinstance(value, dict):
            # Many BTMorton tables are objects containing *column arrays*:
            # {"Item": ["Torch", "Rope"], "Cost": ["1 cp", "1 gp"]}.
            # Zip by row position, but only when all fields are columns of equal length.
            columns = {k:v for k,v in value.items() if isinstance(v, list)}
            if (len(columns) >= 2 and len(columns) == len(value)
                    and all(len(v) == len(next(iter(columns.values()))) for v in columns.values())
                    and len(next(iter(columns.values()))) > 0
                    and all(not isinstance(x, (list, dict)) for v in columns.values() for x in v)):
                count = len(next(iter(columns.values())))
                for i in range(count):
                    record = {k:v[i] for k,v in columns.items()}
                    name = next((str(record[k]) for k in ("Item","Name","Armor","Weapon","Type","Goods")
                                 if k in record and record[k] not in (None,"")), None)
                    db.execute("""INSERT INTO tabular_rows
                               (source_file,json_pointer,row_number,name,data_json)
                               VALUES(?,?,?,?,?)""", (path,pointer,i+1,name,compact(record)))
                    rows += 1
            for key, child in value.items():
                visit(child, pointer + "/" + json_pointer_component(key), str(key), depth+1)
        else:
            for i, child in enumerate(value):
                visit(child, pointer + "/" + str(i), str(i), depth+1)
    visit(obj)
    return nodes, rows


def import_github_json(db: sqlite3.Connection, source: dict, s: requests.Session,
                       limit: int, pause: float) -> list[str]:
    repo = source["repository"]
    branch = source["branch"]
    zip_url = f"https://codeload.github.com/{repo}/zip/refs/heads/{branch}"
    errors: list[str] = []
    collection_ids: set[int] = set()
    file_count = rec_count = node_count = row_count = 0
    suffixes = tuple(x.lower() for x in source.get("file_extensions", [".json"]))
    print(f"  Downloading repository ZIP: {repo}@{branch} (types: {', '.join(suffixes)})", flush=True)
    with tempfile.TemporaryDirectory(prefix="dnd_json_") as d:
        tmp_zip = Path(d) / "source.zip"
        with s.get(zip_url, stream=True, timeout=(20, 180), headers={"Accept": "application/zip"}) as res:
            res.raise_for_status()
            with tmp_zip.open("wb") as f:
                for chunk in res.iter_content(chunk_size=1024 * 1024):
                    if chunk:
                        f.write(chunk)
        with zipfile.ZipFile(tmp_zip) as archive:
            available_types: dict[str,int] = {}
            path_matches = 0
            for member in archive.infolist():
                if member.is_dir():
                    continue
                # The archive root directory's name is branch-dependent.
                parts = member.filename.split("/", 1)
                if len(parts) < 2:
                    continue
                path = parts[1]
                prefixes = source.get("include_prefixes", [""])
                if not any(path.startswith(prefix) for prefix in prefixes):
                    continue
                prefix = source.get("strip_prefix", "")
                if prefix and not path.startswith(prefix):
                    continue
                path_matches += 1
                ext = Path(path).suffix.lower()
                available_types[ext or '<none>'] = available_types.get(ext or '<none>', 0) + 1
                if ext not in suffixes:
                    continue
                if member.file_size > 100 * 1024 * 1024:
                    errors.append(f"{path}: file over 100 MiB safety limit")
                    continue
                try:
                    contents = archive.read(member)
                    raw = contents.decode("utf-8-sig")
                    if ext == ".json":
                        obj = json.loads(raw)
                    elif ext in (".yml", ".yaml"):
                        obj = yaml.safe_load(raw)
                    else:
                        raise ValueError(f"Unsupported file suffix {ext}")
                    if obj is None:
                        continue
                    db.execute("INSERT OR REPLACE INTO source_files(path,sha256,data_json) VALUES (?,?,?)",
                               (path, hashlib.sha256(contents).hexdigest(), compact(obj)))
                    if source.get("expand_nested"):
                        nc, rc = extract_nested_json(db, path, obj)
                        node_count += nc
                        row_count += rc
                    primary_category = category_for_path(path, prefix)
                    for nested, n, item in json_entries(obj):
                        category = nested or primary_category
                        cid = add_collection(db, category, "", f"https://github.com/{repo}/blob/{branch}/{path}")
                        collection_ids.add(cid)
                        external_id = f"{path}#{n}"
                        add_record(db, cid, external_id, item, source_file=path,
                                   source_url=f"https://github.com/{repo}/blob/{branch}/{path}")
                        rec_count += 1
                    file_count += 1
                except (ValueError, UnicodeDecodeError, sqlite3.Error, yaml.YAMLError) as exc:
                    errors.append(f"{path}: {exc}")
                if limit and file_count >= limit:
                    break
                if file_count % 200 == 0 and file_count:
                    db.commit()
                    print(f"    {file_count} parsed source files, {rec_count} records", flush=True)
    if not file_count:
        errors.append("No supported source files matched the configured prefixes/extensions")
        print(f"    Diagnostic: {path_matches} files under prefixes; suffixes: {available_types}", flush=True)
    for cid in collection_ids:
        mark_collection(db, cid, None, "error" if errors else ("sample" if limit else "complete"),
                        " | ".join(errors[:5]) if errors else "")
    print(f"    Finished {repo}: {file_count} files, {rec_count} records" +
          (f", {node_count} nested nodes, {row_count} tabular rows" if source.get("expand_nested") else ""),
          flush=True)
    return errors


def save_database(staging: Path, destination: Path) -> bool:
    """Finalize a source, without losing a downloaded database when Windows locks a file."""
    try:
        staging.replace(destination)
    except PermissionError as exc:
        print(f"  FILE LOCK: Could not replace {destination.name}: {exc}", file=sys.stderr)
        print(f"  PRESERVED: Downloaded data is still at {staging}", file=sys.stderr)
        print("  Close SQLite viewers / VS Code database tabs, then retry or rename this file manually.", file=sys.stderr)
        return False
    return True


def run_source(source: dict, output_dir: Path, limit: int, pause: float) -> bool:
    output_dir.mkdir(parents=True, exist_ok=True)
    base_name = f"{source['id']}{'_sample' if limit else ''}"
    target = output_dir / f"{base_name}.sqlite"
    # Per-process staging name avoids destroying a previous interrupted import.
    staging = output_dir / f"{base_name}.working.{os.getpid()}.sqlite"
    incomplete = output_dir / f"{base_name}.incomplete.sqlite"
    print(f"\n[{source['id']}] {source['name']}", flush=True)
    failures = []
    try:
        with closing(sqlite3.connect(staging)) as db, client() as s:
            init_db(db, source, limit)
            if source["type"] == "dnd5eapi":
                failures = import_five_e_bits(db, source, s, limit, pause)
            elif source["type"] == "open5e":
                failures = import_open5e(db, source, s, limit, pause)
            elif source["type"] == "github_json":
                failures = import_github_json(db, source, s, limit, pause)
            else:
                failures = [f"Unsupported source type: {source['type']}"]
            if not db.execute("SELECT COUNT(*) FROM records").fetchone()[0]:
                failures.append("Import returned zero records")
            if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                failures.append("SQLite integrity check failed")
            set_meta(db, "status", "incomplete" if failures else ("sample" if limit else "complete"))
            set_meta(db, "finished_utc", utcnow())
            set_meta(db, "errors_json", compact(failures))
            db.commit()
    except Exception as exc:
        failures.append(f"Fatal import error: {type(exc).__name__}: {exc}")
        print(f"  ERROR: {failures[-1]}", file=sys.stderr)
    destination = incomplete if failures else target
    if not staging.exists():
        print(f"  ERROR: Staging file was not created: {staging}", file=sys.stderr)
        return False
    if not save_database(staging, destination):
        return False
    if failures:
        print(f"  INCOMPLETE (not a validated full copy): {incomplete}", file=sys.stderr)
        print("  Errors:\n    " + "\n    ".join(failures[:20]), file=sys.stderr)
        return False
    print(f"  OK: {target}", flush=True)
    return True


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="One full-fidelity SQLite DB per D&D 5e API or JSON repository")
    p.add_argument("--config", type=Path, default=ROOT / "sources.json")
    p.add_argument("--output", type=Path, default=ROOT / "databases")
    p.add_argument("--source", action="append", help="Import one source ID (repeatable); default all enabled")
    p.add_argument("--list", action="store_true", help="List configured sources")
    p.add_argument("--max-per-collection", type=int, default=0,
                   help="For preview only: limit records from each API collection / JSON files from a repo")
    p.add_argument("--pause", type=float, default=0.08, help="Delay between API requests in seconds (default 0.08)")
    args = p.parse_args(argv)
    if args.max_per_collection < 0 or args.pause < 0:
        p.error("--max-per-collection and --pause must be nonnegative")
    data = json.loads(args.config.read_text(encoding="utf-8"))
    available = data["sources"]
    if args.list:
        for s in available:
            state = "enabled" if s.get("enabled", True) else "opt-in only"
            print(f"{s['id']:20} {s['type']:12} {state:12} {s['name']}")
        return 0
    if args.source:
        unknown = set(args.source) - {s["id"] for s in available}
        if unknown:
            p.error(f"Unknown source(s): {', '.join(sorted(unknown))}. Use --list.")
        sources = [s for s in available if s["id"] in args.source]
    else:
        sources = [s for s in available if s.get("enabled", True)]
    print(f"Importing {len(sources)} independent sources into {args.output}", flush=True)
    success = [run_source(s, args.output, args.max_per_collection, args.pause) for s in sources]
    print(f"\nSummary: {sum(success)}/{len(success)} sources imported successfully.", flush=True)
    return 0 if all(success) else 1


if __name__ == "__main__":
    raise SystemExit(main())
