"""Diagram box: API gateway + identity (lane A), the verified caller.
Identity is what the gateway hands to every later step after checking the OIDC token.
The token itself comes from a mock identity provider (mocks/identity/oidc_mock.py).
"""
from __future__ import annotations

from enum import Enum

from pydantic import BaseModel


class Role(str, Enum):
    RM = "rm"
    CREDIT_DESK = "credit_desk"


class Identity(BaseModel):
    """The signed-in user. The raw token travels with it so the tool gateway can re-check it per call."""

    user_id: str
    name: str
    roles: list[Role]
    token: str

    def has_role(self, role: Role) -> bool:
        return role in self.roles
