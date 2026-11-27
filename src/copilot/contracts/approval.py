"""Diagram box: Human in the loop (lane B), the approval token.
A signed, single-use, 5-minute token binds one RM to one set of drafts.
The signature is HMAC in this prototype; production signs with a key held in an HSM.
"""
from __future__ import annotations

from pydantic import BaseModel


class ApprovalToken(BaseModel):
    """Claims inside the token. The wire format is base64(claims) + "." + signature."""

    thread_id: str       # checkpoint thread to resume
    draft_ids: list[str]
    rm_id: str
    nonce: str
    expires_at: float    # unix seconds
