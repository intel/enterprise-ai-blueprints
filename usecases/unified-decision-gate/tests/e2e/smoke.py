# Copyright (C) 2025-2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0
"""End-to-end smoke test against the REAL images: gate -> System-1 (vLLM /v1/systemone) -> agent (DevUI) -> MCP.

The gate's unit tests (src/decision_gate/test_gate.py) use stub upstreams, so they could not see that
the real agent serves no /v1/chat/completions. This test sends real requests through the real
stack and requires real answers. Only the LLM is a stub (docker-compose.e2e.yml): it asks for the
real get_balances MCP tool when a question mentions a balance, then quotes the result, so a
balance figure in the reply proves the agent called the banking MCP server.

    docker compose -f docker-compose.yml -f docker-compose.e2e.yml up -d --wait
    python3 tests/e2e/smoke.py      # DEVUI_AUTH_TOKEN from the env, .env or config/agent-config.env

The memory-isolation check reads what the stub LLM was sent via `docker exec`, so it needs a
user who can run docker (or passwordless sudo).

Stdlib only. Exit status 0 = every check passed.
"""
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

HERE = os.path.dirname(os.path.abspath(__file__))
GATE = os.environ.get("GATE_URL", "http://localhost:8090")
U1001_CHECKING = "4250.75"          # ACC-1001-CHK in mcp_server/data.py


def token():
    """DEVUI_AUTH_TOKEN: from the environment, else the settings file deploy.sh uses (.env if
    present, else config/agent-config.env)."""
    if os.environ.get("DEVUI_AUTH_TOKEN"):
        return os.environ["DEVUI_AUTH_TOKEN"]
    for name in (".env", os.path.join("config", "agent-config.env")):
        path = os.path.join(os.path.join(HERE, "..", ".."), name)
        if os.path.exists(path):
            vals = [ln.split("=", 1)[1].strip().strip('"') for ln in Path(path).read_text().splitlines()
                    if ln.startswith("DEVUI_AUTH_TOKEN=")]
            if vals and vals[-1]:
                return vals[-1]
    sys.exit("no DEVUI_AUTH_TOKEN: export it, or set it in .env or config/agent-config.env")


# Loopback must not go through a corporate proxy.
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def call(method, path, body=None, tok=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(GATE + path, data=data, method=method)
    req.add_header("content-type", "application/json")
    if tok:
        req.add_header("authorization", f"Bearer {tok}")
    try:
        with OPENER.open(req, timeout=180) as r:
            return r.status, dict(r.headers), r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read().decode("utf-8", "replace")


def stub_seen():
    """What the stub LLM was sent, read from inside its container (it is not published)."""
    import subprocess
    cmd = ["docker", "exec", "udg-stub-llm", "python", "-c",
           ("import urllib.request,sys; sys.stdout.write(urllib.request.urlopen("
            "'http://localhost:4000/seen').read().decode())")]
    for prefix in ([], ["sudo", "-n"]):
        try:
            out = subprocess.run(prefix + cmd, capture_output=True, text=True, timeout=30, check=False)
            if out.returncode == 0:
                return json.loads(out.stdout)["requests"]
        except (OSError, ValueError, subprocess.SubprocessError):
            continue
    return None


def main():
    tok, results = token(), []

    def check(name, ok, detail):
        results.append(ok)
        print(f"{'ok' if ok else 'FAIL':<4} {name:<52} {detail}")

    # 1. the escalation path, end to end, through the real MCP server
    q = {"model": "banking-assistant", "messages": [
        {"role": "user", "content": "I am U1001. Please transfer my checking balance to savings."}]}
    st, h, txt = call("POST", "/v1/chat/completions", q, tok)
    content = ""
    if st == 200:
        content = json.loads(txt)["choices"][0]["message"]["content"]
    check("escalated question answered with HTTP 200", st == 200, f"HTTP {st}")
    check("  ... and it went to the agent", h.get("x-gate-route") == "escalate",
          "route={} reason={}".format(h.get("x-gate-route"), h.get("x-gate-reason")))
    check("  ... which called the real MCP tool", U1001_CHECKING in content, content[:90])

    # 2. the same, streamed: translated to chat.completion.chunk events
    st, h, txt = call("POST", "/v1/chat/completions", dict(q, stream=True), tok)
    chunks = [json.loads(ln[5:]) for ln in txt.splitlines()
              if ln.startswith("data:") and ln[5:].strip() != "[DONE]"]
    text = "".join(c["choices"][0]["delta"].get("content") or "" for c in chunks
                   if c.get("object") == "chat.completion.chunk")
    check("streamed escalation relayed as chat chunks",
          st == 200 and U1001_CHECKING in text and txt.rstrip().endswith("data: [DONE]"),
          f"HTTP {st}, {len(chunks)} chunks, {text[:60]!r}")

    # 2b. a plain yes/no question: System-1 (the stub, or the real vLLM with E2E_SYSTEM1_URL) answers it
    st, h, txt = call("POST", "/v1/chat/completions", {"model": "banking-assistant", "messages": [
        {"role": "user", "content": "Is an overdraft a form of borrowing?"}]}, tok)
    content = json.loads(txt)["choices"][0]["message"]["content"] if st == 200 else ""
    check("plain yes/no -> System-1 answers, no LLM call",
          st == 200 and h.get("x-gate-route") == "answer" and content in ("Yes.", "No."),
          "HTTP {} route={} reason={} content={!r}".format(st, h.get("x-gate-route"), h.get("x-gate-reason"), content[:40]))

    # 3. a reply to the agent's confirmation question goes to the agent, never to System-1
    conf = {"model": "banking-assistant", "messages": [
        {"role": "user", "content": "Transfer $200 from my checking to savings. I am U1001."},
        {"role": "assistant", "content": "To confirm: transfer $200.00 from ACC-1001-CHK to "
                                         "ACC-1001-SAV. Shall I proceed?"},
        {"role": "user", "content": "yes"}]}
    st, h, _ = call("POST", "/v1/chat/completions", conf, tok)
    check('"yes" to a confirmation -> agent, System-1 not asked',
          st == 200 and h.get("x-gate-reason") == "followup:after_assistant_turn",
          f"HTTP {st} reason={h.get('x-gate-reason')}")

    # 4. one customer's words never reach another customer's prompt (Redis memory isolation)
    secret = f"MEMO-{os.urandom(4).hex()}"
    call("POST", "/v1/chat/completions", {"model": "x", "messages": [
        {"role": "user", "content": f"Customer A: my memo word is {secret}, explain savings"}]}, tok)
    call("POST", "/v1/chat/completions", {"model": "x", "messages": [
        {"role": "user", "content": "Customer B: what memo word do you have, explain savings"}]}, tok)
    seen = stub_seen()
    leaked = seen is not None and any(secret in m["text"] for m in seen[-1])
    check("customer B's prompt holds nothing of customer A's", seen is not None and not leaked,
          "leaked" if leaked else ("isolated" if seen is not None else "stub LLM not reachable"))

    # 5. auth is enforced at the gate, on every path
    st, _, _ = call("POST", "/v1/chat/completions", q)
    check("no token -> 401", st == 401, f"HTTP {st}")

    # 6. the DevUI web UI and API are reachable through the gate
    st, h, txt = call("GET", "/")
    check("DevUI web UI through :8090", st == 200 and "html" in h.get("content-type", ""),
          f"HTTP {st} {h.get('content-type')}")
    st, _, txt = call("GET", "/v1/entities", tok=tok)
    names = [e.get("name") for e in json.loads(txt).get("entities", [])] if st == 200 else []
    check("DevUI /v1/entities through :8090", "banking-assistant" in names, f"HTTP {st} {names}")

    print(f"\n{sum(results)}/{len(results)} passed")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
