# Security policy

## Status of this project

CareLoop is an open-source demonstration project, built for a hackathon. It is not a product and it
carries no long-term support: no maintained release branches, no backports, no security
certification. Work happens on `main`, and that is the only place a fix will appear. What has been
evaluated, and what deliberately has not, is maintained in
[`docs/security-evaluation.md`](../docs/security-evaluation.md) — read its own section on what the
evaluation does not cover before deciding whether something is in scope.

## Reporting a vulnerability

**Use GitHub's private vulnerability reporting: the repository's *Security* tab → *Report a
vulnerability*.** It is enabled here, the report is visible only to the maintainer, and no email
address is needed.

Please do not open a public issue, pull request or discussion, and do not post publicly, with
details that would let somebody exploit the problem — not until it is fixed, or until we have
agreed that publishing is safe.

## What makes a report useful

- the commit or tag you saw it on, if you know it;
- what you observed, and what you expected instead;
- the smallest set of steps that reproduces it;
- whether it happened locally or against the public demo;
- which documented invariant you believe is broken, if you can name one — they are numbered in
  [`CLAUDE.md`](../CLAUDE.md);
- the impact as you understand it.

Reproducible offline evidence — a failing test, or steps against a local checkout — is the most
useful kind, because the suite runs without credentials. Working exploit code is welcome but not
required; please do not build one just to file a report.

## Please do not send real data

Everything in CareLoop's demo is **invented**: the patient, the caregiver, the clinician, the clinic
and the documents were all written for this project. None of it is anonymized or de-identified real
data, because there was never any real data to start from.

Please keep your report the same way. Do not include real patient data, protected health
information, credentials, API keys, tokens, or personal information about anyone — yours or a third
party's — in a report or in a reproduction.

## Testing the public demo

Reproducing something by hand in your own demo session is fine, and is what the demo is for. Please
do not run denial-of-service or load testing, broad automated scanning, credential attacks,
anything aimed at the hosting infrastructure rather than the application's own interface, or
anything that reaches into another visitor's session. The demo is shared, and one person's scan is
everybody else's outage.

## Documented behaviour is not automatically a vulnerability

Several behaviours look like weaknesses and are deliberate, measured and documented. Demo-mode
`X-Caregiver` being an audit identity rather than authentication, and `?session=` being an isolation
namespace rather than a credential, are two of them. The maintained scope lives in the README's
*What this does not claim* and *Honest limits* sections and in
[`docs/security-evaluation.md`](../docs/security-evaluation.md) — please check there first.

**But:** if a documented invariant does not actually hold, or one of these behaviours turns out to
be reachable in a materially different way from the way it is documented, that is a real security
report and we would very much like to hear it.

## What to expect

Reports are read and reviewed by the maintainer. There is no response-time commitment, no bounty, no
CVE process, and no promise of a published advisory for every report — this is one person's project,
and saying otherwise would be a promise nobody can keep. If a report leads to a fix and you would
like to be credited, say so and you will be.

For anything that is not a security issue, see [CONTRIBUTING.md](CONTRIBUTING.md).
