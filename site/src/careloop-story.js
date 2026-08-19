/* CareLoop — living topology engine
 *
 * One system with stable identity and fluid geometry. Every logical node keeps
 * its meaning for the whole page; what changes is where it stands. Narrative
 * states supply target layouts, the engine damps toward them, and edges derive
 * their curvature from wherever their endpoints currently are — so the same
 * network visibly reorganizes rather than being recoloured.
 *
 * scroll → narrative state → target geometry → damped interpolation → render.
 * Never raw scroll pixels driving coordinates.
 *
 * SVG rather than WebGL, deliberately: the argument depends on exact semantic
 * events — a signal stopping at the boundary, a closure edge refusing to
 * connect — and those are art direction, not simulation.
 */

(function () {
  "use strict";

  if (window.__careLoopStory) return;

  const REDUCED = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  const NS = "http://www.w3.org/2000/svg";
  const W = 1000, H = 640, CX = 500, CY = 320;

  const TONES = {
    light: {
      dormant: "#6b675e", graphite: "#5f5b52", structure: "#3f3c35",
      violet: "#695879", amber: "#9c5c2e", sage: "#3f6b5b", terracotta: "#a15a45"
    },
    dark: {
      dormant: "#4d4a44", graphite: "#9c9689", structure: "#b8b2a6",
      violet: "#b49ec4", amber: "#d28a59", sage: "#91b4a7", terracotta: "#cf8a72"
    }
  };

  /* ------------------------------------------------------------ identity */

  /** Stable entities. Radius and label never change; position does. */
  const NODES = {
    event:     { r: 5.2, rank: 2, label: "Care event" },
    gather:    { r: 4.2, rank: 3, label: "Gather" },
    facts:     { r: 4.4, rank: 2, label: "Trusted facts" },
    reason:    { r: 6.0, rank: 1, label: "Reasoning" },
    gate:      { r: 0,   rank: 1, label: "Authority", bar: true },
    human:     { r: 5.4, rank: 1, label: "Human" },
    act:       { r: 5.0, rank: 1, label: "Execution" },
    world:     { r: 4.6, rank: 2, label: "Outside world" },
    verify:    { r: 6.4, rank: 1, label: "Verification" },
    caseN:     { r: 4.8, rank: 2, label: "Case" },
    unsafeEnd: { r: 3.4, rank: 3, label: "", ghost: true }
  };

  /* Edges derive control points from live endpoint positions plus a signed
   * perpendicular bow, so curvature stays elegant through any morph and never
   * develops a kink. `bow` is a fraction of the chord length. */
  const EDGES = [
    { id: "e1", a: "event",  b: "gather", bow: 0.20, w: 1.15 },
    { id: "e2", a: "event",  b: "facts",  bow: -0.20, w: 1.15 },
    { id: "e3", a: "gather", b: "reason", bow: -0.14, w: 1.15 },
    { id: "e4", a: "facts",  b: "reason", bow: 0.14, w: 1.15 },
    { id: "e5", a: "reason", b: "gate",   bow: 0.0, w: 1.35 },
    { id: "e6", a: "gate",   b: "human",  bow: 0.16, w: 1.2 },
    { id: "e7", a: "gate",   b: "act",    bow: 0.0, w: 1.3 },
    { id: "e8", a: "act",    b: "world",  bow: 0.24, w: 1.2 },
    { id: "e9", a: "world",  b: "verify", bow: 0.2, w: 1.25 },
    // The authorized claim, reaching the verifier from a different direction
    // than the observed read-back. Only present where comparison is the point.
    { id: "authz", a: "act", b: "verify", bow: -0.16, w: 1.2, cineOnly: true },
    { id: "closure", a: "verify", b: "caseN", bow: 0.34, w: 1.6, closure: true },
    { id: "back", a: "caseN", b: "event", bow: 0.26, w: 1.05, faint: true },
    { id: "unsafe", a: "reason", b: "unsafeEnd", bow: -0.24, w: 1.1, unsafe: true }
  ];

  /* ---------------------------------------------------- geometry families */

  const p = (x, y, d) => ({ x, y, d: d === undefined ? 1 : d });

  /** CALM — equilibrium. Open, balanced, the quietest shape on the page. */
  const CALM = {
    event: p(126, 300, 0), gather: p(252, 192, 0), facts: p(252, 408, 0),
    reason: p(424, 300, 1), gate: p(560, 300, 1), human: p(560, 508, 0),
    act: p(700, 300, 1), world: p(852, 198, 0), verify: p(880, 402, 1),
    caseN: p(470, 108, 0), unsafeEnd: p(470, 190, 0)
  };

  /** EVENT — equilibrium disturbed. One node pulled inward, the rest lag. */
  const EVENT = {
    event: p(206, 262, 2), gather: p(272, 200, 1), facts: p(258, 402, 0),
    reason: p(428, 298, 1), gate: p(560, 300, 1), human: p(560, 524, 0),
    act: p(716, 306, 0), world: p(858, 206, 0), verify: p(884, 406, 0),
    caseN: p(462, 118, 0), unsafeEnd: p(470, 190, 0)
  };

  /** CONVERGE — evidence gathers toward the reasoning path. */
  const CONVERGE = {
    event: p(132, 300, 0), gather: p(300, 234, 1), facts: p(300, 366, 1),
    reason: p(448, 300, 2), gate: p(560, 300, 1), human: p(568, 520, 0),
    act: p(722, 300, 0), world: p(860, 214, 0), verify: p(882, 396, 0),
    caseN: p(470, 110, 0), unsafeEnd: p(438, 176, 0)
  };

  /** AUTHORITY — momentum arrives at an immovable boundary; downstream recedes. */
  const AUTHORITY = {
    event: p(110, 300, 0), gather: p(268, 248, 0), facts: p(268, 352, 0),
    reason: p(456, 300, 2), gate: p(560, 300, 2), human: p(560, 470, 1),
    act: p(768, 296, 0), world: p(904, 222, 0), verify: p(918, 390, 0),
    caseN: p(480, 106, 0), unsafeEnd: p(452, 172, 0)
  };

  /** HUMAN — upstream alive, downstream drifts out of reach. Unresolved. */
  const HUMAN = {
    event: p(108, 300, 0), gather: p(260, 250, 0), facts: p(260, 350, 0),
    reason: p(448, 300, 1), gate: p(560, 300, 2), human: p(548, 452, 2),
    act: p(838, 288, 0), world: p(948, 228, 0), verify: p(958, 384, 0),
    caseN: p(486, 102, 0), unsafeEnd: p(452, 172, 0)
  };

  /** CORRIDOR — the boundary resolves and a straight permitted route opens. */
  const CORRIDOR = {
    event: p(114, 300, 0), gather: p(256, 264, 0), facts: p(256, 336, 0),
    reason: p(428, 300, 1), gate: p(556, 300, 1), human: p(556, 552, 0),
    act: p(690, 300, 2), world: p(812, 300, 1), verify: p(908, 300, 1),
    caseN: p(470, 122, 0), unsafeEnd: p(440, 190, 0)
  };

  /** COMPARE — authorized above, observed below, the verifier between them. */
  const COMPARE = {
    event: p(108, 300, 0), gather: p(238, 270, 0), facts: p(238, 330, 0),
    reason: p(398, 300, 0), gate: p(524, 300, 1), human: p(524, 548, 0),
    act: p(664, 218, 1), world: p(664, 390, 1), verify: p(856, 300, 2),
    caseN: p(470, 114, 0), unsafeEnd: p(430, 196, 0)
  };

  /** DIVERGE — the same comparison, pulled apart. Two truths, no reconciliation. */
  const DIVERGE = {
    event: p(106, 300, 0), gather: p(234, 272, 0), facts: p(234, 328, 0),
    reason: p(392, 300, 0), gate: p(518, 300, 0), human: p(518, 552, 0),
    act: p(654, 178, 1), world: p(654, 434, 1), verify: p(862, 300, 2),
    caseN: p(452, 106, 0), unsafeEnd: p(424, 200, 0)
  };

  /** RESOLVED — low energy, settled, the missing relation now inevitable. */
  const RESOLVED = {
    event: p(140, 322, 0), gather: p(268, 254, 0), facts: p(268, 384, 0),
    reason: p(416, 320, 0), gate: p(548, 318, 0), human: p(548, 536, 0),
    act: p(676, 304, 1), world: p(792, 246, 1), verify: p(878, 332, 2),
    caseN: p(504, 122, 1), unsafeEnd: p(470, 196, 0)
  };

  /** ATLAS — the compact system unfolded into inspectable layers. */
  const ATLAS = {
    event: p(122, 300, 0), gather: p(150, 148, 0), facts: p(640, 146, 1),
    reason: p(268, 146, 1), gate: p(452, 300, 2), human: p(238, 452, 1),
    act: p(668, 452, 1), world: p(858, 560, 0), verify: p(452, 566, 1),
    caseN: p(806, 300, 0), unsafeEnd: p(300, 232, 0)
  };

  /** FINAL — everything reconciles into one composition around the mark. */
  const FINAL = {
    event: p(214, 226, 0), gather: p(306, 132, 0), facts: p(196, 396, 0),
    reason: p(300, 312, 1), gate: p(410, 470, 1), human: p(552, 536, 0),
    act: p(704, 476, 1), world: p(806, 292, 0), verify: p(818, 398, 2),
    caseN: p(566, 142, 1), unsafeEnd: p(286, 232, 0)
  };

  /* Per-state edge curvature. Tension is semantic: reasoning tightens into the
   * boundary, a corridor flattens, mismatch bows apart, closure draws taut. */
  const BOWS = {
    authority: { e5: 0.0, e3: -0.2, e4: 0.2 },
    human:     { e6: 0.05, e7: 0.3 },
    execute:   { e7: 0.0, e8: 0.06, e9: 0.06 },
    verify:    { e8: -0.16, e9: 0.16 },
    mismatch:  { e8: -0.3, e9: 0.3, closure: 0.5 },
    closed:    { closure: 0.28, back: 0.2 },
    closure:   { closure: 0.3, back: 0.24, e8: 0.18, e9: 0.14 },
    architecture: { e5: 0.1, e7: 0.1, closure: 0.18 }
  };

  const R = { x: 0, y: 0, s: 1, side: "wide" };
  const RIGHT = { x: 215, y: 30, s: 0.82, side: "right" };
  const LEFT = { x: -215, y: 30, s: 0.82, side: "left" };
  const WIDE = { x: 0, y: 110, s: 0.9, side: "wide" };

  /* Narrative states. `base` is resting alpha, `e`/`n` are overrides, `tone`
   * assigns semantic colour. Sage is withheld until verification: action alone
   * is not completion. */
  const STATES = {
    arrival:  { layout: CALM, cam: RIGHT, base: 0.4,
                e: { closure: 0, unsafe: 0, back: 0.2 }, n: { event: 0.8, reason: 0.8, verify: 0.8 } },

    calm:     { layout: CALM, cam: RIGHT, base: 0.3, quiet: true,
                e: { closure: 0, unsafe: 0, back: 0.18 }, n: {} },

    event:    { layout: EVENT, cam: LEFT, base: 0.34,
                e: { e1: 0.85, e2: 0.85, closure: 0, unsafe: 0 }, n: { event: 1 },
                flow: ["e1", "e2"] },

    gather:   { layout: CONVERGE, cam: RIGHT, base: 0.36,
                e: { e1: 0.8, e2: 0.8, e3: 0.8, e4: 0.8, closure: 0, unsafe: 0 },
                n: { gather: 1, facts: 1 }, flow: ["e3", "e4"] },

    reasoning:{ layout: CONVERGE, cam: RIGHT, base: 0.3,
                e: { e5: 0.9, closure: 0, unsafe: 0 }, n: { reason: 1 },
                tone: { reason: "violet", e5: "violet" }, flow: ["e5"], flowTone: "violet" },

    authority:{ layout: AUTHORITY, cam: RIGHT, base: 0.24,
                e: { e5: 0.95, e7: 0.16, closure: 0, unsafe: 0 }, n: { reason: 0.85, gate: 1 },
                tone: { reason: "violet", e5: "violet" }, cine: "stop" },

    human:    { layout: HUMAN, cam: RIGHT, base: 0.22,
                e: { e5: 0.7, e6: 0.95, e7: 0.1, closure: 0, unsafe: 0 }, n: { gate: 1, human: 1 },
                tone: { human: "amber", e6: "amber" }, cine: "stop", parked: true },

    execute:  { layout: CORRIDOR, cam: RIGHT, base: 0.28,
                e: { e7: 0.95, e8: 0.85, closure: 0, unsafe: 0 }, n: { act: 1, world: 0.85 },
                tone: { act: "graphite" }, flow: ["e7", "e8"], flowTone: "graphite" },

    verify:   { layout: COMPARE, cam: RIGHT, base: 0.26,
                e: { e8: 0.5, e9: 0.95, authz: 0.9, closure: 0, unsafe: 0 },
                n: { verify: 1, act: 0.85, world: 0.85 },
                tone: { e9: "sage", verify: "sage", authz: "graphite" },
                cine: "verify", still: 0.92 },

    closed:   { layout: RESOLVED, cam: RIGHT, base: 0.34,
                e: { closure: 0.95, back: 0.45, unsafe: 0 }, n: { verify: 1, caseN: 1 },
                tone: { closure: "sage", verify: "sage", caseN: "sage", e9: "sage" },
                close: true, cine: "close", still: 0.93 },

    unsafe:   { layout: CONVERGE, cam: LEFT, base: 0.26,
                e: { e5: 0.85, unsafe: 0.9, e7: 0.7, closure: 0 }, n: { reason: 1, unsafeEnd: 0.8 },
                tone: { unsafe: "violet", e5: "violet", reason: "violet" },
                sever: true, flow: ["e7"], flowTone: "graphite" },

    mismatch: { layout: DIVERGE, cam: RIGHT, base: 0.24,
                e: { e8: 0.4, e9: 0.9, authz: 0.85, closure: 0.3, unsafe: 0 }, n: { verify: 1, act: 0.85, world: 0.85 },
                e2: 0, tone: { e9: "terracotta", verify: "terracotta", closure: "terracotta", authz: "graphite" },
                cine: "refuse", refuse: true, still: 0.85 },

    architecture: { layout: ATLAS, cam: WIDE, base: 0.24,
                e: { closure: 0.28, unsafe: 0 }, n: {}, atlas: true },

    closure:  { layout: FINAL, cam: R, base: 0.44,
                e: { closure: 0.95, back: 0.5, unsafe: 0 },
                n: { verify: 1, caseN: 1, event: 0.85, gate: 0.85, act: 0.8, world: 0.8, reason: 0.8 },
                tone: { closure: "sage", verify: "sage", caseN: "sage" },
                close: true, cine: "close", finale: true, still: 0.9 }
  };

  const clamp01 = (v) => (v < 0 ? 0 : v > 1 ? 1 : v);
  /** Progress of t through [a,b], clamped. The basis of every scrubbable beat. */
  const span = (t, a, b) => clamp01((t - a) / (b - a));
  const smooth = (t) => t * t * (3 - 2 * t);
  /** Fast, then materially slower over the last stretch. Anticipation. */
  const arrive = (t) => 1 - Math.pow(1 - t, 5);

  /* ------------------------------------------------------------- helpers */

  const el = (n, a) => {
    const e = document.createElementNS(NS, n);
    for (const k in a) e.setAttribute(k, a[k]);
    return e;
  };

  const damp = (cur, target, lambda, dt) => cur + (target - cur) * (1 - Math.exp(-lambda * dt));

  /**
   * Narrow viewports get a genuinely vertical composition, not a squeezed
   * horizontal one: the left-to-right flow becomes top-to-bottom, which is the
   * axis a phone actually has room on. The semantics survive intact.
   */
  function verticalize(pt) {
    return {
      x: CX + (pt.y - CY) * 0.86,
      y: CY + (pt.x - CX) * 0.66,
      d: pt.d
    };
  }

  /* The field must never compete with reading. Two mechanisms, both folded into
   * per-element alpha rather than a CSS mask, so they are exact and capture-safe.
   *
   * 1. sideFade — composition. The camera puts the topology on the empty side.
   * 2. protectFade — measurement. Real glyph and paragraph rects, so centred
   *    states and every narrow viewport (which force side="wide") are covered
   *    too. Priority is typography, then system meaning, then spectacle. */
  function sideFade(localX, cam, side) {
    if (side === "wide") return 1;
    const screenX = CX + (localX - CX) * cam.s + cam.x;
    const t = screenX / W;
    const k = side === "right" ? (t - 0.36) / 0.24 : (0.64 - t) / 0.24;
    const c = Math.max(0, Math.min(1, k));
    return c * c * (3 - 2 * c);
  }

  /* §8's hard/soft distinction. Only text LEAVES are measured — a card or a
   * list item contributes nothing itself, so topology still passes through its
   * empty regions, but never across the words inside it. */
  const HARD_SEL = "h1, h2, p, span, a, button, li";
  const HARD_PAD = 8;         // px grown around each measured glyph run
  const HARD_MARGIN = 44;     // px falloff back to full strength
  const HARD_FLOOR = 0.06;    // what survives directly behind type
  const HARD_CAP = 140;       // rect budget per measurement pass

  /* --------------------------------------------------------------- field */

  class StoryField {
    constructor(host) {
      this.host = host;
      this.theme = this.readTheme();
      this.C = TONES[this.theme];
      this.state = "arrival";
      this.t = 0;
      this.time = 0;
      this.signals = [];
      this.ripples = [];
      this.hover = null;
      this.pointer = { x: 0, y: 0, ax: 0, ay: 0, on: 0 };
      this.cam = { x: 0, y: 0, s: 1 };
      this.narrow = window.innerWidth < 860;
      this.lenFrame = 0;
      this.zoneFrame = 0;
      this.cineWeight = 0;
      this.zones = [];
      this.proj = { ox: 0, oy: 0, k: 1 };
      this.fired = {};
      this.build();
      this.observe();
      this.loop = this.loop.bind(this);
      this.last = performance.now();
      this.raf = requestAnimationFrame(this.loop);
    }

    readTheme() {
      const r = document.querySelector("[data-theme]");
      return r && r.getAttribute("data-theme") === "dark" ? "dark" : "light";
    }

    /**
     * Cache the rects of everything readable that is currently on screen, plus
     * the viewBox→screen projection. Text is measured with getClientRects so a
     * wrapped headline protects its glyph runs, not its whole block box.
     */
    measureZones() {
      const r = this.svg.getBoundingClientRect();
      if (!r.width || !r.height) return;
      const k = Math.min(r.width / W, r.height / H);
      this.proj = { ox: r.left + (r.width - W * k) / 2, oy: r.top + (r.height - H * k) / 2, k };

      const vh = window.innerHeight, vw = window.innerWidth;
      const out = [];
      const rng = document.createRange();
      const els = document.querySelectorAll(HARD_SEL);
      for (const e of els) {
        // Containers are soft: their text-bearing descendants speak for them.
        // The test is direct text, not child count — a headline broken by <br>
        // still owns its words.
        let ownsText = false;
        for (let c = e.firstChild; c; c = c.nextSibling) {
          if (c.nodeType === 3 && c.textContent.trim()) { ownsText = true; break; }
        }
        if (!ownsText) continue;
        const b = e.getBoundingClientRect();
        if (b.bottom < -30 || b.top > vh + 30 || b.width < 6 || b.height < 6) continue;

        let rects;
        try {
          rng.selectNodeContents(e);
          rects = rng.getClientRects();
          if (!rects.length) rects = [b];
        } catch (_) {
          rects = [b];
        }
        for (let i = 0; i < rects.length; i++) {
          const q = rects[i];
          if (q.width < 6 || q.height < 6) continue;
          out.push([
            Math.max(0, q.left - HARD_PAD), Math.max(0, q.top - HARD_PAD),
            Math.min(vw, q.right + HARD_PAD), Math.min(vh, q.bottom + HARD_PAD)
          ]);
        }
        if (out.length >= HARD_CAP) break;
      }
      this.zones = out;

      // Where the copy actually sits this frame. The same narrative state is
      // reused by sections whose text is on opposite sides, so the camera has
      // to follow the measured type rather than a constant on the state.
      let wsum = 0, xsum = 0;
      for (let i = 0; i < out.length; i++) {
        const b = out[i];
        const w = (b[2] - b[0]) * (b[3] - b[1]);
        wsum += w;
        xsum += w * (b[0] + b[2]) / 2;
      }
      this.copySide = wsum > 0 ? (xsum / wsum) / vw : 0.5;
    }

    /** Attenuation for a point in local topology space, from measured type. */
    protectFade(localX, localY) {
      const z = this.zones;
      if (!z.length) return 1;
      const p = this.proj;
      const sx = p.ox + (CX + (localX - CX) * this.cam.s + this.cam.x) * p.k;
      const sy = p.oy + (CY + (localY - CY) * this.cam.s + this.cam.y) * p.k;
      let a = 1;
      for (let i = 0; i < z.length; i++) {
        const b = z[i];
        const dx = Math.max(b[0] - sx, sx - b[2], 0);
        const dy = Math.max(b[1] - sy, sy - b[3], 0);
        const d = Math.sqrt(dx * dx + dy * dy);
        if (d >= HARD_MARGIN) continue;
        const t = d / HARD_MARGIN;
        const c = t * t * (3 - 2 * t);
        a = Math.min(a, HARD_FLOOR + (1 - HARD_FLOOR) * c);
        if (a <= HARD_FLOOR) break;
      }
      return a;
    }

    /** Target position for a node in the current state, viewport-aware. */
    target(key) {
      const S = STATES[this.state] || STATES.calm;
      const pt = (S.layout && S.layout[key]) || CALM[key];
      return this.narrow ? verticalize(pt) : pt;
    }

    build() {
      const host = this.host;
      const svg = el("svg", {
        viewBox: "0 0 " + W + " " + H, preserveAspectRatio: "xMidYMid meet", "aria-hidden": "true"
      });
      svg.style.cssText = "position:absolute;inset:0;width:100%;height:100%;overflow:visible;";
      this.svg = svg;

      this.gCam = el("g", {});
      svg.appendChild(this.gCam);
      this.gEdges = el("g", {});
      this.gRipples = el("g", {});
      this.gNodes = el("g", {});
      this.gSignals = el("g", {});
      this.gLabels = el("g", {});
      this.gCam.appendChild(this.gEdges);
      this.gCam.appendChild(this.gRipples);
      this.gCam.appendChild(this.gNodes);
      this.gCam.appendChild(this.gSignals);
      this.gCam.appendChild(this.gLabels);

      this.nodes = {};
      for (const key in NODES) {
        const def = NODES[key];
        const start = this.target(key);
        let shape;
        if (def.bar) {
          // The authority boundary is a bar, not a node: structurally a
          // different kind of thing from anything the model touches.
          shape = el("rect", { x: -3, y: -34, width: 6, height: 68, rx: 3, opacity: 0 });
        } else {
          shape = el("circle", { cx: 0, cy: 0, r: def.r, opacity: 0 });
        }
        const g = el("g", {});
        const halo = el("circle", { cx: 0, cy: 0, r: def.r + 9, fill: "none", "stroke-width": 0.9, opacity: 0 });
        g.appendChild(halo);
        g.appendChild(shape);
        this.gNodes.appendChild(g);

        const label = el("text", { "text-anchor": "middle", opacity: 0, y: -(def.bar ? 46 : def.r + 14) });
        label.style.cssText = "font-family:'IBM Plex Mono',monospace;font-size:10px;letter-spacing:0.1em;text-transform:uppercase;";
        label.textContent = def.label;
        const lg = el("g", {});
        lg.appendChild(label);
        this.gLabels.appendChild(lg);

        this.nodes[key] = {
          def, g, lg, el: shape, halo, label,
          x: start.x, y: start.y, d: start.d,
          a: 0, s: 1, la: 0
        };
      }

      this.edges = {};
      for (const e of EDGES) {
        const path = el("path", { fill: "none", "stroke-linecap": "round", opacity: 0 });
        if (e.unsafe) path.style.strokeDasharray = "5 6";
        this.gEdges.appendChild(path);
        this.edges[e.id] = { def: e, el: path, a: 0, len: 400, bow: e.bow, drawn: 0 };
      }
      // Cinematic elements are persistent and reused, never spawned per event,
      // so every beat is a pure function of scroll progress and reverses cleanly.
      this.cine = {
        a: el("circle", { r: 2.6, opacity: 0 }),
        b: el("circle", { r: 2.6, opacity: 0 }),
        r1: el("circle", { fill: "none", "stroke-width": 0.9, opacity: 0 }),
        r2: el("circle", { fill: "none", "stroke-width": 0.6, opacity: 0 })
      };
      this.gRipples.appendChild(this.cine.r1);
      this.gRipples.appendChild(this.cine.r2);
      this.gSignals.appendChild(this.cine.a);
      this.gSignals.appendChild(this.cine.b);

      host.appendChild(svg);
      this.measureZones();
      this.geometry(0.0001);
    }

    observe() {
      // The theme root is React-owned; watch the document, not a captured node.
      this.onTheme = new MutationObserver(() => {
        const t = this.readTheme();
        if (t !== this.theme) { this.theme = t; this.C = TONES[t]; }
      });
      this.onTheme.observe(document.documentElement,
        { attributes: true, subtree: true, attributeFilter: ["data-theme"] });

      if (!REDUCED) {
        this.onMove = (ev) => {
          const r = this.svg.getBoundingClientRect();
          if (!r.width) return;
          this.pointer.x = ((ev.clientX - r.left) / r.width) * W;
          this.pointer.y = ((ev.clientY - r.top) / r.height) * H;
          this.pointer.on = ev.clientY > r.top - 120 && ev.clientY < r.bottom + 120 ? 1 : 0;
        };
        window.addEventListener("pointermove", this.onMove, { passive: true });
      }

      this.onResize = () => { this.narrow = window.innerWidth < 860; };
      window.addEventListener("resize", this.onResize, { passive: true });

      this.onVis = () => {
        if (document.hidden) { cancelAnimationFrame(this.raf); this.raf = 0; }
        else if (!this.raf) { this.last = performance.now(); this.raf = requestAnimationFrame(this.loop); }
      };
      document.addEventListener("visibilitychange", this.onVis);
    }

    /* ------------------------------------------------------- public API */

    setState(name, t) {
      if (!STATES[name]) return;
      if (name !== this.state) {
        this.prev = this.state;
        this.state = name;
        this.entered = this.time;
        this.fired = {};
      }
      if (typeof t === "number") this.t = Math.max(0, Math.min(1, t));
    }

    focus(key) {
      this.hover = key && this.nodes[key] ? key : null;
    }

    /** Boundary-removed counterfactual. Nothing dramatic happens — that is the point. */
    setMutant(on) {
      this.mutant = !!on;
    }

    /** Pin a state for inspection. Scroll stops driving until released. */
    pinState(name, t) {
      if (!name) {
        this.pinned = null;
        if (this.rereadScroll) this.rereadScroll();
        return;
      }
      this.pinned = name;
      this.setState(name, t === undefined ? 0.75 : t);
    }

    /** Fail-open demonstration: the decorative field can vanish entirely. */
    /** No valid trust edge forms between equal-looking facts. */
    setNoBridge(on) {
      this.noBridge = !!on;
    }

    setHidden(on) {
      this.hidden = !!on;
      this.host.style.transition = "opacity 420ms ease";
      this.host.style.opacity = on ? "0" : "1";
    }

    /* ------------------------------------------------------- primitives */

    emit(edgeId, tone, opts) {
      if (REDUCED || this.dead) return;
      const e = this.edges[edgeId];
      if (!e) return;
      const o = opts || {};
      this.signals.push({
        edge: edgeId, tone: tone || "graphite", p: 0,
        speed: o.speed || 0.62, park: o.park || 0, alpha: 1, dying: 0, r: o.r || 2.4
      });
    }

    ripple(key, tone) {
      if (REDUCED || this.dead) return;
      const n = this.nodes[key];
      if (!n) return;
      // Two ultra-fine rings, out of phase. The signature verification gesture.
      this.ripples.push({ x: n.x, y: n.y, r: n.def.r, tr: 44, a: 0.5, tone: tone || "sage", w: 0.9 });
      setTimeout(() => {
        if (this.dead) return;
        this.ripples.push({ x: n.x, y: n.y, r: n.def.r, tr: 66, a: 0.28, tone: tone || "sage", w: 0.6 });
      }, 135);
    }

    once(key, fn, delay) {
      if (this.fired[key]) return;
      this.fired[key] = 1;
      if (delay) setTimeout(() => { if (!this.dead) fn(); }, delay);
      else fn();
    }

    /* ------------------------------------------------------------ frame */

    loop(now) {
      const dt = Math.min(0.05, (now - this.last) / 1000);
      this.last = now;
      this.time += dt;
      this.tick(dt);
      this.raf = requestAnimationFrame(this.loop);
    }

    /** Move node positions toward the state's layout. Move, settle, communicate. */
    geometry(dt) {
      // Reduced motion snaps between stable layouts instead of morphing.
      const lam = REDUCED ? 60 : 2.6;
      let moved = 0;
      for (const key in this.nodes) {
        const n = this.nodes[key];
        const t = this.target(key);
        // The boundary is structurally fixed: topology organizes around it,
        // never the other way round. It still repositions between families,
        // but faster and without lag, so it reads as ground truth.
        const l = key === "gate" ? lam * 1.5 : lam;
        const px = n.x, py = n.y;
        n.x = damp(n.x, t.x, l, dt);
        n.y = damp(n.y, t.y, l, dt);
        n.d = damp(n.d, t.d, l, dt);
        moved += Math.abs(n.x - px) + Math.abs(n.y - py);
      }
      return moved;
    }

    tick(dt) {
      const S = STATES[this.state] || STATES.calm;
      const C = this.C;
      const moved = this.geometry(dt);

      const cam = S.cam || R;
      this.zoneFrame = (this.zoneFrame + 1) % 12;
      if (this.zoneFrame === 0) this.measureZones();

      const wide = (cam.side || "wide") === "wide";
      const side = this.narrow || wide ? "wide"
        : ((this.copySide === undefined ? 0.5 : this.copySide) < 0.5 ? "right" : "left");
      const camX = wide ? cam.x : (side === "right" ? 215 : -215);

      this.cam.x = damp(this.cam.x, camX, 2.2, dt);
      this.cam.y = damp(this.cam.y, cam.y, 2.2, dt);
      this.cam.s = damp(this.cam.s, cam.s, 2.2, dt);
      this.gCam.setAttribute("transform",
        "translate(" + (CX + this.cam.x).toFixed(1) + " " + (CY + this.cam.y).toFixed(1) +
        ") scale(" + this.cam.s.toFixed(3) + ") translate(" + -CX + " " + -CY + ")");

      const quiet = window.innerWidth < 860 ? 0.55 : 1;
      const bows = BOWS[this.state] || {};

      /* ---- edges: geometry first, then meaning ---- */
      for (const id in this.edges) {
        const s = this.edges[id];
        const A = this.nodes[s.def.a];
        const B = this.nodes[s.def.b];

        // Control point from live endpoints plus a damped perpendicular bow,
        // so curvature morphs with the layout instead of snapping.
        s.bow = damp(s.bow, bows[id] !== undefined ? bows[id] : s.def.bow, 2.4, dt);
        const dx = B.x - A.x, dy = B.y - A.y;
        const mx = (A.x + B.x) / 2, my = (A.y + B.y) / 2;
        s.el.setAttribute("d",
          "M " + A.x.toFixed(1) + " " + A.y.toFixed(1) +
          " Q " + (mx - dy * s.bow).toFixed(1) + " " + (my + dx * s.bow).toFixed(1) +
          " " + B.x.toFixed(1) + " " + B.y.toFixed(1));

        let want = S.e && S.e[id] !== undefined ? S.e[id]
          : s.def.cineOnly ? 0
          : (s.def.faint ? S.base * 0.5 : S.base);

        if (this.noBridge && (id === "e4" || id === "e2")) want = 0.06;
        // With the gate gone the previously blocked branch is simply reachable.
        if (this.mutant) {
          if (s.def.unsafe) want = 0.9;
          else if (id === "e7") want = Math.max(want, 0.85);
        }

        if (this.hover) {
          const touches = s.def.a === this.hover || s.def.b === this.hover;
          want = touches ? 0.85 : want * (S.atlas ? 0.22 : 0.5);
        }
        if (s.def.unsafe && S.sever) {
          // The rejected branch withers rather than blinking out.
          want *= 1 - Math.max(0, Math.min(1, (this.t - 0.45) / 0.35));
        }

        // Depth: farther structures read lighter and thinner.
        const depth = (A.d + B.d) / 2;
        // Sample the QUADRATIC the edge actually draws, not its chord, and take
        // a coverage average rather than the worst point: a long arc that clears
        // the type for two thirds of its length should read for those two thirds.
        const ccx = mx - dy * s.bow, ccy = my + dx * s.bow;
        let cov = 0;
        for (let q = 0; q <= 8; q++) {
          const u = q / 8, v = 1 - u, w0 = v * v, w1 = 2 * v * u, w2 = u * u;
          cov += this.protectFade(w0 * A.x + w1 * ccx + w2 * B.x, w0 * A.y + w1 * ccy + w2 * B.y);
        }
        let guard = cov / 9;

        const focused = this.hover && (s.def.a === this.hover || s.def.b === this.hover);
        if (focused) guard = guard * 0.35 + 0.65;
        if (this.mutant && (s.def.unsafe || id === "e7")) guard = guard * 0.35 + 0.65;
        // §8 sanctions exactly one intentional overlap: the closing arc passing
        // near the final statement. It is the page's culmination — art-directed,
        // not accidental, so it is allowed to read across the type.
        if (s.def.closure && S.close) guard = guard * 0.3 + 0.7;

        const fade = sideFade((A.x + B.x) / 2, this.cam, side) * guard;
        s.a = damp(s.a, want * fade * quiet * (0.72 + depth * 0.18), 3.4, dt);
        s.el.setAttribute("opacity", s.a.toFixed(3));
        s.el.setAttribute("stroke-width", (s.def.w * (0.82 + depth * 0.14)).toFixed(2));

        let tone = (S.tone && S.tone[id]) || (s.def.unsafe ? "violet" : "dormant");
        // Only the newly reachable relationship changes colour.
        if (this.mutant && (s.def.unsafe || id === "e7")) tone = "terracotta";
        s.el.setAttribute("stroke", C[tone] || C.dormant);

        if (s.def.closure) {
          let drawn = 0;
          // The last stretch is materially slower: the visitor anticipates contact.
          if (S.close) drawn = arrive(span(this.t, 0.34, 0.86));
          // Begins, reaches a legibly partial amount, and stops. Stays open.
          else if (S.refuse) drawn = 0.38 * smooth(span(this.t, 0.3, 0.62));
          s.drawn = damp(s.drawn, drawn, REDUCED ? 60 : 2.6, dt);
          if (this.lenFrame === 0) s.len = s.el.getTotalLength ? s.el.getTotalLength() : 400;
          s.el.style.strokeDasharray = s.len;
          s.el.style.strokeDashoffset = (s.len * (1 - s.drawn)).toFixed(1);

        } else if (this.lenFrame === 0) {
          s.len = s.el.getTotalLength ? s.el.getTotalLength() : 400;
        }
      }
      this.lenFrame = (this.lenFrame + 1) % 10;

      /* ---- nodes ---- */
      for (const key in this.nodes) {
        const n = this.nodes[key];
        let want = S.n && S.n[key] !== undefined ? S.n[key] : S.base + 0.12;
        let labelOn = 0;

        if (n.def.ghost && !(S.n && S.n[key] !== undefined)) want = 0;
        if (this.mutant) {
          if (n.def.bar) want = 0;                 // the boundary is not there
          else if (key === "unsafeEnd") want = 0.9;
        }
        if (this.hover) {
          const focused = this.hover === key;
          want = focused ? 1 : want * (S.atlas ? 0.26 : 0.55);
          labelOn = focused ? 1 : 0;
        }

        n.g.setAttribute("transform", "translate(" + n.x.toFixed(1) + " " + n.y.toFixed(1) + ")");
        n.lg.setAttribute("transform", "translate(" + n.x.toFixed(1) + " " + n.y.toFixed(1) + ")");

        const shield = this.protectFade(n.x, n.y);
        const fade = sideFade(n.x, this.cam, side) *
          (this.hover === key ? shield * 0.3 + 0.7 : shield);
        n.a = damp(n.a, want * fade * quiet * (0.74 + n.d * 0.17), 3.6, dt);
        n.la = damp(n.la, labelOn, 4, dt);
        n.el.setAttribute("opacity", n.a.toFixed(3));
        n.label.setAttribute("opacity", (n.la * 0.8).toFixed(3));

        const tone = (S.tone && S.tone[key]) || (n.def.bar ? "structure" : "graphite");
        n.el.setAttribute("fill", C[tone] || C.graphite);
        n.label.setAttribute("fill", C.graphite);

        // Proximity: the nearest node swells and takes a fine halo.
        let scale = 1, halo = 0;
        if (this.pointer.on && !REDUCED && !n.def.bar) {
          const px = (this.pointer.ax - CX - this.cam.x) / this.cam.s + CX;
          const py = (this.pointer.ay - CY - this.cam.y) / this.cam.s + CY;
          const infl = Math.max(0, 1 - Math.hypot(px - n.x, py - n.y) / 110) * (1 - 0.85 * (this.cineWeight || 0));
          scale += infl * 0.5;
          halo = infl * 0.45;
        }
        n.s = damp(n.s, scale * (0.9 + n.d * 0.07), 6, dt);
        if (!n.def.bar) n.el.setAttribute("r", (n.def.r * n.s).toFixed(2));
        else n.el.setAttribute("transform", "scale(1 " + n.s.toFixed(3) + ")");
        n.halo.setAttribute("opacity", (Math.max(halo, n.la * 0.4) * n.a).toFixed(3));
        n.halo.setAttribute("stroke", C[tone === "dormant" ? "sage" : tone] || C.sage);
      }

      const weight = this.cinematic(S);
      // The narrative outranks the cursor during a critical beat, and stillness
      // after closure is the reward — nothing drifts.
      const settled = S.still !== undefined && this.t > S.still ? 1 : 0;
      this.cineWeight = damp(this.cineWeight || 0, Math.max(weight, settled), 3, dt);

      this.pointer.ax = damp(this.pointer.ax, this.pointer.x, 5, dt);
      this.pointer.ay = damp(this.pointer.ay, this.pointer.y, 5, dt);

      this.script(S, dt, moved);
      this.stepSignals(dt);
      this.stepRipples(dt);
    }

    /** Point at fraction u along an edge's drawn path. */
    onEdge(id, u) {
      const e = this.edges[id];
      if (!e || !e.el.getPointAtLength) return null;
      return e.el.getPointAtLength(clamp01(u) * e.len);
    }

    place(node, pt, tone, alpha, r) {
      if (!pt) return;
      node.setAttribute("cx", pt.x.toFixed(1));
      node.setAttribute("cy", pt.y.toFixed(1));
      node.setAttribute("fill", this.C[tone] || this.C.graphite);
      node.setAttribute("opacity", clamp01(alpha).toFixed(3));
      if (r) node.setAttribute("r", r.toFixed(2));
    }

    ring(node, cx, cy, radius, alpha, tone) {
      node.setAttribute("cx", cx.toFixed(1));
      node.setAttribute("cy", cy.toFixed(1));
      node.setAttribute("r", Math.max(0.1, radius).toFixed(1));
      node.setAttribute("stroke", this.C[tone] || this.C.sage);
      node.setAttribute("opacity", clamp01(alpha).toFixed(3));
    }

    /**
     * The five signature moments. Every value derives from local scroll
     * progress, so each is scrubbable, reversible, and lands coherently if the
     * visitor arrives mid-section. No chained timeouts.
     *
     * Returns how much this frame belongs to the narrative rather than the
     * cursor: during a critical beat the system prioritises its own story.
     */
    cinematic(S) {
      const c = this.cine;
      c.a.setAttribute("opacity", 0);
      c.b.setAttribute("opacity", 0);
      c.r1.setAttribute("opacity", 0);
      c.r2.setAttribute("opacity", 0);
      if (!S.cine) return 0;

      const t = this.t;
      const V = this.nodes.verify;
      // Convert a few screen pixels into local units for the approach gap.
      const px = 1 / Math.max(0.0001, this.proj.k * this.cam.s);

      if (S.cine === "stop") {
        /* Proposal is not permission. The signal decelerates hard and halts a
         * few pixels short of the bar — the gap is the whole argument. */
        const e5 = this.edges.e5;
        const gapU = e5.len > 0 ? (3 + 3.2 * px) / e5.len : 0.02;
        const travel = S.parked ? 1 : arrive(span(t, 0.04, 0.72));
        const u = travel * (1 - gapU);
        const pt = this.onEdge("e5", u);
        // A tiny pulse at the signal itself on arrival. The gate does not react.
        const land = span(t, 0.66, 0.8);
        const r = 2.6 * (1 + 0.35 * (1 - smooth(land)) * (travel > 0.97 ? 1 : 0));
        this.place(c.a, pt, "violet", 0.95, S.parked ? 2.6 * (0.92 + Math.sin(this.time * 2.6) * 0.08) : r);
        return travel > 0.9 ? 1 : 0.55;
      }

      if (S.cine === "verify" || S.cine === "refuse") {
        const bad = S.cine === "refuse";
        const tone = bad ? "terracotta" : "sage";

        /* Two inputs, from two directions. The verifier did not take the
         * executor's word for it. */
        const observed = arrive(span(t, 0.05, 0.46));
        const claimed = arrive(span(t, 0.12, 0.52));
        // On a mismatch the claim never completes its approach: it stalls short.
        const claimU = bad ? claimed * 0.82 : claimed;
        this.place(c.b, this.onEdge("e9", observed), bad ? "terracotta" : "graphite",
          observed > 0.001 ? 0.95 : 0, 2.4);
        this.place(c.a, this.onEdge("authz", claimU), "graphite",
          claimed > 0.001 ? 0.9 * (bad ? 1 - span(t, 0.62, 0.85) * 0.65 : 1) : 0, 2.4);

        // Verifier contracts by a percent or two, then the core resolves.
        const contract = span(t, 0.5, 0.56) * (1 - span(t, 0.56, 0.66));
        V.s = V.s * (1 - 0.02 * contract);

        // First ring, then a second ~140ms of progress later, out of phase.
        const g1 = span(t, 0.54, 0.86), g2 = span(t, 0.6, 0.92);
        if (g1 > 0) this.ring(c.r1, V.x, V.y, V.def.r + g1 * 40, (1 - g1) * 0.5, tone);
        if (g2 > 0) this.ring(c.r2, V.x, V.y, V.def.r + g2 * 62, (1 - g2) * 0.26, tone);

        return span(t, 0.42, 0.56) * (1 - span(t, 0.9, 1));
      }

      if (S.cine === "close") {
        /* Closure is downstream of verification: a restrained acknowledgement
         * fires before the missing relation is constructed. */
        const echo = span(t, 0.06, 0.3);
        if (echo > 0 && echo < 1) {
          this.ring(c.r1, V.x, V.y, V.def.r + echo * 34, (1 - echo) * 0.34, "sage");
          this.ring(c.r2, V.x, V.y, V.def.r + span(t, 0.1, 0.34) * 50,
            (1 - span(t, 0.1, 0.34)) * 0.16, "sage");
        }
        // One sage signal enters the newly completed relation, then stillness.
        const run = span(t, 0.72, 0.92);
        if (run > 0 && run < 1) {
          this.place(c.a, this.onEdge("closure", run), "sage", (1 - Math.pow(run, 4)) * 0.95, 2.7);
        }
        return span(t, 0.3, 0.5) * (1 - span(t, 0.9, 1));
      }

      return 0;
    }

    /** Chapter scripting: causally ordered, each event fired once. */
    script(S, dt, moved) {
      if (REDUCED) return;

      // True silence in the calm state — no signals at all, so the referral
      // arriving actually registers as a disturbance.
      if (S.quiet && !this.mutant) return;
      if (!this.mutant && S.still !== undefined && this.t > S.still) return;

      if (S.flow) {
        this.flowT = (this.flowT || 0) + dt;
        if (this.flowT > 1.5 + Math.random() * 1.4) {
          this.flowT = 0;
          this.emit(S.flow[Math.floor(Math.random() * S.flow.length)], S.flowTone || "graphite");
        }
      }

      if (this.mutant) {
        this.mutantT = (this.mutantT || 0) + dt;
        if (this.mutantT > 1.9) {
          this.mutantT = 0;
          this.emit("unsafe", "terracotta", { speed: 0.5 });
        }
      }

      if (S.sever) {
        this.once("severSignal", () => this.emit("unsafe", "violet", { speed: 0.5 }));
        if (this.t > 0.5) this.once("severLegit", () => this.emit("e7", "graphite"), 250);
      }

      // Nodes acknowledge a large morph a beat after it settles.
      if (moved < 0.6 && this.time - (this.entered || 0) > 0.9) {
        this.once("settled", () => {
          if (S.ripple || S.quiet) return;
          const keys = Object.keys(S.n || {});
          if (keys.length) this.pulse(keys[0]);
        });
      }
    }

    pulse(key) {
      const n = this.nodes[key];
      if (!n) return;
      n.halo.style.transition = "none";
      n.halo.setAttribute("opacity", 0.35);
      setTimeout(() => { if (!this.dead) n.halo.setAttribute("opacity", 0); }, 420);
    }

    stepSignals(dt) {
      const C = this.C;
      const keep = [];
      for (const s of this.signals) {
        const e = this.edges[s.edge];
        if (!e || !e.el.getPointAtLength) continue;
        if (!s.dying) {
          const limit = s.park ? 0.965 : 1;
          s.p = Math.min(limit, s.p + s.speed * dt);
          if (s.p >= limit) {
            if (s.park) {
              s.hold = (s.hold || 0) + dt;
              if (s.hold > 1.5) s.dying = 1;
            } else {
              s.dying = 1;
              if (s.edge === "e9") this.ripple("verify", "sage");
            }
          }
        } else {
          s.alpha -= dt * 2.6;
        }
        if (s.alpha <= 0) { if (s.el) s.el.remove(); continue; }
        if (!s.el) {
          s.el = el("circle", { r: s.r });
          this.gSignals.appendChild(s.el);
        }
        const pt = e.el.getPointAtLength(s.p * e.len);
        s.el.setAttribute("cx", pt.x);
        s.el.setAttribute("cy", pt.y);
        s.el.setAttribute("fill", C[s.tone] || C.graphite);
        const pulse = s.park && s.hold ? 0.7 + Math.sin(this.time * 3.2) * 0.3 : 1;
        s.el.setAttribute("opacity", (Math.max(0, s.alpha) * Math.min(1, e.a * 1.6 + 0.25) * pulse).toFixed(3));
        keep.push(s);
      }
      this.signals = keep;
    }

    stepRipples(dt) {
      const C = this.C;
      const keep = [];
      for (const r of this.ripples) {
        r.r = damp(r.r, r.tr, 1.9, dt);
        r.a -= dt * 0.42;
        if (r.a <= 0) { if (r.el) r.el.remove(); continue; }
        if (!r.el) {
          r.el = el("circle", { fill: "none", "stroke-width": r.w });
          this.gRipples.appendChild(r.el);
        }
        r.el.setAttribute("cx", r.x);
        r.el.setAttribute("cy", r.y);
        r.el.setAttribute("r", r.r.toFixed(1));
        r.el.setAttribute("stroke", C[r.tone] || C.sage);
        r.el.setAttribute("opacity", Math.max(0, r.a).toFixed(3));
        keep.push(r);
      }
      this.ripples = keep;
    }

    destroy() {
      this.dead = true;
      cancelAnimationFrame(this.raf);
      if (this.onMove) window.removeEventListener("pointermove", this.onMove);
      window.removeEventListener("resize", this.onResize);
      document.removeEventListener("visibilitychange", this.onVis);
      if (this.onTheme) this.onTheme.disconnect();
      if (this.host && this.host.parentNode) this.host.remove();
    }
  }

  /* ------------------------------------------------------ scroll control */

  /**
   * Sections declare a chapter with data-story. data-story-scrub makes a
   * section's own progress select among a comma-separated chapter list — that
   * is how the pinned workflow advances without hijacking the scrollbar.
   */
  function bindScroll(field) {
    let ticking = false;
    const read = () => {
      ticking = false;
      const marks = document.querySelectorAll("[data-story]");
      if (!marks.length) return;
      const vh = window.innerHeight;
      const mid = vh * 0.5;
      let best = null, bestD = Infinity;

      for (const m of marks) {
        const r = m.getBoundingClientRect();
        if (r.bottom < 0 || r.top > vh) continue;
        // A pinned section owns the frame for its whole length: its midpoint is
        // only near the viewport centre at ~50%, so nearest-to-centre would
        // hand its final chapters to whatever comes next.
        if (m.hasAttribute("data-story-scrub") && r.top <= 0 && r.bottom >= vh) { best = m; break; }
        const d = Math.abs(r.top + r.height / 2 - mid);
        if (d < bestD) { bestD = d; best = m; }
      }
      if (!best) return;

      const r = best.getBoundingClientRect();
      const total = r.height - vh;
      const t = total > 0 ? Math.max(0, Math.min(1, -r.top / total)) : 0.5;

      if (field.pinned) return;
      if (best.hasAttribute("data-story-scrub")) {
        const chapters = best.getAttribute("data-story").split(",");
        const wAttr = best.getAttribute("data-story-weights");
        const w = wAttr ? wAttr.split(",").map(Number) : null;
        if (w && w.length === chapters.length) {
          // Weighted spans: a setup beat passes quickly, a decisive one dwells.
          const total = w.reduce((a, b) => a + b, 0);
          let acc = 0, idx = chapters.length - 1, local = 1;
          for (let i = 0; i < w.length; i++) {
            const lo = acc / total;
            acc += w[i];
            const hi = acc / total;
            if (t < hi || i === w.length - 1) { idx = i; local = (t - lo) / (hi - lo); break; }
          }
          field.setState(chapters[idx].trim(), clamp01(local));
        } else {
          const raw = t * chapters.length;
          const idx = Math.min(chapters.length - 1, Math.floor(raw));
          field.setState(chapters[idx].trim(), raw - idx);
        }
      } else {
        field.setState(best.getAttribute("data-story"), t);
      }
    };

    const onScroll = () => {
      if (ticking) return;
      ticking = true;
      requestAnimationFrame(read);
    };
    window.addEventListener("scroll", onScroll, { passive: true });
    return read;
    window.addEventListener("resize", onScroll, { passive: true });
    read();
  }

  /** Delegated, so targets survive every template re-render. */
  function bindAtlas(field) {
    const keyOf = (ev) => {
      const t = ev.target && ev.target.closest && ev.target.closest("[data-atlas]");
      return t ? t.getAttribute("data-atlas") : null;
    };
    document.addEventListener("pointerover", (ev) => { const k = keyOf(ev); if (k) field.focus(k); });
    document.addEventListener("pointerout", (ev) => { if (keyOf(ev)) field.focus(null); });
    document.addEventListener("focusin", (ev) => field.focus(keyOf(ev)));
    // Tap-driven on touch: hover cannot be a requirement.
    document.addEventListener("click", (ev) => {
      const k = keyOf(ev);
      if (k) field.focus(field.hover === k ? null : k);
    });
  }

  function mount() {
    if (!document.body) return false;
    try {
      // The module owns its container. A React-owned div gets replaced on every
      // re-render, orphaning the SVG with it.
      const host = document.createElement("div");
      host.id = "careloop-story";
      host.setAttribute("aria-hidden", "true");
      host.style.cssText = "position:fixed;inset:0;z-index:0;pointer-events:none;";
      document.body.appendChild(host);

      const field = new StoryField(host);
      field.rereadScroll = bindScroll(field);
      bindAtlas(field);
      window.__careLoopStory = field;
      return true;
    } catch (err) {
      console.warn("[CareLoop] story field unavailable:", err && err.message);
      return true;
    }
  }

  if (!mount()) {
    const obs = new MutationObserver(() => { if (mount()) obs.disconnect(); });
    obs.observe(document.documentElement, { childList: true });
    setTimeout(() => obs.disconnect(), 15000);
  }
})();
