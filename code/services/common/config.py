"""Stack configuration: one map from each logical dependency to a base URL, plus tunables.

Switching a dependency to real Azure is a configuration edit: set `RMC_URL_<NAME>` (for example
`RMC_URL_SEARCH=https://<search>.search.windows.net`) and `stack.py` will not start that mock. Names, real
endpoints and token audiences are listed in `DEPENDENCIES`; README.md has the full table and the caveats.
Every tunable below can be overridden with an environment variable of the same name in upper case, prefixed
`RMC_` (for example `RMC_APIM_TOKENS_PER_MINUTE=50`).
"""
from __future__ import annotations

import os
import tempfile
import uuid
from dataclasses import dataclass, field, fields
from pathlib import Path

# logical name -> (real Azure endpoint shape, token audience / scope resource, mock that stands in for it)
DEPENDENCIES: dict[str, tuple[str, str, str]] = {
    "entra": ("https://login.microsoftonline.com", "-", "azmock.entra"),
    "keyvault": ("https://<vault>.vault.azure.net", "https://vault.azure.net", "azmock.keyvault"),
    "appconfig": ("https://<store>.azconfig.io", "https://azconfig.io", "azmock.appconfig"),
    "cosmos": ("https://<account>.documents.azure.com", "https://cosmos.azure.com", "azmock.cosmos"),
    "eventhubs": ("https://<namespace>.servicebus.windows.net", "https://eventhubs.azure.net", "azmock.eventhubs"),
    "aoai-primary": ("https://<foundry-primary>.openai.azure.com", "https://cognitiveservices.azure.com", "azmock.aoai"),
    "aoai-secondary": ("https://<foundry-secondary>.openai.azure.com", "https://cognitiveservices.azure.com", "azmock.aoai"),
    "apim": ("https://<apim>.azure-api.net", "api://rm-copilot-apim", "azmock.apim"),
    "content-safety": ("https://<contentsafety>.cognitiveservices.azure.com", "https://cognitiveservices.azure.com", "azmock.content_safety"),
    "language": ("https://<language>.cognitiveservices.azure.com", "https://cognitiveservices.azure.com", "azmock.language"),
    "search": ("https://<search>.search.windows.net", "https://search.azure.com", "azmock.search"),
    "crm": ("Bank CRM API (behind APIM)", "-", "azmock.systems"),
    "ods": ("Core banking ODS API (behind APIM)", "-", "azmock.systems"),
    "los": ("Lending/trade finance LOS API (behind APIM)", "-", "azmock.systems"),
    "kyc": ("KYC/CDD API (behind APIM)", "-", "azmock.systems"),
    "news": ("Licensed news feed (behind APIM, firewall egress)", "-", "azmock.systems"),
    "graph": ("https://graph.microsoft.com", "https://graph.microsoft.com", "azmock.systems"),
}
SERVICES = ("audit-writer", "tool-gateway", "orchestrator", "web-bff", "outbox-worker")
SOR_SYSTEMS = ("crm", "ods", "los", "kyc", "news")

# Audiences (app ID URIs) of our own APIs and of the Azure resources we call.
AUD = {
    "web-bff": "api://rm-copilot-web-bff",
    "orchestrator": "api://rm-copilot-orchestrator",
    "tool-gateway": "api://rm-copilot-tool-gateway",
    "apim": "api://rm-copilot-apim",
    "audit": "api://rm-copilot-audit",
    "cognitive": "https://cognitiveservices.azure.com",
    "search": "https://search.azure.com",
    "vault": "https://vault.azure.net",
    "cosmos": "https://cosmos.azure.com",
    "eventhubs": "https://eventhubs.azure.net",
    "appconfig": "https://azconfig.io",
    "graph": "https://graph.microsoft.com",
}


def scope(aud_key: str, name: str = ".default") -> str:
    return f"{AUD[aud_key]}/{name}"


@dataclass
class Settings:
    tenant_id: str = "8d7c6b5a-1f2e-4d3c-9b8a-00000000c0de"  # fictional tenant
    latency: str = "none"                 # none | realistic (mock service latencies)
    dev_password: str = "rm-copilot-demo"  # mock sign-in only (ROPC); production: MSAL / Teams SSO with MFA
    as_of: str = "2026-11-26"             # the Bank's "today" for effective-date filters (fixed for reproducibility)
    index_name: str = "policies-2026-11-26"
    # APIM AI gateway policies (per RM)
    apim_tokens_per_minute: int = 40000
    apim_token_quota_per_day: int = 400000
    apim_calls_per_minute: int = 120
    apim_breaker_trip_s: float = 10.0
    apim_min_failover_s: float = 4.0      # only fail over to the second backend if this much deadline remains
    sor_breaker_failures: int = 5
    sor_breaker_window_s: float = 30.0
    sor_breaker_trip_s: float = 10.0
    # deadlines set at web-bff (plan §5)
    deadline_uc1_s: float = 8.0
    deadline_uc2_s: float = 7.0
    deadline_uc3_s: float = 10.0
    deadline_commit_s: float = 2.0
    tool_timeout_s: float = 2.0
    entitlement_ttl_s: float = 60.0
    portfolio_poll_s: float = 0.25        # tool-gateway consumer of Event Hub `crm-portfolio-events` (cache invalidation)
    flags_ttl_s: float = 5.0
    writes_flag_max_stale_s: float = 900.0  # LLD §4.4 step 3: `writes` must be fresh within 15 min, else writes fail closed
    step_up_max_age_s: int = 600
    approval_ttl_s: int = 300
    approval_skew_s: int = 30
    outbox_poll_s: float = 0.25
    outbox_backoff_s: float = 0.5
    outbox_max_attempts: int = 12
    audit_poll_s: float = 0.2
    run_dir: str = ""

    @classmethod
    def from_env(cls, **overrides) -> "Settings":
        s = cls()
        for f in fields(cls):
            raw = os.environ.get("RMC_" + f.name.upper())
            if raw is not None:
                setattr(s, f.name, type(getattr(s, f.name))(raw))
        for k, v in overrides.items():
            setattr(s, k, v)
        if not s.run_dir:
            s.run_dir = tempfile.mkdtemp(prefix="rmc-run-")
        Path(s.run_dir).mkdir(parents=True, exist_ok=True)
        return s


@dataclass
class StackConfig:
    settings: Settings
    urls: dict[str, str] = field(default_factory=dict)
    secrets: dict[str, str] = field(default_factory=dict)  # client_id -> client secret (in memory only)

    def url(self, name: str) -> str:
        return self.urls[name].rstrip("/")

    @property
    def issuer(self) -> str:
        return f"{self.url('entra')}/{self.settings.tenant_id}/v2.0"

    @property
    def token_url(self) -> str:
        return f"{self.url('entra')}/{self.settings.tenant_id}/oauth2/v2.0/token"

    @property
    def jwks_url(self) -> str:
        return f"{self.url('entra')}/{self.settings.tenant_id}/discovery/v2.0/keys"


def env_url_overrides() -> dict[str, str]:
    """`RMC_URL_<NAME>` overrides, e.g. RMC_URL_AOAI_PRIMARY or RMC_URL_CONTENT_SAFETY."""
    out = {}
    for name in [*DEPENDENCIES, *SERVICES]:
        v = os.environ.get("RMC_URL_" + name.upper().replace("-", "_"))
        if v:
            out[name] = v.rstrip("/")
    return out


def user_oid(upn: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, "rm-copilot-user:" + upn))
