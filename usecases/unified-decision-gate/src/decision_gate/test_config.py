# Copyright (C) 2025-2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for the gate's settings validation, through the real environment parsing.

Each case starts a fresh interpreter with the environment variables set, imports
decision_gate.config as a deployment would, and checks config.invalid_settings(): a bad value must
be reported (so the gate turns System-1's answers off), a good one must not.

Run:  <env>/python -m decision_gate.test_config      (no extra dependencies)
"""
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROBE = "import json; from decision_gate import config; print(json.dumps(config.invalid_settings()))"


def problems(env: dict) -> list:
    full = {k: v for k, v in os.environ.items() if not k.startswith(("SYSTEM1_", "UPSTREAM_"))}
    full.update(env)
    out = subprocess.run([sys.executable, "-c", PROBE], cwd=HERE, env=full,  # nosec B603 - fixed argv
                         capture_output=True, text=True, check=True).stdout
    return json.loads(out)


CASES = [
    # (name, env, expect a problem naming this setting, or None for "valid")
    ("defaults", {}, None),
    ("shipped cut-offs", {"SYSTEM1_YES_MIN": "0.9952", "SYSTEM1_NO_MAX": "0.9906"}, None),
    ("YES_MIN above 1", {"SYSTEM1_YES_MIN": "1.2"}, "SYSTEM1_YES_MIN"),
    ("NO_MAX below 0", {"SYSTEM1_NO_MAX": "-0.1"}, "SYSTEM1_NO_MAX"),
    ("YES_MIN NaN", {"SYSTEM1_YES_MIN": "nan"}, "SYSTEM1_YES_MIN"),
    ("NO_MAX inf", {"SYSTEM1_NO_MAX": "inf"}, "SYSTEM1_NO_MAX"),
    ("crossed cut-offs", {"SYSTEM1_YES_MIN": "0.2", "SYSTEM1_NO_MAX": "0.5"}, "no unsure band"),
    ("equal cut-offs", {"SYSTEM1_YES_MIN": "0.5", "SYSTEM1_NO_MAX": "0.5"}, "no unsure band"),
    ("veto above 1", {"SYSTEM1_MAX_P_ACTION": "1.5"}, "SYSTEM1_MAX_P_ACTION"),
    ("label mass NaN", {"SYSTEM1_MIN_LABEL_MASS": "nan"}, "SYSTEM1_MIN_LABEL_MASS"),
    ("deadline zero", {"SYSTEM1_TIMEOUT_S": "0"}, "SYSTEM1_TIMEOUT_S"),
    ("deadline inf", {"SYSTEM1_TIMEOUT_S": "inf"}, "SYSTEM1_TIMEOUT_S"),
    ("agent timeout negative", {"UPSTREAM_TIMEOUT_S": "-5"}, "UPSTREAM_TIMEOUT_S"),
    ("not a number falls back to the default", {"SYSTEM1_YES_MIN": "abc"}, None),
]


def main() -> int:
    failed = 0
    for name, env, expect in CASES:
        got = problems(env)
        ok = (not got) if expect is None else any(expect in p for p in got)
        failed += not ok
        print(f"  {'ok  ' if ok else 'FAIL'} {name}: {got or 'valid'}")
    print(f"\n{len(CASES) - failed}/{len(CASES)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
