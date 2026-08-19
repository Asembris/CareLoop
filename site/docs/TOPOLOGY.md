# Topology reference

`src/careloop-story.js` — one persistent scene, mounted to `#careloop-story`,
which the module creates and appends to `document.body` itself.

That last detail matters: it used to live in the page markup, but the framework
owned that node and replaced it on every re-render, orphaning the scene. The
module now owns its own container.

## Graph

Ten nodes, twelve edges, fixed identity across the whole page.

| Node | Role |
|---|---|
| `event` | An incoming care event |
| `gather` | Evidence collection |
| `facts` | Validated facts with provenance |
| `reason` | The agent's proposal |
| `gate` | The authority boundary — a bar, not a node |
| `human` | The person |
| `act` | Execution |
| `world` | External systems |
| `verify` | Independent read-back |
| `caseN` | Durable case state |

The gate is drawn as a bar precisely because it is structurally a different kind
of thing from anything the model touches.

Two edges carry special behaviour:

- `closure` exists at zero length and resolves only where the world agreed.
- `authz` (act → verify) exists so verification receives two inputs from two
  directions — the authorized claim and the observed record — rather than a
  single chain. Without it, comparison could not be shown.

## States

```
event · gather · reasoning · authority · human · execute
verify · mismatch · closed · unsafe · architecture · closure · final
```

Scroll selects a state; a pinned section scrubs through several. Inspection
controls can pin a state, which suspends scroll until released.

## Public API

```js
window.__careLoopStory.setState(name, t)   // 0..1 within the state
window.__careLoopStory.focus(atlasId)      // emphasise a subsystem
window.__careLoopStory.pinState(name, t)   // pin; null releases and re-reads
window.__careLoopStory.setMutant(bool)     // remove the boundary
window.__careLoopStory.setNoBridge(bool)   // suppress a trust relation
window.__careLoopStory.setHidden(bool)     // fail-open demonstration
window.__careLoopStory.destroy()
```

## Text protection

Node and edge alpha is attenuated by measured distance from text on screen.

- Only text **leaves** are measured, via `getClientRects` on their contents. A
  card contributes nothing; the words inside it do. This is what lets topology
  pass through empty card regions without ever crossing type.
- Edges sample the real quadratic curve at nine points and take a coverage
  average. Sampling the straight chord instead — or taking `Math.min` — kills an
  arc that clears the type for most of its length.
- Focused elements blend their shield toward 1. Direct interaction is the one
  sanctioned overlap.

Measurement runs every twelfth frame at roughly 0.5ms.

## Performance

One SVG group each for edges, nodes and signals, updated in place rather than
rebuilt. The loop stops when
the tab is hidden. Around 0.3–0.4ms per frame in steady state.

## Reduced motion

The field renders a static composition. The pinned section drops to a normal
length, because there is nothing for a long scroll to reveal once the morphs are
off. Every semantic difference between states survives as a still frame.
