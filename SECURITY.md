# Security Policy

## Report a Vulnerability

Please report security issues or vulnerabilities to the [Intel Security Center].

For more information on how Intel works to resolve security issues, see
[Vulnerability Handling Guidelines].

[Intel Security Center]:https://www.intel.com/security

[Vulnerability Handling Guidelines]:https://www.intel.com/content/www/us/en/security-center/vulnerability-handling-guidelines.html

## Accepted Scan Exceptions

The following findings are reviewed and accepted for the released use cases
(`coding-agent-maf`, `banking-assistant`). They are documented here so the
rationale is traceable; they are not open action items.

### GPL / "restricted" license — base-image OS packages

- **Finding:** ~300+ Debian OS packages in the `python:3.12-slim` base image are
  licensed under GPL/LGPL and classified "restricted" by the license scan
  (reported as 317 packages for the banking-assistant images and 330 for the
  coding-agent-maf image).
- **Assessment:** This is a license-classification finding, not a vulnerability,
  and it is not individually actionable — it is inherent to the Debian base
  distribution. These GPL binaries are shipped as **mere aggregation** in the
  container (they are not statically linked into, or a derivative of, the
  application code), which does not extend copyleft obligations to the
  application.
- **Decision:** Accepted. Eliminating it would require switching every image to a
  minimal base (e.g. distroless or Chainguard), which is tracked separately as a
  potential hardening improvement rather than a release blocker.

### CVE-2023-48022 (Ray "ShadowRay") — no upstream fix

- **Finding:** Ray's job submission / dashboard API allows unauthenticated code
  execution. No fixed version is published, so the CVE can persist on scans
  regardless of the Ray version.
- **Assessment:** The Ray maintainers treat this as expected behavior for a
  framework designed to run inside a **trusted network boundary**. Ray is
  optional here (used only when `RAY_ENABLED=true`) and the RayCluster runs
  in-cluster with no public ingress on the dashboard (8265) or client (10001)
  ports.
- **Decision:** Accepted, mitigated by network isolation. The Ray client is
  pinned to `2.56.0`, which clears all other Ray advisories (CVE-2025-34351,
  CVE-2025-62593, CVE-2025-1979, CVE-2026-57516, CVE-2026-27482, CVE-2026-41486).