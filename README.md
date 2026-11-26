# SME RM Copilot: assignment submission

An AI assistant for SME relationship managers, built around three use cases: a **pre-meeting brief**, a **policy and product copilot**, and a **post-meeting follow-up agent**.

| Deliverable | Where |
|---|---|
| Presentation (29 slides, with speaker notes) | https://claude.ai/artifact/SQ99QXKTFqqkcG8qZvvGB1 (source in `deck/project/`). Export to PowerPoint or PDF from the deck's Share menu. |
| Clickable prototype | https://claude.ai/artifact/QMmwAsgAQZnoUtVf2iTkWA (source: `prototype/rm-copilot.html`, which also opens offline in any browser). The "How it works" tab shows the Azure architecture and lights up what the last request touched |
| Azure architecture pack | `docs/architecture/`: plan and baseline (`00-plan.md`), HLD (`01`), LLD (`02`), 22 ADRs with trade-offs (`adr/`), interfaces and SLAs (`04`), NFRs, failure modes, DR and model risk (`05`), deployment and operations (`06`), ARB review log and verdict (`07`), step-by-step AI system design walkthrough with trade-offs (`08`); one-page diagrams in `diagrams/` (Azure target architecture, current application) |
| Working prototype on mock Azure | `code/`. See `code/README.md`. Python standard library only. `python3 stack.py` starts 7 services over HTTP against mock Azure APIs (Entra ID, APIM, Azure OpenAI, Content Safety, AI Language, AI Search, Key Vault, App Configuration, Cosmos DB, Event Hubs) with a console; `python3 -m unittest`, `python3 -m evals.run_evals`, `python3 loadtest.py`, `python3 demo.py` |

## How the deck maps to the brief

1. **Business problem and use cases**: slides 3 to 8
2. **Solution architecture** (layers, read and write data flows, component map, Azure deployment and network, data, AI components, RAG, agents, security): slides 9 to 18
3. **Delivery approach** (roadmap, risks, success metrics): slides 19 to 21
4. **Technical deep dive** (decisions, technology trade-offs on Azure, interface agreements and SLOs, hallucination, evaluation, governance): slides 22 to 27
5. **Demo and next steps**: slides 28 and 29

## Assumptions to confirm or replace before presenting

- The bank is not named. Placeholders `[Presenter name]` and `[Client / Bank name]` are on the cover.
- Currency is AED and Arabic support is assumed (a UAE-style context). Change these if the bank is elsewhere.
- All figures marked as a target or illustrative (time split, 4+ hours saved, ~5 FTE, KPI targets) are hypotheses to baseline in discovery, not measured results.
- All customers, RMs and policy clauses in the deck, prototype and code are fictional.
- Microsoft Azure (UAE North) is the committed platform and the Azure services named in the deck are the design baseline (`docs/architecture/00-plan.md`). Exact model versions, regional availability and quotas are still to be confirmed with Microsoft.
