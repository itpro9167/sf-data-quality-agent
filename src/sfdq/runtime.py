"""Shared wiring for every front end (CLI, MCP server, eval): settings + rules + queue + clients."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

from sfdq.agent import build_llm_client
from sfdq.agent.llm import LLMClient
from sfdq.queue import QueueStore
from sfdq.rules import Rules, load_rules
from sfdq.salesforce.base import SalesforceClient
from sfdq.salesforce.factory import build_salesforce_client
from sfdq.settings import Settings, load_settings


@dataclass
class Runtime:
    """Everything one invocation needs, built from the environment."""

    settings: Settings
    rules: Rules
    store: QueueStore
    _sf: SalesforceClient | None = field(default=None, repr=False)

    def salesforce(self) -> SalesforceClient:
        """Return a Salesforce client (real: connected once and reused; mock: reloaded each call).

        Reloading the mock org on every call means a long-running process (the MCP server) sees
        changes that another process made, e.g. ``sfdq apply`` in a terminal.
        """
        if self.settings.salesforce_mode == "real":
            if self._sf is None:
                self._sf = build_salesforce_client(self.settings)
            return self._sf
        return build_salesforce_client(self.settings)

    def llm(self) -> LLMClient:
        """Claude when ``ANTHROPIC_API_KEY`` is set, otherwise the canned mock."""
        return build_llm_client(self.settings)


def build_runtime(rules_path: Path | None = None, *, load_env_file: bool = True) -> Runtime:
    """Load ``.env`` (optional), settings and rules, and open the queue database.

    Raises:
        sfdq.settings.ConfigError: unusable configuration.
        sfdq.rules.RulesError: missing or invalid rules file.
    """
    if load_env_file:
        load_dotenv()  # optional .env for real-mode credentials; never required
    settings = load_settings()
    rules = load_rules(rules_path or settings.rules_path)
    return Runtime(settings, rules, QueueStore(settings.db_path))
