# Content map

Sixteen sections, twelve numbered. The three unnumbered sub-chapters sit inside
a numbered arc and are intentionally unlabelled.

| # | Eyebrow | Does | Topology state |
|---|---|---|---|
| — | `CARELOOP` | Hero. The thesis in one line. | `event` |
| 01 | `THE PERSON` | Who this is for, and the administrative load. | `event` |
| — | `A REFERRAL ARRIVES` | The concrete case the rest of the page follows. | `event` |
| 02 | `THE LAST-MILE PROBLEM` | Why coordination fails, not diagnosis. | `gather` |
| 03 | `ONE REAL WORKFLOW` | **Pinned.** Eight scrubbed steps, weighted. | eight states |
| 04 | `THE RULE THAT MATTERS` | The authority boundary, stated plainly. | `authority` |
| 05 | `WHEN CARELOOP REFUSES` | Refusal as a designed state. | `unsafe` |
| — | `VERIFICATION DISAGREEMENT` | The world contradicts the agent's claim. | `mismatch` |
| — | `AND WHEN IT AGREES` | Independent read-back matches; the loop closes. | `closed` |
| 06 | `DEMO FILM` | Poster over a real product frame. | `closure` |
| 07 | `MEASURED` | Proof ledger. Six inspectable entries. | `closure` |
| 08 | `LOAD-BEARING` | Counterfactual: remove the boundary, watch it change. | `authority` |
| 09 | `UNDER THE HOOD` | Architecture. Eight subsystems as topology controls. | `architecture` |
| 10 | `FAILURE MODES` | Seven failures, each a live simulation. | per failure |
| 11 | `HONEST SCOPE` | What is real and what is simulated. | `closure` |
| 12 | `EVALUATE IT` | Three ways in, and the closing statement. | `closure` |

## Section 03 — the pinned chapter

Eight steps, unequally weighted so the important ones get more scroll:

```
Observe 8.6%  ·  Gather  ·  Reason  ·  Authority 15.4%
Human 15.4%   ·  Execute ·  Verify  ·  Close
```

The narrative column and the topology both read `data-story-weights`, so they
cannot drift apart. The line "The model can propose this action. It cannot
authorize itself." surfaces exactly at the authority step.

## Product captures

| File | Placed | Proves |
|---|---|---|
| `calm.png` | Beside "Nothing needs you right now" | The default state is quiet |
| `authority.png` | Under "One decision is waiting" | The stop is real UI, not a metaphor |
| `verification.png` | After the mismatch chapter | Authorized vs observed, side by side |
| `execution.png` | Demo film poster | The workflow running |

All captured from the running build at 3×, flattened onto the page background.
Clicking any opens a restrained lightbox; Escape closes.

Note: the lightbox image is a CSS background, not an `<img>`. A URL in a
template hole gets preloaded literally before hydration, which throws a 404 on
every load. Keep it as a background.
