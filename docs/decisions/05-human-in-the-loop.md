# 05. Human in the loop

Status: Accepted · Date: 27 Nov 2026

## Context
Some actions change records or commit the Bank: CRM writes, credit and pricing positions, escalations. Target: zero unapproved writes. Autonomy is limited on purpose; the copilot assists, people decide.

## Decision
- Approval required on critical paths: writes, credit, pricing and escalations.
- The RM approves with a signed, single-use approval token that expires in 5 minutes.
- A write is never an agent action: after approval, code verifies the token and commits through a retry-safe outbox with an idempotency key.
- The checkpointer saves state, the flow pauses for approval and resumes from the same point.
- Weak evidence on a policy question escalates to the credit desk; every approval is written to the audit log.

## Trade-offs
| Gain | Cost | How we contain the cost |
|---|---|---|
| No unapproved or replayed writes | Extra RM click per write | Agent prepares the draft; the RM only reviews |
| No lost or duplicate CRM writes | Eventual consistency on CRM | Approval recorded at once; outbox retries the commit |
| Clear accountability | Expired tokens need re-approval | 5 minutes covers a normal review; resume from checkpoint |

## Alternatives considered
- Autonomous actions: lower friction, but unacceptable for credit, pricing and customer records.

## Where in code
- `src/copilot/execution/approvals.py` (token issue and verify, outbox)
- `src/copilot/state/memory/` (checkpointer), `src/copilot/state/audit_log.py`

## Azure implementation
Key Vault Managed HSM signs approval tokens; Cosmos DB holds the outbox; immutable Blob Storage (WORM) holds the audit trail.
