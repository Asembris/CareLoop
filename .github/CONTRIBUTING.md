# Contributing to CareLoop

CareLoop is an autonomous administrative agent for family caregivers, built for the AWS
*Agents for Humans* hackathon and licensed [Apache-2.0](https://github.com/Asembris/CareLoop/blob/main/LICENSE). Its position is that the
model proposes, deterministic code authorises, and a separate read of an external system confirms
what happened. Contributions are welcome on those terms.

## Before you start

Read [`CLAUDE.md`](https://github.com/Asembris/CareLoop/blob/main/CLAUDE.md). It carries the architecture invariants, the trust model and the
reasoning behind both.

**Those invariants are correctness constraints, not style preferences.** They are what the security
evaluation measures and what the audit trail rests on. If a change appears to require weakening
one, raise it in an issue or in the pull request description and let it be discussed — do not work
around it locally.

## Local setup

Python **3.12** and Node **24** (`.nvmrc`). The full quickstart is in the
[README](https://github.com/Asembris/CareLoop/blob/main/README.md#quickstart); the short form is:

```bash
python -m venv .venv
.venv/Scripts/pip install -e ".[dev]"     # Linux/macOS: .venv/bin/pip
```

## Checks before a pull request

```bash
ruff check .
ruff format --check .
python -m pytest -q
python -m evals.replay
```

The suite runs offline on a mock model provider, so all four are free and need nothing configured.

For the frontend and the end-to-end suite, follow [`frontend/README.md`](https://github.com/Asembris/CareLoop/blob/main/frontend/README.md)
and [`e2e/README.md`](https://github.com/Asembris/CareLoop/blob/main/e2e/README.md) rather than a copy of their commands here.

**CI runs more than the Python suite.** The schema, frontend and end-to-end jobs are separate and
every one of them blocks a merge. Check the workflow run on your pull request rather than assuming
a green local `pytest` is the whole story.

## Offline, and no secrets

The default test suite is designed to run with no cloud credentials and no provider SDK reachable,
and CI asserts that rather than assuming it. Please keep it that way:

- never commit a credential, key or token, and never paste one into an issue, a pull request or a
  commit message;
- do not add a test that requires credentials to pass. If a change genuinely needs a real provider,
  it belongs behind an opt-in path, not in the default suite.

## What a change must not weaken

Kept deliberately high-level; [`CLAUDE.md`](https://github.com/Asembris/CareLoop/blob/main/CLAUDE.md) is the source of truth for each of these.

- **Deny-by-default authority.** The model never holds authority, and every tool call is decided by
  the deterministic policy engine.
- **Rules live in `rules.yaml`.** A policy rule encoded in Python is a rule nobody can audit.
- **Argument classes belong to the tool registry.** Two declaration sites means one place to forget
  a `DESTINATION`.
- **`ReasonCode` stays closed and uninterpolated.** Denial text reaches the model, so no argument
  value, trust level, rule id or allowlist content may be interpolated into it.
- **The policy loader's validation has no bypass.** There is no parameter that turns a validation
  step off, and none may be added.
- **`TRUSTED` provenance stays tightly controlled** — one producer, from trusted configuration, and
  nothing else may manufacture it.
- **`DESTINATION` and `AUTHORITY` arguments are never promotable**, whatever a validator says.
- **Independent verification stays an independent read.** Verification observes the provider's own
  record with a separate read; reusing what the effect returned is re-reading CareLoop's receipt
  and deletes the only thing verification buys.
- **Every security control needs a test that fails when the control is removed.** A test that still
  passes with the guard disabled was never testing the guard.

## Pull requests

- Keep changes small and focused; one concern per pull request.
- Explain what changed and why, and say what you measured.
- Behaviour changes arrive with tests. Report failures honestly — a known failing case stated
  plainly is far more useful than a green summary that hides it.
- Update the documentation that owns a claim in the same pull request. Several tests assert the
  docs against the source, so a stale claim is a red build.

Commit convention, which this repository follows throughout: **one-line subject, no body, no bullet
list, no trailers.** Prefer several small commits over one large one.

## Security

Please do not report a suspected vulnerability in a public issue or pull request. Report it
privately to the maintainer first — the repository's security policy describes the channel and what
to include.
