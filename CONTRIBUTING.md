# Contributing

Thank you for your interest in contributing to **Agentic AI Blueprints**. This
repository is a catalog of deployable AI use cases ("blueprints"). Contributions
are welcome, including:

- **New blueprints** — a use case can target the Agent Toolkit or any other
  purpose (inference, RAG, tooling demos, integrations).
- **Improvements** — bug fixes, hardening, performance, or docs for an existing
  blueprint.
- **Documentation** — the top-level guides in [`docs/`](docs/) and use-case READMEs.

By participating you agree to abide by our [Code of Conduct](CODE_OF_CONDUCT.md).

## Table of Contents

- [License](#license)
- [Getting started](#getting-started)
- [Adding a new use case](#adding-a-new-use-case)
- [Development standards](#development-standards)
- [Bill of Materials (SBOM)](#bill-of-materials-sbom)
- [Automated scans on your pull request](#automated-scans-on-your-pull-request)
- [Sign your work (DCO)](#sign-your-work-dco)
- [Commit messages](#commit-messages)
- [Create a pull request](#create-a-pull-request)
- [Review process](#review-process)

## License

This project is licensed under the terms in the [LICENSE](LICENSE) file
(Apache-2.0). Any contribution you submit is understood to be released under the
same license, and you certify authorship/rights via the [DCO](#sign-your-work-dco).

Every shipped `.py` and `.sh` file must carry the license header (see
[Development standards](#development-standards)); the **License Check** workflow
enforces this.

## Getting started

1. **Fork** this repository and clone your fork.
2. Create a topic branch from `main`:
   ```bash
   git checkout -b my-use-case main
   ```
3. Make sure you can deploy at least one existing blueprint first — this confirms
   your **Agent Toolkit** is reachable (see the repository
   [Prerequisites](README.md#prerequisites)).

## Adding a new use case

A blueprint is **self-contained** and lives flat under `usecases/<name>/`. The
top-level `deploy-usecase.sh` and the CI security scans auto-discover any
directory that contains a `deploy.sh`, so **no central file needs editing** to
register it.

Follow the full step-by-step guide: **[docs/adding-new-usecases.md](docs/adding-new-usecases.md)**.

Minimum required layout:

```
usecases/<name>/
├── README.md           # Overview, prerequisites, quick start, config, API, troubleshooting
├── deploy.sh           # One-click deploy (Kubernetes)
├── docker-compose.yml  # Local development
├── examples/           # At least one runnable client (.py and/or .sh)
├── helm-chart/         # Chart.yaml, values.yaml, templates/
└── src/                # Application source + Dockerfile
```

When you add a use case, also:

1. **Pick a category** for the [Use Case Catalog](README.md#use-case-catalog)
   (e.g. `Agent Toolkit`, `Inference`, `RAG`). Reuse an existing category where
   it fits; propose a new one in your PR description if none does.
2. **Add a row** to the catalog table in [README.md](README.md).
3. **Read all configuration from environment variables** — no hardcoded secrets,
   endpoints, or credentials.

## Development standards

Your contribution must pass the same automated gates that run in CI. Run them
locally before opening a PR.

- **License headers.** Every `.py` and `.sh` file starts with, within the first
  15 lines:
  ```
  # Copyright (C) 2025-2026 Intel Corporation
  # SPDX-License-Identifier: Apache-2.0
  ```
- **Pin dependencies.** Use exact `==` versions in every `requirements*.txt`, not
  `>=`/ranges — this keeps builds reproducible and lets the dependency scanner
  resolve and check each package.
- **Container hardening.** Dockerfiles run as a non-root user and drop unneeded
  privileges; Helm and Compose set an explicit `securityContext` /
  `cap_drop: [ALL]`. Static IaC scanners cannot render Helm templating, so set
  security fields as literal values, not via `{{ toYaml }}`.
- **Security scans.** Fix Medium/High findings before review; document any
  accepted exception in [SECURITY.md](SECURITY.md) with a rationale, or suppress
  a genuine false-positive inline (e.g. `# nosec <ID>`) with a reason. The full
  scan suite and how to read results is described in
  [Automated scans on your pull request](#automated-scans-on-your-pull-request).
- **Linting.** The **Linter** (Super-Linter) workflow lints on every PR. To run
  it locally:
  ```bash
  docker run --rm -e RUN_LOCAL=true -e DEFAULT_BRANCH=main \
    -v "$PWD":/tmp/lint ghcr.io/super-linter/super-linter:latest
  ```

## Bill of Materials (SBOM)

As the owner of a use case you are responsible for its **Software Bill of
Materials (SBOM)** — the complete, versioned inventory of everything your
blueprint ships: Python dependencies and the contents of its container image(s).
The SBOM is used for license and vulnerability review, so **prepare it before
raising your PR** and regenerate it whenever dependencies change. Pinning
dependencies to exact `==` versions keeps it deterministic.


## Automated scans on your pull request

When you open (or update) a pull request, the security and quality scans run
**automatically** — you do not trigger them manually. Watch them, then fix any
issues before requesting review.

**Where to see results:**

- **`Actions` tab** — pick the workflow run for your PR's latest commit, open it,
  and read each job's **summary** and logs. Downloadable reports are attached to
  the run under **Artifacts**.

**Scans that run:**

| Scan | Type | Checks | Results in |
|---|---|---|---|
| Linter | Super-Linter | style/syntax across languages | Actions |
| License Check | Header check | Intel SPDX header on every `.py`/`.sh` | Actions |
| Bandit | Python SAST | insecure Python patterns | Actions |
| Semgrep | SAST | cross-language security rules | Actions |
| Trivy | SCA / IaC | dependency + config (Helm/Compose) vulnerabilities | Actions |
| Container Scan | Image | image OS + package vulnerabilities, SBOM | Actions |
| Coverity | SAST | deeper static analysis | Actions |
| ClamAV | Antivirus | malware in committed files | Actions |
| Secret Scan | Secrets | committed credentials/keys/tokens | Security |
| Dependency Graph | Supply chain | dependency inventory + Dependabot alerts | Security |

**Fix the findings.** All scans must pass. For a Medium/High security finding,
either fix it, or — if it is a verified false-positive or an unavoidable
base-distro issue — record the accepted exception in [SECURITY.md](SECURITY.md)
with a rationale (or suppress inline with a justified `# nosec <ID>` / equivalent).
Push the fix and the scans re-run automatically.

## Sign your work (DCO)

This project requires the **Developer Certificate of Origin (DCO)**. Every commit
must be signed off, certifying you wrote the code or otherwise have the right to
submit it under the project's license.

Add the sign-off automatically with `-s`:

```bash
git commit -s -m "Add my-use-case blueprint"
```

This appends a line using your real name and email:

```
Signed-off-by: Jane Doe <jane.doe@example.com>
```

Use your **real name** — no pseudonyms or anonymous contributions. Set your git
identity once with:

```bash
git config user.name  "Jane Doe"
git config user.email "jane.doe@example.com"
```

<details>
<summary>Developer Certificate of Origin 1.1</summary>

```
By making a contribution to this project, I certify that:

(a) The contribution was created in whole or in part by me and I have the right
    to submit it under the open source license indicated in the file; or
(b) The contribution is based upon previous work that, to the best of my
    knowledge, is covered under an appropriate open source license and I have
    the right under that license to submit that work with modifications, whether
    created in whole or in part by me, under the same open source license
    (unless I am permitted to submit under a different license), as indicated in
    the file; or
(c) The contribution was provided directly to me by some other person who
    certified (a), (b) or (c) and I have not modified it.
(d) I understand and agree that this project and the contribution are public and
    that a record of the contribution (including all personal information I
    submit with it, including my sign-off) is maintained indefinitely and may be
    redistributed consistent with this project or the open source license(s)
    involved.
```
</details>

## Commit messages

- Keep the subject short, imperative, and specific (e.g. `Add tender-eval blueprint`).
- Explain the *what* and *why* in the body when the change is non-trivial.
- Group related changes; avoid unrelated changes in one commit.

## Create a pull request

1. Push your branch to your fork and open a pull request against `main`.
2. Fill in the description: what the change does, which use case/category, how
   you tested it, and a reference to your [SBOM](#bill-of-materials-sbom).
3. The scans start automatically. Make every check green — see
   [Automated scans on your pull request](#automated-scans-on-your-pull-request)
   for the full list and where to read the results.
4. Keep the PR focused; large use cases are fine, but avoid bundling unrelated
   changes.

## Review process

A maintainer will review your PR and may request changes via comments. Once the
checks pass and the review is approved, a maintainer will merge it. Thank you for
contributing!
