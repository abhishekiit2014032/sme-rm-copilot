"""Red-team follow-ups (own stack, because key rotation and allow-list changes are global state):

1. `allowed_kids`: tool-gateway accepts an approval token only if its key version is in the allow-list in App
   Configuration (`rmc:tool-gateway:approval:allowed_kids`, LLD §4.4 step 7, §4.7): emergency revocation and
   rotation with overlap.
2. `approval_rejected` audit event for every rejected commit (LLD §4.4), with the reason and no draft content.
3. Event-based entitlement invalidation (plan §4.1 step 4, LLD §7.3): CRM -> APIM `rmc-crm-events` -> Event Hub
   `crm-portfolio-events` -> tool-gateway evicts the RM's cached entitlements at once.
4. Digit-script normalisation before PII matching (ADR-012): Arabic-Indic and Eastern Arabic-Indic digits.
"""
from __future__ import annotations

import hashlib
import json
import time
import unittest

from rm_copilot import data
from rm_copilot.guardrails import mask_pii
from rm_copilot.llm import MockLLM
from rm_copilot.orchestrator import Copilot
from rm_copilot.tools import User
from services.common import jose, p256
from services.common.config import user_oid
from services.common.contracts import approval_claims

from .stackutil import LAYLA, Workload, client, commit_direct, mint_approval, reset, start_stack, stop_stack, wait_for

STACK = None
ALLOWED_KIDS = "rmc:tool-gateway:approval:allowed_kids"
DRAFT = {"call_report": "Met the CFO; working capital need for Q1.", "tasks": [{"type": "follow_up", "title": "Send checklist", "due": "2026-11-27"}],
         "email": "Dear Mr Haddad"}


def setUpModule():
    global STACK
    STACK = start_stack()


def tearDownModule():
    stop_stack(STACK)


class Base(unittest.TestCase):
    def setUp(self):
        self.stack = STACK
        reset(self.stack)

    # Key Vault as web-bff (the signer) and App Configuration as ops-admin (the pipeline / incident commander)
    def vault(self):
        wb = Workload(self.stack, "web-bff")
        return wb, {"authorization": "Bearer " + wb.app_token("vault")}

    def current_kid(self) -> str:
        wb, hdr = self.vault()
        return wb.request("GET", f"{self.stack.url('keyvault')}/keys/approval-signing?api-version=7.4", headers=hdr).json()["key"]["kid"]

    def set_allowed(self, kids: list[str]) -> None:
        ops = Workload(self.stack, "ops-admin")
        r = ops.request("PUT", f"{self.stack.url('appconfig')}/kv/{ALLOWED_KIDS.replace(':', '%3A')}?api-version=2023-11-01",
                        headers={"authorization": "Bearer " + ops.app_token("appconfig")},
                        json_body={"value": json.dumps(kids), "content_type": "application/json"})
        self.assertEqual(r.status, 200, r.body[:200])

    def allowed(self) -> list[str]:
        return json.loads(self.stack.components["appconfig"]._kv[ALLOWED_KIDS]["value"])

    def sign(self, kid: str, customer_id: str = "harbourline", draft: dict = DRAFT) -> str:
        wb, hdr = self.vault()

        def sign(si: bytes) -> bytes:
            r = wb.request("POST", f"{kid}/sign?api-version=7.4", headers=hdr, json_body={"alg": "ES256", "value": jose.b64u(hashlib.sha256(si).digest())})
            return jose.b64u_decode(r.json()["value"])
        return jose.encode({"alg": "ES256", "typ": "approval+jwt", "kid": kid}, approval_claims(user_oid(LAYLA), customer_id, draft, kid), sign)

    def assertReason(self, r, status, reason):
        self.assertEqual(r.status, status, r.body[:200])
        self.assertEqual(r.json()["error"].get("reason"), reason, r.body[:200])


# ---------------------------------------------------------------------------------------------------- 1. allowed_kids
class AllowedKids(Base):
    def test_revoking_a_key_version_rejects_fresh_tokens_signed_with_it(self):
        before = self.allowed()
        self.assertIn(self.current_kid().rsplit("/", 1)[-1], [k.rsplit("/", 1)[-1] for k in before])  # published at start-up
        try:
            self.set_allowed([])  # emergency revocation of every version
            r = commit_direct(self.stack, LAYLA, "harbourline", DRAFT, mint_approval(self.stack, LAYLA, "harbourline", DRAFT))
            self.assertReason(r, 403, "kid_not_allowed")
            self.assertEqual(r.json()["error"]["code"], "APPROVAL_INVALID")
        finally:
            self.set_allowed(before)
        self.assertEqual(commit_direct(self.stack, LAYLA, "harbourline", DRAFT, mint_approval(self.stack, LAYLA, "harbourline", DRAFT)).status, 201)

    def test_rotation_with_overlap_then_retirement_of_the_old_version(self):
        old = self.current_kid()
        wb, hdr = self.vault()
        r = wb.request("POST", f"{self.stack.url('keyvault')}/keys/approval-signing/rotate?api-version=7.4", headers=hdr)
        self.assertEqual(r.status, 200, r.body[:200])
        new = r.json()["key"]["kid"]
        self.assertNotEqual(old, new)
        try:
            r = commit_direct(self.stack, LAYLA, "harbourline", DRAFT, self.sign(new))
            self.assertReason(r, 403, "kid_not_allowed")  # D0: new version exists but is not yet allowed
            self.set_allowed([old.rsplit("/", 1)[-1], new.rsplit("/", 1)[-1]])  # D0 + 1 h: overlap
            self.assertEqual(commit_direct(self.stack, LAYLA, "harbourline", DRAFT, self.sign(old)).status, 201)
            self.assertEqual(commit_direct(self.stack, LAYLA, "harbourline", DRAFT, self.sign(new)).status, 201)
            self.set_allowed([new])  # D0 + 8 d (or emergency): the old version is retired; full kid URLs are accepted too
            self.assertReason(commit_direct(self.stack, LAYLA, "harbourline", DRAFT, self.sign(old)), 403, "kid_not_allowed")
            self.assertEqual(commit_direct(self.stack, LAYLA, "harbourline", DRAFT, self.sign(new)).status, 201)
        finally:
            self.set_allowed([old.rsplit("/", 1)[-1], new.rsplit("/", 1)[-1]])  # web-bff in this stack still signs with `old`


# ---------------------------------------------------------------------------------------------------- 2. approval_rejected
class ApprovalRejectedAudit(Base):
    def test_every_rejected_commit_is_audited_with_its_reason_and_no_draft_content(self):
        marker = "ZEBRA-SECRET-DRAFT-77"
        expected: dict[str, str] = {}

        def jti_of(tok: str) -> str:
            return jose.decode_unverified(tok).claims["jti"]
        good = mint_approval(self.stack, LAYLA, "harbourline", DRAFT)
        d = jose.decode_unverified(good)
        attacker = p256.PrivateKey.generate()
        forged = jose.encode(d.header, {**d.claims, "jti": "forged-jti-000000000001"}, lambda si: attacker.sign(hashlib.sha256(si).digest()))
        self.assertReason(commit_direct(self.stack, LAYLA, "harbourline", DRAFT, forged), 403, "bad_signature")
        expected["forged-jti-000000000001"] = "bad_signature"
        tok = mint_approval(self.stack, LAYLA, "harbourline", DRAFT)
        self.assertReason(commit_direct(self.stack, LAYLA, "crescent", DRAFT, tok), 403, "wrong_customer")
        expected[jti_of(tok)] = "wrong_customer"
        tok = mint_approval(self.stack, LAYLA, "harbourline", DRAFT)
        self.assertReason(commit_direct(self.stack, LAYLA, "harbourline", {**DRAFT, "call_report": marker}, tok), 403, "draft_mismatch")
        expected[jti_of(tok)] = "draft_mismatch"
        tok = mint_approval(self.stack, LAYLA, "harbourline", DRAFT, exp=int(time.time()) - 120, iat=int(time.time()) - 400)
        self.assertReason(commit_direct(self.stack, LAYLA, "harbourline", DRAFT, tok), 403, "expired")
        expected[jti_of(tok)] = "expired"
        self.assertEqual(commit_direct(self.stack, LAYLA, "harbourline", DRAFT, good).status, 201)
        r = commit_direct(self.stack, LAYLA, "harbourline", DRAFT, good)
        self.assertEqual((r.status, r.json()["error"]["code"]), (409, "REPLAYED"))
        expected[jti_of(good)] = "replayed"
        self.assertReason(commit_direct(self.stack, LAYLA, "harbourline", {**DRAFT, "email": marker}, "not.a.token"), 403, "malformed")

        writer = self.stack.components["audit-writer"]
        time.sleep(0.2)
        writer.flush()
        text = writer.chain_path.read_text(encoding="utf-8")
        rejected = [json.loads(x)["event"] for x in text.splitlines() if '"approval_rejected"' in x]
        by_jti = {e["metadata"].get("jti"): e for e in rejected if e["event"] == "approval_rejected"}
        for jti, reason in expected.items():
            self.assertIn(jti, by_jti, f"no approval_rejected event for {reason}")
            ev = by_jti[jti]
            self.assertEqual(ev["metadata"]["reason"], reason)
            self.assertEqual(ev["metadata"]["op"], "crm.commit_followup")
            self.assertEqual(len(ev["metadata"]["token_sha256"]), 64)
            self.assertLessEqual(set(ev["metadata"]), {"op", "reason", "jti", "kid", "token_sha256"})  # metadata only, never the draft
            self.assertEqual(ev["actor"]["oid"], user_oid(LAYLA))
        self.assertTrue(any(e["metadata"]["reason"] == "malformed" and e["metadata"].get("jti") is None for e in rejected))
        self.assertNotIn(marker, text)  # no draft content in the chain


# ---------------------------------------------------------------------------------------------------- 3. portfolio events
class PortfolioEvents(Base):
    def publish(self, workload: str, events) -> object:
        w = Workload(self.stack, workload)
        return w.request("POST", f"{self.stack.url('apim')}/crm-events/v1/portfolio", headers={"authorization": "Bearer " + w.app_token("apim")},
                         json_body={"events": events})

    def test_portfolio_change_event_evicts_cached_entitlements_at_once(self):
        layla = client(self.stack)
        self.assertEqual(layla.brief("crescent")["_status"], 200)  # entitlements now cached (TTL 60 s)
        data.RMS["layla"]["portfolio"].discard("crescent")
        try:
            self.assertEqual(layla.brief("crescent")["_status"], 200)  # without an event: stale until the TTL
            r = self.publish("crm-integration", [{"event_id": "evt-0001", "type": "portfolio.changed", "occurred_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                                                   "rm_oid": user_oid(LAYLA), "customer_id": "crescent", "change": "removed"}])
            self.assertEqual(r.status, 202, r.body[:200])
            t0 = time.monotonic()
            denied = wait_for(lambda: layla.raw("POST", "/api/uc1/brief", {"customer_id": "crescent"})[0] == 403, timeout=5, interval=0.05)
            self.assertTrue(denied, "access still allowed after the portfolio-change event")
            self.assertLess(time.monotonic() - t0, 5)
            metrics = self.stack.components["tool-gateway"].metrics.render()
            self.assertIn("rmc_portfolio_events_total", metrics)
            self.assertIn("rmc_portfolio_event_lag_seconds", metrics)
        finally:
            data.RMS["layla"]["portfolio"].add("crescent")
            self.stack.components["tool-gateway"]._entitlements.clear()
        self.assertEqual(layla.brief("crescent")["_status"], 200)

    def test_only_the_crm_identity_may_publish_and_events_carry_ids_only(self):
        ev = {"event_id": "evt-0002", "type": "portfolio.changed", "occurred_at": "2026-11-26T05:40:00Z", "rm_oid": user_oid(LAYLA),
              "customer_id": "crescent", "change": "removed"}
        self.assertEqual(self.publish("orchestrator", [ev]).status, 403)
        for bad in ([{**ev, "customer_name": "Crescent Foods Trading LLC"}], [{**ev, "customer_id": "x/../y"}], [{**ev, "type": "drop.tables"}], [], "x"):
            self.assertEqual(self.publish("crm-integration", bad).status, 400, bad)
        orch = Workload(self.stack, "orchestrator")  # nobody else may write to the hub directly
        r = orch.request("POST", f"{self.stack.url('eventhubs')}/crm-portfolio-events/messages", headers={"authorization": "Bearer " + orch.app_token("eventhubs")},
                         json_body=ev)
        self.assertEqual(r.status, 403)


# ---------------------------------------------------------------------------------------------------- 4. digit scripts
ARABIC_INDIC = str.maketrans("0123456789", "٠١٢٣٤٥٦٧٨٩")
EASTERN = str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹")


class DigitScripts(Base):
    RAW = ("784-1985-1234567-1".translate(ARABIC_INDIC), "784 1985 1234567 1".translate(EASTERN),
           "+971 50 123 4567".translate(ARABIC_INDIC), "050 123 4567".translate(EASTERN), "AE07 0331 2345 6789 0123 456".translate(ARABIC_INDIC))

    def test_arabic_indic_digits_are_detected_and_other_text_is_kept(self):
        expect = ("national_id", "national_id", "phone", "phone", "iban")
        for raw, kind in zip(self.RAW, expect):
            masked, found = mask_pii(f"رقم {raw} ٣ trucks")
            self.assertEqual(found, [kind], raw)
            self.assertEqual(masked, f"رقم [{kind.upper()}] ٣ trucks")  # non-PII Arabic digits untouched

    def test_uc3_notes_in_arabic_digits_never_reach_the_model(self):
        layla = client(self.stack)
        before = sum(len(self.stack.components[n].received) for n in ("aoai-primary", "aoai-secondary"))
        d = layla.draft("harbourline", "Met Mr Haddad. I promised to send the checklist by Friday. His ID " + ", ".join(self.RAW) + ".")
        self.assertIn("national_id", d["pii_masked"])
        self.assertIn("phone", d["pii_masked"])
        sent = "\n".join(json.dumps(x["body"], ensure_ascii=False) for n in ("aoai-primary", "aoai-secondary")
                         for x in list(self.stack.components[n].received))
        self.assertGreater(sum(len(self.stack.components[n].received) for n in ("aoai-primary", "aoai-secondary")), before)
        to_ascii = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")
        for raw in self.RAW:
            self.assertNotIn(raw, sent)
            self.assertNotIn(raw.translate(to_ascii), sent.translate(to_ascii))

    def test_uc1_facts_are_masked_before_json_escaping(self):
        seen: list[str] = []

        class Recording(MockLLM):
            def complete(self, system, user):
                seen.append(user)
                return super().complete(system, user)
        sig = {"text": "Owner asks for a call on " + "+971 50 123 4567".translate(ARABIC_INDIC), "nba": "Call the owner"}
        data.CUSTOMERS["crescent"]["signals"].append(sig)
        try:
            Copilot(Recording()).brief(User.load("layla"), "crescent")
        finally:
            data.CUSTOMERS["crescent"]["signals"].remove(sig)
        prompt = seen[0]
        self.assertIn("[PHONE]", prompt)
        self.assertNotIn("\\u0669", prompt)
        self.assertNotIn("٩٧١", json.loads(prompt.split("FACTS:", 1)[1]).get("signals", [""])[-1])


if __name__ == "__main__":
    unittest.main()
