"""FB4/FB5: actual node storage, event identity and repeated Runs in all origins/modes."""

import importlib.util
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]
from blocs.business_records.block import BusinessRecordsBlock
from bloxsmith_app.block_api import BlockRuntimeContext
from block_test_packages import install_test_package, surface_payload
from ui_smoke_common import (create_project_api, create_run_api, data_edge, graph_payload,
    graph_storage_dir, http_json, isolated_server, text_node, wait_for_run_terminal)

SPEC = importlib.util.spec_from_file_location("records_fixture", Path(__file__).with_name("F5.01_business_records.py"))
FIX = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(FIX)


def main():
    block = BusinessRecordsBlock()
    assert block.execute_runtime(BlockRuntimeContext(config={})).status == "skipped"
    # Incoming port events are authoritative even if the other input is cached.
    with TemporaryDirectory(prefix="records-context-") as temporary:
        def context(port, value):
            return SimpleNamespace(config=FIX.CONFIG, input_events=[SimpleNamespace(input_port_id=port, value=json.dumps(value))],
                input_ports=[SimpleNamespace(id=1, name="request"), SimpleNamespace(id=2, name="decision")],
                output_ports=[SimpleNamespace(id=index+1, name=name) for index, name in enumerate(("records", "proposal", "receipt", "status"))],
                services={"get_block_storage_dir": lambda: Path(temporary)},
                input_value=lambda *_args: (_ for _ in ()).throw(AssertionError("Stale cached values must not be read")))
        prepared = block.execute_runtime(context(1, FIX.proposal()))
        assert prepared.status == "success", prepared.error
        plan = json.loads(next(item.value for item in prepared.outputs if item.port_name == "proposal"))
        applied = block.execute_runtime(context(2, FIX.decision(plan)))
        assert applied.status == "success" and {item.port_name for item in applied.outputs} == {"receipt", "status"}
        ambiguous = context(2, FIX.decision(plan))
        ambiguous.input_events *= 2
        assert block.execute_runtime(ambiguous).status == "failed"
        missing = context(1, FIX.read())
        missing.services = {}
        assert block.execute_runtime(missing).status == "failed"

    for mode in ("centralized", "zeromq_active"):
        for origin in (None, "managed", "linked"):
            with isolated_server() as server:
                node = block.build_node_payload(node_id="records", position={"x": 420, "y": 180}, config_overrides=FIX.CONFIG)
                if origin:
                    model = install_test_package(server, "business_records", origin=origin)
                    node["block_version"] = model["version"]
                    for surface in ("modal", "inspector_panel", "node_card"):
                        payload = surface_payload(server, model, node, surface)
                        assert "{{" not in payload["html"]
                        if surface != "node_card":
                            assert 'data-block-config-field="allow_writes"' in payload["html"]
                document = graph_payload("Local reference records", [text_node("source", "Explicit request", "", 70, 180), node],
                    [data_edge("input", "source", 1, "records", 1)])
                project = create_project_api(server, document=document)["project"]

                def execute(value, port=1):
                    document["nodes"][0]["outputs"][0]["text"] = json.dumps(value)
                    document["edges"][0]["to"]["port"] = port
                    (graph_storage_dir(server, project) / "graph.json").write_text(
                        json.dumps({**document, "graph_id": project["graph_id"]}), encoding="utf-8")
                    http_json(server.base_url, f"/api/projects/{project['graph_id']}/graph/reload", method="POST", payload={})
                    run = create_run_api(server, document, project_id=project["graph_id"], runtime_mode=mode)
                    result = wait_for_run_terminal(server, run["run_id"], timeout_sec=30)
                    assert result["status"] == "success", result.get("logs")
                    return {key: json.loads(row["value"]) for key, row in result["output_values"].items() if key.startswith("records:")}

                before = execute(FIX.read())
                assert before["records:1"]["items"][0]["revision"] == 1
                planned = execute(FIX.proposal())["records:2"]
                applied = execute(FIX.decision(planned), 2)
                assert applied["records:3"]["revision"] == 2 and applied["records:3"]["source"] == "local"
                replay = execute(FIX.decision(planned), 2)
                assert "records:3" not in replay and replay["records:4"]["code"] == "already_applied"
                final = execute(FIX.read())["records:1"]["items"][0]
                assert final["revision"] == 2 and final["data"]["quantity"] == 15
            print("[ok] Business Records persistence " + mode + " " + str(origin), flush=True)


if __name__ == "__main__":
    main()
