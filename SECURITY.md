# Security Policy

`sdo` (`sdo-sdk`, `import sdo`) is the official Python SDK for Scrape.do. It is a
token-bearing HTTP client: it holds your Scrape.do API token and attaches it to
every outbound request. The token is the sensitive asset this SDK handles, so we
treat any issue that can leak, log, mishandle, or weaken protection of that token
as a security vulnerability — not just classic memory/parsing bugs.

This document explains how to report a vulnerability privately, what we support,
and how we coordinate disclosure.

## Reporting a Vulnerability

**Do not open a public GitHub issue, pull request, or discussion for a security
problem.** Public disclosure before a fix is available puts every user's token
and traffic at risk.

Report privately through either channel — both reach the maintainer directly:

1. **GitHub Private Vulnerability Reporting (preferred).**
   Go to the repository's **Security** tab → **Report a vulnerability**. This
   opens a private advisory draft visible only to you and the maintainers, keeps
   the full report history in one place, and is the channel we use to issue the
   GHSA advisory and request a CVE.

2. **Email:** `security@scrape.do`
   Use this if you cannot use GitHub's private reporting, or for anything
   sensitive enough that you'd rather not file even a private GitHub draft. PGP is
   available on request — email first and we'll exchange a key before you send
   details.

A good report includes:

- The SDK version (`python -c "import sdo; print(sdo.__version__)"`) and Python
  version.
- A description of the issue and its impact — especially whether it can expose,
  log, transmit, or weaken handling of the API token, or alter where requests are
  sent.
- A minimal reproduction: code snippet, `ScrapeConfig` used, and observed vs.
  expected behavior. **Redact your real token** — use a placeholder.
- Any known workaround.

Please report responsibly: do not run automated scans against Scrape.do
production endpoints as part of your research, and do not include real customer
tokens or scraped data in your report.

## Scope

In scope:

- Token leakage: the token appearing in exceptions, `repr()`/`str()` output,
  tracebacks, the local variables of any stack frame that is live when the SDK
  raises (what error trackers such as Sentry record), log records (including
  the `httpx`/`httpcore` loggers), or anywhere not strictly necessary to
  authenticate the request.
- Token precedence flaws: the documented resolution order is
  **explicit parameter → `SDO_TOKEN` env var**; any bug that lets a
  lower-priority or attacker-controlled source override a higher-priority token is
  in scope.
- Transport downgrade: the token must only travel over HTTPS (a plain-http base
  URL is refused unless the host is loopback or the caller explicitly opts in).
- Request integrity: anything that redirects, downgrades, or silently rewrites
  the destination of a request, or sends the token to an unintended host.
- Transport handling: TLS verification bypass, and decompression flaws in the
  transparent `Accept-Encoding: gzip, deflate, br` handling (e.g. decompression
  bombs, memory exhaustion, or mismatched content-length).
- `SdoError` taxonomy or `ScrapeResult` parsing bugs that cause untrusted server
  data to be mishandled in a way that affects the calling process.
- Vulnerable or malicious transitive dependencies (notably `httpx` and its stack)
  that are exploitable through normal SDK usage.

Out of scope:

- Vulnerabilities in the Scrape.do API service itself — report those through
  Scrape.do's main responsible-disclosure channel, not this repo.
- Issues that require a pre-compromised local machine, a malicious local config
  file the attacker already controls, or physical access.
- Findings against internal build/spec tooling that is not distributed in this SDK.

## No-Telemetry Posture

`sdo` ships **no telemetry**. It does not phone home, collect usage analytics,
emit crash reports, or transmit anything to Scrape.do beyond the scrape requests
you explicitly make. There is no background reporting channel and no UI. This is a
deliberate security property: the only outbound traffic is the request you author,
so the SDK never becomes a side channel for your token or your targets. A change
that introduces any such outbound reporting is treated as a vulnerability.

## Supported Versions

We support the **latest released minor version**. Security fixes land on `main`
and ship in a new patch release of the current minor; we do not backport to older
minors. Upgrade to the latest minor to receive security updates.

| Version            | Supported          |
| ------------------ | ------------------ |
| Latest minor (`x.y.*`) | :white_check_mark: |
| Any older minor    | :x:                |

If you are pinned to an older minor, the remediation for a reported vulnerability
is to upgrade to the patched latest minor.

## Triage SLA

We are a small Solo+AI project; timelines are intentionally honest rather than
aspirational:

- **Acknowledgement:** within **3 business days** of receipt.
- **Initial assessment** (in scope? severity? reproducible?): within
  **7 business days** of acknowledgement.
- **Fix and coordinated release:** target **30 days** for High/Critical severity,
  **90 days** for Low/Moderate. If we expect to exceed these, we'll tell you and
  agree on a revised timeline.

Severity is assessed using CVSS v3.1, with weight given to token exposure and
request-integrity impact for this SDK's threat model.

We ask reporters to keep the issue private until a fixed release is available, or
until 90 days have passed if no fix has shipped — whichever comes first. We're
happy to coordinate a disclosure date with you.

## GHSA → CVE Process

We coordinate disclosure through GitHub's advisory system:

1. **Triage.** We confirm the report and assign a severity within the SLA above.
2. **Draft GHSA.** We open a private GitHub Security Advisory (GHSA) for the
   repository, listing affected versions, the fixed version, severity, and
   workarounds. The reporter is credited unless they ask to remain anonymous.
3. **Fix privately.** Where the change is sensitive, we develop and review the fix
   in a private fork tied to the advisory.
4. **Request CVE.** We use GitHub as a CVE Numbering Authority (CNA) to request a
   **CVE ID** through the advisory. The CVE is linked to the GHSA.
5. **Release.** We publish a patched release of the latest minor, reference the
   advisory in the changelog, and (where applicable) yank affected versions from
   PyPI.
6. **Publish.** We publish the GHSA — which propagates the CVE to the
   GitHub Advisory Database, PyPI, and downstream scanners — so users running
   `pip-audit` / dependency scanners are alerted automatically.

## Credit

Unless you request otherwise, we credit reporters by name (or handle) in the
published advisory and release notes. We do not currently run a paid bug-bounty
program.

## License

This project is licensed under **Apache-2.0**. Reporting a vulnerability does not
transfer any rights; it simply helps us keep the SDK and its users safe.
