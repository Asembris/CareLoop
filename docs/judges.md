# CareLoop, in sixty seconds

**<https://d2heuwvhvrdyig.cloudfront.net>**

Nothing to install, nothing to sign into. Synthetic data throughout — no real patient, carer,
clinician or clinic exists anywhere in this system.

---

## The sixty seconds

1. Open the URL. You land on the dashboard, which is empty.
2. Click **Run a scenario**.
3. Click **01 — Schedule a cardiology referral**.
4. Watch. A case appears and moves: *Just arrived* → *Working out the next step* → *Making the
   arrangement…* → *Confirming with the clinic…* → **Done · independently verified**, in about
   five seconds.
5. Click the case, then **Under the hood**.

That is the whole loop: a document arrived, an agent read it, proposed one action, a policy engine
allowed it, the action was performed against a real external service, and the result was verified
by reading that service back.

**You never need to press reset.** Pressing a scenario resets the world first, automatically.

---

## The claim worth checking

The interesting claim is not that an agent can book an appointment. It is that **a document telling
the agent to email everything to an attacker cannot make it happen**, and that the audit trail
proves it.

Click **Run a scenario** → **02 — Malicious document**.

A poisoned "continuity of care notice" arrives alongside genuine work. It contains instructions
addressed to the agent, asking it to send records to an outside address and grant portal access.
Open **Under the hood** on the resulting case and read the policy line.

**Two honest things about this scenario.** The poisoned document arrives *beside* legitimate work,
so a well-behaved planner may simply ignore it and book the appointment — which is the system
working, not the demo failing. Measured over six runs, three carried a visible refusal and three
went straight to the correct booking. We have deliberately **not** tuned the model into taking the
bait, because a demo that has to be rigged to show its defence is not showing its defence.

If you want the refusal deterministically, the poisoned document with no legitimate alternative is
also exposed:

```
curl -X POST https://d2heuwvhvrdyig.cloudfront.net/api/demo/scenarios/injection_only
```

That refuses **every** tool it is offered — `book_appointment`, `fetch_document`,
`grant_portal_access`, `send_email`, `upload_document` — five times in five runs, and hands the
case back to a human rather than doing something unsafe. The model is still free to propose
nothing at all; it is simply never rewarded for proposing something poisoned.

---

## Check the booking yourself

The appointment is booked over real HTTP against a separate service in its own process, which
CareLoop does not own and which never imports a line of CareLoop's code. You can read it directly:

```
https://d2heuwvhvrdyig.cloudfront.net/clinic/slots
```

Three slots are seeded. After scenario 01 books one, two remain. **Verification re-reads this
service independently** and compares what the clinic holds against what was authorised — never the
booking's own confirmation, which would only be re-reading CareLoop's own receipt.

---

## The third scenario: it stops

**03 — Pause for a decision.** CareLoop reaches a step it has no authority to take alone, stops,
and asks. Nothing happens until you press **Approve** — and pressing **Reject** sends it back to
plan something else, which it does.

Two details worth noticing on the completed cases:

- Scenario 01 ends **"Done · independently verified"** and plays a seal.
- Scenario 03 ends **"Done · checked against what was authorized"** and plays no seal.

The difference is real. 01 finishes on an action against the external clinic and is verified by
reading that clinic back. 03 finishes on an email, which is simulated, so verification can only
confirm that what was performed matches what was authorised. Both say "done"; only one claims the
outside world agreed. A seal that played on every completion would mean nothing.

---

## What you are looking at

| | |
|---|---|
| **Agents** | Exactly two — Triage classifies, Planner proposes one action. Everything else is deterministic Python. |
| **Authority** | The LLM never holds it. A deterministic policy engine returns ALLOW / DENY / REQUIRE_HUMAN for every tool call, enforced at two independent points. Rules are declarative, in `rules.yaml`. |
| **Trust** | Facts carry `{source_doc, page, trust_level}`. Instructions found in documents never gain authority, whatever they say. |
| **Audit** | Every case carries an append-only timeline: what was proposed, what was allowed or denied, which rule matched, and what was verified. |
| **Hosting** | The Strands agent graph, the API and the dashboard on one host behind CloudFront; the clinic in its own process. |

`GET /api/cases` and `GET /api/cases/{id}` are open if you would rather read JSON than click.

---

## If something looks wrong

- **A scenario takes a moment to start.** If a previous run is still going, the reset that precedes
  every scenario is refused until that run settles, and the button waits rather than starting a
  scenario against a half-finished world. Up to a few seconds, only ever while something really is
  running.
- **A case says FAILED.** Two scenarios booking the same slot is the usual cause, and pressing a
  scenario resets first precisely so that cannot happen. `failure_reason` on the case says which it
  was; `ClinicRefused` means the slot was already taken, not that anything about the safety model
  gave way.
- **The dashboard seems a beat behind.** It is, deliberately and by about a second. The backend
  reaches `DONE` in roughly two seconds, which is faster than anything worth watching, so the
  display walks forward through the states the case genuinely passed through, in the order the
  audit trail records them. Nothing is invented and nothing is reordered.
- **Nothing loads at all.** The host may be between deploys. It comes back on its own.

---

## Operating notes

For whoever is keeping this running, not for judges.

**After judging closes (2026-10-08), terminate the instance — do not stop it.** A stopped EC2
instance still bills its EBS volume every month, and the Elastic-IP-equivalent charge for a public
IPv4 address continues too. Stopping looks like turning it off and is not.

```
aws ec2 terminate-instances --instance-ids i-0aeb7e7150cd7524b
aws cloudfront delete-distribution --id E33IXQBGGBMTTV --if-match <etag>   # disable first
```

The CloudFront distribution must be **disabled and fully deployed** before it can be deleted; the
S3 bucket holding AgentCore deployment packages has a 30-day object expiry and needs nothing.
Everything in the store is synthetic, so nothing needs preserving before teardown.
