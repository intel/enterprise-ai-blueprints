# Copyright (C) 2025-2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0
"""Render the Helm chart and check it the way a kubelet would, not the way a YAML parser would.

`helm lint` passed on a chart whose System-1 sidecar probes ran an executable literally named
"python'": valid YAML, impossible pod. This renders the chart (gate on AND off) and checks:

  static   named probe ports exist on their container; the Service's targetPort names exactly
           one container port; every secretKeyRef points at a key the chart's Secrets define
  live     every container with an exec probe is started FROM ITS REAL IMAGE with the rendered
           command, env, user and a read-only root filesystem (plus the chart's writable
           mounts), and the RENDERED probe argv is run inside it until it passes

    python tests/helm/check_chart.py            # needs helm, docker (or nerdctl) and the built images
    python tests/helm/check_chart.py --static   # no docker: static checks only

Exit status 0 = every check passed.
"""
import os
import shutil
import subprocess
import sys
import time
import uuid

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
CHART = os.environ.get("CHART_DIR") or os.path.join(HERE, "..", "..", "helm-chart")
SETS = "agent.openaiApiKey=k,agent.devuiAuthToken=t,agent.modelName=m"
DOCKER = ["docker"] if shutil.which("docker") and subprocess.run(  # nosec B607 - a local test tool on PATH
    ["docker", "info"], capture_output=True, check=False).returncode == 0 else ["sudo", "-n", "docker"]


def render(gate: bool):
    out = subprocess.run(["helm", "template", "t", CHART, "--set",  # nosec B607 - helm on PATH
                          f"{SETS},gate.enabled={str(gate).lower()}"],
                         capture_output=True, text=True, check=True).stdout
    return [d for d in yaml.safe_load_all(out) if d]


def probes(c):
    return [(k, c[k]) for k in ("startupProbe", "readinessProbe", "livenessProbe") if k in c]


def static_checks(docs, label, check):
    dep = next(d for d in docs if d["kind"] == "Deployment")
    pod = dep["spec"]["template"]["spec"]
    secrets = {d["metadata"]["name"]: set((d.get("stringData") or {}).keys())
               for d in docs if d["kind"] == "Secret"}
    svc = next(d for d in docs if d["kind"] == "Service")
    target = svc["spec"]["ports"][0]["targetPort"]
    owners = [c["name"] for c in pod["containers"] for p in c.get("ports", []) if p.get("name") == target]
    check(f"{label}: Service targetPort {target!r} -> exactly one container", len(owners) == 1,
          str(owners))
    ports_seen = {}
    for c in pod["containers"]:
        names = {p["name"] for p in c.get("ports", [])}
        for kind, pr in probes(c):
            for h in ("tcpSocket", "httpGet"):
                if h in pr and isinstance(pr[h]["port"], str):
                    check("{}: {} {} port {!r} is declared".format(label, c["name"], kind, pr[h]["port"]),
                          pr[h]["port"] in names, str(sorted(names)))
            if "exec" in pr:
                argv = pr["exec"]["command"]
                check("{}: {} {} argv[0] looks like a program".format(label, c["name"], kind),
                      isinstance(argv[0], str) and argv[0].replace("-", "").replace("_", "").replace(
                          "/", "").replace(".", "").isalnum(), repr(argv[0]))
        for e in c.get("env", []):
            ref = (e.get("valueFrom") or {}).get("secretKeyRef")
            if ref:
                check("{}: {} env {} -> secret key exists".format(label, c["name"], e["name"]),
                      ref["key"] in secrets.get(ref["name"], set()), "{}/{}".format(ref["name"], ref["key"]))
        for p in c.get("ports", []):
            ports_seen.setdefault(p["containerPort"], []).append(c["name"])
    dup = {p: cs for p, cs in ports_seen.items() if len(cs) > 1}
    check(f"{label}: no two containers bind the same port (one network namespace)", not dup, str(dup))
    # Pod Security Standard 'restricted', which the platform enforces on the namespace: a pod that
    # misses one of these is rejected at admission, not when it runs.
    psc = pod.get("securityContext") or {}
    check(f"{label}: pod runAsNonRoot + seccomp RuntimeDefault (PSS restricted)",
          psc.get("runAsNonRoot") is True and (psc.get("seccompProfile") or {}).get("type") == "RuntimeDefault",
          str(psc))
    for c in pod["containers"]:
        sc = c.get("securityContext") or {}
        user = sc.get("runAsUser", psc.get("runAsUser"))
        check("{}: {} restricted securityContext (no escalation, drop ALL, numeric non-root UID)".format(
              label, c["name"]),
              sc.get("allowPrivilegeEscalation") is False and "ALL" in ((sc.get("capabilities") or {}).get("drop") or [])
              and isinstance(user, int) and user > 0, str(sc))
    return pod


def live_check(pod, c, check):
    """Run container `c` as the pod would, then the rendered exec probe, inside its own image."""
    execs = [(k, pr) for k, pr in probes(c) if "exec" in pr]
    if not execs:
        return
    name = "chartcheck-{}-{}".format(c["name"], uuid.uuid4().hex[:6])
    sc = c.get("securityContext") or {}
    args = DOCKER + ["run", "-d", "--name", name]
    if sc.get("readOnlyRootFilesystem"):
        args.append("--read-only")
    if sc.get("runAsUser") is not None:
        args += ["--user", str(sc["runAsUser"])]
    vols = {v["name"]: v for v in pod.get("volumes", [])}
    for m in c.get("volumeMounts", []):          # chart volumes become writable tmpfs mounts
        uid = sc.get("runAsUser", 0)
        args += ["--tmpfs", "{}:rw,size=3g,uid={}".format(m["mountPath"], uid)]
        assert m["name"] in vols, m
    for e in c.get("env", []):
        if "value" in e:
            args += ["-e", "{}={}".format(e["name"], e["value"])]
    for k in ("https_proxy", "HTTPS_PROXY"):      # the build host's own egress, for downloads
        if os.environ.get(k):
            args += ["-e", f"{k}={os.environ[k]}"]
    args.append(c["image"])
    args += c.get("command", []) + c.get("args", [])
    subprocess.run(args, check=True, capture_output=True)
    try:
        for kind, pr in execs:
            argv = pr["exec"]["command"]
            budget = pr.get("periodSeconds", 10) * pr.get("failureThreshold", 3)
            deadline, ok, err = time.time() + min(budget, 600), False, ""
            while time.time() < deadline and not ok:
                r = subprocess.run(DOCKER + ["exec", name] + argv, capture_output=True, text=True, check=False)
                ok, err = r.returncode == 0, (r.stderr or r.stdout).strip()[-120:]
                if r.returncode in (126, 127):   # not executable / not found: never recovers
                    break
                if not ok:
                    time.sleep(5)
            check("live: {} {} passes (rendered argv, real image)".format(c["name"], kind), ok,
                  "ok" if ok else err)
    finally:
        subprocess.run(DOCKER + ["rm", "-f", name], capture_output=True, check=False)


def main():
    static_only = "--static" in sys.argv
    results = []

    def check(name, ok, detail=""):
        results.append(bool(ok))
        print(f"{'ok' if ok else 'FAIL':<4} {name:<74} {'' if ok else detail}")

    for gate in (True, False):
        label = "gate=%s" % ("on" if gate else "off")
        pod = static_checks(render(gate), label, check)
        if gate and not static_only:
            for c in pod["containers"]:
                live_check(pod, c, check)

    print(f"\n{sum(results)}/{len(results)} passed")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
