"""Diagram box: Brief agent (lane B).
Builds the pre-meeting customer brief from live tool data; the model writes only wording, with {{placeholders}}.
Mocked: the systems of record and the model; a missing non-critical source is shown as "unavailable".
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from copilot.contracts.context import RunContext
from copilot.contracts.money import format_aed, format_pct
from copilot.contracts.plan import Task
from copilot.contracts.results import AgentResult, SourceRef
from copilot.contracts.tools import ToolResult
from copilot.contracts.trace import Component
from copilot.execution.agents import Agent, fill_placeholders
from copilot.execution.model_gateway import ModelTier

# Non-critical reads. Only the CRM profile is critical; any of these may be missing.
READS = {
    "interactions": "get_interactions",
    "requests": "get_open_requests",
    "accounts": "get_accounts_summary",
    "facilities": "get_facilities",
    "trade": "get_trade_positions",
}
SECTION_LABELS = {"interactions": "interaction history", "requests": "open request data",
                  "accounts": "account data", "facilities": "facility data", "trade": "trade finance data"}


class BriefDraft(BaseModel):
    """Structured output the model must return."""

    summary: str
    talking_points: list[str]


class BriefAgent(Agent):
    name = "brief"
    agent_id = "agent-brief"

    def run(self, task: Task, ctx: RunContext) -> AgentResult:
        with ctx.trace.span(Component.BRIEF_AGENT, "prepare brief") as span:
            customer_id = task.customer_id or ""
            profile = self.call_tool("customer-data", "get_customer_profile", ctx, customer_id=customer_id)
            if not profile.ok:
                span.status = "declined"
                return AgentResult(agent=self.name, status="declined",
                                   text="The CRM profile is unavailable, so no brief can be prepared yet.")
            reads = {key: self.call_tool("customer-data", tool, ctx, customer_id=customer_id)
                     for key, tool in READS.items()}
            episodes = self.call_tool("memory", "recall_episodes", ctx, customer_id=customer_id)
            data = {key: r.data for key, r in reads.items() if r.ok}
            recalled = episodes.data if episodes.ok else []
            facts = build_facts(profile.data, data)
            products = self.call_tool("product-rules", "get_product_suggestions", ctx,
                                      signals=build_signals(data)).data or []
            for i, product in enumerate(products[:2], start=1):
                facts[f"product_{i}"], facts[f"product_{i}_reason"] = product["product"], product["reason"]
            unavailable = [key for key, r in reads.items() if not r.ok]

            draft = self._models.generate(ModelTier.PRIMARY, "brief.summarise",
                                          {"fact_keys": sorted(facts), "unavailable": unavailable}, BriefDraft, ctx)
            text = render_brief(profile.data, data, facts, draft, recalled, products, unavailable)
            span.status = "degraded" if unavailable else "ok"
            span.detail = f"{len(facts)} facts filled from tools" + (
                f"; unavailable: {', '.join(unavailable)}" if unavailable else "")
            return AgentResult(
                agent=self.name, status="degraded" if unavailable else "ok", text=text,
                evidence=[str(profile.data), str(data), str(products)] + list(facts.values()),
                sources=_sources(profile, reads, episodes),
                notices=[f"{SECTION_LABELS[k].capitalize()} unavailable" for k in unavailable],
            )


# ---- facts and signals: every number comes from here, not from the model -------------------------
def _working_capital(data: dict[str, Any]) -> dict[str, Any] | None:
    facilities = data.get("facilities", {}).get("facilities", [])
    return next((f for f in facilities if f["type"] == "Working capital limit"), None)


def build_facts(profile: dict[str, Any], data: dict[str, Any]) -> dict[str, str]:
    meeting = profile.get("next_meeting", {})
    facts = {"customer_name": profile["name"], "meeting_date": meeting.get("date", "not scheduled"),
             "meeting_purpose": meeting.get("purpose", "not set")}
    wc = _working_capital(data)
    if wc:
        facts["wc_limit"] = format_aed(wc["limit_aed"])
        facts["wc_utilisation_pct"] = format_pct(round(100 * wc["utilised_aed"] / wc["limit_aed"], 1))
    requested = data.get("facilities", {}).get("requested_limit_aed")
    if requested:
        facts["requested_limit"] = format_aed(requested)
    complaints = data.get("requests", {}).get("complaints", [])
    if complaints:
        facts["open_complaint"] = f"{complaints[0]['id']}: {complaints[0]['summary']}"
    return facts


def build_signals(data: dict[str, Any]) -> dict[str, float]:
    signals: dict[str, float] = {}
    wc = _working_capital(data)
    if wc:
        signals["working_capital_utilisation_pct"] = round(100 * wc["utilised_aed"] / wc["limit_aed"], 1)
    if "trade" in data:
        signals["import_volume_12m_aed"] = data["trade"].get("import_volume_12m_aed", 0)
    if "accounts" in data:
        signals["idle_balance_aed"] = sum(a["balance_aed"] for a in data["accounts"]["accounts"])
    return signals


# ---- rendering: plain text with "### " headings and "- " bullets ----------------------------------
def render_brief(profile: dict[str, Any], data: dict[str, Any], facts: dict[str, str], draft: BriefDraft,
                 episodes: list[dict[str, Any]], products: list[dict[str, Any]], unavailable: list[str]) -> str:
    lines = [f"**{profile['name']}** · {profile['industry']} · risk rating {profile['risk_rating']}",
             "", "### Summary", fill_placeholders(draft.summary, facts)]
    lines += _section("Facilities", "facilities", unavailable, [
        f"{f['type']} {f['id']}: {format_aed(f['limit_aed'])}, {format_aed(f['utilised_aed'])} used, "
        f"{'secured' if f['secured'] else 'unsecured'}, expires {f['expiry']}"
        for f in data.get("facilities", {}).get("facilities", [])])
    lines += _section("Accounts", "accounts", unavailable, [
        f"{a['account']}: {format_aed(a['balance_aed'])}" for a in data.get("accounts", {}).get("accounts", [])]
        + ([f"Average monthly credits: {format_aed(data['accounts']['avg_monthly_credits_aed'])}"]
           if "accounts" in data else []))
    trade = data.get("trade", {})
    lines += _section("Trade finance", "trade", unavailable, [
        f"{t['ref']} {t['type']}: {format_aed(t['amount_aed'])}, {t['status']}"
        for t in trade.get("letters_of_credit", []) + trade.get("guarantees", [])])
    requests = data.get("requests", {})
    lines += _section("Open requests and pending items", "requests", unavailable, [
        f"{r['id']} {r['type']}: {r['detail']} ({r['status']}; waiting on {r['waiting_on']})"
        for r in requests.get("requests", [])])
    lines += _section("Complaints and service requests", "requests", unavailable, [
        f"{c['id']}: {c['summary']} ({c['status']}" + (f", due {c['sla_due']})" if c.get("sla_due") else ")")
        for c in requests.get("complaints", []) + requests.get("service_requests", [])])
    lines += _section("Recent interactions", "interactions", unavailable, [
        f"{i['date']} {i['channel']}: {i['summary']}" for i in data.get("interactions", [])[:3]])
    lines += ["", "### Last approved meeting summary"] + (
        [f"- {e['date']}: {e['summary']}" for e in episodes[:1]] or ["- None on file"])
    lines += ["", "### Suggested products"] + (
        [f"- {p['product']}: {p['reason']}" for p in products] or ["- No rule matched"])
    lines += ["", "### Talking points"] + [f"- {fill_placeholders(p, facts)}" for p in draft.talking_points]
    return "\n".join(lines)


def _section(title: str, key: str, unavailable: list[str], items: list[str]) -> list[str]:
    if key in unavailable:
        body = [f"- {SECTION_LABELS[key].capitalize()} unavailable (source did not respond); "
                "check the source system."]
    else:
        body = [f"- {item}" for item in items] or ["- None"]
    return ["", f"### {title}"] + body


def _sources(profile: ToolResult, reads: dict[str, ToolResult], episodes: ToolResult) -> list[SourceRef]:
    sources = [SourceRef(system=profile.source_system, detail="customer profile", as_of=profile.as_of)]
    for key, result in reads.items():
        sources.append(SourceRef(system=result.source_system, detail=key, as_of=result.as_of or "unavailable",
                                 available=result.ok))
    sources.append(SourceRef(system=episodes.source_system, detail="approved meeting summaries",
                             as_of=episodes.as_of))
    return sources
