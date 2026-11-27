"""Builds hld-generic.html: the complete four-lane architecture with cloud-specific product names replaced by
generic component names (cloud-agnostic HLD). Source of truth stays complete-architecture.html."""
import pathlib
here = pathlib.Path(__file__).parent
src = (here / 'complete-architecture.html').read_text(encoding='utf-8')
SUBS = [
    ('AZURE UAE NORTH · FICTIONAL BANK · ARCHITECTURE-REVIEW VIEW', 'CLOUD-AGNOSTIC HLD · FICTIONAL BANK · ARCHITECTURE-REVIEW VIEW'),
    ('SME RM Copilot · complete architecture on Azure', 'SME RM Copilot · high-level architecture'),
    ("'CRM sidebar · Teams tab'", "'CRM sidebar · chat app'"),
    ("'App Gateway WAF · Entra ID SSO, OBO'", "'WAF · SSO (OIDC) · user token passed on'"),
    ("'APIM per RM and per use case'", "'per RM and per use case'"),
    ("'Prompt Shields · PII masking · quote quarantine'", "'injection checks · PII masking · quote quarantine'"),
    ("title: 'Bank APIs via APIM', lines: ['CRM · core ODS · LOS · KYC, RM identity enforced']",
     "title: 'Systems of record', lines: ['CRM · core · lending · trade · cases']"),
    ("'APIM · small or large model'", "'small or large model · token budgets'"),
    ("'Azure OpenAI primary / secondary', lines: ['Regional, UAE North; degraded mode if both fail']",
     "'Primary LLM / fallback LLM', lines: ['in-region · degraded mode if both fail']"),
    ("'RM approves every write · Key Vault signs · declines go to the credit desk'",
     "'RM approves every write · signed approval token · weak evidence to credit desk'"),
    ("'UC1 customer brief · gen-large'", "'customer brief · large model'"),
    ("'UC2 cited answers · gen-small / large'", "'policy + product Q&A · cited'"),
    ("'UC3 notes to drafts · gen-small'", "'follow-up actions · small model'"),
    ("title: 'MCP gateway (APIM)'", "title: 'Tool gateway (MCP)'"),
    ("'AI Search hybrid + rerank'", "'hybrid search + rerank'"),
    ("'RM (OIDC/OBO) ∩ agent service principal\\n∩ pod managed identity'", "'RM identity (OIDC) ∩ agent identity\\n∩ workload identity'"),
    ("'policy DMS publish → parse", "'policy library publish → parse"),
    ("'Cosmos DB, encrypted · per RM, never across RMs'", "'encrypted · per RM, never across RMs'"),
    ("'Key Vault HSM · immutable audit (WORM) · encrypted content'", "'HSM key store · write-once audit log · encrypted content'"),
    ("'AKS 3 zones · KEDA · circuit breakers · outbox queue'", "'3 zones · autoscaling · circuit breakers · outbox queue'"),
]
for a, b in SUBS:
    n = src.count(a)
    assert n >= 1, a
    src = src.replace(a, b)
(here / 'hld-generic.html').write_text(src, encoding='utf-8')
print('ok')
