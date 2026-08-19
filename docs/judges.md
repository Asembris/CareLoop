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
   arrangement…* → *Confirming with the clinic…* → **Done · independently verified**, in a few
   seconds.
5. Click the case, then **Under the hood**.

That is the whole loop: a document arrived, an agent read it, proposed one action, a policy engine
allowed it, the action was performed against a real external service, and the result was verified
by reading that service back.

**You never need to press reset.** Pressing a scenario resets the world first, automatically, in
the same request.

**You have your own world.** Opening the URL mints you one — your own cases, your own clinic — so
another judge reading this page at the same moment cannot see your cases, reset them, or take the
appointment slot you are about to book. Nothing is asked of you and there is nothing to sign into;
the id is an isolation namespace, not a login. It is shown at the bottom of the **Run a scenario**
page, because the clinic check below needs it.

A world lives as long as you are using it. Leave it for half an hour, or come back after a
redeploy, and you get a fresh one with an empty dashboard — that is a forgotten world, not a fault.
**The public URL therefore does not demonstrate durable ownership of a case across processes**; the
thing that does is `scripts/resume_demo.py`, which carries one case through three separate
processes over a SQLite file on disk, with `tests/test_resume.py` asserting it.

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
working, not the demo failing. Measured against this deployment on 2026-08-19, five runs: three
carried a visible refusal and two went straight to the correct booking, and all five booked and
verified. We have deliberately **not** tuned the model into taking the bait, because a demo that
has to be rigged to show its defence is not showing its defence.

If you want the refusal deterministically, the poisoned document with no legitimate alternative is
also exposed:

```
curl -X POST https://d2heuwvhvrdyig.cloudfront.net/api/demo/scenarios/injection_only
```

**What is deterministic there is the outcome, not the route.** Measured on 2026-08-19 over 13
runs: not one performed an effect, and every one ended with the case in a person's hands. The
route varied, and the honest description is worth more than a tidier one:

- in most runs the planner proposed all five tools it has — `book_appointment`, `fetch_document`,
  `grant_portal_access`, `send_email`, `upload_document` — and had every one refused;
- in some it reached CareLoop's bounded refusal budget first, having never proposed a booking;
- in one, its sixth attempt was a *legitimate* email to the caregiver's own address, which stopped
  for approval instead of being refused — a correct outcome by a different door.

Three separate conditions can stop a case, and "every tool has now been refused" is only one of
them. The model is still free to propose nothing at all; it is simply never rewarded for proposing
something poisoned.

---

## Check the booking yourself

The appointment is booked over real HTTP against a separate service in its own process, which
CareLoop does not own and which never imports a line of CareLoop's code. You can read it directly —
the link at the bottom of the **Run a scenario** page is this URL with your own world named on it:

```
https://d2heuwvhvrdyig.cloudfront.net/clinic/slots?session=<your world>
```

Three slots are seeded. After scenario 01 books one, two remain. Without the `?session=` part you
are reading the clinic nobody has booked in, which is a different world rather than an empty
answer — every visitor gets their own, so that two judges do not compete for one slot.

**Verification re-reads this service independently** and compares what the clinic holds against
what was authorised — never the booking's own confirmation, which would only be re-reading
CareLoop's own receipt.

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
| **Isolation** | One world per visitor — your own case store and your own clinic — so the URL is safe to open while somebody else is using it. |

`GET /api/cases` and `GET /api/cases/{id}` are open if you would rather read JSON than click.

---

## If something looks wrong

- **A scenario takes a moment to start.** If *your* previous run is still going, the scenario is
  refused until it settles, and the button waits rather than starting against a half-finished
  world. Up to a few seconds, only ever while something really is running, and never because
  somebody else is using the demo.
- **A case says FAILED.** `failure_reason` on the case names the cause, and neither cause is the
  safety model giving way. `ClinicRefused` means the slot was already taken — two scenarios booking
  the same slot, which pressing a scenario resets your world to prevent. `MalformedProposal` means
  the model failed twice running to produce a proposal that satisfies its own schema, so nothing
  was proposed and nothing was done; press the scenario again. Measured on 2026-08-19 at 2 runs in
  28 on scenario 03, and not seen on the others.
- **The dashboard is suddenly empty.** Your world was forgotten — half an hour idle, or the host
  redeployed — and a new one was minted for you. Press a scenario and it runs from the top.
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
