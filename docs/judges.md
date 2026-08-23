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

**If you only have time for two, make the second one 04.** Scenario 01 is the loop working; 04 is
the loop surviving an outcome it could not read, which is the harder thing and the one nothing else
on the page shows.

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

## The fourth scenario: the answer gets lost

**04 — The answer gets lost.** This is the one that is hard to see anywhere else, and it is worth
the ten seconds.

Press it and watch the row. It books, and then it stops on a line saying **"Sent, with no answer
back"**. That is true: the clinic accepted the appointment and created it, and the reply was thrown
away before CareLoop could read it. From CareLoop's position an outside system has simply not
answered, and it does not know whether the booking happened.

Then — with nothing clicked, nothing refreshed and nothing restarted — the row moves on by itself
and finishes **Done · independently verified**. Open the case and the trail says what it did:

```
The exact booking was written down before anything was sent
No answer came back — CareLoop does not yet know whether it happened
Retrying the same frozen booking identity — never a new booking
Appointment booked
Clinic record checked independently — match
```

**We throw the reply away on purpose, and the card says so.** That is the only thing this scenario
adds; everything after it is the system that ships — the same policy verdict, the same action
identity frozen before anything left the process, the same five-second reconciliation the deployed
build uses, and the same independent read of the clinic's own record.

**The distinction worth getting right.** CareLoop *did* send the booking again. It re-sent the exact
identity it had frozen beforehand, which is why the clinic answered with the appointment it already
had instead of making a second one. So:

| | |
|---|---|
| requests that really reached the clinic | **2** |
| action identities | **1** |
| appointments at the clinic | **1** |
| plans made | **1** — none after the answer was lost |

Check the last one yourself at `/clinic/slots?session=<your world>`: two of the three seeded slots
are still free, not one. The evidence block on the finished case says the same thing in the same
words, and `tests/test_recovery_copy.py` fails the build if any screen in this product ever starts
claiming CareLoop avoided the second request.

**What the live demo does not show, and where it is.** A CareLoop process that is *killed*
mid-booking recovers the same way, from a durable store, in a brand-new process. That cannot be
demonstrated on this URL — every visitor's world is held in memory so the public demo can give each
judge their own, so a process death there would destroy the very durability the claim is about (and
everyone else's cases with it). It is proven in the repository instead, against a real child
process killed with `os._exit` on the line after the clinic confirms:

```bash
.venv/Scripts/python -m pytest tests/test_saga_chaos.py -q
```

`scripts/resume_demo.py` is the same property over a file on disk, carrying one case through three
separate processes.

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
- **A case says FAILED.** `failure_reason` on the case names the cause, and it is not the safety
  model giving way. `ClinicRefused` means the slot was already taken — two scenarios booking the
  same slot, which pressing a scenario resets your world to prevent.
- **A case says "CareLoop could not work out a safe next step".** The model failed twice running to
  produce a proposal that satisfies its own schema, so nothing was proposed, nothing was evaluated
  and nothing was done. The case stops in **Needs You** rather than failing, because that is what
  actually happened — CareLoop stopped and a person is needed. There is nothing on the card to
  press, by design: no action was proposed, so there is none to approve. Press the scenario again.
  Measured on 2026-08-19 at 2 runs in 28 on scenario 03, and not seen on the others; at that point
  it still ended `FAILED`.
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
