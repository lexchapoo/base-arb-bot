# Security Policy

This project executes on-chain trading logic and handles flash-loaned funds.
Security reports are taken seriously.

## Supported versions

This is an evolving research/searcher codebase with no long-term support
branches. Only the latest `master` receives security fixes. Pin a commit if you
depend on it.

## Reporting a vulnerability

**Do not open a public issue, pull request, or discussion for a security
vulnerability.** Public disclosure of a live execution flaw can be exploited
against funds before it is fixed.

Report privately:

- Preferred: GitHub **"Report a vulnerability"** (Security → Advisories) on this
  repository, which opens a private advisory.
- Alternatively, contact the maintainer directly via GitHub
  [@lexchapoo](https://github.com/lexchapoo).

Please include:

- affected component (Solidity executor, Rust submission boundary, Python
  planner, signer gateway, or config),
- a description of the impact and the conditions required to trigger it,
- a proof of concept or the exact code path, if available,
- any suggested remediation.

## What to expect

- Acknowledgement of the report as soon as practical.
- An assessment of severity and scope, and a fix or mitigation plan.
- Coordinated disclosure: please allow the issue to be remediated before any
  public write-up. Credit is given to reporters who want it.

## Scope

In scope — anything that could cause loss of funds, incorrect execution, or a
bypass of a safety gate, for example:

- flash-loan repayment or net-profit invariant bypass in `BaseArbExecutor`,
- route-hash commitment, allowlist, target-block, or deadline bypass,
- the Rust `/submit-plan` pre-submission simulation being skippable,
- signer-gateway authentication or key-isolation weaknesses,
- leakage of secrets/keys through code, logs, or config.

Out of scope — issues that do not affect execution safety, for example:

- findings that require already-compromised infrastructure or a malicious
  operator with signer access,
- missing hardening on the optional dry-run/observability tooling,
- results produced by intentionally misconfigured deployments.

## Operational safety posture

- `DRY_RUN=true` is the default; nothing is broadcast in dry-run mode.
- The application never accepts a raw private key; live signing is delegated to
  an external signer (`docs/EXTERNAL_SIGNER.md`, `signer-gateway/`).
- Live trading requires explicit multi-flag acknowledgement and a configured
  external signer.
- Never commit secrets or `.env` files. If you find a committed secret, report
  it privately as above.
