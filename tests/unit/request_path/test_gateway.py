"""Diagram box: API gateway + identity (lane A).
Proves the gateway verifies the OIDC token and enforces per-user rate and daily token limits.
"""
from __future__ import annotations

import pytest

from copilot.contracts import AnswerStatus, Trace
from copilot.request_path.gateway import ApiGateway, GatewayDenied
from helpers import FEE_QUESTION, ask
from mocks.data import load_json
from mocks.identity.oidc_mock import MockIdentityProvider


class Clock:
    def __init__(self, now: float = 1_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def idp() -> MockIdentityProvider:
    return MockIdentityProvider(load_json("customers.json"))


def test_valid_token_yields_the_verified_identity(idp):
    identity = ApiGateway(idp).admit(idp.issue_token("rm-1"), Trace())
    assert (identity.user_id, [r.value for r in identity.roles]) == ("rm-1", ["rm"])


def test_malformed_tampered_and_expired_tokens_are_rejected_with_401(idp):
    header, _, signature = idp.issue_token("rm-1").split(".")
    forged = f"{header}.{idp.issue_token('rm-2').split('.')[1]}.{signature}"   # rm-2 claims, rm-1 signature
    expired = MockIdentityProvider(load_json("customers.json"), lifetime_s=-1).issue_token("rm-1")
    for token in ("", "not-a-jwt", forged, expired):
        trace = Trace()
        with pytest.raises(GatewayDenied) as denied:
            ApiGateway(idp).admit(token, trace)
        assert denied.value.status_code == 401 and trace.spans[0].status == "denied"


def test_rate_limit_is_per_user_and_frees_up_after_a_minute(idp):
    clock = Clock()
    gateway = ApiGateway(idp, requests_per_minute=1, clock=clock)
    gateway.admit(idp.issue_token("rm-1"), Trace())
    with pytest.raises(GatewayDenied) as denied:
        gateway.admit(idp.issue_token("rm-1"), Trace())
    assert denied.value.status_code == 429
    assert gateway.admit(idp.issue_token("rm-2"), Trace()).user_id == "rm-2"
    clock.now += 61
    assert gateway.admit(idp.issue_token("rm-1"), Trace()).user_id == "rm-1"


def test_tokens_spent_by_the_pipeline_count_toward_the_daily_limit(copilot):
    copilot.pipeline.gateway = ApiGateway(copilot.idp, daily_token_limit=1)
    assert ask(copilot, "rm-1", FEE_QUESTION).status == AnswerStatus.ANSWERED
    second = ask(copilot, "rm-1", FEE_QUESTION)
    assert (second.status, second.text) == (AnswerStatus.DENIED, "Daily usage limit reached.")


@pytest.mark.xfail(strict=True, reason="BUG request_path/gateway.py ApiGateway._tokens_today: the daily token "
                                       "counter is never reset, so an RM who hits the limit stays locked out on "
                                       "later days")
def test_daily_token_limit_resets_the_next_day(idp):
    clock = Clock()
    gateway = ApiGateway(idp, daily_token_limit=1_000, clock=clock)
    gateway.record_tokens("rm-1", 1_000)
    clock.now += 24 * 3600 + 1
    assert gateway.admit(idp.issue_token("rm-1"), Trace()).user_id == "rm-1"
