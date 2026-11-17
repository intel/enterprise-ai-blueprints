# Copyright (C) 2025-2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""
The decision: display System-1's yes/no, or escalate to the agent.

    answer    -> the gate replies "Yes." or "No." itself; no LLM call at all
    escalate  -> banking-assistant, the LLM agent with the MCP banking tools

The rules, in order -- the first that fires decides:

  1. Every question asked must come back as a usable noul answer (a finite probability and
     confidence in [0, 1]). Anything else escalates: a broken response must never be shown as
     a confident answer.
  2. LABEL MASS (when SYSTEM1_MIN_LABEL_MASS > 0): a question whose label_mass from vLLM is below it
     escalates -- the model's reply was mostly not yes or no, so its P(yes) means little.
  3. VETO: if System-1 thinks the customer is asking for something to be DONE (P(action) above
     SYSTEM1_MAX_P_ACTION), escalate. "Can you transfer $200?" must never be answered "Yes.".
  4. Display "Yes." if P(yes) >= SYSTEM1_YES_MIN, "No." if P(yes) <= SYSTEM1_NO_MAX.
  5. Anything between the two cut-offs escalates: the design's "System-1 cannot answer yes/no with
     confidence" branch.

The two cut-offs are the deployment's CALIBRATION, fitted on its own labelled questions
on a calibration set. They are not centred on 0.5 on purpose: System-1's P(yes) is
shifted for a given question style (on simple banking definitions the boundary sat near 0.10),
and centring on 0.5 turned a model that ranks yes above no (AUC 0.92) into one that says "No"
to almost everything.

Every decision carries a `reason`; group the decision log by it to see which rule is spending
the traffic.
"""

import math
from collections.abc import Iterable
from dataclasses import dataclass, field, replace

from . import config
from .questions import ANSWER, IS_ACTION_REQUEST

ESCALATE = "escalate"
ANSWERED = "answer"


@dataclass
class Decision:
    route: str                       # ESCALATE or ANSWERED
    reason: str                      # which rule fired; the decision log groups on this
    confident: bool = True           # did P(yes) clear one of the two display cut-offs?
    scores: dict[str, float] = field(default_factory=dict)        # qid -> P(true)
    confidences: dict[str, float] = field(default_factory=dict)   # qid -> answer_confidence
    answer: str | None = None     # "yes" / "no": what System-1 would display, even if it is not
    system1_ms: float | None = None
    routing_model: str | None = None   # checkpoint that actually served the answers

    @property
    def escalated(self) -> bool:
        return self.route == ESCALATE


def _prob(v) -> float | None:
    """A finite probability in [0, 1], or None. Never raises: NaN, inf, strings and bools are
    all unusable, and an unusable number must escalate rather than slip past a comparison
    (NaN < 0.9 and NaN > 0.4 are both False, so a NaN would otherwise pass every check)."""
    if isinstance(v, bool):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) and 0.0 <= f <= 1.0 else None


def decide(answers: dict[str, dict], asked: Iterable[str],
           system1_ms: float | None = None) -> Decision:
    """Apply the rules above to System-1's answers.

    `asked` is the question ids that were put to System-1, and it is what the answers are checked
    against: an answer System-1 left out escalates. Checking only what came back would let a
    partial response through as if it covered everything asked.
    """
    asked = list(asked)
    scores, confs, invalid = {}, {}, []
    for qid in asked:
        a = answers.get(qid) if isinstance(answers, dict) else None
        p = _prob(a.get("noul")) if isinstance(a, dict) and a.get("type") == "noul" else None
        c = _prob(a.get("answer_confidence")) if isinstance(a, dict) else None
        if p is None or c is None:
            invalid.append(qid)
            continue
        scores[qid], confs[qid] = p, c

    d = Decision(route=ESCALATE, reason="", scores=scores, confidences=confs, system1_ms=system1_ms)
    if ANSWER in scores:
        # what System-1 leans to, for the decision log: the side of the calibrated boundary it is on
        mid = (config.YES_MIN + config.NO_MAX) / 2.0
        d.answer = "yes" if scores[ANSWER] >= mid else "no"

    if invalid:
        d.reason, d.confident = "invalid_answer:{}".format(",".join(sorted(invalid))), False
        return d

    # 2. label mass: on for a deployment that has fitted SYSTEM1_MIN_LABEL_MASS. An answer with no
    # label_mass then escalates too: the check cannot be made, and the gate errs toward the agent.
    if config.MIN_LABEL_MASS > 0:
        low = sorted(q for q in asked if not (isinstance(answers[q].get("label_mass"), (int, float))
                                              and answers[q]["label_mass"] >= config.MIN_LABEL_MASS))
        if low:
            d.reason, d.confident = "low_label_mass:{}".format(",".join(low)), False
            return d

    # 3. the veto: a request to DO something is never answered with a bare yes/no
    if IS_ACTION_REQUEST in scores and scores[IS_ACTION_REQUEST] > config.MAX_P_ACTION:
        d.reason = "needs_agent:is_action_request"
        return d

    if ANSWER not in asked:
        d.reason, d.confident = "no_answer_question", False
        return d

    # 4. past a calibrated cut-off: display it
    p = scores[ANSWER]
    if p >= config.YES_MIN or p <= config.NO_MAX:
        d.answer = "yes" if p >= config.YES_MIN else "no"
        d.route, d.reason = ANSWERED, f"answered:{d.answer}"
        return d

    # 5. between the cut-offs: not confident -> the LLM agent
    d.reason, d.confident = "low_confidence:answer", False
    return d


def decision_for_failure(detail: str) -> Decision:
    """System-1 unreachable, slow, busy or malformed. Fail OPEN: escalate."""
    return Decision(route=ESCALATE, reason=f"system1_unavailable:{detail}", confident=False)


def decision_for_disabled() -> Decision:
    return Decision(route=ESCALATE, reason="gate_disabled")


def apply_shadow(d: Decision) -> Decision:
    """Shadow mode: keep the decision -- including the answer System-1 WOULD have displayed -- for
    the decision log, but always serve from the agent.

    This is how a first deployment should run: no customer-visible path changes. Each row then
    pairs System-1's would-be answer with the agent's reply and the tools it called (agent_tools),
    which is the material for measuring displayed-answer accuracy and for calibrating System-1 on
    the customer's real traffic.
    """
    if d.route == ANSWERED:
        return replace(d, route=ESCALATE, reason=f"shadow:{d.reason}")
    return d
