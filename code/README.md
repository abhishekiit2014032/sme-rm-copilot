# RM Copilot: sample code

A small, runnable reference for the controls described in the deck. Python 3.10+, standard library only.
It uses a deterministic mock model, so it runs offline with no keys. All data is fictional.

```bash
cd code
python3 demo.py                    # walk through UC1, UC2, UC3 and the controls
python3 -m evals.run_evals         # release gate on the golden set (passes)
python3 -m evals.run_evals --careless   # same gate with a hallucinating model (fails, exit 1)
python3 -m unittest -v             # 9 tests for controls that must never regress
```

## What each file shows

| File | Deck slide | What it demonstrates |
|---|---|---|
| `rm_copilot/tools.py` | Agents, Security | Typed tool catalogue; entitlements enforced in code per call; field minimisation (national ID never leaves the tool); write tools that need a signed, draft-bound, expiring RM approval token; audit log |
| `rm_copilot/retrieval.py` | RAG | Filter first (current version, user clearance), then BM25 recall, then a reranking step. Production swaps in hybrid vector + keyword search and a cross-encoder |
| `rm_copilot/llm.py` | AI components | One `LLMClient` interface (the LLM gateway); the real system prompts; `MockLLM` for reproducible runs; `CarelessLLM` to prove the checks work; `GatewayLLM` skeleton |
| `rm_copilot/guardrails.py` | Hallucination, Security | Prompt-injection detection and quote quarantine; PII masking; claim-by-claim groundedness with number matching; "no free numbers" check |
| `rm_copilot/orchestrator.py` | Architecture | UC1 as a fixed workflow where the model writes `{placeholders}` and the system inserts figures; UC2 with decline threshold, verify, retry once, then decline; UC3 drafts with deterministic date resolution and approval-gated commit |
| `evals/` | Evaluation | Golden set (QA, access, restricted, superseded, injection cases) and a release gate with thresholds |
| `tests/` | Governance | Unit tests for the non-negotiable controls |

## Swapping in a real model

Set `LLM_GATEWAY_URL` and `LLM_GATEWAY_TOKEN`, then construct `Copilot(GatewayLLM())`. The gateway owns model
routing, PII masking, logging and quotas. Run `python3 -m evals.run_evals` against it before any release.
