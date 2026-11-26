"""Procedural memory registry (plan §3.8): how the copilot does its job, versioned and hashed, pinned in App
Configuration. Never changed at runtime by the model; engineers change it through Git and the eval gate only.

* prompt templates (the system prompts in `rm_copilot/llm.py`), each with a version and a SHA-256;
* the agent graph definition (`GRAPH_SPEC`, executed by `services/graph.py`), version and SHA-256;
* the MCP tool definitions (`mcp.py`), SHA-256 per tool (tool poisoning, C25).

The deployment pipeline publishes `pins()` to App Configuration key `rmc:procedural:pins` (locally `stack.py`
does it). At run time the orchestrator refuses to run a graph or send a prompt whose hash is not the pinned one
(503 `PROCEDURAL_PIN_MISMATCH`), and tool-gateway refuses to list or call a tool whose definition does not match.
"""
from __future__ import annotations

from rm_copilot.llm import BRIEF_SYSTEM, EXTRACT_SYSTEM, POLICY_QA_SYSTEM, QUERY_REWRITE_SYSTEM, STRICT_SUFFIX

from . import jose
from .mcp import tool_pins

PINS_KEY = "rmc:procedural:pins"
END = "__end__"

PROMPTS = {  # name -> (version, text)
    "brief": ("brief/3", BRIEF_SYSTEM),
    "policy_qa": ("policy-qa/4", POLICY_QA_SYSTEM),
    "policy_qa_strict": ("policy-qa-strict/4", POLICY_QA_SYSTEM + STRICT_SUFFIX),
    "extract": ("extract/2", EXTRACT_SYSTEM),
    "query_rewrite": ("query-rewrite/2", QUERY_REWRITE_SYSTEM),
}

# The fixed multi-agent graph (plan §3.7, C28). Agent nodes run under their own Entra identity; `code` nodes are
# deterministic (verification and commit are code, not agents); `interrupt` pauses the run for the RM's approval.
# Edges are fixed: a dict edge is chosen by the request's use case, never by model output.
GRAPH_SPEC = {
    "id": "rm-copilot",
    "version": "graph/2.6.0",
    "entry": "supervisor",
    "max_agent_hops": 2,
    "nodes": {
        "supervisor": {"kind": "agent", "agent": "supervisor"},
        "brief-agent": {"kind": "agent", "agent": "brief-agent"},
        "policy-agent": {"kind": "agent", "agent": "policy-agent"},
        "followup-agent": {"kind": "agent", "agent": "followup-agent"},
        "verify": {"kind": "code", "deterministic": True},
        "await_approval": {"kind": "interrupt"},
        "commit": {"kind": "code"},
        "respond": {"kind": "code"},
    },
    "edges": {
        "supervisor": {"uc1": "brief-agent", "uc2": "policy-agent", "uc3": "followup-agent"},
        "brief-agent": "verify",
        "policy-agent": "verify",
        "followup-agent": "verify",
        "verify": {"uc1": "respond", "uc2": "respond", "uc3": "await_approval"},
        "await_approval": "commit",
        "commit": "respond",
        "respond": END,
    },
}


def sha(value: object) -> str:
    return jose.sha256_hex(jose.canonical_json(value))


def prompt_sha(text: str) -> str:
    return jose.sha256_hex(text.encode("utf-8"))


def local_registry(graph_spec: dict | None = None, prompts: dict | None = None, tools: dict | None = None) -> dict:
    """The registry as this build sees it (what the pipeline would publish)."""
    g = graph_spec or GRAPH_SPEC
    return {"graph": {"id": g["id"], "version": g["version"], "sha256": sha(g)},
            "prompts": {n: {"version": v, "sha256": prompt_sha(t)} for n, (v, t) in (prompts or PROMPTS).items()},
            "tools": tool_pins(tools)}


def pins() -> dict:
    return local_registry()
