"""Local reference catalog with immutable proposals and transactionally applied approvals."""

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import time

DEFAULTS = {
    "namespace": "demo", "object_types": ["inventory", "orders"],
    "field_mapping": {"reference": "reference", "label": "label", "quantity": "quantity", "status": "status"},
    "match_fields": ["reference"], "writable_fields": [], "allow_writes": False,
    "seed_data": [], "proposal_ttl_sec": 600, "max_records": 1000,
    "max_proposals": 10000, "max_bytes": 131072,
}
KEY = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")


class RecordsError(ValueError):
    """An invalid contract or unavailable local store, never a fabricated ERP result."""


def identifier(value):
    """Validate business keys independently from filesystem locations."""
    if not isinstance(value, str) or not KEY.fullmatch(value):
        raise RecordsError("Identifiers require 1–128 ASCII letters, digits, dots, colons, underscores or hyphens.")
    return value


def encode(value, limit=262144):
    """Bound UTF-8 JSON and depth; reject non-JSON Python values and non-finite numbers."""
    pending = [(value, 0)]
    while pending:
        current, depth = pending.pop()
        if depth > 24:
            raise RecordsError("JSON nesting exceeds 24 levels.")
        if isinstance(current, dict):
            if any(type(key) is not str for key in current):
                raise RecordsError("JSON keys must be strings.")
            pending.extend((child, depth + 1) for child in current.values())
        elif isinstance(current, list):
            pending.extend((child, depth + 1) for child in current)
        elif type(current) not in (str, int, float, bool, type(None)):
            raise RecordsError("Only JSON-compatible data is accepted.")
    try:
        raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
        if len(raw.encode("utf-8")) > limit:
            raise RecordsError("JSON exceeds its byte limit.")
        return raw
    except (ValueError, TypeError, UnicodeError, OverflowError) as exc:
        raise RecordsError("A finite, bounded JSON value is required.") from exc


def parse(value, limit):
    """Reject duplicate keys before resolving the operation, target or approval."""
    if isinstance(value, str):
        def unique(items):
            result = {}
            for key, child in items:
                if key in result:
                    raise RecordsError("Duplicate JSON keys are not accepted.")
                result[key] = child
            return result
        try:
            if len(value.encode("utf-8")) > limit:
                raise RecordsError("Request exceeds its byte limit.")
            value = json.loads(value, object_pairs_hook=unique,
                               parse_constant=lambda _: (_ for _ in ()).throw(RecordsError("Non-finite JSON.")))
        except (ValueError, UnicodeError, RecursionError) as exc:
            raise RecordsError("Malformed or oversized JSON request.") from exc
    encode(value, limit)
    if not isinstance(value, dict):
        raise RecordsError("A single JSON object is required.")
    return value


def configuration(raw):
    """Keep initialization and permissions trusted, separate from request data."""
    if not isinstance(raw, dict) or set(raw) - set(DEFAULTS) - {"execution", "runtime_path", "runtime_path_label"}:
        raise RecordsError("Unknown setting.")
    config = {**DEFAULTS, **{key: value for key, value in raw.items() if key in DEFAULTS}}
    identifier(config["namespace"])
    for key, low, high in (("proposal_ttl_sec", 30, 86400), ("max_records", 1, 10000),
                          ("max_proposals", 1, 20000), ("max_bytes", 1024, 262144)):
        if type(config[key]) is not int or not low <= config[key] <= high:
            raise RecordsError(f"{key} must be an integer between {low} and {high}.")
    if type(config["allow_writes"]) is not bool:
        raise RecordsError("allow_writes must be a boolean.")
    mapping = config["field_mapping"]
    if not isinstance(mapping, dict) or not 1 <= len(mapping) <= 64:
        raise RecordsError("field_mapping must contain 1–64 canonical-to-source keys.")
    for key, value in mapping.items():
        identifier(key)
        identifier(value)
    if len(set(mapping.values())) != len(mapping):
        raise RecordsError("Two canonical fields cannot alias the same source field.")
    for key in ("object_types", "match_fields", "writable_fields"):
        value = config[key]
        if not isinstance(value, list) or len(value) > 64:
            raise RecordsError(f"{key} must be an array of at most 64 keys.")
        for item in value:
            identifier(item)
        if len(set(value)) != len(value) or (key != "object_types" and set(value) - set(mapping)):
            raise RecordsError(f"{key} contains duplicate or unmapped keys.")
    if not config["object_types"] or not config["match_fields"]:
        raise RecordsError("object_types and match_fields cannot be empty.")
    seed = config["seed_data"]
    if not isinstance(seed, list) or len(seed) > config["max_records"]:
        raise RecordsError("seed_data must be an array within max_records.")
    encode(seed, 4 * 1024 * 1024)
    seen = set()
    for row in seed:
        if not isinstance(row, dict) or set(row) != {"object_type", "source_id", "data"}:
            raise RecordsError("Each seed record requires object_type, source_id and data only.")
        kind, source_id = identifier(row["object_type"]), identifier(row["source_id"])
        if kind not in config["object_types"] or (kind, source_id) in seen:
            raise RecordsError("Seed records must have distinct allowed type/ID pairs.")
        data = row["data"]
        if not isinstance(data, dict) or len(data) > 64:
            raise RecordsError("Each source record requires a data object with at most 64 fields.")
        for key in data:
            identifier(key)
        encode(data, config["max_bytes"])
        seen.add((kind, source_id))
    return config


def digest(value, limit=262144):
    return hashlib.sha256(encode(value, limit).encode("utf-8")).hexdigest()


@contextmanager
def database(directory, cancel):
    """One node's public storage directory; FULL SQLite commit includes receipt and mutation."""
    directory = Path(directory)
    if not directory.is_absolute() or not directory.is_dir() or directory.is_symlink():
        raise RecordsError("An existing canonical block storage directory is required.")
    path = directory / "records.sqlite3"
    if path.is_symlink():
        raise RecordsError("The database must not be a symbolic link.")
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0), 0o600)
    except FileExistsError:
        if not path.is_file():
            raise RecordsError("The database must be a regular file.")
    else:
        os.close(fd)
    db = sqlite3.connect(path, timeout=2)
    try:
        db.execute("PRAGMA busy_timeout=2000")
        db.execute("PRAGMA max_page_count=16384")
        db.execute("PRAGMA synchronous=FULL")
        db.execute("BEGIN IMMEDIATE")
        if db.execute("PRAGMA user_version").fetchone()[0] not in (0, 1):
            raise RecordsError("Unsupported database schema; no automatic reset.")
        db.execute("CREATE TABLE IF NOT EXISTS datasets(namespace TEXT PRIMARY KEY, seed_hash TEXT)")
        db.execute("CREATE TABLE IF NOT EXISTS records(namespace TEXT, kind TEXT, source_id TEXT, revision INTEGER, data TEXT, PRIMARY KEY(namespace,kind,source_id))")
        db.execute("CREATE TABLE IF NOT EXISTS proposals(namespace TEXT, id TEXT, request_hash TEXT, proposal TEXT, digest TEXT, state TEXT, receipt TEXT, PRIMARY KEY(namespace,id))")
        db.execute("PRAGMA user_version=1")
        yield db
        if cancel():
            raise RecordsError("Operation cancelled before commit.")
        db.commit()
    except BaseException:
        db.rollback()
        raise
    finally:
        db.close()


def normalized(row, config):
    """Projection is explicit; absent source fields remain absent rather than becoming zero."""
    kind, source_id, revision, raw = row
    data = json.loads(raw)
    return {"source": "local", "namespace": config["namespace"], "object_type": kind,
            "source_id": source_id, "revision": revision,
            "data": {key: data[value] for key, value in config["field_mapping"].items() if value in data}}


def process(value, port, config, directory, cancel=lambda: False, now=None):
    """Handle a read, immutable proposal or exact decision; never execute arbitrary SQL or HTTP."""
    config = configuration(config)
    request = parse(value, config["max_bytes"])
    request_id = identifier(request.get("request_id"))
    namespace = config["namespace"]
    now = time.time() if now is None else now
    if cancel():
        raise RecordsError("Operation cancelled.")
    with database(directory, cancel) as db:
        seed_hash = digest(config["seed_data"], 4 * 1024 * 1024)
        seed_row = db.execute("SELECT seed_hash FROM datasets WHERE namespace=?", (namespace,)).fetchone()
        if seed_row and seed_row[0] != seed_hash:
            raise RecordsError("seed_data changed for an initialized namespace. Select a new namespace; existing records are untouched.")
        if not seed_row:
            db.executemany("INSERT INTO records VALUES (?,?,?,?,?)", [(namespace, row["object_type"], row["source_id"],
                1, encode(row["data"], config["max_bytes"])) for row in config["seed_data"]])
            db.execute("INSERT INTO datasets VALUES (?,?)", (namespace, seed_hash))
        policy_hash = digest({key: config[key] for key in ("namespace", "object_types", "field_mapping", "writable_fields", "allow_writes")})

        def status(code, **extra):
            return {"status": {"request_id": request_id, "source": "local", "namespace": namespace, "code": code, **extra}}

        if port == "decision":
            if set(request) != {"request_id", "proposal_id", "digest", "approved"} or type(request["approved"]) is not bool:
                raise RecordsError("Decision requires request_id, proposal_id, digest and boolean approved only.")
            proposal_id = identifier(request["proposal_id"])
            if not isinstance(request["digest"], str) or not re.fullmatch(r"[0-9a-f]{64}", request["digest"]):
                raise RecordsError("Decision requires the exact proposal SHA-256 digest.")
            row = db.execute("SELECT proposal,digest,state,receipt FROM proposals WHERE namespace=? AND id=?", (namespace, proposal_id)).fetchone()
            if row is None:
                return status("proposal_not_found")
            proposal = json.loads(row[0])
            if row[1] != request["digest"]:
                return status("digest_mismatch")
            if row[2] != "pending":
                return status("already_" + row[2], proposal_id=proposal_id)
            if not request["approved"]:
                db.execute("UPDATE proposals SET state='rejected' WHERE namespace=? AND id=?", (namespace, proposal_id))
                return status("rejected", proposal_id=proposal_id)
            if now >= proposal["expires_at"]:
                return status("expired", proposal_id=proposal_id)
            if not config["allow_writes"]:
                return status("read_only", proposal_id=proposal_id)
            if proposal["policy_hash"] != policy_hash:
                return status("policy_changed", proposal_id=proposal_id)
            target = db.execute("SELECT kind,source_id,revision,data FROM records WHERE namespace=? AND kind=? AND source_id=?",
                                (namespace, proposal["object_type"], proposal["source_id"])).fetchone()
            if target is None or target[2] != proposal["expected_revision"]:
                return status("revision_conflict", proposal_id=proposal_id, actual_revision=target[2] if target else None)
            data = json.loads(target[3])
            for key, value in proposal["changes"].items():
                data[config["field_mapping"][key]] = value
            revision = target[2] + 1
            raw = encode(data, config["max_bytes"])
            db.execute("UPDATE records SET revision=?,data=? WHERE namespace=? AND kind=? AND source_id=? AND revision=?",
                       (revision, raw, namespace, target[0], target[1], target[2]))
            receipt = {"request_id": request_id, "proposal_id": proposal_id, "digest": row[1], "source": "local",
                       "namespace": namespace, "object_type": target[0], "source_id": target[1],
                       "previous_revision": target[2], "revision": revision, "applied_at": now, "status": "applied"}
            db.execute("UPDATE proposals SET state='applied',receipt=? WHERE namespace=? AND id=?",
                       (encode(receipt), namespace, proposal_id))
            return {**status("applied"), "receipt": receipt}

        if port != "request":
            raise RecordsError("Unknown input port.")
        action = request.get("operation")
        if action == "get_receipt":
            if set(request) != {"request_id", "operation", "proposal_id"}:
                raise RecordsError("get_receipt accepts proposal_id only.")
            proposal_id = identifier(request["proposal_id"])
            row = db.execute("SELECT state,receipt FROM proposals WHERE namespace=? AND id=?", (namespace, proposal_id)).fetchone()
            # Inspection stays on status so recovery cannot silently re-emit a mutation receipt.
            return status("proposal_status", proposal_id=proposal_id, state=row[0] if row else "not_found",
                          receipt=json.loads(row[1]) if row and row[1] else None)
        kind = identifier(request.get("object_type"))
        if kind not in config["object_types"]:
            return status("object_type_refused")
        if action in {"get", "propose_update"}:
            source_id = identifier(request.get("source_id"))
            target = db.execute("SELECT kind,source_id,revision,data FROM records WHERE namespace=? AND kind=? AND source_id=?",
                                (namespace, kind, source_id)).fetchone()
        if action == "get":
            if set(request) != {"request_id", "operation", "object_type", "source_id"}:
                raise RecordsError("get accepts object_type and source_id only.")
            return {**status("found" if target else "not_found"), "records": {"request_id": request_id,
                "source": "local", "items": [normalized(target, config)] if target else [], "next_after": None}}
        if action in {"list", "match"}:
            allowed = {"request_id", "operation", "object_type", "limit", "after"}
            if action == "match":
                allowed |= {"field", "value"}
                if request.get("field") not in config["match_fields"] or type(request.get("value")) not in (str, int, float, bool):
                    raise RecordsError("match requires an allowed field and a scalar exact value.")
            if set(request) - allowed:
                raise RecordsError("Unknown lookup field.")
            limit = request.get("limit", 20)
            if type(limit) is not int or not 1 <= limit <= 100:
                raise RecordsError("limit must be between 1 and 100.")
            after = identifier(request["after"]) if "after" in request else ""
            rows = db.execute("SELECT kind,source_id,revision,data FROM records WHERE namespace=? AND kind=? AND source_id>? ORDER BY source_id LIMIT ?",
                              (namespace, kind, after, config["max_records"] + 1))
            found = []
            for row in rows:
                if cancel():
                    raise RecordsError("Operation cancelled.")
                item = normalized(row, config)
                if action == "match" and (request["field"] not in item["data"] or
                        encode(item["data"][request["field"]]) != encode(request["value"])):
                    continue
                found.append(item)
                if len(found) > limit:
                    break
            result = {"request_id": request_id, "source": "local", "items": found[:limit],
                      "next_after": found[limit - 1]["source_id"] if len(found) > limit else None}
            encode(result, 4 * 1024 * 1024)
            return {**status("found" if found else "not_found"), "records": result}
        if action != "propose_update":
            raise RecordsError("Unknown operation. Use list, get, match, propose_update or get_receipt.")
        if set(request) != {"request_id", "operation", "object_type", "source_id", "expected_revision", "changes"}:
            raise RecordsError("propose_update requires exact target, expected_revision and changes.")
        revision, changes = request["expected_revision"], request["changes"]
        if type(revision) is not int or revision < 1 or not isinstance(changes, dict) or not changes:
            raise RecordsError("A positive expected_revision and nonempty changes object are required.")
        if set(changes) - set(config["writable_fields"]):
            return status("fields_refused")
        request_hash = digest(request, config["max_bytes"])
        previous = db.execute("SELECT request_hash,proposal,digest,state FROM proposals WHERE namespace=? AND id=?", (namespace, request_id)).fetchone()
        if previous:
            if previous[0] != request_hash:
                return status("request_id_reused")
            return {**status("proposal_replayed", state=previous[3]), "proposal": {**json.loads(previous[1]), "digest": previous[2], "state": previous[3]}}
        if target is None or target[2] != revision:
            return status("revision_conflict", actual_revision=target[2] if target else None)
        if db.execute("SELECT count(*) FROM proposals WHERE namespace=?", (namespace,)).fetchone()[0] >= config["max_proposals"]:
            return status("proposal_capacity")
        before = normalized(target, config)["data"]
        after = {**before, **changes}
        encode(after, config["max_bytes"])
        # Verify the full source object, including unmapped fields, before asking
        # for approval. A preview must not hide an already-known storage overflow.
        candidate = json.loads(target[3])
        candidate.update({config["field_mapping"][key]: value for key, value in changes.items()})
        encode(candidate, config["max_bytes"])
        proposal = {"proposal_id": request_id, "source": "local", "namespace": namespace, "object_type": kind,
                    "source_id": source_id, "expected_revision": revision, "before": before, "after": after,
                    "changes": changes, "expires_at": now + config["proposal_ttl_sec"], "policy_hash": policy_hash}
        raw = encode(proposal, 3 * config["max_bytes"])
        fingerprint = hashlib.sha256(raw.encode("utf-8")).hexdigest()
        db.execute("INSERT INTO proposals VALUES (?,?,?,?,?,'pending',NULL)", (namespace, request_id, request_hash, raw, fingerprint))
        return {**status("proposed"), "proposal": {**proposal, "digest": fingerprint, "state": "pending"}}
