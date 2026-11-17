# Unified Decision Gate: Implementation

A gating mechanism: a fast System-1 decision model decides each request in one forward pass,
and the gate either returns that decision or hands the request to the LLM agent. This page
covers how a request is routed, what System-1 is asked, how its scores become a displayed
"Yes." or "No.", and what happens when anything fails. The domain-specific parts are shown
with the banking example the use case ships with.

**Names.** *System-1* is the fast decision model in front of the LLM agent. The gate is the
`gate` service (code in `src/decision_gate`); its settings are `SYSTEM1_*` for the System-1 model
it asks and `GATE_*` for the gate itself, and its `X-Gate-*` headers carry the decision. System-1
is a vLLM *structured decisions* model
([vLLM docs](https://docs.vllm.ai/en/latest/models/structured_decisions_models/)), by default
**Qwen/Qwen3-0.6B** (0.75B parameters, BF16), deployed by Model Manager as the catalog model
`qwen3-0-6b-system1` (README, Deploy System-1). This use case deploys no System-1 container:
`SYSTEM1_URL` points at that model's route on the inference AI gateway.

The figures below (question counts, AUCs, cut-offs) come from the banking benchmark these defaults were
fitted on: 200 labelled calibration questions and 391 labelled test questions (263 plain yes/no, 128
that need the agent). That data and its scripts are not part of this blueprint; "re-measure" below means
measuring on your own labelled questions (shadow mode with `GATE_LOG_QUESTION_TEXT=true`, or your own
capture: by default the decision log keeps question hashes, not text).

---

## 1. Components

| Component | Container (Docker) | Port | What it does |
|---|---|---|---|
| `gate` | `udg-gate` | 8090 | The front door. OpenAI-compatible `/v1/chat/completions`; decides per question |
| System-1 (not deployed here) | none: a vLLM instance at `SYSTEM1_URL` | yours | `POST /v1/systemone`: one read per question returns P(yes) |
| `banking-assistant` | `udg-agent` | 8091 (8090 inside) | The LLM agent (Microsoft Agent Framework, DevUI, Responses API) |
| `banking-mcp` | `udg-mcp` | 8000 | FastMCP banking tools (balances, transactions, loans, transfers ...) |
| `redis-stack` | `udg-redis` | 6380 on the host | Agent memory |

In Kubernetes the pod holds gate + agent + MCP only; `gate.system1Url` is required.

### How the models are served

Both models run on vLLM, deployed by Intel Inference Toolkit's Model Manager. The serving LLM is
reached by the agent through LiteLLM. System-1 is a second model (one vLLM server serves one model),
reached by the gate over `POST /v1/systemone` on the inference AI gateway, not through LiteLLM,
which has no such route (section 5). Its vLLM must have the endpoint (0.32.0 does; the 0.24.0 /
0.28.0 the catalog uses by default do not) and serve the model with `--logprobs-mode raw_logprobs`:
the catalog entry `qwen3-0-6b-system1` in the README sets both. Outside the platform, the same with
plain vLLM:

```bash
vllm serve Qwen/Qwen3-0.6B --logprobs-mode raw_logprobs --enable-prefix-caching
```

The table (what each does, what serves it, how it is reached):
[README, "How the models are served"](../README.md#how-the-models-are-served). The LLM never calls MCP
itself; it replies with a tool request, and the agent runs the tool on the MCP server and sends the
result back.

The gate owns port 8090, which the plain banking assistant serves, so clients, the HTTPRoute and
the Service keep the same URL; the agent moves to 8091. The agent speaks only the Responses
API, so the gate translates chat completions in both directions
(`src/decision_gate/agent_client.py`). Every other path (the DevUI web UI, `/v1/responses`,
`/v1/entities` ...) is proxied to the agent unchanged.

---

## 2. One request, step by step

`src/decision_gate/server.py`, `chat_completions()`:

1. **Auth.** The bearer token must equal `DEVUI_AUTH_TOKEN` (401 otherwise), checked before
   System-1 is asked anything.
2. **Replies to the agent go to the agent.** Any earlier assistant turn in the conversation,
   a message made only of yes/no/confirm words ("ok do it"), or a short consent ("yes I
   confirm") goes to the agent: System-1 sees only the last message, so it can never judge a
   "yes" out of context.
3. **Text rules** (`src/decision_gate/text_rules.json`, below). Exact and free; each one errs toward the agent:

   | Rule | Example | Reason in the log |
   |---|---|---|
   | Does not open with an auxiliary verb (is, can, does, ...) | "What is a mortgage?" | `not_yes_no_question` |
   | A polite request | "Can you explain ...?" | `not_yes_no:indirect_request` |
   | About this customer's own records | "Is my card blocked?", "Do I have ...?" | `needs_customer_data` |
   | An urgent action | "Can my card be blocked right now?" | `needs_agent:urgent_action` |
   | A capability-phrased action ("Can I / Could I / Should I / Is it possible to" + block, unblock, close, cancel, transfer, withdraw) | "Is it possible to block my card?" | `needs_agent:capability_action` |
   | Contains a number, %, or currency | "Is 5% on $1000 more than $40?" | `needs_calculation` |
   | Two questions in one | "Is APR yearly? And how is it worked out?" | `not_yes_no:compound` |

   The first row is built into the gate; the others are regexes in the rules file, which is
   where a deployment adds its own ("Adding your own text rule", below).

   `needs_agent:capability_action` ships because the action veto is off (section 4). Its cost on
   the 391 test questions: it matches 9, of which 8 are plain yes/no questions System-1 should
   answer ("Can I cancel a transaction ...?") and 1 needs the agent but is already caught by the
   numbers rule. It errs toward the agent, as every rule should.

4. **One System-1 call** for whatever is left (below).
5. **Decision** on the scores (below): show "Yes." / "No." with no LLM call, or forward to the
   agent.
6. **Shadow mode** (`GATE_SHADOW_MODE=true`): the decision is logged, but every question is
   answered by the agent.

A displayed answer is a normal chat-completions response (streamed too, if asked for) whose
content is `Yes.` or `No.`.

---

### Adding your own text rule

The text rules are a JSON file, `src/decision_gate/text_rules.json`: an ordered list, checked on
the lower-cased question after the built-in yes/no check; the first rule that matches sends the
question to the agent, and its `reason` goes into the decision log and the `X-Gate-Reason`
header. A rule matches when **any** of its `any` patterns matches, or when **all** of its `all`
patterns match (Python regular expressions, `re.search`). Each rule also carries an `example`
and a `why`, which the gate ignores.

To add one, append it to the list with a reason of your own. For example, the shipped
capability-opener rule (section 4, "Known limitation: capability-phrased requests"; its `why` is
shortened here) looks like this:

```json
{
  "reason": "needs_agent:capability_action",
  "all": ["^\\s*(can|could|should)\\s+i\\b|^\\s*is\\s+it\\s+possible\\s+to\\b",
          "\\b(block|unblock|close|cancel|transfer|withdraw)\\w*\\b"],
  "example": "Is it possible to block my card?",
  "why": "A request to act, phrased as a question about what is possible: the agent's, not a bare yes/no."
}
```

(In JSON every backslash of the regex is doubled.) Then redeploy: `./deploy.sh` rebuilds the gate
image, which carries the file. To keep your rules outside the image instead (Docker), mount your
file into the gate container and point `GATE_TEXT_RULES` at it, with a `docker-compose.override.yml`
next to `docker-compose.yml` (Compose reads it automatically):

```yaml
services:
  gate:
    volumes:
      - ./my_text_rules.json:/rules/my_text_rules.json:ro
    environment:
      GATE_TEXT_RULES: /rules/my_text_rules.json
```

In Kubernetes the chart has no setting for a rules file of your own: change
`src/decision_gate/text_rules.json` and redeploy with `./deploy.sh` (or
`./deploy-usecase.sh unified-decision-gate --mode kubernetes` from the repository root), which rebuilds
the gate image and upgrades the release.

The gate checks every rule when it starts and refuses to start on a malformed one, naming it: a
missing or misspelt key, both `any` and `all` in one rule, a `reason` that is not a short
lower-case token, a pattern with upper-case letters (the question is matched lower-cased, so it
could never match; escapes such as `\S` or `\W` are fine, but write characters themselves rather
than `\N{name}`), or a pattern that is not a valid regex. It warns if there are no rules, or two
rules share a reason. The rules run on every question in full, with no length limit: keep patterns
simple and anchored, and avoid nested repetition such as `(.*)*`, which can take very long on a long
question. Every rule should err toward the agent: a question it wrongly catches costs one LLM call,
one it wrongly lets through gets a yes/no shown that may be meaningless. Re-measure the routing
on labelled questions afterwards to see what the rule moved.

## 3. What System-1 is asked

`POST {SYSTEM1_URL}/v1/systemone`, one call per question (`src/decision_gate/system1_client.py`,
`src/decision_gate/questions.py`). The state is the customer's question as plain text; there is
no system message (`SYSTEM_INSTRUCTIONS = None`, so no `instructions` field is sent):

```json
{"model": "qwen3-0-6b-system1",
 "state": "Is it possible to have more than one bank account?",
 "questions": {
   "answer": {"type": "noul",
              "instructions": "A banking expert is asked the question above. Would the expert answer yes?",
              "criteria": {"true": "the expert answers yes", "false": "the expert answers no"}}}}
```

With the veto on (`SYSTEM1_MAX_P_ACTION` < 1.0) a second question, `is_action_request`, is added;
with it off (the default) the gate does not ask it, saving one vLLM read.

vLLM builds each question's prompt as: the state, then `Question: <instructions>`, one line per
label with its criteria, then `Answer with yes or no only.`, and reads the label probabilities
from the first token's raw logprobs.

**Response mapping.** Per question, `noul` is P(yes); vLLM's `confidence` becomes the gate's
`answer_confidence`; the question's `label_mass` from `diagnostics` (the share of the
vocabulary's probability on the label tokens), when sent, is copied onto the answer. The served
`model` goes into the decision log.

**Named errors** (the decision log gets the status and a fixed diagnosis, never a response body):

| vLLM answers | Logged as |
|---|---|
| 404, route unknown | `no /v1/systemone on this vLLM` (needs e.g. v0.32.0; v0.28.0 has none) |
| 404, model unknown | `vLLM does not serve SYSTEM1_MODEL` |
| 400 | `vLLM rejected the request` (a label that is not a single token, or a field it does not accept) |
| 501 | `no read strategy` (is vLLM serving it with `--logprobs-mode raw_logprobs`?) |
| 503 | `busy (503)` |
| no answer in time | `timeout` |

`SYSTEM1_TIMEOUT_S` (2.0 s) is **one hard deadline** for the whole call (`asyncio.wait_for`), not a
per-phase timeout.

**Health.** `GET /live` answers from the gate alone and never calls System-1; the Helm probes and
the compose healthcheck use it. `GET /health` reports gate status plus `system1_ok`: each call makes
a real one-word `/v1/systemone` read and a `GET /v1/models` (which must list `SYSTEM1_MODEL`),
concurrently under one deadline, so a vLLM without the endpoint does not look healthy. Do not
scrape it every few seconds.

**Wording matters more than thresholds.** An earlier wording ("Is the answer to this banking question
yes: `question`?") scored test AUC 0.434 on Qwen3-0.6B, below chance. Change these strings only with
a measurement on labelled questions behind the change; section 4 says how the current one was
chosen.

---

## 4. From scores to a decision

`src/decision_gate/policy.py`, `decide()`, in this order:

| Check | Result |
|---|---|
| A score is missing or invalid | agent |
| label_mass < `SYSTEM1_MIN_LABEL_MASS` (0 = off, the default) | agent (`low_label_mass`) |
| P(action) > `SYSTEM1_MAX_P_ACTION` (1.0 = off, the default) | agent: a request to *do* something never gets a bare "Yes." |
| P(yes) ≥ `SYSTEM1_YES_MIN` (0.9952) | show "Yes." |
| P(yes) ≤ `SYSTEM1_NO_MAX` (0.9906) | show "No." |
| in between | agent (`low_confidence:answer`) |

The values are those Docker, `config/agent-config.env` and Helm set; `config.py`'s own fallbacks
are stricter (0.90 / 0.10 / 0.60). The settings are validated (`config.invalid_settings()`): the
cut-offs, `SYSTEM1_MAX_P_ACTION` and `SYSTEM1_MIN_LABEL_MASS` must be finite probabilities in [0, 1] with
`SYSTEM1_NO_MAX < SYSTEM1_YES_MIN`, and the deadlines finite and positive. If any is not (a typo such as
`SYSTEM1_YES_MIN=1.2`, NaN, or crossed cut-offs), System-1's answers are off: the gate sends every question
to the agent (reason `config:invalid_settings`) and logs each problem at startup, instead of displaying
everything or nothing.

**Label mass** is unmeasured and off. It overlaps with the confidence: vLLM's confidence is the
label share × label_mass. When on, an answer without a `label_mass` escalates too.

**The veto is off.** Qwen3-0.6B gives P(action) ≈ 0.998-1.0 to every question, and every action
wording tried had AUC 0.14-0.50, so the veto has no signal with this model. While it is off the gate
does not ask `is_action_request`.

**Known limitation: capability-phrased requests.** A request phrased as a question about what is
possible, such as "Can I close my account?" or "Is it possible to block my card?", opens with an
auxiliary verb and is not "Can you ...?", so without the veto nothing but a text rule can send it to
the agent, and a "No." to "Is it possible to block my card?" is the worst a banking gate can show.
The `needs_agent:capability_action` rule (section 2) **ships by default** for that: a capability
opener followed by an account or card action verb goes to the agent. Its cost is the 8 plain
yes/no test questions it also catches; the requests it exists for do not occur in the 391.
Re-measure it on shadow traffic before shadow mode is switched off: keep it if real "can I
block / close ..." requests show up there, and drop or narrow it if they don't.

### Calibration of the cut-offs

Qwen3-0.6B's P(yes) is **squeezed into 0.98-1.0**, so a 0.01-grid fit has a single step and
fails. The rule (the cut that maximises accuracy of "Yes. if P ≥ c, else No.") is applied over the
midpoints between the sorted calibration scores, with an unsure band around the cut.

The shipped `SYSTEM1_YES_MIN = 0.9952` / `SYSTEM1_NO_MAX = 0.9906` are **0.9929 ± 0.0023**: the
cut fitted on 200 labelled calibration questions, separate from the 391 test questions, and a band
equal to the run-to-run noise one request at a time. None of the 391 test questions was used for
the cut. Re-scored against the `qwen3-0-6b-system1` deployment on the platform, the 591 questions
show no systematic shift (median change 0.0000, sd 0.0017-0.0030); refitting moves the cut by
0.0004, inside the noise, so the same cut-offs ship. About 10% of decisions move between a shown
answer and "agent" from one serving setup to another (never Yes↔No). **Refit for other traffic, after any fine-tuning of System-1, and for any other model**:
cut-offs never transfer between models.

**Scores are not deterministic, and depend on load.** At 16 concurrent requests P(yes) moved by up
to 0.0225 against one request at a time (vLLM batches them differently). With the shipped band
that gave 0 Yes↔No reversals, but about 17% of the yes/no questions (45 of 263) moved between
"shown" and "agent": whether a question gets a shown answer can depend on load.
`VLLM_BATCH_INVARIANT=1` is not supported on CPU (vLLM refuses: `batch_invariant mode is not
supported for CPU_ATTN`), so fit the cut and the band at the load you expect.

**How the wording was chosen.** The "expert" wording and the plain-text state were chosen on the first
32 of the calibration questions; the cut-offs were then fitted on all 200. No system message is sent. The 391
test questions were not used to choose any setting.

**Controls**: P(yes) 0.93-0.99 on obvious questions ("Is the sky
green?" 0.974), and 3 of 5 negation pairs ranked by meaning, by only 0.001-0.008: the model mostly
reads the surface form. `enable_thinking` unset and false score identically.

**Shadow mode is the default** (`GATE_SHADOW_MODE=true` in Docker and Helm), because the veto is off,
the cut-offs are fitted for the banking example and the model mostly reads the surface form. Turn it off only after
the answers System-1 would have shown have been checked against labelled requests from your own traffic,
with the cut-offs refitted on that traffic at its load.

---

## 5. Design notes

**"Unified" means both models are served the same way (vLLM, deployed by Model Manager), not one
gateway for both.** The LLM goes through LiteLLM (keys, routing, Langfuse traces); System-1 goes
through the inference AI gateway, not LiteLLM, which has no `/v1/systemone` route: no LiteLLM key,
usage tracking or trace for it (`SYSTEM1_API_KEY` is sent if the model's vLLM sets an `--api-key`). A
LiteLLM pass-through route for `/v1/systemone` is a possible next step, not done.

**Prefix caching.** Serve System-1 with `--enable-prefix-caching`: each question is a separate
vLLM read over the same state, so with the veto on the two questions share the state's prefix.
With the veto off only one question is asked, so it matters less.

---

## 6. Fail open

Every System-1-side failure (down, slower than the 2 s `SYSTEM1_TIMEOUT_S` deadline, busy,
malformed reply, any of the named errors in section 3, gate disabled) forwards the question to the agent: the assistant never
degrades because System-1 is unhappy. The only errors the gate returns itself are 401 (bad
token), 400 (a body that is not a chat completion) and 502 (the agent is unreachable).
`/health` reports the gate as up even when System-1 is not (`system1_ok: false` for monitoring),
and the probes use `/live`, so they never restart the gate for System-1's problem.

---

## 7. Decision log

Every request appends one JSON line to `/app/telemetry/gate.jsonl`
(`src/decision_gate/telemetry.py`): route, reason, scores, confidences, System-1 and total
milliseconds, the model vLLM reports, upstream status, the MCP tools the agent called, and the client's
`X-Session-Id`. The question is stored as an HMAC (`GATE_TELEMETRY_HASH_KEY`) and a length; the
text only with `GATE_LOG_QUESTION_TEXT=true`. It does not record the agent's answer, so it is not a
labelled data set: it shows what System-1 would have answered and where each request went, and labels
for refitting the cut-offs come from your own review. In Helm the log is on an `emptyDir` by default,
so it is lost with the pod; set `gate.telemetry.existingClaim` to a PVC to keep it.

With `GATE_DEBUG_HEADERS=true` each response also carries the decision in headers, so the body
keeps the exact OpenAI schema: `X-Gate-Route`, `X-Gate-Reason`, `X-Gate-Confident`, `X-Gate-System1-Ms`,
`X-Gate-Total-Ms`, `X-Gate-P-Answer`, and `X-Gate-P-Is-Action-Request` when that question is asked. Off by default; turn it
on to measure routing.

---

## 8. Source map

| Path | What it is |
|---|---|
| `src/decision_gate/server.py` | the gate: auth, follow-up and text rules, System-1 call, answer or forward, proxy |
| `src/decision_gate/text_rules.json` | the text rules: regexes that send a question to the agent without asking System-1 |
| `src/decision_gate/policy.py` | scores → decision; shadow mode; failure decisions |
| `src/decision_gate/questions.py` | the questions System-1 is asked and the state |
| `src/decision_gate/system1_client.py` | System-1 client for vLLM `/v1/systemone`: deadline, named errors, response mapping, health probe |
| `src/decision_gate/agent_client.py` | chat completions ⇄ the agent's Responses API, streaming |
| `src/decision_gate/telemetry.py` | the decision log |
| `src/decision_gate/config.py` | every `SYSTEM1_*` and `GATE_*` setting and its default |
| `src/decision_gate/test_gate.py` | unit tests (stub upstreams) |
| `src/decision_gate/test_backends.py` | unit tests for the System-1 client (canned vLLM responses) |
| `src/decision_gate/test_config.py` | unit tests for the settings validation, through the real environment parsing |
| `src/agent.py`, `src/main.py` | the banking assistant agent (DevUI) |
| `src/mcp_server/` | the banking MCP tools and their sample data |
| `tests/e2e/smoke.py` | end-to-end smoke test against the real images, with a stub LLM that also serves `/v1/systemone` |
| `tests/helm/check_chart.py` | static Helm chart checks (renders the chart; ports, secret keys, PSS restricted); needs helm and PyYAML. Its live mode runs exec probes only, and this chart has none |
