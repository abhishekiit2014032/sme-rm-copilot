# 02. MCP tool gateway

Status: Accepted · Date: 27 Nov 2026

## Context
Agents read customer data from CRM, core banking, lending, trade and case systems. An RM may see only their own portfolio, and an agent may use only the tools its task needs. Prompt injection or a planning error must not widen access.

## Decision
- The MCP tool gateway is the only path from an agent to a system of record.
- Every call checks four things: OIDC (the RM's identity, On-Behalf-Of token), RBAC (role and customer entitlement), the agent's identity, and the workload identity.
- Each source sits behind an MCP server with typed tools; each agent sees only its own allow-listed tools.
- No write tool is exposed to any agent; writes go through approval (see 05).
- Source systems re-check entitlement on the RM's token; every call is audited without payload content.

## Trade-offs
| Gain | Cost | How we contain the cost |
|---|---|---|
| Granular, per-call access control | Extra latency per tool call | Reads from replicas; timeouts; parallel waves |
| Single choke point for audit, quotas and breakers | Gateway becomes critical path | Autoscaling, circuit breakers, missing source shows "unavailable" |
| Agents cannot call what they cannot see | More MCP servers and schemas to maintain | Typed tools versioned under procedural memory |

## Alternatives considered
- Direct API calls from agents: faster to build, but access control spread across agents and impossible to audit consistently.

## Where in code
- `src/copilot/execution/tool_gateway.py`
- `src/copilot/execution/mcp_servers/` (customer-data, policy-search, crm-drafts, memory, product-rules)
- `src/copilot/state/audit_log.py`

## Azure implementation
Entra ID (OIDC, OBO, app roles, service principal per agent) with AKS workload identity, enforced by the APIM MCP gateway (validate-jwt, tool filtering, entitlement policy).
