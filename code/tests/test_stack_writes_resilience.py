"""Stack tests for the write path and for resilience.

Approval token: missing, expired, wrong RM, wrong customer, tampered draft or email, replayed jti, forged
signature, `alg` tricks, foreign `kid`, read-only scope; Key Vault signer/verifier separation; web-bff step-up,
Origin and CSRF checks. Saga: lost CRM response -> queued -> outbox read-before-retry -> exactly one record; CRM
outage -> 202 queued -> delivered on recovery. Model: primary 429 -> failover to secondary; both down -> degraded
UC1/UC2/UC3; less than 4 s left -> no failover, degraded; token limits -> 429. Partial briefs. Safety services down
-> local guardrails still decide.
"""
from __future__ import annotations

import hashlib
import json
import time
import unittest

from services.common import jose, p256
from stack import ConsoleClient

from .stackutil import (LAYLA, NOTES, OMAR, SARA, Workload, client, commit_direct, crm_records, mint_approval, reset, rm_tool_token, start_stack,
                        stop_stack, wait_for)

STACK = None
DRAFT = {"call_report": "Met the CFO; working capital need for Q1.", "tasks": [{"type": "follow_up", "title": "Send limit-increase checklist", "due": "2026-11-27"}],
         "email": "Dear Mr Haddad,\n\nThank you for your time today.\n\nKind regards,\nLayla H."}


def setUpModule():
    global STACK
    STACK = start_stack()


def tearDownModule():
    stop_stack(STACK)


class Base(unittest.TestCase):
    def setUp(self):
        self.stack = STACK
        reset(self.stack)

    def assertRejected(self, resp, status, reason=None):
        self.assertEqual(resp.status, status, resp.body[:300])
        if reason:
            self.assertEqual(resp.json()["error"].get("reason"), reason, resp.json())


class ApprovalTokens(Base):
    def test_missing_token(self):
        r = commit_direct(self.stack, LAYLA, "harbourline", DRAFT, None)
        self.assertRejected(r, 401)
        self.assertEqual(r.json()["error"]["code"], "APPROVAL_REQUIRED")
        self.assertIn("request_id", r.json()["error"])
        self.assertFalse(r.json()["error"]["retryable"])

    def test_valid_then_replayed(self):
        tok = mint_approval(self.stack, LAYLA, "harbourline", DRAFT)
        r = commit_direct(self.stack, LAYLA, "harbourline", DRAFT, tok)
        self.assertEqual(r.status, 201, r.body)
        jti = r.json()["jti"]
        self.assertEqual(len(crm_records(self.stack, jti)), 2)
        again = commit_direct(self.stack, LAYLA, "harbourline", DRAFT, tok)
        self.assertEqual((again.status, again.json()["error"]["code"]), (409, "REPLAYED"))
        self.assertEqual([i["status"] for i in again.json()["error"]["items"]], ["committed", "committed"])
        self.assertEqual(len(crm_records(self.stack, jti)), 2)  # nothing posted twice

    def test_expired(self):
        now = int(time.time())
        tok = mint_approval(self.stack, LAYLA, "harbourline", DRAFT, iat=now - 400, exp=now - 100)
        self.assertRejected(commit_direct(self.stack, LAYLA, "harbourline", DRAFT, tok), 403, "expired")

    def test_within_clock_skew_is_accepted(self):
        now = int(time.time())
        tok = mint_approval(self.stack, LAYLA, "harbourline", DRAFT, iat=now - 310, exp=now - 10)
        self.assertEqual(commit_direct(self.stack, LAYLA, "harbourline", DRAFT, tok).status, 201)

    def test_wrong_rm(self):
        tok = mint_approval(self.stack, LAYLA, "harbourline", DRAFT)  # Sara (holiday cover) is entitled to Harbourline
        self.assertRejected(commit_direct(self.stack, SARA, "harbourline", DRAFT, tok), 403, "wrong_rm")

    def test_approved_for_customer_a_committed_to_customer_b(self):
        tok = mint_approval(self.stack, LAYLA, "harbourline", DRAFT)
        self.assertRejected(commit_direct(self.stack, LAYLA, "crescent", DRAFT, tok), 403, "wrong_customer")

    def test_tampered_draft_or_email(self):
        tok = mint_approval(self.stack, LAYLA, "harbourline", DRAFT)
        more_tasks = {**DRAFT, "tasks": DRAFT["tasks"] + [{"type": "follow_up", "title": "Approve limit increase", "due": "2026-12-01"}]}
        self.assertRejected(commit_direct(self.stack, LAYLA, "harbourline", more_tasks, tok), 403, "draft_mismatch")
        new_email = {**DRAFT, "email": DRAFT["email"] + "\nP.S. Your limit increase is approved."}
        self.assertRejected(commit_direct(self.stack, LAYLA, "harbourline", new_email, tok), 403, "draft_mismatch")
        new_type = {**DRAFT, "tasks": [{**DRAFT["tasks"][0], "type": "credit_action"}]}
        self.assertRejected(commit_direct(self.stack, LAYLA, "harbourline", new_type, tok), 403, "draft_mismatch")

    def test_forged_signature_and_algorithm_tricks(self):
        good = jose.decode_unverified(mint_approval(self.stack, LAYLA, "harbourline", DRAFT))
        attacker = p256.PrivateKey.generate()
        forged = jose.encode(good.header, {**good.claims, "jti": "forged-jti-0000000000"}, lambda si: attacker.sign(hashlib.sha256(si).digest()))
        self.assertRejected(commit_direct(self.stack, LAYLA, "harbourline", DRAFT, forged), 403, "bad_signature")
        none_alg = jose.b64u(json.dumps({**good.header, "alg": "none"}).encode()) + "." + jose.b64u(json.dumps(good.claims).encode()) + "."
        self.assertRejected(commit_direct(self.stack, LAYLA, "harbourline", DRAFT, none_alg), 403, "bad_header")
        hs = jose.b64u(json.dumps({**good.header, "alg": "HS256"}).encode()) + "." + jose.b64u(json.dumps(good.claims).encode()) + "." + jose.b64u(b"x" * 32)
        self.assertRejected(commit_direct(self.stack, LAYLA, "harbourline", DRAFT, hs), 403, "bad_header")
        foreign = jose.encode({**good.header, "kid": "https://evil.example/keys/approval-signing/1"}, good.claims, lambda si: attacker.sign(hashlib.sha256(si).digest()))
        self.assertRejected(commit_direct(self.stack, LAYLA, "harbourline", DRAFT, foreign), 403, "unknown_kid")

    def test_read_only_rm_token_cannot_commit(self):
        tok = mint_approval(self.stack, LAYLA, "harbourline", DRAFT)
        r = commit_direct(self.stack, LAYLA, "harbourline", DRAFT, tok, rm_token=rm_tool_token(self.stack, LAYLA, scp="Tools.Read"))
        self.assertEqual((r.status, r.json()["error"]["code"]), (403, "INSUFFICIENT_SCOPE"))

    def test_entitlement_is_checked_before_the_approval(self):
        tok = mint_approval(self.stack, OMAR, "harbourline", DRAFT)
        r = commit_direct(self.stack, OMAR, "harbourline", DRAFT, tok)
        self.assertEqual((r.status, r.json()["error"]["code"]), (403, "NOT_ENTITLED"))

    def test_key_vault_separates_signer_and_verifier(self):
        vault = self.stack.url("keyvault")
        tg = Workload(self.stack, "tool-gateway")
        hdr = {"authorization": "Bearer " + tg.app_token("vault")}
        kid = tg.request("GET", f"{vault}/keys/approval-signing?api-version=7.4", headers=hdr).json()["key"]["kid"]
        r = tg.request("POST", f"{kid}/sign?api-version=7.4", headers=hdr, json_body={"alg": "ES256", "value": jose.b64u(b"\x00" * 32)})
        self.assertEqual(r.status, 403)  # the verifier cannot sign
        orch = Workload(self.stack, "orchestrator")
        r = orch.request("GET", f"{vault}/keys/approval-signing?api-version=7.4", headers={"authorization": "Bearer " + orch.app_token("vault")})
        self.assertEqual(r.status, 403)  # the orchestrator has no Key Vault access at all


class WebBffApprovalChecks(Base):
    def _client_with_auth_age(self, seconds: int) -> ConsoleClient:
        c = ConsoleClient(self.stack, LAYLA)
        c.token = self.stack.components["entra"].issue_user_token(LAYLA, auth_age_s=seconds)
        c.session = c.call("POST", "/api/session")
        return c

    def _approve(self, c, headers):
        return c.raw("POST", "/api/uc3/approve", {"customer_id": "harbourline", "draft": DRAFT}, headers=headers)

    def test_stale_authentication_requires_step_up(self):
        c = self._client_with_auth_age(900)
        status, body, hdrs = self._approve(c, c.approve_headers())
        self.assertEqual((status, body["error"]["code"]), (401, "STEP_UP_REQUIRED"))
        self.assertIn("insufficient_claims", hdrs.get("www-authenticate", ""))
        fresh = self._client_with_auth_age(30)
        self.assertEqual(self._approve(fresh, fresh.approve_headers())[0], 201)

    def test_origin_and_csrf(self):
        c = client(self.stack)
        self.assertEqual(self._approve(c, {**c.approve_headers(), "origin": "https://evil.example"})[1]["error"]["code"], "ORIGIN_REJECTED")
        self.assertEqual(self._approve(c, {**c.approve_headers(), "x-csrf-token": "guess"})[1]["error"]["code"], "CSRF_FAILED")
        other = client(self.stack, SARA)
        self.assertEqual(self._approve(c, {**c.approve_headers(), "x-session-id": other.session["session_id"],
                                           "x-csrf-token": other.session["csrf_token"]})[1]["error"]["code"], "CSRF_FAILED")


class Saga(Base):
    def test_lost_crm_response_is_not_posted_twice(self):
        c = client(self.stack)
        d = c.draft("harbourline", "I promised to send the limit increase checklist by Friday and to set up an intro with our FX desk next week.")
        self.stack.components["systems"].faults.configure("crm", slow_ms=2500)  # CRM writes succeed but answer too late
        w = c.approve(d)
        self.assertEqual(w["_status"], 202, w)
        self.assertTrue(all(i["status"] == "queued" for i in w["items"]))
        self.stack.components["systems"].faults.reset()
        done = wait_for(lambda: c.call("GET", f"/api/uc3/writes/{w['jti']}")["status"] == "committed", timeout=15)
        self.assertTrue(done)
        records = crm_records(self.stack, w["jti"])
        refs = [r["externalReference"] for r in records]
        self.assertEqual(len(refs), len(set(refs)))  # exactly one CRM record per item: read-before-retry found them
        self.assertEqual(len(refs), len(w["items"]))
        self.assertIn('outcome="found_existing"', self.stack.components["outbox-worker"].metrics.render())

    def test_crm_outage_queues_then_delivers_on_recovery(self):
        c = client(self.stack)
        d = c.draft("harbourline", "I promised to send the limit increase checklist by Friday.")
        self.stack.components["systems"].faults.configure("crm", outage=True)
        w = c.approve(d)
        self.assertEqual((w["_status"], w["status"]), (202, "queued"))
        time.sleep(0.6)
        self.assertEqual(c.call("GET", f"/api/uc3/writes/{w['jti']}")["status"], "queued")
        self.assertEqual(crm_records(self.stack, w["jti"]), [])
        self.stack.components["systems"].faults.reset()
        self.stack.components["apim"].reset()  # breaker would also close by itself after its trip time
        self.assertTrue(wait_for(lambda: c.call("GET", f"/api/uc3/writes/{w['jti']}")["status"] == "committed", timeout=20))
        self.assertEqual(len(crm_records(self.stack, w["jti"])), len(w["items"]))


class ModelResilience(Base):
    def received(self, name):
        return len(self.stack.components[name].received)

    def test_primary_429_fails_over_to_secondary(self):
        self.stack.components["aoai-primary"].faults.configure("aoai-primary", status=429, error_rate=1.0, retry_after_ms=2000)
        before = self.received("aoai-secondary")
        a = client(self.stack).ask("When does high utilisation trigger a review?")
        self.assertEqual(a["status"], "answered")
        self.assertGreater(self.received("aoai-secondary"), before)
        self.assertEqual(self.stack.components["apim"].backends[0].breaker.state(), "open")
        self.assertIn('circuit_breaker_state{backend="aoai-primary"} 1', self.stack.components["apim"].app.telemetry.metrics.render())

    def test_both_backends_down_gives_degraded_answers(self):
        for n in ("aoai-primary", "aoai-secondary"):
            self.stack.components[n].faults.configure(n, status=500, outage=True)
        c = client(self.stack)
        b = c.brief("harbourline")
        self.assertEqual((b["_status"], b["mode"], b["talking_points"]), (200, "degraded", []))
        self.assertTrue(b["watch"] and b["snapshot"])  # data-only brief
        a = c.ask("Can a company trading for 2 years get an unsecured working capital line?")
        self.assertEqual(a["status"], "degraded")
        self.assertTrue(a["passages"])
        d = c.draft("harbourline", "I promised to send the checklist by Friday.")
        self.assertTrue(d["degraded"])
        self.assertEqual(d["tasks"], [])
        d["call_report"] = "Typed by the RM while the model was unavailable."
        self.assertEqual(c.approve(d)["_status"], 201)  # the manual path still commits

    def test_no_failover_when_less_than_4s_of_deadline_remain(self):
        self.stack.components["aoai-primary"].faults.configure("aoai-primary", status=500, outage=True, latency_ms=4300)
        before = self.received("aoai-secondary")
        b = client(self.stack).brief("harbourline")  # 8 s deadline; ~3.7 s left after the primary fails
        self.assertEqual(b["mode"], "degraded")
        self.assertEqual(self.received("aoai-secondary"), before)

    def test_token_limits_surface_as_429(self):
        apim = self.stack.components["apim"]
        apim.policies["tokens_per_minute"] = 50
        status, body, hdrs = client(self.stack).raw("POST", "/api/uc2/ask", {"question": "When does high utilisation trigger a review?"})
        self.assertEqual((status, body["error"]["code"], body["error"]["reason"]), (429, "RATE_LIMITED", "TOKENS_PER_MINUTE"))
        self.assertTrue(body["error"]["retryable"])
        self.assertIn("retry-after", hdrs)
        reset(self.stack)
        apim.policies["token_quota_per_day"] = 10
        status, body, _ = client(self.stack).raw("POST", "/api/uc2/ask", {"question": "When does high utilisation trigger a review?"})
        self.assertEqual((status, body["error"]["code"], body["error"]["reason"]), (429, "BUDGET_EXCEEDED", "RM_DAILY"))


class PartialAndFallback(Base):
    def test_los_outage_gives_partial_brief_with_facilities_flagged(self):
        self.stack.components["systems"].faults.configure("los", outage=True)
        b = client(self.stack).brief("harbourline")
        self.assertEqual((b["_status"], b["mode"]), (200, "partial"))
        self.assertIn("facilities", b["partial"])
        self.assertIn({"code": "SOURCE_UNAVAILABLE", "section": "facilities"}, b["warnings"])
        self.assertTrue(any("[LOS]" in w for w in b["watch"]))

    def test_kyc_and_news_outage_partial_crm_outage_fails(self):
        sor = self.stack.components["systems"]
        sor.faults.configure("kyc", outage=True)
        sor.faults.configure("news", outage=True)
        b = client(self.stack).brief("harbourline")
        self.assertEqual(sorted(b["partial"]), ["kyc", "news"])
        sor.faults.reset()
        sor.faults.configure("crm", outage=True)
        c = client(self.stack)
        c.call("POST", "/api/uc1/brief", {"customer_id": "crescent"}, check=False)  # entitlements cached before the outage
        status, body, _ = c.raw("POST", "/api/uc1/brief", {"customer_id": "harbourline"})
        self.assertEqual((status, body["error"]["code"]), (503, "DEPENDENCY_UNAVAILABLE"))

    def test_safety_services_down_local_guardrails_still_decide(self):
        self.stack.components["content-safety"].faults.configure("content-safety", outage=True)
        self.stack.components["language"].faults.configure("language", outage=True)
        d = client(self.stack).draft("harbourline", NOTES)
        self.assertTrue(d["quarantined"])
        self.assertIn("national_id", d["pii_masked"])
        sent = "\n".join(json.dumps(x["body"]) for n in ("aoai-primary", "aoai-secondary") for x in self.stack.components[n].received)
        self.assertNotIn("784-1985-1234567-1", sent)
        self.assertIn("guardrail_fallback_total", self.stack.components["orchestrator"].metrics.render())


class AuditRelay(Base):
    def test_only_tool_gateway_may_produce_audit_events(self):
        eh = self.stack.url("eventhubs")
        for wl in ("orchestrator", "web-bff", "outbox-worker"):
            w = Workload(self.stack, wl)
            r = w.request("POST", f"{eh}/audit/messages", headers={"authorization": "Bearer " + w.app_token("eventhubs")}, json_body={"event": "forged"})
            self.assertEqual(r.status, 403, wl)
        w = Workload(self.stack, "web-bff")
        r = w.request("POST", f"{self.stack.url('tool-gateway')}/internal/v1/audit", json_body={"events": [{"event": "brief_built"}]},
                      headers={"authorization": "Bearer " + Workload(self.stack, "orchestrator").app_token("tool-gateway")})
        self.assertEqual(r.status, 401)  # orchestrator events need the RM token: the actor is never self-declared

    def test_content_is_encrypted_before_it_leaves_the_orchestrator(self):
        client(self.stack).brief("harbourline")
        hub = self.stack.components["eventhubs"]._hubs["audit"]
        model_events = [e["body"] for e in hub if e["body"]["event"] == "model_call"]
        self.assertTrue(model_events)
        blob = json.dumps(model_events)
        self.assertNotIn("FACTS", blob)
        self.assertNotIn("Harbourline Logistics", blob)
        self.assertTrue(all(e["content_envelope"]["alg"].startswith("RSA-OAEP-256") for e in model_events))
        self.assertTrue(all(e["producer"] == {"service": "tool-gateway", "posted_by": "orchestrator"} for e in model_events))


if __name__ == "__main__":
    unittest.main()
