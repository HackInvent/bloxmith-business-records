"""FB1/FB2/FB3: real SQLite catalog, exact approvals, revisions, concurrency and bounds."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import copy
import json
import sqlite3
import sys
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]
from blocs.business_records import logic

SEED = [{"object_type": "inventory", "source_id": "item-1", "data": {"sku": "REF-1", "stock": 12, "name": "Sample"}},
        {"object_type": "inventory", "source_id": "item-2", "data": {"sku": "REF-1", "stock": 4}},
        {"object_type": "orders", "source_id": "order-1", "data": {"sku": "REF-1", "stock": True}}]
CONFIG = {"seed_data": SEED, "field_mapping": {"reference": "sku", "quantity": "stock", "label": "name"},
          "writable_fields": ["quantity"], "allow_writes": True}


def read(operation="get", **extra):
    return {"request_id": "read-1", "operation": operation, "object_type": "inventory",
            **({"source_id": "item-1"} if operation == "get" else {}), **extra}


def proposal(request_id="change-1", revision=1, **extra):
    return {"request_id": request_id, "operation": "propose_update", "object_type": "inventory",
            "source_id": "item-1", "expected_revision": revision, "changes": {"quantity": 15}, **extra}


def decision(plan, **extra):
    return {"request_id": "approve-" + plan["proposal_id"], "proposal_id": plan["proposal_id"],
            "digest": plan["digest"], "approved": True, **extra}


def rejected(action):
    try:
        action()
    except (logic.RecordsError, sqlite3.Error):
        return
    raise AssertionError("Invalid data or corrupt storage was accepted")


def main():
    with TemporaryDirectory(prefix="business-records-") as temporary:
        root = Path(temporary)
        config = logic.configuration(CONFIG)
        def call(value, port="request", **kwargs):
            return logic.process(value, port, config, root, now=1000, **kwargs)
        first = call(read())
        assert first["records"]["items"][0]["data"] == {"reference": "REF-1", "quantity": 12, "label": "Sample"}
        matches = call(read("match", field="reference", value="REF-1", limit=1))["records"]
        assert matches["next_after"] == "item-1" and len(matches["items"]) == 1
        second = call(read("match", field="reference", value="REF-1", after=matches["next_after"], limit=1))["records"]
        assert second["items"][0]["source_id"] == "item-2" and second["next_after"] is None
        assert "label" not in second["items"][0]["data"]
        assert call(read("match", field="reference", value="missing"))["records"]["items"] == []
        assert call(read(source_id="not-found"))["status"]["code"] == "not_found"
        assert call(read(object_type="forbidden"))["status"]["code"] == "object_type_refused"
        typed = {**config, "match_fields": ["quantity"]}
        assert logic.process(read("match", object_type="orders", field="quantity", value=1), "request", typed, root)["records"]["items"] == []
        assert len(logic.process(read("match", object_type="orders", field="quantity", value=True), "request", typed, root)["records"]["items"]) == 1
        plan = call(proposal())["proposal"]
        assert plan["before"]["quantity"] == 12 and plan["after"]["quantity"] == 15 and len(plan["digest"]) == 64
        assert call(read())["records"]["items"][0]["revision"] == 1, "A proposal cannot modify its target"
        assert call(proposal())["proposal"] == plan
        assert call(proposal(changes={"quantity": 99}))["status"]["code"] == "request_id_reused"
        assert call(decision(plan, digest="0" * 64), "decision")["status"]["code"] == "digest_mismatch"
        assert logic.process(decision(plan), "decision", config, root, now=2000)["status"]["code"] == "expired"
        assert logic.process(decision(plan), "decision", config, root, now=plan["expires_at"])["status"]["code"] == "expired"
        assert logic.process(decision(plan), "decision", {**config, "allow_writes": False}, root, now=1001)["status"]["code"] == "read_only"
        assert logic.process(decision(plan), "decision", {**config, "writable_fields": []}, root, now=1001)["status"]["code"] == "policy_changed"
        result = call(decision(plan), "decision")
        assert result["receipt"]["revision"] == 2 and result["receipt"]["source"] == "local"
        replay = call(decision(plan), "decision")
        assert replay["status"]["code"] == "already_applied" and "receipt" not in replay
        assert call(read())["records"]["items"][0]["data"]["quantity"] == 15
        receipt = call({"request_id": "inspect", "operation": "get_receipt", "proposal_id": plan["proposal_id"]})
        assert receipt["status"]["receipt"] == result["receipt"] and "receipt" not in receipt
        assert call(proposal("stale"))["status"]["code"] == "revision_conflict"
        plans = [call(proposal("race-" + str(index), revision=2, changes={"quantity": index}))["proposal"] for index in (1, 2)]
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda p: call(decision(p), "decision"), plans))
        assert sum("receipt" in result for result in results) == 1
        assert sorted(result["status"]["code"] for result in results) == ["applied", "revision_conflict"]
        assert call(read())["records"]["items"][0]["revision"] == 3
        declined = call(proposal("decline", revision=3))["proposal"]
        assert call(decision(declined, approved=False), "decision")["status"]["code"] == "rejected"
        assert call(decision(declined), "decision")["status"]["code"] == "already_rejected"
        cancelled = call(proposal("cancelled", revision=3))["proposal"]
        counter = iter((False, True))
        rejected(lambda: call(decision(cancelled), "decision", cancel=lambda: next(counter)))
        assert call(read())["records"]["items"][0]["revision"] == 3
        assert call({"request_id": "inspect", "operation": "get_receipt", "proposal_id": "cancelled"})["status"]["state"] == "pending"
        assert logic.process(proposal("full", revision=3), "request", {**config, "max_proposals": 1}, root)["status"]["code"] == "proposal_capacity"
        altered = copy.deepcopy(config)
        altered["seed_data"][0]["data"]["stock"] = 999
        rejected(lambda: logic.process(read(), "request", altered, root))
        new_partition = logic.process(read(), "request", {**altered, "namespace": "other"}, root)
        assert new_partition["records"]["items"][0]["data"]["quantity"] == 999
        assert call(read())["records"]["items"][0]["revision"] == 3
        for bad in (read(source_id="../escape"), read("list", limit=True), read("list", limit=101),
                    read("match", field="quantity", value=1), read("match", field="reference", value=None),
                    read("list", arbitrary="SQL"), {**read(), "request_id": "' OR 1=1"},
                    proposal("bad", revision=True), {**read(), "data": float("nan")},
                    '{"request_id":"a","request_id":"b"}', [read()]):
            rejected(lambda: call(bad))
        for bad in ({"namespace": "../escape"}, {"allow_writes": "true"}, {"field_mapping": {"a": "x", "b": "x"}},
                    {"match_fields": []}, {"object_types": []}, {"max_records": True},
                    {"seed_data": [SEED[0], SEED[0]]}, {"writable_fields": ["missing"]}, {"token": "secret"}):
            rejected(lambda: logic.configuration(bad))
        assert call(proposal("bad-fields", revision=3, changes={"label": "Unauthorized"}))["status"]["code"] == "fields_refused"
        assert (root / "records.sqlite3").stat().st_mode & 0o777 == 0o600
        with sqlite3.connect(root / "records.sqlite3") as db:
            db.execute("PRAGMA user_version=999")
        rejected(lambda: call(read()))
        with sqlite3.connect(root / "records.sqlite3") as db:
            assert db.execute("SELECT revision FROM records WHERE namespace='demo' AND source_id='item-1'").fetchone()[0] == 3
    with TemporaryDirectory(prefix="records-links-") as temporary:
        root = Path(temporary)
        target = root / "target"
        target.write_bytes(b"unchanged")
        (root / "records.sqlite3").symlink_to(target)
        rejected(lambda: logic.process(read(), "request", CONFIG, root))
        assert target.read_bytes() == b"unchanged"
    print("[ok] FB1/FB2/FB3 local records, proposals, atomic receipts, concurrency, expiry and bounds", flush=True)


if __name__ == "__main__":
    main()
