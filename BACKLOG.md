# Backlog (agreed with the owner, 26 Nov 2026)

Deadline: Friday 27 Nov 2026. Keep everything fictional and unbranded ("the Bank").

## 1. Data flow diagram: read path (new slide after `arch`)
One request end to end for the pre-meeting brief, with numbered arrows and a numbered legend:
RM (CRM sidebar / Teams) → SSO + API gateway → orchestrator → tool layer (entitlement check first) →
systems of record (CRM, core banking, lending, KYC, news) → masked facts → LLM gateway → private in-region model →
placeholders filled from tool output, numeric check → brief back to RM. The tool layer writes every call to the audit log.
Mark the bank's private network boundary. Slide id: `flow-read`.

## 2. Data flow diagram: write path (new slide after `flow-read`)
Follow-up agent: notes → guardrails (quarantine injected text, mask PII) → model extracts JSON drafts → RM edits →
on approve, the UI backend signs a token (RM + draft hash + 5-minute expiry) → orchestrator commits with the token →
write API verifies signature, user and hash → CRM and tasks. Email stays a draft the RM sends. Slide id: `flow-write`.

## 3. Component architecture diagram
Slide `arch` is a layered stack without arrows. Add or redraw a component view with connectors between components.

## 4. Prototype: "How it works" tab
Add a fourth tab to `prototype/rm-copilot.html` with an architecture diagram that highlights the components the last
request touched (driven by the existing trace steps). Keep it offline-capable, light and dark themes, phone width.

## 5. Implementation detail (optional)
Deployment view (environments, networking, secrets/KMS), tool API contracts, latency and cost budget per use case.

## Rules for the deck source (Slides artifact format)
- Each slide is `deck/project/slides/<id>.html`: exactly one `<section id="<id>">`; last child is an `<aside>` of speaker notes.
- Canvas 1920×1080; slides use `padding:112px 128px 160px` (808 px vertical budget); every font ≥ 24 px.
- Shared styles live in `deck/project/deck.css`; slide order is `order` in `deck/project/deck.json`.
- Pinned diagrams: one `position:relative` host div with at most 24 pinned children; arrows are `<x-connector x1 y1 x2 y2>`.
- After changes, run `cd code && python3 -m unittest && python3 -m evals.run_evals` if code was touched.

## Published links (private to the owner)
- Deck: https://claude.ai/artifact/SQ99QXKTFqqkcG8qZvvGB1
- Prototype: https://claude.ai/artifact/QMmwAsgAQZnoUtVf2iTkWA
If the Artifact tool is available, read the deck first (`action: read`) and publish changed files to the same URL
with `root` = `deck`. If it is not available, commit the source changes and say so.
