"""Agent identities (plan §3.7, C24): one Entra service principal per agent, app roles per tool, no client secrets.

Three identities on every MCP tool call, and the effective permission is their intersection:

1. User (OIDC): the RM's token. The agent exchanges it On-Behalf-Of for a token with audience `api://rm-copilot-mcp`
   that keeps the RM `oid` and delegated `Tools.Read`, and names the agent in an actor claim
   `act = {"sub": <agent SP oid>, "azp": <agent>, "xms_fic_sub": <workload>}`. `Tools.Write` is never granted to an
   agent: commit is not an agent tool.
2. Agent (service principal): the agent's own app token for the same audience, carrying exactly its tools' app roles.
3. Workload (managed identity): the orchestrator pod's workload identity. Its assertion (audience
   `api://AzureADTokenExchange`, subject = the Kubernetes service account) is the ONLY credential an agent identity
   accepts (a federated identity credential). Agent tokens therefore carry `xms_fic_sub`, and the MCP gateway and
   tool-gateway refuse an agent token that does not name the registered workload.

Production: Microsoft Entra Agent ID where available (agent identity blueprint + agent identities), otherwise one app
registration per agent with a federated identity credential trusting the AKS OIDC issuer and the orchestrator's
service account [confirm]. The `xms_fic_*` claim names are this mock's; real tokens expose the federation differently.
"""
from __future__ import annotations

# The specialist agents. The orchestrator node of the graph is deterministic code with no model call; it runs under the
# orchestrator workload identity below (no agent service principal of its own, no data tools), and the intent
# classifier inside the orchestrator service uses that same identity for the model gateway.
AGENTS = ("brief-agent", "policy-agent", "followup-agent")
ORCHESTRATOR_SUBJECT = "system:serviceaccount:rm-copilot:orchestrator"
WORKLOAD_SUBJECTS = {"orchestrator": ORCHESTRATOR_SUBJECT}      # workloads that may obtain a federation assertion
AGENT_WORKLOAD = {a: ORCHESTRATOR_SUBJECT for a in AGENTS}      # federated identity credential registered on each agent
JWT_BEARER_ASSERTION = "urn:ietf:params:oauth:client-assertion-type:jwt-bearer"

# App roles per tool (tool-gateway MCP servers). The orchestrator holds none: it is not an agent identity and has no data
# tools (Entra refuses it a token for the MCP gateway).
AGENT_ROLES: dict[str, tuple[str, ...]] = {
    "brief-agent": ("Tools.CustomerData.Read", "Tools.CustomerData.Profile", "Tools.Memory.Read"),
    "policy-agent": ("Tools.PolicySearch",),
    "followup-agent": ("Tools.CustomerData.Profile", "Tools.CrmDrafts.Write"),
}
# MCP servers each agent's client is configured to connect to (client-side allow-list; the server decides by role).
AGENT_SERVERS: dict[str, tuple[str, ...]] = {
    "brief-agent": ("customer-data", "memory"),
    "policy-agent": ("policy-search",),
    "followup-agent": ("customer-data", "crm-drafts"),
}
AGENT_DELEGATED_SCOPES = frozenset({"Tools.Read"})  # what an agent OBO token may carry for the RM (never Tools.Write)


def aks_issuer(entra_url: str) -> str:
    """Stand-in for the AKS cluster's OIDC issuer URL (the issuer of projected service-account tokens)."""
    return f"{entra_url.rstrip('/')}/aks-oidc/rm-copilot"
