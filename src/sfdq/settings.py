"""Runtime settings and mock/real mode detection.

Mock mode is the default. A component goes "real" only when its credentials are present:

* Salesforce: ``SF_USERNAME``, ``SF_PASSWORD`` and ``SF_SECURITY_TOKEN`` are all set.
* Claude: ``ANTHROPIC_API_KEY`` is set.

``SFDQ_MODE=mock`` forces both to mock even if credentials exist; ``SFDQ_MODE=real`` demands both
and fails loudly if any are missing.

``SFDQ_PUBLIC_DEMO=1`` marks a publicly hosted demo (see the Dockerfile). The dashboard is
unauthenticated, so this mode is only allowed with mock data: it refuses to start if real
credentials are present.
"""

from __future__ import annotations

import getpass
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

DEFAULT_MODEL = "claude-opus-5-5"

Mode = Literal["mock", "real"]


class ConfigError(RuntimeError):
    """Raised for unusable configuration (e.g. SFDQ_MODE=real without credentials)."""


@dataclass(frozen=True)
class SalesforceCredentials:
    """Username/password/security-token login for simple-salesforce."""

    username: str
    password: str = ""
    security_token: str = ""
    domain: str = "login"

    def __repr__(self) -> str:  # never leak secrets into logs or tracebacks
        return f"SalesforceCredentials(username={self.username!r}, domain={self.domain!r})"


@dataclass(frozen=True)
class Settings:
    """Everything the app needs to wire itself together."""

    home: Path
    salesforce_mode: Mode
    llm_mode: Mode
    model: str
    reviewer: str
    rules_path: Path | None = None
    salesforce: SalesforceCredentials | None = None
    anthropic_api_key: str | None = field(default=None, repr=False)  # never log secrets
    public_demo: bool = False

    @property
    def db_path(self) -> Path:
        """SQLite file holding findings, the fix queue and the audit log."""
        return self.home / "queue.db"

    @property
    def mock_org_path(self) -> Path:
        """JSON snapshot of the mock Salesforce org (so mock applies persist between commands)."""
        return self.home / "mock_org.json"

    def describe(self) -> str:
        """One-line banner describing which components are real."""
        llm = f"claude ({self.model})" if self.llm_mode == "real" else "mock"
        return f"Salesforce: {self.salesforce_mode} | LLM: {llm}"


def load_settings(env: Mapping[str, str] | None = None) -> Settings:
    """Build :class:`Settings` from environment variables (pure; does not read ``.env``)."""
    e = os.environ if env is None else env

    def get(name: str) -> str:
        return (e.get(name) or "").strip()

    forced = get("SFDQ_MODE").lower()
    if forced not in ("", "mock", "real"):
        raise ConfigError(f"SFDQ_MODE must be 'mock' or 'real', got {forced!r}")

    sf_creds = None
    if get("SF_USERNAME") and get("SF_PASSWORD") and get("SF_SECURITY_TOKEN"):
        sf_creds = SalesforceCredentials(
            username=get("SF_USERNAME"),
            password=get("SF_PASSWORD"),
            security_token=get("SF_SECURITY_TOKEN"),
            domain=get("SF_DOMAIN") or "login",
        )
    api_key = get("ANTHROPIC_API_KEY") or None

    if forced == "real":
        missing = []
        if sf_creds is None:
            missing.append("SF_USERNAME/SF_PASSWORD/SF_SECURITY_TOKEN")
        if api_key is None:
            missing.append("ANTHROPIC_API_KEY")
        if missing:
            raise ConfigError("SFDQ_MODE=real but missing: " + ", ".join(missing))

    public_demo = get("SFDQ_PUBLIC_DEMO").lower() in ("1", "true", "yes", "on")
    if public_demo and forced != "mock" and (sf_creds is not None or api_key is not None):
        raise ConfigError(
            "SFDQ_PUBLIC_DEMO=1 serves an unauthenticated dashboard, so it only runs on mock data: "
            "unset the SF_* / ANTHROPIC_API_KEY credentials or set SFDQ_MODE=mock"
        )
    use_mock = forced == "mock"
    home = Path(get("SFDQ_HOME") or ".sfdq").expanduser()
    return Settings(
        home=home,
        salesforce_mode="real" if sf_creds and not use_mock else "mock",
        llm_mode="real" if api_key and not use_mock else "mock",
        model=get("SFDQ_MODEL") or DEFAULT_MODEL,
        reviewer=get("SFDQ_REVIEWER") or _default_reviewer(),
        rules_path=Path(get("SFDQ_RULES")).expanduser() if get("SFDQ_RULES") else None,
        salesforce=None if use_mock else sf_creds,
        anthropic_api_key=None if use_mock else api_key,
        public_demo=public_demo,
    )


def _default_reviewer() -> str:
    try:
        return getpass.getuser()
    except Exception:
        return "reviewer"
