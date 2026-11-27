# 09. Regional deployment

Status: Accepted · Date: 27 Nov 2026

## Context
The Bank's customer data, prompts, model calls and logs must stay in-country. Global model endpoints may process data outside the region.

## Decision
- All data, models, indexes and logs in UAE North.
- Regional (not global or data-zone) model deployments, pinned versions.
- Private network only: hub-spoke VNet, private endpoints, firewall, internal WAF, private AKS; no public path.
- Encryption in transit (TLS 1.2+) and at rest with Bank-managed keys.
- No customer data used for training; modified abuse monitoring requested.

## Trade-offs
| Gain | Cost | How we contain the cost |
|---|---|---|
| Data residency and regulatory fit | Fewer models and lower quota in-region | Model gateway routes across regional deployments |
| No public exposure | More network components to run | Managed Azure network services |
| Bank-held keys | HSM cost | Limited to keys and approval signatures |

## Alternatives considered
- Global endpoints: more capacity and newer models, but processing location not guaranteed.

## Where in code
- `src/copilot/execution/model_gateway.py` (regional deployments only)
- `src/copilot/operations/flags.py` (deployment pins)

## Azure implementation
UAE North: regional Azure OpenAI, Private Endpoints, Azure Firewall, Key Vault Managed HSM; regional model availability and abuse monitoring to confirm with Microsoft.
