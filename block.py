"""Independent local reference-data block using only the public runtime and UI contracts."""

import json
import sqlite3
from bloxsmith_app.block_api import APPLICATION_JSON, BlockDefinition, BlockRuntimeOutput, BlockRuntimeResult
from . import logic, ui


# FB1 - Query explicit local seed records with bounded exact matching and source IDs.
# FB2 - Persist immutable proposals and apply exact, revision-checked decisions atomically.
# FB3 - Refuse replay, stale policy, expiry and overflow without resetting retained data.
# FB4 - Preserve input-event identity, per-node storage and outputs across runtimes/origins.
# FB5 - Provide translated responsive modal and inspector with explicit local/write policy.
class BusinessRecordsBlock(BlockDefinition):
    """This is a local catalog, not an implicit connection to a production ERP."""

    kind = "business_records"

    def ui_assets(self, surface="modal"):
        return list(self.model.get("ui_assets", {}).get(surface, []))

    def execute_runtime(self, context):
        try:
            config = logic.configuration(context.config)
            if context.input_events:
                names = {int(port.id): port.name for port in context.input_ports}
                messages = [(names.get(event.input_port_id), event.value) for event in context.input_events]
            else:
                messages = [(name, context.input_value(name, str(ident))) for ident, name in ((1, "request"), (2, "decision"))
                            if context.has_input_value(name, str(ident))]
            if not messages:
                return BlockRuntimeResult(status="skipped", outputs=[])
            if len(messages) != 1:
                raise logic.RecordsError("Send one request or decision per activation; ambiguous batches are refused.")
            resolver = context.services.get("get_block_storage_dir")
            if not callable(resolver):
                raise logic.RecordsError("Persistent block storage is unavailable; no fallback path is used.")
            port, value = messages[0]
            result = logic.process(value, port, config, resolver(), context.services.get("cancel_requested", lambda: False))
            outputs = [BlockRuntimeOutput(port_id=int(port.id), port_name=port.name,
                       value=json.dumps(result[port.name], ensure_ascii=False, allow_nan=False), content_type=APPLICATION_JSON)
                       for port in context.output_ports if port.name in result]
            return BlockRuntimeResult(status="success", outputs=outputs, content_type=APPLICATION_JSON,
                metadata={self.kind: {"source": "local", "code": result["status"]["code"]}},
                logs=["[business-records] Local operation completed: " + result["status"]["code"]])
        except (ValueError, TypeError, RecursionError, RuntimeError, OSError, sqlite3.Error) as exc:
            detail = str(exc) if isinstance(exc, logic.RecordsError) else "Local data storage is unavailable or invalid; nothing was reset."
            message = self.translate("block.business_records.error", {"detail": detail}, fallback="Record operation failed: {detail}")
            return BlockRuntimeResult(status="failed", outputs=[], error=message, exit_code=1,
                                      logs=["[business-records-error] " + detail])

    def handle_ui_action(self, *, node, action, values, payload=None):
        if action in {"save_settings", "modal_update_fields", "inspector_update_fields"}:
            patch = (values or {}).get("node_patch") or {}
            if "config" in patch:
                try:
                    logic.configuration({**self.default_config(), **(node.get("config") or {}), **patch["config"]})
                except (ValueError, TypeError) as exc:
                    return {"error": self.translate("block.business_records.error", {"detail": str(exc)}, fallback="Record operation failed: {detail}")}
        return ui.replace_config(super().handle_ui_action(node=node,
            action="modal_update_fields" if action == "save_settings" else action, values=values, payload=payload), node)

    def render_modal(self, *, node, payload=None):
        return ui.modal(self, node, payload)

    def render_inspector_panel(self, *, node, payload=None):
        return ui.inspector(self, node, payload)

    def render_node_card(self, *, node, payload=None):
        config = {**self.default_config(), **(node.get("config") or {})}
        mode = "write_mode" if config["allow_writes"] else "read_mode"
        fallback = "Approved writes" if config["allow_writes"] else "Read only"
        return ui.card(self, node, str(config["namespace"]) + " · " + self.translate("block.business_records." + mode, fallback=fallback))
