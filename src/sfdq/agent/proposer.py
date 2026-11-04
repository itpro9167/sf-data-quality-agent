"""The proposal agent: runs the tool-use loop for each finding and queues the result.

Control flow is plain Python, not a framework: ask the model, execute the read-only tools it
requests, feed results back, stop when ``submit_proposal`` is accepted by the write policy. If
the model never produces an acceptable proposal (refusal, token limit, repeated invalid output),
we queue a conservative *flag-for-review* proposal instead, so no finding silently disappears.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass

from sfdq.agent.llm import LLMClient, LLMError, LLMFatalError, LLMTurn
from sfdq.agent.prompts import NUDGE, SYSTEM_PROMPT, build_user_message
from sfdq.agent.tools import ToolBox
from sfdq.models import ActionType, Confidence, Finding, ProposalDraft
from sfdq.policy import WritePolicy
from sfdq.queue.store import DuplicateProposal, QueueStore
from sfdq.rules import Rules
from sfdq.salesforce.base import SalesforceClient

log = logging.getLogger(__name__)

MAX_CONSECUTIVE_LLM_ERRORS = 3


@dataclass
class ProposeStats:
    """Totals for one ``propose`` run (printed by the CLI)."""

    proposed: int = 0
    fallbacks: int = 0
    skipped: int = 0
    llm_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0


class FixProposer:
    """Turns a :class:`Finding` into a :class:`ProposalDraft` using an LLM with read-only tools."""

    def __init__(
        self,
        llm: LLMClient,
        sf: SalesforceClient,
        rules: Rules,
        policy: WritePolicy | None = None,
        max_turns: int = 8,
    ) -> None:
        self._llm = llm
        self._sf = sf
        self._rules = rules
        self._policy = policy or WritePolicy(rules)
        self._max_turns = max_turns

    def propose(self, finding: Finding, stats: ProposeStats | None = None) -> ProposalDraft:
        """Return a policy-compliant proposal for ``finding`` (never raises for model failures).

        Raises:
            LLMFatalError: if the LLM itself is unusable (bad credentials, unknown model).
        """
        stats = stats if stats is not None else ProposeStats()
        toolbox = ToolBox(self._sf, finding, self._policy, self._llm.name, self._llm.model)
        messages: list[dict[str, object]] = [
            {"role": "user", "content": build_user_message(finding, self._rules)}
        ]
        nudged = False
        for _ in range(self._max_turns):
            turn = self._complete(messages, toolbox, stats)
            messages.append({"role": "assistant", "content": turn.content})
            if turn.stop_reason == "refusal":
                return self.fallback(finding, "the model declined to answer")
            if turn.tool_calls:
                results = [(call, toolbox.run(call)) for call in turn.tool_calls]
                if toolbox.accepted is not None:
                    return toolbox.accepted
                # All tool results go back together in ONE user message.
                messages.append(
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": call.id,
                                "content": result.content,
                                **({"is_error": True} if result.is_error else {}),
                            }
                            for call, result in results
                        ],
                    }
                )
                continue
            if turn.stop_reason == "max_tokens":
                return self.fallback(finding, "the model ran out of output tokens")
            if nudged:
                break
            nudged = True
            messages.append({"role": "user", "content": NUDGE})
        return self.fallback(finding, "the model did not submit a valid proposal")

    # -- helpers ---------------------------------------------------------------------------
    def _complete(
        self, messages: list[dict[str, object]], toolbox: ToolBox, stats: ProposeStats
    ) -> LLMTurn:
        turn = self._llm.complete(
            system=SYSTEM_PROMPT, messages=messages, tools=toolbox.definitions
        )
        stats.llm_calls += 1
        stats.input_tokens += turn.input_tokens
        stats.output_tokens += turn.output_tokens
        return turn

    def fallback(self, finding: Finding, reason: str) -> ProposalDraft:
        log.warning("falling back to flag_for_review for %s: %s", finding.id, reason)
        return ProposalDraft(
            finding_id=finding.id,
            action=ActionType.FLAG_FOR_REVIEW,
            confidence=Confidence.LOW,
            explanation=(
                f"Automated analysis could not produce a fix ({reason}). "
                f"The scanner reported: {finding.summary}. A person should take a look."
            ),
            review_note=f"Please review this data-quality finding manually: {finding.summary}",
            source="fallback",
            model=self._llm.model,
        )


def propose_all(
    findings: Iterable[Finding],
    proposer: FixProposer,
    store: QueueStore,
    actor: str,
    on_progress: Callable[[Finding, ProposalDraft], None] | None = None,
) -> ProposeStats:
    """Propose a fix for each finding and queue it as ``pending``.

    Aborts (re-raising :class:`LLMFatalError`) when the LLM is unusable or fails repeatedly;
    proposals queued before that point are kept.
    """
    stats = ProposeStats()
    consecutive_errors = 0
    for finding in findings:
        try:
            draft = proposer.propose(finding, stats)
            consecutive_errors = 0
        except LLMFatalError:
            raise
        except LLMError as exc:
            consecutive_errors += 1
            log.warning("LLM error on %s: %s", finding.id, exc)
            if consecutive_errors >= MAX_CONSECUTIVE_LLM_ERRORS:
                raise LLMFatalError(
                    f"{consecutive_errors} consecutive LLM failures; last error: {exc}"
                ) from exc
            draft = proposer.fallback(finding, f"LLM error: {exc}")
        try:
            store.add_proposal(draft, actor)
        except DuplicateProposal:
            stats.skipped += 1
            continue
        stats.proposed += 1
        stats.fallbacks += draft.source == "fallback"
        if on_progress:
            on_progress(finding, draft)
    return stats
