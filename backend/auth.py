"""Employee sign-in (OIDC single sign-on) and group-based access control.

Production: set OIDC_ISSUER / OIDC_CLIENT_ID / OIDC_CLIENT_SECRET to use your
company identity provider (Google Workspace, Microsoft Entra ID, Okta, ...).

Local development: with no OIDC settings, AUTH_MODE defaults to "dev", which
shows a "pick a demo account" sign-in. Never run dev mode on a real server.

Who belongs to which group is defined in config/access.json. If the identity
provider sends a "groups" claim, those groups are added too.
"""
from __future__ import annotations

import fnmatch
import hashlib
import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path

from fastapi import HTTPException, Request

log = logging.getLogger("hr_assist.auth")

EVERYONE = "everyone"
ADMIN = "hr-admin"


@dataclass
class User:
    email: str
    name: str
    groups: set[str]

    @property
    def is_admin(self) -> bool:
        return ADMIN in self.groups

    def public(self) -> dict:
        return {"email": self.email, "name": self.name, "groups": sorted(self.groups), "is_admin": self.is_admin}


class AccessConfig:
    """Loads config/access.json: allowed email domains, group membership, default document access."""

    def __init__(self, path: Path):
        self.path = path
        cfg = json.loads(path.read_text()) if path.exists() else {}
        self.allowed_domains = [d.lower() for d in cfg.get("allowed_domains", [])]
        self.groups: dict[str, list[str]] = cfg.get("groups", {})
        self.documents: dict[str, list[str]] = cfg.get("documents", {})
        self.default_document_groups: list[str] = cfg.get("default_document_groups", [ADMIN])
        self.demo_users: list[dict] = cfg.get("demo_users", [])

    def domain_allowed(self, email: str) -> bool:
        domain = email.rsplit("@", 1)[-1].lower()
        return not self.allowed_domains or domain in self.allowed_domains

    def groups_for(self, email: str, idp_groups: list[str] | None = None) -> set[str]:
        email = email.lower()
        groups = {EVERYONE, *(idp_groups or [])}
        for group, members in self.groups.items():
            if any(fnmatch.fnmatch(email, m.lower()) for m in members):  # "*@hr.qorvexa.com" works
                groups.add(group)
        return groups

    def all_groups(self) -> list[str]:
        return sorted({EVERYONE, ADMIN, *self.groups})


def can_read(user: User, doc_groups: list[str]) -> bool:
    return user.is_admin or bool(user.groups & set(doc_groups))


def pseudonym(email: str, secret: str) -> str:
    """Stable, non-reversible id so feedback can be tied to a question without storing emails."""
    return hashlib.sha256(f"{secret}:{email.lower()}".encode()).hexdigest()[:16]


# ---------- session helpers ----------
def login(request: Request, email: str, name: str, groups: set[str]) -> None:
    request.session["user"] = {"email": email, "name": name, "groups": sorted(groups)}


def current_user(request: Request) -> User | None:
    data = request.session.get("user")
    if not data:
        return None
    return User(data["email"], data["name"], set(data["groups"]))


def require_user(request: Request) -> User:
    user = current_user(request)
    if not user:
        raise HTTPException(401, "Please sign in.")
    return user


def require_admin(request: Request) -> User:
    user = require_user(request)
    if not user.is_admin:
        raise HTTPException(403, "HR admin access required.")
    return user


# ---------- OIDC ----------
class OIDC:
    def __init__(self):
        self.issuer = os.getenv("OIDC_ISSUER", "").rstrip("/")
        self.client_id = os.getenv("OIDC_CLIENT_ID", "")
        self.client_secret = os.getenv("OIDC_CLIENT_SECRET", "")
        self.provider_name = os.getenv("OIDC_PROVIDER_NAME", "SSO")
        self.client = None
        if self.issuer and self.client_id:
            from authlib.integrations.starlette_client import OAuth

            oauth = OAuth()
            oauth.register(
                name="sso",
                server_metadata_url=f"{self.issuer}/.well-known/openid-configuration",
                client_id=self.client_id,
                client_secret=self.client_secret,
                client_kwargs={"scope": os.getenv("OIDC_SCOPES", "openid email profile")},
            )
            self.client = oauth.sso

    @property
    def enabled(self) -> bool:
        return self.client is not None


def auth_mode(oidc: OIDC) -> str:
    mode = os.getenv("AUTH_MODE", "oidc" if oidc.enabled else "dev").lower()
    if mode == "oidc" and not oidc.enabled:
        raise RuntimeError("AUTH_MODE=oidc but OIDC_ISSUER / OIDC_CLIENT_ID are not set.")
    if mode == "dev":
        log.warning("AUTH_MODE=dev: anyone can sign in as a demo user. Do not use in production.")
    return mode
