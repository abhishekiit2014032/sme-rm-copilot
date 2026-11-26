"""Evaluation architecture (plan §3.9, LLD §17, §11.8): runtime gates, RAGAS-style metrics, the online sampled
evaluation, the scheduled and release gates, and the runtime operations metrics.

In-process tests first (formulas, gates, release gate, nightly and drift reports), then the stack over HTTP.
"""
from __future__ import annotations

import contextlib
import io
import json
import re
import shutil
import tempfile
import time
import unittest
from pathlib import Path

from evals import run_evals, scheduled
from rm_copilot import data, ragas
from rm_copilot import tools as t
from rm_copilot.gates import RUNTIME_THRESHOLDS, retrieval_gate, thresholds
from rm_copilot.llm import POLICY_QA_SYSTEM, CarelessLLM, MockLLM
from rm_copilot.orchestrator import Copilot
from services.common import opsmetrics
from services.common.config import user_oid

from .stackutil import LAYLA, OMAR, Workload, client, reset, start_stack, stop_stack, wait_for

LLD = {**RUNTIME_THRESHOLDS, "candidate_floor": 1.0, "top_min": 2.0, "keep_min": 2.0, "keep_margin": 1.0}  # calibrated 0-4 scale
P = {p["id"]: p["text"] for p in data.POLICY_PASSAGES}
CAREFUL_Q = "Can a company trading for 2 years get an unsecured working capital line?"
WEAK_Q = "What is the interest rate on retail savings accounts?"


def hits(*scores: float) -> list[dict]:
    return [{"id": f"p{i}", "score": s, "text": "x"} for i, s in enumerate(scores)]


class CountingLLM:
    def __init__(self, inner):
        self.inner, self.calls = inner, []

    def complete(self, system: str, user: str) -> str:
        self.calls.append(system)
        return self.inner.complete(system, user)


class OffTopicLLM(MockLLM):
    """Grounded and cited, but always quotes the first sentence, even on the strict retry: never answers the question."""

    def complete(self, system: str, user: str) -> str:
        if system.startswith(POLICY_QA_SYSTEM[:40]):
            return self._qa(user, strict=False)
        return super().complete(system, user)


# --------------------------------------------------------------------------- formulas and gates (in-process)
class Formulas(unittest.TestCase):
    def test_retrieval_gate_follows_the_lld_examples(self):
        self.assertEqual(retrieval_gate(hits(3.1, 2.6), LLD)["context_relevance"], 1.0)
        g = retrieval_gate(hits(2.2, 1.9, 1.6, 1.4, 1.2), LLD)
        # Plan v2.7b: low context relevance is flagged, not declined; only the kept passage goes to the model.
        self.assertEqual((g["passed"], g["context_relevance"], g["sub_reason"]), (True, 0.2, "low_context_relevance"))
        self.assertEqual([h["score"] for h in g["kept"]], [2.2])
        self.assertEqual(retrieval_gate(hits(1.7, 1.5), LLD)["sub_reason"], "top_below_threshold")
        self.assertEqual(retrieval_gate(hits(3.0, 0.9), LLD)["candidates"], [hits(3.0)[0]])  # below the candidate floor

    def test_ragas_metrics(self):
        ctx = [P["doa-14"], P["covenant-6.1"]]
        good = "Policy exceptions for unsecured SME lending up to AED 1m require approval at Credit Risk level 2 [1]."
        bad = good + " Pricing starts at 2.1% over the benchmark [1]."
        self.assertEqual(ragas.faithfulness(good, ctx)["score"], 1.0)
        f = ragas.faithfulness(bad, ctx)
        self.assertEqual((f["score"], f["citation_accuracy"], f["cited_all"]), (0.5, 0.5, True))
        self.assertFalse(ragas.faithfulness("Unsecured lines need 1 year [1]. No citation here.", ctx)["cited_all"])
        ref = P["doa-14"]
        self.assertEqual(ragas.context_precision("q", ctx, ref), 1.0)          # the relevant chunk is ranked first
        self.assertEqual(ragas.context_precision("q", ctx[::-1], ref), 0.5)    # ... second: precision@2 = 1/2
        self.assertEqual(ragas.context_recall(ref, ctx), 1.0)
        self.assertEqual(ragas.context_recall(ref, ctx[1:]), 0.0)
        self.assertGreater(ragas.answer_correctness(good, ref), 0.85)
        self.assertLess(ragas.answer_correctness(P["covenant-6.1"], ref), 0.3)
        self.assertEqual(ragas.answer_relevancy("Who approves exceptions for unsecured lending?", "INSUFFICIENT_EVIDENCE"), 0.0)
        self.assertEqual(ragas.context_utilisation(good, ctx), 1.0)

    def test_wilson_lower_bound_matches_the_plan(self):
        self.assertGreaterEqual(ragas.wilson_lower_bound(293, 300), 0.95)   # plan §5: 293/300 for a 0.95 bound
        self.assertLess(ragas.wilson_lower_bound(292, 300), 0.95)
        self.assertGreaterEqual(ragas.wilson_lower_bound(59, 60), 0.90)     # LLD §17.1: 59 of 60 for 0.90
        self.assertEqual(ragas.wilson_lower_bound(0, 0), 0.0)

    def test_mock_judge_agrees_with_the_lexical_scorer_and_falls_back(self):
        sample = {"question": CAREFUL_Q, "answer": "Unsecured SME working capital lines require a minimum of 3 years audited trading history [1]. "
                  "Pricing starts at 2.1% [1].", "contexts": [P["credit-5.3.1"]]}
        judge = ragas.LLMJudgeScorer(MockLLM())
        self.assertEqual(ragas.evaluate(sample, judge)["faithfulness"], ragas.evaluate(sample)["faithfulness"])
        self.assertEqual(judge.fallbacks, 0)
        self.assertGreater(judge.calls, 0)

        class Down:
            def complete(self, system, user):
                raise RuntimeError("judge unavailable")
        broken = ragas.LLMJudgeScorer(Down())
        self.assertEqual(ragas.evaluate(sample, broken)["faithfulness"], 0.5)
        self.assertGreater(broken.fallbacks, 0)

    def test_thresholds_merge_by_key_and_reject_out_of_range(self):
        rejected: list[str] = []
        th = thresholds({"rmc:orchestrator:gate:answer_relevancy:min": 0.8, "rmc:orchestrator:gate:retrieval:top_min": 99,
                         "rmc:orchestrator:gate:version": "gates/x"}, rejected)
        self.assertEqual((th["answer_relevancy_min"], th["top_min"], th["version"]), (0.8, RUNTIME_THRESHOLDS["top_min"], "gates/x"))
        self.assertEqual(rejected, ["top_min"])

    def test_histogram_quantile(self):
        b = [(0.1, 10), (0.5, 90), (1.0, 100), (float("inf"), 100)]
        self.assertAlmostEqual(opsmetrics.histogram_quantile(0.5, b), 0.3)
        self.assertAlmostEqual(opsmetrics.histogram_quantile(0.95, b), 0.75)


class RuntimeGates(unittest.TestCase):
    def setUp(self):
        self.layla = t.User.load("layla")

    def test_retrieval_gate_declines_without_calling_the_generation_model(self):
        llm = CountingLLM(MockLLM())
        r = Copilot(llm).ask(self.layla, WEAK_Q)
        self.assertEqual((r["status"], r["reason_code"]), ("declined", "WEAK_RETRIEVAL"))
        self.assertFalse(any(s.startswith(POLICY_QA_SYSTEM[:40]) for s in llm.calls))  # only the query rewrite ran
        self.assertEqual(r["evaluation"]["gates"][-1]["gate"], "retrieval")

    def test_faithfulness_gate_retries_once_with_time_and_declines_without(self):
        llm = CountingLLM(CarelessLLM())
        r = Copilot(llm).ask(self.layla, CAREFUL_Q)
        outcomes = [(g["gate"], g["outcome"]) for g in r["evaluation"]["gates"]]
        self.assertEqual(outcomes, [("retrieval", "passed"), ("faithfulness", "retried"), ("faithfulness", "declined")])
        self.assertEqual((r["status"], r["reason_code"]), ("declined", "FAILED_GROUNDEDNESS"))
        self.assertEqual(sum(s.startswith(POLICY_QA_SYSTEM[:40]) for s in llm.calls), 2)
        llm = CountingLLM(CarelessLLM())
        r = Copilot(llm, time_left=lambda: 3.9).ask(self.layla, CAREFUL_Q)  # < 4 s left: no retry (plan §4.3 step 10)
        self.assertEqual(r["reason_code"], "NO_TIME_FOR_RETRY")
        self.assertEqual(sum(s.startswith(POLICY_QA_SYSTEM[:40]) for s in llm.calls), 1)

    def test_answer_relevancy_gate(self):
        q = "Does an expired trade licence block new drawdowns?"
        r = Copilot(OffTopicLLM()).ask(self.layla, q)  # grounded but off-topic twice
        self.assertEqual((r["status"], r["reason_code"], r["failed_gate"]), ("declined", "LOW_ANSWER_RELEVANCY", "answer_relevancy"))
        r = Copilot(MockLLM()).ask(self.layla, q)       # off-topic first, the strict retry quotes the right sentence
        self.assertEqual(r["status"], "answered")
        self.assertEqual([g["outcome"] for g in r["evaluation"]["gates"] if g["gate"] == "answer_relevancy"], ["retried", "passed"])
        self.assertGreaterEqual(r["evaluation"]["scores"]["answer_relevancy"], 0.7)

    def test_uc1_profile_gate_and_line_checks(self):
        b = Copilot(CarelessLLM()).brief(self.layla, "harbourline")
        gates = {g["gate"]: g["outcome"] for g in b["evaluation"]["gates"]}
        self.assertEqual(gates["profile_present"], "passed")
        self.assertEqual(gates["numeric"], "dropped")  # "Utilisation is 97%" was dropped, the placeholder line kept


class ReleaseAndScheduled(unittest.TestCase):
    def test_release_gate_passes_and_careless_fails(self):
        good = run_evals.run(MockLLM())
        self.assertTrue(all(run_evals.verdicts(good).values()), run_evals.verdicts(good))
        self.assertGreaterEqual(good["metrics"]["faithfulness"]["n"], 73)  # enough answers for a 0.95 Wilson bound
        bad = run_evals.verdicts(run_evals.run(CarelessLLM()))
        self.assertFalse(all(bad.values()))
        self.assertFalse(bad["faithfulness"])
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(run_evals.main(["--careless"]), 1)

    def test_nightly_report_contains_every_metric(self):
        out = Path(tempfile.mkdtemp(prefix="rmc-nightly-"))
        try:
            report, jpath, mpath = scheduled.nightly(out_dir=out)
            j = json.loads(jpath.read_text())
            for k in ("faithfulness", "answer_relevancy", "context_precision", "context_recall", "answer_correctness", "citation_accuracy",
                      "correct_decline", "entitlement_leaks", "numeric_mismatches", "injection_successes", "latency_p95_uc2_s", "tokens_in_p95"):
                self.assertIn(k, j["metrics"])
                self.assertIn(k, mpath.read_text())
            self.assertIsNotNone(j["info"]["context_utilisation"])
            self.assertTrue(j["passed"])
            for c in data.CUSTOMERS.values():  # metadata only
                self.assertNotIn(c["name"], jpath.read_text())
            self.assertNotIn(CAREFUL_Q, jpath.read_text() + mpath.read_text())
        finally:
            shutil.rmtree(out, ignore_errors=True)

    def test_drift_report_flags_shifts(self):
        ops = Path(tempfile.mkdtemp(prefix="rmc-ops-"))
        try:
            (ops / "snapshots").mkdir()

            def snap(day, decline, p95, tokens):
                s = {"generated_at": 0, "retrieval_decline_rate": decline, "faithfulness_gate_failure_rate": 0.01,
                     "latency_s": {"uc2": {"p95": p95}}, "tokens_per_request": {"uc2": {"input": {"p95": tokens}}}}
                (ops / "snapshots" / f"{day}.jsonl").write_text(json.dumps(s) + "\n")
            snap("2026-11-23", 0.10, 4.0, 500)
            snap("2026-11-24", 0.12, 4.2, 520)
            snap("2026-11-25", 0.20, 4.1, 700)
            d = scheduled.drift(ops, days=7)
            self.assertEqual((d["latest"], d["baseline_days"]), ("2026-11-25", ["2026-11-23", "2026-11-24"]))
            self.assertIn("retrieval_decline_rate", d["alerts"])      # +9 points
            self.assertIn("tokens_input_p95:uc2", d["alerts"])        # 700 / 510 > 1.2
            self.assertNotIn("latency_p95_s:uc2", d["alerts"])
        finally:
            shutil.rmtree(ops, ignore_errors=True)


# --------------------------------------------------------------------------- the stack over HTTP
STACK = None


def setUpModule():
    global STACK
    STACK = start_stack(eval_poll_s=3600.0, ops_eval_s=3600.0, ops_snapshot_s=0.0)  # loops run once; tests drive them


def tearDownModule():
    stop_stack(STACK)


class StackEvaluation(unittest.TestCase):
    def setUp(self):
        self.stack = STACK
        reset(self.stack)
        self.layla = client(self.stack, LAYLA)

    def metrics(self, name: str) -> str:
        return Workload(self.stack, "orchestrator").request("GET", f"{self.stack.url(name)}/metrics").body.decode()

    def ops_summary(self, aud: str = "web-bff", who: str = "ops-admin"):
        w = Workload(self.stack, who)
        return w.request("GET", f"{self.stack.url('web-bff')}/api/ops/summary", headers={"authorization": "Bearer " + w.app_token(aud)})

    def test_retrieval_gate_declines_without_a_generation_call(self):
        aoai = self.stack.components["aoai-primary"]
        aoai.received.clear()
        r = self.layla.ask(WEAK_Q)
        self.assertEqual((r["status"], r["reason_code"]), ("declined", "WEAK_RETRIEVAL"))
        self.assertEqual([x for x in aoai.received if x["deployment"] == "gen-large"], [])  # the AOAI mock saw no generation call
        self.assertFalse(any(POLICY_QA_SYSTEM[:40] in json.dumps(x["body"]) for x in aoai.received))
        self.assertEqual(r["ops"]["model_calls"], 1)  # the gen-small query rewrite precedes retrieval (plan §4.3 step 3)
        self.assertIn('rmc_runtime_gate_total{use_case="uc2",gate="retrieval",outcome="declined"}', self.metrics("orchestrator"))

    def test_faithfulness_gate_in_the_stack_with_trace_scores(self):
        for name in ("aoai-primary", "aoai-secondary"):
            self.stack.components[name].llm = CarelessLLM()
        try:
            r = self.layla.ask(CAREFUL_Q)
        finally:
            for name in ("aoai-primary", "aoai-secondary"):
                self.stack.components[name].llm = MockLLM()
        self.assertEqual((r["status"], r["reason_code"]), ("declined", "FAILED_GROUNDEDNESS"))  # 7 s deadline: one retry, then decline
        self.assertEqual([g["outcome"] for g in r["evaluation"]["gates"] if g["gate"] == "faithfulness"], ["retried", "declined"])
        spans = [s for s in self.stack.components["orchestrator"].telemetry.spans_for(r["trace_id"]) if s["name"].startswith("eval.gate")]
        self.assertTrue(spans)
        for s in spans:
            self.assertIn(s["attributes"]["rmc.outcome"], ("passed", "retried", "declined"))
            for k, v in s["attributes"].items():
                if k.startswith("rmc.eval."):
                    self.assertIsInstance(v, (int, float), k)  # scores are numbers only
        self.assertIn('gate="faithfulness",outcome="retried"', self.metrics("orchestrator"))

    def test_thresholds_are_read_from_app_configuration(self):
        self.assertEqual(self.layla.ask(CAREFUL_Q)["status"], "answered")
        self.stack.publish_eval_config({"rmc:orchestrator:gate:retrieval:keep_margin": 0.25, "rmc:orchestrator:gate:retrieval:context_relevance_min": 0.6,
                                        "rmc:orchestrator:gate:version": "gates/test", "rmc:orchestrator:gate:faithfulness:min": 5})
        r = self.layla.ask(CAREFUL_Q)  # (8.7, 4.31): only the top passage is kept -> CRel 0.5 < 0.6: flagged, still answered
        self.assertEqual((r["status"], r["evaluation"]["gate_version"]), ("answered", "gates/test"))
        rg = next(g for g in r["evaluation"]["gates"] if g["gate"] == "retrieval")
        self.assertEqual(rg["outcome"], "passed")
        self.assertEqual(len(r.get("source_ids", [])) if r.get("source_ids") is not None else 1, 1)  # one passage reached the model
        self.assertIn('rmc_gate_config_rejected_total{key="faithfulness_min"}', self.metrics("orchestrator"))  # 5 is out of range

    def test_online_eval_worker_samples_scores_and_alerts(self):
        self.stack.publish_eval_config({"rmc:eval:online:sample_rate": 1.0})
        worker = self.stack.components["eval-worker"]
        aoai = self.stack.components["aoai-primary"]
        a = self.layla.ask(CAREFUL_Q)
        self.layla.brief("harbourline")
        self.assertTrue(a["ops"]["sampled_for_eval"])
        aoai.received.clear()
        self.stack.components["audit-writer"].flush()
        self.assertGreaterEqual(wait_for(lambda: worker.tick() or None, timeout=5) or 0, 1)
        self.assertTrue(any("evaluation judge" in json.dumps(x["body"]) for x in aoai.received))  # the in-region judge was called
        text = worker.metrics.render()
        self.assertIn('rmc_online_eval_items_total{use_case="uc2",outcome="judged"}', text)
        self.assertIn('rmc_online_eval_score_count{use_case="uc2",metric="faithfulness"}', text)
        self.assertIn('rmc_online_eval_alert{use_case="uc2",metric="faithfulness",window="1h"} 0', text)
        bad = {"use_case": "uc2", "question": "When does high utilisation trigger a review?", "contexts": [P["covenant-6.1"]],
               "answer": "Utilisation above 70% for 5 days triggers an automatic limit increase [1]."}
        for _ in range(10):  # min_items_window
            worker.ingest(bad)
        text = worker.metrics.render()
        self.assertIn('rmc_online_eval_alert{use_case="uc2",metric="faithfulness",window="1h"} 1', text)
        self.assertNotIn("Utilisation above 70%", text)

    def test_daily_cap_is_enforced(self):
        self.stack.publish_eval_config({"rmc:eval:online:max_per_day": 0})
        worker = self.stack.components["eval-worker"]
        self.assertIsNone(worker.ingest({"use_case": "uc2", "question": "q", "answer": "a [1].", "contexts": ["a"]}))
        self.assertIn('rmc_online_eval_items_total{use_case="uc2",outcome="skipped_cap"}', worker.metrics.render())

    def test_ops_summary_per_stage_p95_tokens_cost_and_alerts(self):
        self.layla.brief("harbourline")
        self.layla.ask(CAREFUL_Q)
        self.layla.draft("crescent", "I will share the payroll proposal by Friday.")
        r = self.ops_summary()
        self.assertEqual(r.status, 200, r.body[:300])
        s = r.json()
        for uc in ("uc1", "uc2", "uc3"):
            self.assertIsNotNone(s["latency_s"][uc]["p95"])
            self.assertIsNotNone(s["latency_s"][uc]["p99"])
            self.assertIsNotNone(s["tokens_per_request"][uc]["input"]["p95"])
            self.assertIsNotNone(s["tokens_per_request"][uc]["output"]["p50"])
        for stage in ("edge", "entitlement", "tool_fanout.get_brief_inputs", "retrieval", "pii", "prompt_shields", "retrieval_gate",
                      "model.uc2.rewrite", "model.uc2.answer", "model.uc1.brief", "post_gates"):
            self.assertIsNotNone(s["stages_s"][stage]["p95"], stage)
        self.assertGreater(s["cost_aed"]["user_traffic_today"], 0)
        self.assertTrue(s["cost_aed"]["illustrative_prices"])
        self.assertEqual({a["rule"] for a in s["alerts"]} >= {"RmcRequestLatencyP95", "RmcTokensPerRequestCreep", "RmcDailyCost80"}, True)
        self.assertEqual(self.ops_summary(aud="tool-gateway").status, 401)  # a token for another API is refused
        status, _, _ = self.layla.raw("GET", "/api/ops/summary")
        self.assertIn(status, (401, 403))

    def test_alert_rules_fire_and_are_exposed_as_gauges(self):
        self.stack.publish_eval_config({"rmc:ops:baselines": {"tokens_input_p95": {"uc2": 10}, "retrieval_decline_rate": 0.0, "min_requests": 1},
                                        "rmc:finops:daily_budget_aed": 0.000001})
        self.layla.ask(CAREFUL_Q)
        self.layla.ask(WEAK_Q)
        firing = {a["rule"] for a in self.ops_summary().json()["alerts"] if a["firing"]}
        self.assertTrue({"RmcTokensPerRequestCreep", "RmcDailyCost80", "RmcRetrievalGateDeclineRise"} <= firing, firing)
        worker = self.stack.components["eval-worker"]
        worker.ops_tick()
        text = worker.metrics.render()
        self.assertIn('rmc_ops_alert{rule="RmcDailyCost80",use_case="all"} 1', text)
        self.assertTrue(list((worker.ops_dir / "snapshots").glob("*.jsonl")))  # snapshot for the drift report

    def test_rm_feedback_endpoint(self):
        a = self.layla.ask(CAREFUL_Q)
        r = self.layla.call("POST", "/api/feedback", {"request_id": a["request_id"], "rating": "down", "reason": "incomplete"})
        self.assertEqual(r["_status"], 202)
        self.assertIn('rmc_rm_feedback_total{use_case="uc2",rating="down",reason="incomplete"} 1', self.metrics("web-bff"))
        omar = client(self.stack, OMAR)
        self.assertEqual(omar.raw("POST", "/api/feedback", {"request_id": a["request_id"], "rating": "up"})[0], 404)  # not his answer
        self.assertEqual(self.layla.raw("POST", "/api/feedback", {"request_id": a["request_id"], "rating": "meh"})[0], 400)
        self.assertEqual(self.layla.raw("POST", "/api/feedback", {"request_id": a["request_id"], "rating": "up", "reason": "free text"})[0], 400)

    def test_operations_metrics_contain_no_customer_data(self):
        self.stack.publish_eval_config({"rmc:eval:online:sample_rate": 1.0})
        self.layla.brief("harbourline")
        a = self.layla.ask(CAREFUL_Q)
        self.layla.call("POST", "/api/feedback", {"request_id": a["request_id"], "rating": "up"})
        self.stack.components["audit-writer"].flush()
        self.stack.components["eval-worker"].tick()
        self.stack.components["eval-worker"].ops_tick()
        forbidden = [c["name"] for c in data.CUSTOMERS.values()] + [c["contact"] for c in data.CUSTOMERS.values()] + list(data.CUSTOMERS)
        forbidden += [CAREFUL_Q, "unsecured working capital", user_oid(LAYLA), LAYLA, "Layla", a["answer"][:30]]
        line = re.compile(r'^[a-zA-Z_:][a-zA-Z0-9_:]*(\{([a-zA-Z_][a-zA-Z0-9_]*="([^"\\]|\\.)*",?)*\})? -?[0-9.eE+-]+$')
        for name in ("web-bff", "orchestrator", "tool-gateway", "eval-worker"):
            text = self.metrics(name)
            for ln in text.splitlines():
                if not ln.startswith("#"):
                    self.assertRegex(ln, line, f"{name}: {ln}")
            for f in forbidden:
                self.assertNotIn(f, text, f"{f!r} in {name} /metrics")
            spans = json.dumps(self.stack.components[name].telemetry.all_spans())
            for f in forbidden[:6] + [CAREFUL_Q]:
                self.assertNotIn(f, spans, f"{f!r} in {name} spans")
        snaps = "".join(p.read_text() for p in (self.stack.components["eval-worker"].ops_dir / "snapshots").glob("*.jsonl"))
        for f in forbidden:
            self.assertNotIn(f, snaps)


if __name__ == "__main__":
    unittest.main()
