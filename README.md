# SME RM Copilot: assignment submission

An AI assistant for SME relationship managers, built around three use cases: a **pre-meeting brief**, a **policy and product copilot**, and a **post-meeting follow-up agent**.

| Deliverable | Where |
|---|---|
| Presentation (24 slides, with speaker notes) | https://claude.ai/artifact/SQ99QXKTFqqkcG8qZvvGB1 (source in `deck/project/`). Export to PowerPoint or PDF from the deck's Share menu. |
| Clickable prototype | https://claude.ai/artifact/QMmwAsgAQZnoUtVf2iTkWA (source: `prototype/rm-copilot.html`, which also opens offline in any browser) |
| Sample code | `code/`. See `code/README.md`. Python standard library only; `python3 demo.py`, `python3 -m evals.run_evals`, `python3 -m unittest` |

## How the deck maps to the brief

1. **Business problem and use cases**: slides 3–8
2. **Solution architecture** (layers, data, AI components, RAG, agents, security): slides 9–14
3. **Delivery approach** (roadmap, risks, success metrics): slides 15–17
4. **Technical deep dive** (decisions, technology trade-offs, hallucination, evaluation, governance): slides 18–22
5. **Demo and next steps**: slides 23–24

## Assumptions to confirm or replace before presenting

- The bank is not named. Placeholders `[Presenter name]` and `[Client / Bank name]` are on the cover.
- Currency is AED and Arabic support is assumed (a UAE-style context). Change these if the bank is elsewhere.
- All figures marked as a target or illustrative (time split, 4+ hours saved, ~5 FTE, KPI targets) are hypotheses to baseline in discovery, not measured results.
- All customers, RMs and policy clauses in the deck, prototype and code are fictional.
- Vendor and model names are listed as options, not commitments.
