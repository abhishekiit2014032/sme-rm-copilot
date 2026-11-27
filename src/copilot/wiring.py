"""Diagram box: the whole diagram (lanes A to D), the composition root.
Builds every box and plugs an adapter into each port; demos, tests, the release gate and the API start here.
Mocked: every adapter is a mock from mocks/; COPILOT_LLM=azure selects the untested Azure OpenAI adapter.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

from copilot.contracts.ports import LlmClient
from copilot.contracts.trace import Trace
from copilot.execution.agents.brief import BriefAgent
from copilot.execution.agents.followup import FollowUpAgent
from copilot.execution.agents.policy import PolicyAgent
from copilot.execution.approvals import ApprovalService, CommitOutbox
from copilot.execution.mcp_servers.crm_drafts import CrmDraftsServer
from copilot.execution.mcp_servers.customer_data import CustomerDataServer
from copilot.execution.mcp_servers.memory import MemoryServer
from copilot.execution.mcp_servers.policy_search import PolicySearchServer
from copilot.execution.mcp_servers.product_rules import ProductRulesServer
from copilot.execution.model_gateway import ModelGateway
from copilot.execution.retrieval import ClauseIndex, HybridRetriever
from copilot.execution.tool_gateway import ToolGateway
from copilot.operations.flags import Flags
from copilot.operations.tracing import Tracer
from copilot.request_path.gateway import ApiGateway
from copilot.request_path.input_guardrails import InputGuardrails
from copilot.request_path.orchestrator import Orchestrator
from copilot.request_path.output_guardrails import OutputGuardrails
from copilot.request_path.pipeline import RequestPipeline
from copilot.request_path.router import Router
from copilot.request_path.semantic_cache import SemanticCache
from copilot.state.audit_log import AuditLog
from copilot.state.ingestion import IngestionPipeline
from copilot.state.memory.checkpointer import Checkpointer
from copilot.state.memory.episodic import EpisodicMemory
from copilot.state.memory.procedural import PromptRegistry
from copilot.state.memory.session import SessionMemory
from mocks.data import BUSINESS_DATE, POLICIES_DIR, load_json
from mocks.identity.oidc_mock import MockEntitlementService, MockIdentityProvider
from mocks.llm.scripted_llm import ScriptedLlm
from mocks.systems.case_management import MockCaseManagement
from mocks.systems.core_banking_ods import MockCoreBankingOds
from mocks.systems.crm import MockCrm
from mocks.systems.lending import MockLending
from mocks.systems.trade_finance import MockTradeFinance


@dataclass
class Copilot:
    """Handles on the built system. Demos and tests use the mock handles to inject faults."""

    pipeline: RequestPipeline
    ingestion: IngestionPipeline
    tracer: Tracer
    flags: Flags
    audit: AuditLog
    idp: MockIdentityProvider
    llm: LlmClient
    crm: MockCrm
    core_banking: MockCoreBankingOds
    lending: MockLending
    trade: MockTradeFinance
    cases: MockCaseManagement
    crm_drafts: CrmDraftsServer
    tool_gateway: ToolGateway
    models: ModelGateway
    retriever: HybridRetriever
    cache: SemanticCache

    def token_for(self, user_id: str) -> str:
        """Sign in a fictional user at the mock identity provider."""
        return self.idp.issue_token(user_id)


def select_llm() -> LlmClient:
    if os.environ.get("COPILOT_LLM", "scripted").lower() == "azure":
        from mocks.llm.azure_openai_adapter import AzureOpenAIAdapter  # untested stub
        return AzureOpenAIAdapter()
    return ScriptedLlm()


def build_copilot(flags: Flags | None = None, llm: LlmClient | None = None) -> Copilot:
    dataset = load_json("customers.json")
    flags = flags or Flags.from_env()
    llm = llm or select_llm()
    tracer, audit = Tracer(), AuditLog()

    # Lane C: state
    session, checkpointer = SessionMemory(), Checkpointer()
    episodic = EpisodicMemory(dataset, BUSINESS_DATE)

    # Lane B: gateways, retrieval, systems of record
    models = ModelGateway(llm, PromptRegistry())
    index = ClauseIndex()
    retriever = HybridRetriever(index, models.embed, BUSINESS_DATE)
    cache = SemanticCache(models.embed)
    ingestion = IngestionPipeline(index, models.embed, cache.invalidate_doc)
    ingestion.ingest_directory(POLICIES_DIR, Trace(request_id="startup-ingestion"))

    idp, entitlements = MockIdentityProvider(dataset), MockEntitlementService(dataset)
    crm, core_banking, lending = MockCrm(dataset), MockCoreBankingOds(dataset), MockLending(dataset)
    trade, cases = MockTradeFinance(dataset), MockCaseManagement(dataset)
    crm_drafts = CrmDraftsServer()
    servers = [
        CustomerDataServer(crm, core_banking, lending, trade, cases),
        PolicySearchServer(retriever),
        crm_drafts,
        MemoryServer(episodic),
        ProductRulesServer(load_json("product_rules.json")["rules"]),
    ]
    tools = ToolGateway(servers, idp, entitlements, audit)
    agents = {
        "brief": BriefAgent(tools, models),
        "policy": PolicyAgent(tools, models),
        "followup": FollowUpAgent(tools, models, today=BUSINESS_DATE.isoformat()),
    }

    # Lane A: the online request path
    orchestrator = Orchestrator(agents, models, checkpointer, ApprovalService(), CommitOutbox(crm), episodic)
    pipeline = RequestPipeline(
        gateway=ApiGateway(idp), input_guardrails=InputGuardrails(), cache=cache,
        router=Router(models, flags), orchestrator=orchestrator, output_guardrails=OutputGuardrails(),
        tracer=tracer, flags=flags, session=session, audit=audit,
    )
    return Copilot(pipeline=pipeline, ingestion=ingestion, tracer=tracer, flags=flags, audit=audit, idp=idp,
                   llm=llm, crm=crm, core_banking=core_banking, lending=lending, trade=trade, cases=cases,
                   crm_drafts=crm_drafts, tool_gateway=tools, models=models, retriever=retriever, cache=cache)
