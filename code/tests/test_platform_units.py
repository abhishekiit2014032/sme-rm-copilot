"""Unit tests for the platform building blocks (no stack needed)."""
from __future__ import annotations

import hashlib
import json
import time
import unittest

from azmock.content_safety import HARM
from azmock.language import find_entities
from azmock.search import matches, parse_filter
from rm_copilot import guardrails as g
from rm_copilot import tools as t
from rm_copilot.llm import BRIEF_SYSTEM, MockLLM, ModelUnavailable
from rm_copilot.orchestrator import Copilot
from services.common import jose, p256, rsa
from services.common.contracts import draft_sha256, outlook_compose_link
from services.common.envelope import seal, seal_envelope, unseal
from services.common.httpkit import HttpError
from services.common.resilience import CircuitBreaker
from services.common.telemetry import Metrics, Telemetry, parse_traceparent, safe_fields


class Crypto(unittest.TestCase):
    def test_es256_matches_rfc6979_test_vector(self):
        # RFC 6979 A.2.5, P-256, SHA-256, message "sample"
        key = p256.PrivateKey(0xC9AFA9D845BA75166B5C215767B1D6934E50C3DB36E89B127B8A622B120F6721)
        self.assertEqual(key.public_key().x, 0x60FED4BA255A9D31C961EB74C6356D68C049B8923B61FA6CE669622E60F29FB6)
        sig = key.sign(hashlib.sha256(b"sample").digest())
        self.assertEqual(sig.hex().upper(), "EFD48B2AACB6A8FD1140DD9CD45E81D69D2C877B56AAF991C34D0EA84EAF3716"
                                            "F7CB1C942D657C41D436C7A1B6E29F65F3E900DBB9AFF4064DC4AB2F843ACDA8")
        self.assertTrue(key.public_key().verify(hashlib.sha256(b"sample").digest(), sig))
        self.assertFalse(key.public_key().verify(hashlib.sha256(b"sampl3").digest(), sig))
        self.assertFalse(p256.PrivateKey.generate().public_key().verify(hashlib.sha256(b"sample").digest(), sig))

    def test_rsa_sign_verify_and_oaep(self):
        k = rsa.PrivateKey.generate(1024)
        s = k.sign(b"payload")
        self.assertTrue(k.public_key().verify(b"payload", s))
        self.assertFalse(k.public_key().verify(b"payloaD", s))
        dek = bytes(range(32))
        self.assertEqual(k.decrypt_oaep(k.public_key().encrypt_oaep(dek)), dek)

    def test_jose_rejects_none_hmac_and_unknown_kid(self):
        k = p256.PrivateKey.generate()
        keys = {"k1": jose.VerifyingKey.from_jwk(jose.ec_jwk(k.public_key(), "k1"))}
        tok = jose.encode({"alg": "ES256", "kid": "k1"}, {"a": 1}, lambda si: k.sign(hashlib.sha256(si).digest()))
        self.assertEqual(jose.verify(tok, keys).claims, {"a": 1})
        for header in ({"alg": "none", "kid": "k1"}, {"alg": "HS256", "kid": "k1"}, {"alg": "ES256", "kid": "k2"}, {"alg": "RS256", "kid": "k1"}):
            bad = jose.signing_input(header, {"a": 1}).decode() + "." + tok.split(".")[2]
            with self.assertRaises(jose.JoseError):
                jose.verify(bad, keys)

    def test_envelope_and_mock_cipher(self):
        k = rsa.PrivateKey.generate(1024)
        jwk = {**jose.rsa_jwk(k.public_key(), "kv/audit-transport/1")}
        env = seal_envelope(jwk, b'{"input": "masked"}')
        dek = k.decrypt_oaep(jose.b64u_decode(env["wrapped_dek"]))
        self.assertEqual(unseal(dek, jose.b64u_decode(env["ciphertext"])), b'{"input": "masked"}')
        blob = bytearray(seal(b"k" * 32, b"secret"))
        blob[10] ^= 1
        with self.assertRaises(ValueError):
            unseal(b"k" * 32, bytes(blob))


class Contracts(unittest.TestCase):
    DRAFT = {"call_report": "Met the CFO.", "tasks": [{"title": "Send checklist", "due": "2026-11-27"}], "email": "Dear Ms Rao"}

    def test_canonical_hash_is_order_independent_and_binds_everything(self):
        h = draft_sha256("crescent", self.DRAFT)
        reordered = {"email": "Dear Ms Rao", "tasks": [{"due": "2026-11-27", "title": "Send checklist"}], "call_report": "Met the CFO."}
        self.assertEqual(draft_sha256("crescent", reordered), h)
        self.assertNotEqual(draft_sha256("harbourline", self.DRAFT), h)
        self.assertNotEqual(draft_sha256("crescent", {**self.DRAFT, "email": "Dear Ms Rao,"}), h)
        self.assertNotEqual(draft_sha256("crescent", {**self.DRAFT, "tasks": [{**self.DRAFT["tasks"][0], "type": "credit_action"}]}), h)
        self.assertEqual(jose.canonical_json({"b": "é", "a": [1, {"d": 1, "c": 2}]}), '{"a":[1,{"c":2,"d":1}],"b":"é"}'.encode())

    def test_compose_link_carries_only_text(self):
        link = outlook_compose_link("Follow-up", "Dear Mr Haddad,\nthanks")
        self.assertTrue(link.startswith("https://outlook.office.com/mail/deeplink/compose?subject=Follow-up&body=Dear%20Mr%20Haddad"))


class MockServiceLogic(unittest.TestCase):
    def test_search_filter_subset(self):
        doc = {"status": "current", "clearance_rank": 1, "effective_from": "2026-01-01T00:00:00Z"}
        f = parse_filter("status eq 'current' and clearance_rank le 1 and effective_from le 2026-11-26T00:00:00Z")
        self.assertTrue(matches(doc, f))
        self.assertFalse(matches({**doc, "effective_from": "2027-03-01T00:00:00Z"}, f))
        self.assertFalse(matches({**doc, "clearance_rank": 2}, f))
        self.assertFalse(matches({**doc, "status": "superseded"}, f))
        with self.assertRaises(HttpError):
            parse_filter("status eq 'current' or clearance_rank le 1")

    def test_banking_language_is_not_harmful(self):
        text = "Use the kill switch if exposure to the defaulted obligor grows; execute the transfer and attack the backlog."
        self.assertEqual([c for c, rules in HARM.items() if any(rx.search(text) for rx, _ in rules)], [])

    def test_pii_entities_for_language_mock(self):
        ents = find_entities("ID 784-1985-1234567-1, IBAN AE07 0331 2345 6789 0123 456, call +971 50 123 4567, TL-123456")
        self.assertEqual([e["category"] for e in ents], ["EmiratesIdNumber", "InternationalBankingAccountNumber", "PhoneNumber", "TradeLicenceNumber"])


class GuardrailAdditions(unittest.TestCase):
    def test_number_words_english_and_arabic(self):
        self.assertTrue(g.free_numbers("Utilisation is ninety percent"))
        self.assertTrue(g.free_numbers("الاستخدام خمسة وتسعون بالمائة"))
        self.assertTrue(g.free_numbers("٩٢٪"))
        self.assertEqual(g.free_numbers("Ask whether the extra need on the {type_F-209} is seasonal; someone often asks."), [])

    def test_uc1_policy_check_drops_promises(self):
        class Promising(MockLLM):
            def complete(self, system, user):
                if system.startswith(BRIEF_SYSTEM[:40]):
                    return json.dumps(["We can approve a higher limit on the {type_F-209}.", "Collect the renewed {doc_0_name} before {doc_0_expiry}."])
                return super().complete(system, user)
        b = Copilot(Promising()).brief(t.User.load("layla"), "harbourline")
        self.assertEqual(len(b["rejected_model_output"]), 1)
        self.assertTrue(all("approve" not in p for p in b["talking_points"]))

    def test_degraded_modes_in_process(self):
        class Down:
            def complete(self, system, user):
                raise ModelUnavailable("all backends down")
        cp, layla = Copilot(Down()), t.User.load("layla")
        b = cp.brief(layla, "harbourline")
        self.assertTrue(b["degraded"])
        self.assertEqual(b["talking_points"], [])
        self.assertTrue(b["watch"])
        a = cp.ask(layla, "Can a company trading for 2 years get an unsecured working capital line?")
        self.assertEqual(a["status"], "degraded")
        self.assertTrue(a["passages"])
        d = cp.draft_followup(layla, "harbourline", "I promised to send the checklist by Friday.")
        self.assertTrue(d["degraded"])

    def test_uc2_retries_only_with_enough_deadline_left(self):
        from rm_copilot.llm import CarelessLLM
        r = Copilot(CarelessLLM(), time_left=lambda: 2.0).ask(t.User.load("layla"), "Can a company trading for 2 years get an unsecured working capital line?")
        self.assertEqual(r["reason"], "answer failed verification and there is no time to retry")


class Telemetry_(unittest.TestCase):
    def test_traceparent_and_safe_fields(self):
        self.assertIsNotNone(parse_traceparent("00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"))
        self.assertIsNone(parse_traceparent("00-00000000000000000000000000000000-00f067aa0ba902b7-01"))
        self.assertIsNone(parse_traceparent("garbage"))
        self.assertEqual(safe_fields({"http.route": "/x", "prompt": "secret", "customer_name": "X", "rmc.count": 3, "rmc.reason": {"a": 1}}),
                         {"http.route": "/x", "rmc.count": 3})

    def test_spans_nest_and_record_errors_by_type_only(self):
        tel = Telemetry("svc")
        with self.assertRaises(ValueError):
            with tel.span("outer"):
                with tel.span("inner"):
                    raise ValueError("customer Harbourline said no")
        spans = tel.all_spans()
        self.assertEqual(spans[0]["parent_id"], spans[1]["span_id"])
        self.assertEqual(spans[1]["attributes"]["error.type"], "ValueError")
        self.assertNotIn("Harbourline", json.dumps(spans))

    def test_prometheus_rendering(self):
        m = Metrics()
        m.counter("x_total", "help", ("a",)).inc(a='q"uote')
        m.histogram("d_seconds", "help", ("r",)).observe(0.3, r="/p")
        text = m.render()
        self.assertIn('x_total{a="q\\"uote"} 1', text)
        self.assertIn('d_seconds_bucket{r="/p",le="+Inf"} 1', text)
        self.assertIn("# TYPE d_seconds histogram", text)

    def test_circuit_breaker(self):
        b = CircuitBreaker(failures=2, window_s=10, trip_s=0.2)
        b.failure()
        self.assertEqual(b.state(), "closed")
        b.failure()
        self.assertEqual(b.state(), "open")
        self.assertFalse(b.allow())
        time.sleep(0.25)
        self.assertTrue(b.allow())       # half-open trial
        self.assertFalse(b.allow())      # only one trial at a time
        b.success()
        self.assertEqual(b.state(), "closed")


if __name__ == "__main__":
    unittest.main()
