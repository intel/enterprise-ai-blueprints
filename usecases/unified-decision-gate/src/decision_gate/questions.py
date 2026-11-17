# Copyright (C) 2025-2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""
The questions the gate puts to System-1, both in ONE call over the banking question itself:

    state = <the banking question, as plain text>

  answer             the question's own yes/no answer. P(true) is P(the answer is yes). This is
                     what gets displayed when System-1 is confident.
  is_action_request  a VETO: "Can you transfer $200 to savings?" reads like a yes/no question but
                     asks for something to be done, and a displayed "Yes." would sound like
                     confirmation while nothing moves. On Qwen3-0.6B it has no signal (P ~1 on
                     every question), so the veto is off (SYSTEM1_MAX_P_ACTION=1.0) and the
                     gate does not ask this question.

The answer question is GENERIC on purpose: one wording for every banking yes/no question,
rather than a template per question type. How well it performs on a customer's real traffic is
then a matter of calibration and fine-tuning the System-1 model on their own labelled data.

Wording matters more than thresholds -- the same intent phrased two ways has measured AUC 0.274
against 0.912 with a System-1 model -- so change these strings only with a measurement on labelled
questions behind the change. On Qwen3-0.6B an earlier wording ranked below chance (test AUC 0.434).
The figures here come from the banking benchmark the defaults were fitted on (200 calibration and 391
test questions; not shipped with this blueprint). The "expert" wording below with a plain-text state
was chosen on the first 32 calibration questions. No system message is sent (SYSTEM_INSTRUCTIONS = None).
The test questions were not used to choose any setting.

Controls: P(yes) 0.93-0.99 on obvious questions ("Is the sky
green?" 0.974), and 3 of 5 negations ranked by meaning, by 0.001-0.008: the model mostly reads the
surface form. Thinking unset and enable_thinking false score identically.

`criteria` names both sides of each noul question so there is no doubt which way P(true) points.
"""


ANSWER = "answer"
IS_ACTION_REQUEST = "is_action_request"

CATALOGUE: dict[str, dict] = {
    ANSWER: {
        "type": "noul",
        "instructions": "A banking expert is asked the question above. Would the expert answer yes?",
        "criteria": {"true": "the expert answers yes", "false": "the expert answers no"},
    },
    IS_ACTION_REQUEST: {
        "type": "noul",
        "instructions": (
            "Is the customer in the message above asking to carry out an action on their account, "
            "such as transferring money, paying a bill, blocking a card or applying for "
            "something, rather than asking to be told something?"
        ),
        "criteria": {
            "true": "asking for an action to be carried out",
            "false": "asking to be told something",
        },
    },
}

# The request's top-level `instructions`, which vLLM sends as a system message; None sends none.
# Without one Qwen3-0.6B leans to yes (P(yes) 0.98-1.0, "Is the sky green?" 0.986). None: no system
# message is sent.
SYSTEM_INSTRUCTIONS: str | None = None

DEFAULT_QUESTION_IDS = (ANSWER, IS_ACTION_REQUEST)


def select(question_ids=None) -> dict[str, dict]:
    """The question payload for a /v1/systemone call. Unknown ids are ignored rather than
    erroring (an upstream typo must not take the assistant down); the answer question is
    always included, since without it there is nothing to display."""
    ids = [q for q in (question_ids or DEFAULT_QUESTION_IDS) if q in CATALOGUE]
    if ANSWER not in ids:
        ids.insert(0, ANSWER)
    return {q: CATALOGUE[q] for q in ids}


def build_state(text: str) -> str:
    """The state is the customer's question itself, as plain text: vLLM puts it before each question
    ("... the question above"). Chosen with the answer wording on the calibration questions."""
    return text
