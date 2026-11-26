"""Red-team tests: attacks against the local stack's controls (plan §3.4, §4.2, C4-C7, C18-C22; LLD §1, §4, §5).

Each class targets one control. Tests marked "regression" reproduce a bypass or correctness bug that was found and
fixed; the others are attacks that held up and are kept so they keep holding.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import shutil
import tempfile
import threading
import time
import unicodedata
import unittest
from pathlib import Path

from rm_copilot import data
from rm_copilot.guardrails import mask_pii
from services.audit_writer import AuditWriter, verify_chain
from services.common import jose
from services.common.config import AUD, Settings, StackConfig, user_oid
from services.common.contracts import approval_claims

from .stackutil import (LAYLA, OMAR, Workload, client, commit_direct, crm_records, mint_approval, reset, rm_tool_token, start_stack,
                        stop_stack, wait_for)

STACK = None
DRAFT = {"call_report": "Met the CFO; working capital need for Q1.", "tasks": [{"type": "follow_up", "title": "Send checklist", "due": "2026-11-27"}],
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
        self.stack.components["appconfig"].faults.reset()

    def model_inputs(self) -> str:
        return "\n".join(json.dumps(x["body"]) for n in ("aoai-primary", "aoai-secondary") for x in self.stack.components[n].received)

    def warm_entitlements(self, upn: str) -> None:
        self.stack.components["tool-gateway"].entitlements(user_oid(upn))

    def queue_commit(self, upn: str, customer_id: str, draft: dict = DRAFT) -> str:
        """Commit while the CRM is down: every item is queued in the outbox. Returns the jti."""
        self.warm_entitlements(upn)  # the entitlement source is the CRM too
        self.stack.components["systems"].faults.configure("crm", outage=True)
        r = commit_direct(self.stack, upn, customer_id, draft, mint_approval(self.stack, upn, customer_id, draft))
        self.assertEqual((r.status, r.json().get("status")), (202, "queued"), r.body[:300])
        return r.json()["jti"]

    def outbox_items(self, jti: str) -> list[dict]:
        return [d for d in self.stack.components["cosmos"]._colls["outbox"].values() if d["jti"] == jti]


# ---------------------------------------------------------------------------------------------------- kill switch
class KillSwitch(Base):
    def test_outbox_holds_queued_items_while_writes_kill_switch_is_off(self):  # regression
        jti = self.queue_commit(LAYLA, "harbourline")
        self.stack.components["appconfig"].set_flag("writes", False)  # ops pulls the kill switch
        self.stack.components["systems"].faults.reset()
        self.stack.components["apim"].reset()
        time.sleep(2.0)
        self.assertEqual(crm_records(self.stack, jti), [], "the outbox wrote to the CRM while the writes kill switch was off")
        self.assertTrue(all(i["status"] == "queued" for i in self.outbox_items(jti)))
        self.stack.components["appconfig"].set_flag("writes", True)
        self.assertTrue(wait_for(lambda: len(crm_records(self.stack, jti)) == 2, timeout=15))

    def test_tool_gateway_enforces_writes_kill_switch_and_keeps_the_approval_usable(self):  # regression
        tok = mint_approval(self.stack, LAYLA, "harbourline", DRAFT)
        self.stack.components["appconfig"].set_flag("writes", False)
        r = commit_direct(self.stack, LAYLA, "harbourline", DRAFT, tok)  # a caller holding the orchestrator identity
        self.assertEqual((r.status, r.json()["error"]["code"]), (503, "FEATURE_DISABLED"), r.body[:200])
        self.stack.components["appconfig"].set_flag("writes", True)
        self.assertEqual(commit_direct(self.stack, LAYLA, "harbourline", DRAFT, tok).status, 201)  # the jti was not burnt

    def test_writes_fail_closed_when_the_flag_store_has_been_unreachable_too_long(self):  # regression
        s = self.stack.settings
        old = s.writes_flag_max_stale_s if hasattr(s, "writes_flag_max_stale_s") else None
        c = client(self.stack)
        d = c.draft("harbourline", "I promised to send the limit increase checklist by Friday.")
        try:
            s.writes_flag_max_stale_s = 0.3
            self.stack.components["appconfig"].faults.configure("appconfig", outage=True)
            time.sleep(0.5)
            status, body, _ = c.raw("POST", "/api/uc3/approve", {"customer_id": "harbourline", "draft": {**d, "email": d["email_draft"]}},
                                    headers=c.approve_headers())
            self.assertEqual((status, body.get("error", {}).get("code")), (503, "FEATURE_DISABLED"), body)
            self.assertEqual(c.brief("harbourline")["_status"], 200)  # read use cases keep the last known value
        finally:
            self.stack.components["appconfig"].faults.reset()
            if old is not None:
                s.writes_flag_max_stale_s = old


# ---------------------------------------------------------------------------------------------------- entitlements
class Entitlements(Base):
    def test_outbox_does_not_write_after_the_rm_lost_the_customer(self):  # regression (LLD §5.4 ENTITLEMENT_REVOKED)
        jti = self.queue_commit(LAYLA, "crescent")
        data.RMS["layla"]["portfolio"].discard("crescent")  # portfolio change in the CRM after the approval
        try:
            self.stack.components["systems"].faults.reset()
            self.stack.components["apim"].reset()
            self.assertTrue(wait_for(lambda: all(i["status"] == "failed" for i in self.outbox_items(jti)), timeout=10), self.outbox_items(jti))
            self.assertEqual(crm_records(self.stack, jti), [])
            self.assertTrue(all(i.get("reason") == "ENTITLEMENT_REVOKED" for i in self.outbox_items(jti)))
        finally:
            data.RMS["layla"]["portfolio"].add("crescent")
            self.stack.components["tool-gateway"]._entitlements.clear()

    def test_entitlement_cache_is_bounded_by_its_ttl(self):  # held up (plan C22: <= 60 s)
        s = self.stack.settings
        old = s.entitlement_ttl_s
        layla = client(self.stack)
        try:
            s.entitlement_ttl_s = 0.3
            self.stack.components["tool-gateway"]._entitlements.clear()
            self.assertEqual(layla.brief("crescent")["_status"], 200)
            data.RMS["layla"]["portfolio"].discard("crescent")
            time.sleep(0.4)
            status, body, _ = layla.raw("POST", "/api/uc1/brief", {"customer_id": "crescent"})
            self.assertEqual((status, body["error"]["code"]), (403, "NOT_ENTITLED"))
        finally:
            data.RMS["layla"]["portfolio"].add("crescent")
            s.entitlement_ttl_s = old
            self.stack.components["tool-gateway"]._entitlements.clear()


# ---------------------------------------------------------------------------------------------------- identity chain
class IdentityChain(Base):
    def _tg(self, path: str, rm_token: str, workload: str = "orchestrator"):
        w = Workload(self.stack, workload)
        return w.request("GET", f"{self.stack.url('tool-gateway')}{path}",
                         headers={"authorization": "Bearer " + w.app_token("tool-gateway"), "x-rm-authorization": "Bearer " + rm_token})

    def test_user_token_attacks_on_tool_gateway(self):  # held up
        entra = self.stack.components["entra"]
        good = jose.decode_unverified(rm_tool_token(self.stack, LAYLA))
        cases = {
            "web-bff audience": entra.issue_user_token(LAYLA),
            "expired beyond leeway": rm_tool_token(self.stack, LAYLA, lifetime=-120),
            "alg none": jose.b64u(json.dumps({"alg": "none", "kid": entra.kid}).encode()) + "." + jose.b64u(json.dumps(good.claims).encode()) + ".",
        }
        pub = json.dumps(jose.rsa_jwk(entra._key.public_key(), entra.kid)).encode()
        hs_hdr = {"alg": "HS256", "kid": entra.kid, "typ": "JWT"}
        si = jose.signing_input(hs_hdr, good.claims)
        cases["HS256 keyed with the RSA public key"] = si.decode() + "." + jose.b64u(hmac.new(pub, si, hashlib.sha256).digest())
        tampered = {**good.claims, "oid": user_oid(OMAR)}
        cases["oid swapped, signature kept"] = jose.signing_input(good.header, tampered).decode() + "." + jose.b64u(good.signature)
        for name, tok in cases.items():
            r = self._tg("/tools/customers/harbourline/profile", tok)
            self.assertEqual(r.status, 401, f"{name}: {r.body[:150]}")
        r = self._tg("/tools/customers/harbourline/profile", rm_tool_token(self.stack, LAYLA), workload="outbox-worker")
        self.assertEqual(r.status, 403)  # only the orchestrator workload may call the read tools
        r = self._tg("/tools/customers/gulfstar/brief-inputs", rm_tool_token(self.stack, LAYLA))
        self.assertEqual((r.status, r.json()["error"]["code"]), (403, "NOT_ENTITLED"))

    def test_concurrent_double_commit_of_one_approval_writes_once(self):  # held up
        tok = mint_approval(self.stack, LAYLA, "harbourline", DRAFT)
        statuses: list[int] = []
        jtis: list[str] = []

        def go():
            r = commit_direct(self.stack, LAYLA, "harbourline", DRAFT, tok)
            statuses.append(r.status)
            jtis.append(r.json().get("jti") or r.json()["error"].get("jti"))
        threads = [threading.Thread(target=go) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(sorted(statuses), [201] + [409] * 7)
        self.assertEqual(len(crm_records(self.stack, jtis[0])), 2)

    def test_apim_takes_the_rm_quota_key_only_from_the_orchestrator_and_tool_gateway(self):  # regression
        apim = self.stack.components["apim"]
        ing = Workload(self.stack, "ingestion-worker")
        hdr = {"authorization": "Bearer " + ing.app_token("apim"), "x-rm-oid": user_oid(LAYLA)}
        r = ing.request("POST", f"{self.stack.url('apim')}/openai/deployments/embed/embeddings?api-version=2024-10-21", headers=hdr,
                        json_body={"input": "x " * 4000})
        self.assertEqual(r.status, 200)
        self.assertNotIn(user_oid(LAYLA), apim._minute, "a non-orchestrator workload spent an RM's token quota")
        r = ing.request("POST", f"{self.stack.url('apim')}/openai/deployments/gen-small/chat/completions?api-version=2024-10-21", headers=hdr,
                        json_body={"messages": [{"role": "system", "content": "You route and rewrite questions for a policy search engine at a bank."},
                                                {"role": "user", "content": "x"}]})
        self.assertEqual(r.status, 403, "only the orchestrator may call the chat deployments")


# ---------------------------------------------------------------------------------------------------- input handling
class InputValidation(Base):
    def test_customer_id_cannot_alias_a_customer_or_inject_into_internal_urls(self):  # regression
        layla = client(self.stack)
        for cid in ("harbourline/brief-inputs?a=", "harbourline/profile?", "harbour line", "harbourline\n", "../meetings", "a" * 65):
            status, body, _ = layla.raw("POST", "/api/uc1/brief", {"customer_id": cid})
            self.assertEqual((status, body.get("error", {}).get("code")), (400, "VALIDATION_FAILED"), f"{cid!r}: {status} {str(body)[:120]}")
        for body in ({"customer_id": {"a": 1}}, ["harbourline"]):
            status, out, _ = layla.raw("POST", "/api/uc1/brief", body)
            self.assertEqual(status, 400, out)
        status, out, _ = layla.raw("GET", "/api/uc3/writes/%0d%0aX-Evil:1")
        self.assertIn(status, (400, 404), out)

    def test_crypto_shred_cannot_be_evaded_with_an_aliased_customer_id(self):  # regression (C21)
        layla = client(self.stack)
        layla.raw("POST", "/api/uc1/brief", {"customer_id": "harbourline/brief-inputs?a="})
        writer = self.stack.components["audit-writer"]
        writer.flush()
        content = Path(writer.dir) / "content"
        aliases = [p.name for p in content.iterdir() if p.name.startswith("audit-content-harbourline") and p.name != "audit-content-harbourline"]
        self.assertEqual(aliases, [], "Harbourline content was encrypted under a key that erasure of 'harbourline' does not shred")
        # The audit relay itself refuses customer references that are not plain ids (defence in depth).
        orch = Workload(self.stack, "orchestrator")
        r = orch.request("POST", f"{self.stack.url('tool-gateway')}/internal/v1/audit",
                         headers={"authorization": "Bearer " + orch.app_token("tool-gateway"),
                                  "x-rm-authorization": "Bearer " + rm_tool_token(self.stack, LAYLA, scp="Tools.Read")},
                         json_body={"events": [{"event": "brief_built", "customer": "harbourline/../x", "metadata": {}}]})
        self.assertEqual(r.status, 400, r.body[:200])

    def test_approved_draft_is_schema_validated(self):  # regression (LLD §4.4 step 4)
        layla = client(self.stack)
        bad = [["x"], {"call_report": {"x": 1}, "tasks": []}, {"call_report": "ok", "tasks": "abc"},
               {"call_report": "ok", "tasks": [{"title": f"T{i}", "due": "2026-12-01"} for i in range(11)]}]
        for draft in bad:
            status, body, _ = layla.raw("POST", "/api/uc3/approve", {"customer_id": "harbourline", "draft": draft}, headers=layla.approve_headers())
            self.assertEqual((status, body.get("error", {}).get("code")), (400, "VALIDATION_FAILED"), f"{draft!r}: {body}")
        many = {**DRAFT, "tasks": [{"type": "follow_up", "title": f"T{i}", "due": "2026-12-01"} for i in range(11)]}
        r = commit_direct(self.stack, LAYLA, "harbourline", many, mint_approval(self.stack, LAYLA, "harbourline", many))
        self.assertEqual(r.status, 400, r.body[:200])
        status, out, _ = layla.raw("POST", "/api/uc2/ask", ["not", "an", "object"])
        self.assertEqual(status, 400, out)


# ---------------------------------------------------------------------------------------------------- approval token
def _sign_custom(stack, header_extra: dict, claims: dict) -> str:
    wb = Workload(stack, "web-bff")
    hdr = {"authorization": "Bearer " + wb.app_token("vault")}
    kid = claims["kid"]

    def sign(si: bytes) -> bytes:
        r = wb.request("POST", f"{kid}/sign?api-version=7.4", headers=hdr, json_body={"alg": "ES256", "value": jose.b64u(hashlib.sha256(si).digest())})
        return jose.b64u_decode(r.json()["value"])
    return jose.encode({"alg": "ES256", "typ": "approval+jwt", "kid": kid, **header_extra}, claims, sign)


class ApprovalToken(Base):
    def _kid(self) -> str:
        wb = Workload(self.stack, "web-bff")
        return wb.request("GET", f"{self.stack.url('keyvault')}/keys/approval-signing?api-version=7.4",
                          headers={"authorization": "Bearer " + wb.app_token("vault")}).json()["key"]["kid"]

    def assertReason(self, r, reason):
        self.assertEqual(r.status, 403, r.body[:200])
        self.assertEqual(r.json()["error"].get("reason"), reason)

    def test_exact_claim_set_lifetime_and_header_members(self):  # regression (LLD §4.4 steps 6, 7, 9, 11)
        now = int(time.time())
        self.assertReason(commit_direct(self.stack, LAYLA, "harbourline", DRAFT,
                                        mint_approval(self.stack, LAYLA, "harbourline", DRAFT, scope="admin")), "unexpected_claim")
        self.assertReason(commit_direct(self.stack, LAYLA, "harbourline", DRAFT,
                                        mint_approval(self.stack, LAYLA, "harbourline", DRAFT, iat=now + 20, exp=now + 10)), "bad_lifetime")
        claims = approval_claims(user_oid(LAYLA), "harbourline", DRAFT, self._kid())
        del claims["schema"]
        self.assertReason(commit_direct(self.stack, LAYLA, "harbourline", DRAFT, _sign_custom(self.stack, {}, claims)), "missing_claim")
        claims = approval_claims(user_oid(LAYLA), "harbourline", DRAFT, self._kid())
        tok = _sign_custom(self.stack, {"jku": "https://evil.example/jwks"}, claims)
        self.assertReason(commit_direct(self.stack, LAYLA, "harbourline", DRAFT, tok), "bad_header")
        self.assertReason(commit_direct(self.stack, LAYLA, "harbourline", DRAFT, tok + "A" * 2100), "malformed")

    def test_unicode_equivalent_draft_verifies_and_nfc_text_is_written(self):  # regression (LLD §4.2 step 2)
        nfc = {**DRAFT, "call_report": unicodedata.normalize("NFC", "Café visit with Zoë; ok to proceed with the école account")}
        nfd = {**nfc, "call_report": unicodedata.normalize("NFD", nfc["call_report"])}
        self.assertNotEqual(nfc["call_report"], nfd["call_report"])
        r = commit_direct(self.stack, LAYLA, "harbourline", nfd, mint_approval(self.stack, LAYLA, "harbourline", nfc))
        self.assertEqual(r.status, 201, r.body[:200])
        report = next(x for x in crm_records(self.stack, r.json()["jti"]) if "text" in x)
        self.assertEqual(report["text"], nfc["call_report"])

    def test_approval_for_one_draft_cannot_carry_extra_or_retyped_fields(self):  # held up
        tok = mint_approval(self.stack, LAYLA, "harbourline", DRAFT)
        sneaky = {**DRAFT, "tasks": DRAFT["tasks"] + [{"type": "follow_up", "title": "Increase limit to AED 9m", "due": "2026-12-01"}]}
        r = commit_direct(self.stack, LAYLA, "harbourline", sneaky, tok)
        self.assertEqual((r.status, r.json()["error"].get("reason")), (403, "draft_mismatch"))
        r = commit_direct(self.stack, LAYLA, "crescent", DRAFT, tok)
        self.assertEqual((r.status, r.json()["error"].get("reason")), (403, "wrong_customer"))
        r = commit_direct(self.stack, LAYLA, "harbourline", {**DRAFT, "email": DRAFT["email"] + " "}, tok)
        self.assertEqual((r.status, r.json()["error"].get("reason")), (403, "draft_mismatch"))  # whitespace is significant


# ---------------------------------------------------------------------------------------------------- edge (web-bff)
class EdgeRequestContext(Base):
    def test_client_request_id_and_traceparent_are_not_trusted_at_the_edge(self):  # regression
        layla, omar = client(self.stack), client(self.stack, OMAR)
        pii_id = "Harbourline Logistics LLC 784-1985-1234567-1"
        status, body, headers = layla.raw("POST", "/api/uc2/ask", {"question": "When does high utilisation trigger a review?"},
                                          headers={"x-request-id": pii_id})
        self.assertEqual(status, 200)
        self.assertNotEqual(headers.get("x-request-id"), pii_id)
        for name, comp in self.stack.components.items():
            self.assertNotIn("784-1985", json.dumps(comp.app.telemetry.all_spans()), f"client request id reached {name} telemetry")
        b = layla.brief("harbourline")
        status, out, _ = omar.raw("POST", "/api/uc2/ask", {"question": "When does high utilisation trigger a review?"},
                                  headers={"traceparent": f"00-{b['trace_id']}-{'1' * 16}-01", "x-request-id": b["request_id"]})
        self.assertNotEqual(out["trace_id"], b["trace_id"], "Omar joined Layla's trace")
        self.assertEqual(layla.trace(b["request_id"])["trace_id"], b["trace_id"])  # Layla's trace view still hers


# ---------------------------------------------------------------------------------------------------- PII floor
class PiiFloor(Base):
    FORMATS = ("784.1985.1234567.1", "784 1985 1234567 1", "+971 50 123 45 67", "(050) 123-4567", "+971 (0)50 123 4567", "971501234567",
               "ae07 0331 2345 6789 0123 456", "AE07-0331-2345-6789-0123-456")

    def test_common_uae_formats_are_masked(self):  # regression
        for raw in self.FORMATS:
            masked, found = mask_pii(f"Contact {raw} today.")
            self.assertTrue(found, raw)
            for chunk in [c for c in raw.replace("(", " ").replace(")", " ").replace("-", " ").replace(".", " ").split() if len(c) >= 3]:
                self.assertNotIn(chunk, masked.replace("[", " "), f"{raw!r} -> {masked!r}")
        for benign in ("Due 2026-12-03, AED 4.6m of AED 5m, 45 days, 38% this quarter", "Call at 10:30 on 27 Nov", "F-209 and TF-102"):
            self.assertEqual(mask_pii(benign)[1], [], benign)

    def test_formats_never_reach_the_model(self):  # regression
        layla = client(self.stack)
        before = len(self.model_inputs())
        layla.draft("harbourline", "Met Mr Haddad. I promised to send the checklist by Friday. Reach him on " + ", ".join(self.FORMATS) + ".")
        layla.ask(f"Customer on {self.FORMATS[2]} asks: when does high utilisation trigger a review?")
        sent = self.model_inputs()[before:]
        self.assertTrue(sent)
        for raw in self.FORMATS:
            self.assertNotIn(raw, sent)


# ---------------------------------------------------------------------------------------------------- audit trail
class AuditWriterRobustness(Base):
    def test_restarted_writer_continues_the_existing_chain(self):  # regression
        run_dir = tempfile.mkdtemp(prefix="rmc-redteam-")
        try:
            cfg = StackConfig(Settings(run_dir=run_dir), dict(self.stack.cfg.urls), dict(self.stack.cfg.secrets))
            w1 = AuditWriter(cfg)
            for i in range(3):
                w1._write({"event": "tool_call", "metadata": {"i": i}}, None)
            w2 = AuditWriter(cfg)  # pod restart on the same store
            w2._write({"event": "tool_call", "metadata": {"i": 3}}, None)
            v = verify_chain(w2.chain_path)
            self.assertTrue(v["ok"], v)
            self.assertEqual(v["records"], 4)
        finally:
            shutil.rmtree(run_dir, ignore_errors=True)

    def test_shredded_content_stays_gone_after_new_content_for_the_same_customer(self):  # regression
        layla = client(self.stack)
        writer = self.stack.components["audit-writer"]
        layla.draft("crescent", "I promised to share the payroll proposal by Friday.")
        writer.flush()
        recs = [json.loads(x) for x in writer.chain_path.read_text(encoding="utf-8").splitlines()]
        old = [r for r in recs if r["event"]["event"] == "model_call" and r["event"]["customer"] == "crescent"][-1]["event"]["event_id"]
        ops = Workload(self.stack, "ops-admin")
        hdr = {"authorization": "Bearer " + ops.app_token("audit")}
        base = self.stack.url("audit-writer")
        self.assertEqual(ops.request("POST", f"{base}/audit/erasure", headers=hdr, json_body={"customer": "crescent"}).status, 200)
        layla.draft("crescent", "I promised to share the payroll proposal by Friday.")  # in-flight request after erasure
        writer.flush()
        r = ops.request("GET", f"{base}/audit/content/crescent/{old}", headers=hdr)
        self.assertEqual((r.status, r.json()["error"]["code"]), (410, "CONTENT_SHREDDED"), r.body[:200])
        self.assertTrue(verify_chain(writer.chain_path)["ok"])


# ---------------------------------------------------------------------------------------------------- console
class Console(Base):
    def test_console_recognises_the_step_up_error_web_bff_returns(self):  # regression
        c = client(self.stack)
        c.token = self.stack.components["entra"].issue_user_token(LAYLA, auth_age_s=3600)
        status, body, _ = c.raw("POST", "/api/uc3/approve", {"customer_id": "harbourline", "draft": DRAFT}, headers=c.approve_headers())
        code = body["error"]["code"]
        self.assertEqual((status, code), (401, "STEP_UP_REQUIRED"))
        js = (Path(__file__).resolve().parents[1] / "services" / "web" / "console.js").read_text(encoding="utf-8")
        self.assertTrue(f'"{code}"' in js, "the console never re-authenticates: it looks for a different error code")
        self.assertFalse(any(s in js for s in (".innerHTML", ".outerHTML", "insertAdjacentHTML", "document.write", "eval(")))


if __name__ == "__main__":
    unittest.main()
