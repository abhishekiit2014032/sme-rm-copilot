"""A small, fixed-graph runtime with a checkpointer (plan §3.7, §3.8, ADR-025; C28).

Stand-in for a graph framework such as Microsoft Agent Framework workflows or LangGraph: nodes, fixed edges, state,
a checkpoint after every node, interrupts for human approval and resume from a checkpoint.

* Nodes are `agent` (a specialist runs under its own agent identity; the `orchestrator` node is deterministic code
  with no model call, under the orchestrator workload identity, and is counted as an agent hop), `code`
  (deterministic: verification, commit, assembly) or `interrupt` (pauses the run: the state is checkpointed as
  `interrupted` and the run returns).
* Edges are fixed in the graph definition. A dict edge is selected by the request's use case, never by model output,
  so an agent cannot hand off to another agent of its choosing.
* At most `max_agent_hops` agent nodes run per request (orchestrator + one specialist = 2); a third is refused with
  429 `BUDGET_EXCEEDED` / `AGENT_HOPS`, whatever the graph says.
* `replay` re-executes the deterministic nodes from the checkpoint before them and checks that the stored state is
  reproduced exactly (debugging; operators fetch the checkpoints with a PIM-gated role).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Protocol

from services.common.httpkit import HttpError
from services.common.memory import memory_safe
from services.common.procedural import END


@dataclass
class RunContext:
    """Working memory of one request: never checkpointed (raw notes, question, approval token live here)."""
    run_id: str
    thread_id: str
    user: object
    customer_id: str | None = None
    inputs: dict = field(default_factory=dict)
    hops: int = 0
    step: int = 0
    resumed: bool = False


class Checkpointer(Protocol):
    def save(self, ctx: RunContext, node: str, step: int, status: str, state: dict) -> None: ...


class NullCheckpointer:
    def __init__(self) -> None:
        self.saved: list[dict] = []

    def save(self, ctx: RunContext, node: str, step: int, status: str, state: dict) -> None:
        self.saved.append({"run_id": ctx.run_id, "node": node, "step": step, "status": status, "state": memory_safe(state)})


Handler = Callable[[dict, RunContext], dict]


class GraphRuntime:
    def __init__(self, spec: dict, handlers: dict[str, Handler], checkpointer: Checkpointer, telemetry=None):
        nodes, edges = spec["nodes"], spec["edges"]
        if spec["entry"] not in nodes or set(nodes) - set(handlers) or set(edges) - set(nodes):
            raise ValueError("graph definition: unknown entry, node without handler or edge from an unknown node")
        for e in edges.values():
            for target in (e.values() if isinstance(e, dict) else [e]):
                if target != END and target not in nodes:
                    raise ValueError(f"graph definition: edge to unknown node {target}")
        self.spec, self.handlers, self.checkpointer, self.telemetry = spec, handlers, checkpointer, telemetry

    def next_node(self, node: str, state: dict) -> str:
        e = self.spec["edges"].get(node, END)
        if isinstance(e, dict):
            if state.get("use_case") not in e:
                raise HttpError(400, "VALIDATION_FAILED", "no route for this request in the graph", details={"reason": "GRAPH_ROUTE"})
            return e[state["use_case"]]
        return e

    def run(self, state: dict, ctx: RunContext) -> dict:
        return self._loop(self.spec["entry"], state, ctx)

    def resume(self, paused_node: str, state: dict, ctx: RunContext) -> dict:
        """Continue a run from its `interrupted` checkpoint (also in another orchestrator process)."""
        if self.spec["nodes"].get(paused_node, {}).get("kind") != "interrupt":
            raise HttpError(409, "RUN_STATE", "this run is not waiting for an approval", details={"reason": "NOT_INTERRUPTED"})
        ctx.resumed = True
        return self._loop(self.next_node(paused_node, state), state, ctx)

    def _span(self, node: str, ctx: RunContext):
        if self.telemetry is None:
            from contextlib import nullcontext
            return nullcontext()
        n = self.spec["nodes"][node]
        return self.telemetry.span(f"graph.node {node}", **{"rmc.node": node, "rmc.agent": n.get("agent"), "rmc.hop": ctx.hops,
                                                             "rmc.step": ctx.step, "rmc.graph": self.spec["version"]})

    def _loop(self, node: str, state: dict, ctx: RunContext) -> dict:
        max_hops = int(self.spec.get("max_agent_hops", 2))
        while node != END:
            kind = self.spec["nodes"][node]["kind"]
            if kind == "agent":
                if ctx.hops >= max_hops:
                    self._save_quietly(ctx, node, "failed", {**state, "failed_at": node, "failure": "AGENT_HOPS"})
                    raise HttpError(429, "BUDGET_EXCEEDED", "agent hop budget exceeded", details={"reason": "AGENT_HOPS", "max_agent_hops": max_hops},
                                    retryable=False)
                ctx.hops += 1
            with self._span(node, ctx):
                try:
                    updates = self.handlers[node](state, ctx) or {}
                except BaseException:
                    self._save_quietly(ctx, node, "failed", {**state, "path": [*state.get("path", []), node], "failed_at": node})
                    ctx.step += 1
                    raise
            state = {**state, **updates, "path": [*state.get("path", []), node]}
            nxt = END if kind == "interrupt" else self.next_node(node, state)
            status = "interrupted" if kind == "interrupt" else ("completed" if nxt == END else "running")
            self.checkpointer.save(ctx, node, ctx.step, status, state)
            ctx.step += 1
            if kind == "interrupt":
                return state
            node = nxt
        return state

    def _save_quietly(self, ctx: RunContext, node: str, status: str, state: dict) -> None:
        try:
            self.checkpointer.save(ctx, node, ctx.step, status, state)
        except Exception:  # noqa: BLE001 - a failed checkpoint must not hide the node's own error
            pass

    def replay(self, checkpoints: list[dict]) -> dict:
        """Re-run each deterministic node on the state of the checkpoint before it; the result must equal the stored
        state. Returns {"ok", "replayed": [nodes], "mismatches": [nodes], "state": final state}."""
        ordered = sorted(checkpoints, key=lambda c: c["step"])
        replayed, mismatches = [], []
        for prev, cur in zip(ordered, ordered[1:]):
            n = self.spec["nodes"].get(cur["node"], {})
            if not n.get("deterministic") or prev.get("state") is None or cur.get("state") is None:
                continue
            ctx = RunContext(run_id=cur["run_id"], thread_id="replay", user=None)
            st = prev["state"]
            expected = memory_safe({**st, **(self.handlers[cur["node"]](st, ctx) or {}), "path": [*st.get("path", []), cur["node"]]})
            (replayed if expected == cur["state"] else mismatches).append(cur["node"])
        return {"ok": not mismatches, "replayed": replayed, "mismatches": mismatches, "state": ordered[-1]["state"] if ordered else None}
