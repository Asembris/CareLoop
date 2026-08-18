/* CareLoop — boot choreography, "Close the Loop"
 *
 * The origin state of the same orchestration system that runs behind the app:
 * same palette, same hairline grammar, same signal and ripple vocabulary.
 *
 * The composition is a deliberately unresolved structure. One long arc enters
 * from the upper left and passes BEHIND the mark, ending at a dead end on the
 * right. A second run descends through the reasoning junction to verification.
 * Those two ends do not meet — that asymmetric gap is the whole point, and the
 * final verified connection is what makes the structure whole.
 *
 * Sequence: origin → awakening → decision → traversal → verification → closure.
 * A state machine, not a pile of timeouts: a fast load runs every beat
 * compressed, a slow load breathes instead of replaying.
 *
 * SVG-first by requirement: it must paint before any WebGL context exists,
 * download nothing, and never be the reason the app fails to appear.
 *
 * ---------------------------------------------------------------------------
 * Vendored from `careloop-motion` (frontend/docs/BOOT.md records the version
 * and every delta). Kept as close to upstream as the integration allows: each
 * departure is marked `LOCAL DELTA` and numbered, so the next upgrade is a
 * diff rather than a merge.
 *
 *   1  run policy      once per tab, `?boot=off` sticky, `?boot=on|full` replay
 *   2  dark paper      #26241f -> #1a1713, CareLoop's actual --background
 *   3  theme chain     data-theme -> .dark -> storage -> system -> light
 *   4  status copy     one neutral line; CareLoop performs no boot-time work
 *   5  accessibility   the overlay leaves the accessibility tree entirely
 *   6  fail open       idempotent finalize(), guarded FLIP, hard exit timer
 *   7  timing          condensed path tuned to this application's readiness
 * ---------------------------------------------------------------------------
 */

(function () {
  "use strict";

  // Idempotent: a second evaluation of this classic script must be a no-op.
  if (window.CareLoopBootController) return;

  const REDUCED = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  const MIN_VISIBLE = 1100;   // never flash: a boot shorter than this still gets a gesture
  const CONDENSED = 1800;     // fast load: every beat, compressed
  const FULL_RUN = 3900;      // full cold-load choreography

  /* LOCAL DELTA 6a — the hand-off has its own deadline, independent of whether it works.
   *
   * Upstream's only hard timer forces `ready()` at nine seconds, which decides *when to leave*
   * and guarantees nothing about *leaving*. Everything after that point — measuring two rects,
   * moving a node to `document.body`, one transform — is unguarded, and anything that throws in
   * there leaves an opaque overlay at z-index 9000 over a working application with the navbar
   * mark invisible underneath it. This is the timer that makes `exit()` finish whatever
   * `exit()` does.
   *
   * Comfortably longer than the exit choreography it bounds, because it is an outer bound and
   * not a schedule. `index.html` holds a third one at twelve seconds that does not depend on
   * this object existing at all. */
  const HARD_EXIT = 2000;

  /* When the overlay starts becoming transparent, measured from the start of the hand-off. The
   * application is perceptible from here, so it is also the moment the overlay stops taking
   * clicks. */
  const FADE_AT = 140;

  const FORCE = (() => {
    try {
      return new URLSearchParams(location.search).get("boot");
    } catch (_) {
      return null;
    }
  })();

  /* LOCAL DELTA 1 — the run policy, which upstream leaves as "every page load".
   *
   * Two properties, and both are about a switch meaning what it says.
   *
   * **`?boot=off` is sticky for the tab.** Upstream reads it out of
   * `location.search` once. CareLoop routes on the client, so `navigate("/")`
   * drops the query and the next reload boots again — a kill switch a
   * navigation silently undoes is not a kill switch. It is written to
   * `sessionStorage` and read back on every load.
   *
   * **The loader runs once per tab.** It is an entrance, and a judge who
   * reloads mid-demo has already seen it. `?boot=on` and `?boot=full` are the
   * explicit replays; both clear a sticky "off" as well, so one flag is always
   * enough to get back to a known state.
   *
   * `sessionStorage` rather than `localStorage`, deliberately: a second tab is
   * a second arrival and deserves the entrance. Every access is guarded — a
   * browser that refuses storage falls back to the URL flag alone, which is
   * upstream's behaviour and fails towards showing the loader rather than
   * towards suppressing it.
   */
  const RUN_KEY = "careloop:boot";

  function bootStore() {
    try {
      return window.sessionStorage;
    } catch (_) {
      return null;
    }
  }

  function shouldRun() {
    const store = bootStore();
    if (FORCE === "off") {
      try {
        if (store) store.setItem(RUN_KEY, "off");
      } catch (_) {
        /* Nothing to remember it with; the flag still holds for this load. */
      }
      return false;
    }
    if (FORCE === "on" || FORCE === "full") {
      try {
        if (store) store.removeItem(RUN_KEY);
      } catch (_) {
        /* An explicit replay that cannot clear the flag still replays. */
      }
      return true;
    }
    try {
      if (store && store.getItem(RUN_KEY) !== null) return false;
    } catch (_) {
      /* Unreadable storage means an unremembered arrival, which is an arrival. */
    }
    return true;
  }

  function rememberShown() {
    try {
      const store = bootStore();
      if (store) store.setItem(RUN_KEY, "shown");
    } catch (_) {
      /* Then it shows again on the next load, which is upstream's behaviour. */
    }
  }

  const PALETTE = {
    light: {
      paper: "#f7f4ee", ink: "#3b3833", statusInk: "#5c584f",
      graphite: "#6b675e", dormant: "#b3afa5", sage: "#4a7d6c", violet: "#7d6a8f",
      forest: "#315e52", sageMark: "#a6b09d"
    },
    /* LOCAL DELTA 2 — the dark paper is CareLoop's, not the package's.
     *
     * Upstream ships `#26241f` and its own token table claims that is
     * `oklch(0.205 0.008 70)`. It is not: that colour resolves to `#1a1713`,
     * and `#1a1713` is what `--background` paints in `tokens.css`. Twelve
     * levels per channel is not a rounding difference — the overlay reads as a
     * lighter sheet over the page, and it announces itself at exactly the
     * moment the hand-off is supposed to be seamless. Light was already within
     * one level and is untouched. */
    dark: {
      paper: "#1a1713", ink: "#e9e5dd", statusInk: "#b5afa4",
      graphite: "#9c9689", dormant: "#4d4a44", sage: "#91b4a7", violet: "#b49ec4",
      forest: "#91b4a7", sageMark: "#c7cfbe"
    }
  };

  /* Composition in a 1000×620 viewBox. The mark sits at (500, 262) — every node
   * below earns its place in the eye path; nothing floats decoratively.
   *
   *   entry ─ gather ──(behind the mark)── close        ← arc with a dead end
   *              └── junction ── verify ── outward      ← reasoning and traversal
   *                       └ muted
   *
   * verify sits at roughly 4–5 o'clock from the mark; close at 1 o'clock. The
   * missing relationship between them is the structural gap.
   */

  const NODES = {
    entry:    { p: [178, 132], rank: 2 },
    gather:   { p: [292, 236], rank: 2 },
    junction: { p: [452, 402], rank: 1 },
    muted:    { p: [356, 520], rank: 2 },
    verify:   { p: [682, 396], rank: 1 },
    outward:  { p: [918, 296], rank: 2 },
    close:    { p: [614, 188], rank: 1 }
  };

  const PATHS = [
    { id: "frame1", from: "entry", to: "gather", q: [212, 178], tone: "dormant", w: 1, rest: 0.34 },
    // Passes behind the mark: the one important path that touches the centre.
    { id: "frame2", from: "gather", to: "close", q: [446, 306], tone: "graphite", w: 1.05, rest: 0.42 },
    { id: "reason", from: "gather", to: "junction", q: [332, 328], tone: "graphite", w: 1, rest: 0.4 },
    { id: "branchMuted", from: "junction", to: "muted", q: [396, 466], tone: "dormant", w: 1, rest: 0.15 },
    { id: "traverse", from: "junction", to: "verify", q: [556, 438], tone: "graphite", w: 1.1, rest: 0.46 },
    { id: "outwardP", from: "verify", to: "outward", q: [812, 372], tone: "dormant", w: 1, rest: 0.18 },
    // The missing connection. Bows outward to the right — asymmetric, never a circle.
    { id: "closure", from: "verify", to: "close", q: [742, 288], tone: "sage", w: 1.2, rest: 0.5 }
  ];

  /* LOCAL DELTA 4 — one line, because CareLoop is not doing five things.
   *
   * Upstream cycles "Connecting context", "Preparing trusted actions", "Verifying workspace". Not
   * one of those describes anything happening during this application's startup: there is no async
   * bootstrap, no session to establish, nothing to verify — the loader is running beside a React
   * mount and nothing else. Narrating work that is not occurring is the same small lie a decided
   * card refuses to tell elsewhere in this product, and it would be the one piece of copy in
   * CareLoop that is decorative rather than true.
   *
   * So the copy says only what is true, and the *choreography* carries the orchestration story —
   * which is what it was drawn to do. The state machine is untouched: every state still exists,
   * still advances, and still reads back through `CareLoopBootController.state()`. Only the
   * sentence is the same one. */
  const PREPARING = "Preparing CareLoop";

  const STATUS = {
    entering: PREPARING,
    orchestrating: PREPARING,
    authorizing: PREPARING,
    verifying: PREPARING,
    waiting: PREPARING,
    ready: PREPARING
  };

  const EASE = "cubic-bezier(0.22, 0.61, 0.36, 1)";      // settles into equilibrium
  const EASE_SOFT = "cubic-bezier(0.33, 0.0, 0.2, 1)";

  function svgEl(name, attrs) {
    const el = document.createElementNS("http://www.w3.org/2000/svg", name);
    for (const k in attrs) el.setAttribute(k, attrs[k]);
    return el;
  }

  function pathD(p) {
    const a = NODES[p.from].p;
    const b = NODES[p.to].p;
    return `M ${a[0]} ${a[1]} Q ${p.q[0]} ${p.q[1]} ${b[0]} ${b[1]}`;
  }

  class CareLoopBoot {
    constructor() {
      this.theme = this.detectTheme();
      this.P = PALETTE[this.theme];
      this.state = "entering";
      this.t0 = performance.now();
      this.appReady = false;
      this.done = false;
      this.finalized = false;
      this.timers = [];
      this.ambient = null;
      this.build();
      this.run();
    }

    /* LOCAL DELTA 3 — the application decides the theme; this only reads its decision.
     *
     * In practice the first branch always answers. The blocking script in `index.html` writes
     * `data-theme` on `<html>` before the first stylesheet and before this file is parsed, and it
     * has already folded the system preference into that value — so a first-time visitor whose OS
     * is dark gets a dark loader on the very first painted frame, with no branch here involved.
     *
     * The rest of the chain exists so the loader is never the thing that disagrees. `.dark` is the
     * class every token keys off and is the same decision in the form the stylesheet reads;
     * storage is the persisted choice; the system preference is the default that choice overrides;
     * light is canonical. Agreeing with the application matters more than being independently
     * right, which is why the attribute is consulted first and everything else is a fallback for a
     * page that has not written one. */
    detectTheme() {
      const root = document.querySelector("[data-theme]");
      if (root) return root.getAttribute("data-theme") === "dark" ? "dark" : "light";
      if (document.documentElement.classList.contains("dark")) return "dark";
      try {
        const stored = window.localStorage.getItem("careloop:theme");
        if (stored === "dark" || stored === "light") return stored;
      } catch (_) {
        /* No storage access. The system preference is still readable. */
      }
      try {
        return window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
      } catch (_) {
        return "light";
      }
    }

    at(ms, fn) {
      this.timers.push(setTimeout(fn, ms));
    }

    /* ------------------------------------------------------------- BootLogo */

    markSvg(size) {
      const s = svgEl("svg", { viewBox: "0 0 64 64", width: size, height: size, fill: "none" });
      s.style.display = "block";
      s.appendChild(svgEl("circle", { cx: 18.5, cy: 11.5, r: 5.4, fill: this.P.forest }));
      s.appendChild(svgEl("circle", { cx: 45.5, cy: 15, r: 4.3, fill: this.P.sageMark }));
      s.appendChild(svgEl("path", {
        d: "M32 55.5c11.5-7.2 24.5-14.7 24.5-24.2 0-8-6.8-11.3-12-7.9-2.5 1.6-3.9 3.7-4.3 6",
        stroke: this.P.sageMark, "stroke-width": 5.8, "stroke-linecap": "round", "stroke-linejoin": "round"
      }));
      s.appendChild(svgEl("path", {
        d: "M32 55.5C20 48 7.5 40.5 7.5 30.5c0-8.5 7.5-12 13-8.5 2.6 1.7 4.1 3.9 4.5 6.5",
        stroke: this.P.forest, "stroke-width": 6.6, "stroke-linecap": "round", "stroke-linejoin": "round"
      }));
      return s;
    }

    /* --------------------------------------------------------------- build */

    build() {
      const root = document.createElement("div");
      root.id = "careloop-boot";
      /* LOCAL DELTA 5 — the loader is decoration and leaves the accessibility tree entirely.
       *
       * Upstream makes the overlay a `role="status"` live region with a label, and then changes
       * the text five times: five polite announcements for an animation, over an application that
       * is *not* hidden from assistive technology. Nothing here is `inert`, nothing is
       * `aria-hidden` on `#root`, and no focus is moved — so a screen-reader user is already on
       * the real, usable CareLoop from the first committed frame, and the honest thing to say
       * about a picture they cannot see is nothing.
       *
       * The same treatment `#careloop-field` gets, for the same reason. It also means the overlay
       * contains nothing focusable and can trap nothing, which `e2e/tests/boot.spec.ts` asserts
       * rather than assumes. */
      root.setAttribute("aria-hidden", "true");
      root.style.cssText = `position:fixed;inset:0;z-index:9000;background:${this.P.paper};display:flex;align-items:center;justify-content:center;font-family:'Instrument Sans',system-ui,sans-serif;opacity:1;transition:opacity 560ms ${EASE_SOFT};`;
      this.root = root;

      const svg = svgEl("svg", { viewBox: "0 0 1000 620", preserveAspectRatio: "xMidYMid slice", "aria-hidden": "true" });
      svg.style.cssText = "position:absolute;inset:0;width:100%;height:100%;";
      this.svg = svg;
      this.net = svgEl("g", {});
      svg.appendChild(this.net);

      this.pathEls = {};
      for (const p of PATHS) {
        const el = svgEl("path", {
          d: pathD(p), fill: "none", stroke: this.P[p.tone],
          "stroke-width": p.w, "stroke-linecap": "round", opacity: 0
        });
        el.style.strokeDasharray = 900;
        el.style.strokeDashoffset = REDUCED ? 0 : 900;
        this.net.appendChild(el);
        this.pathEls[p.id] = el;
      }

      // Nodes carry rank: primary workflow nodes read slightly larger.
      this.nodeEls = {};
      for (const key in NODES) {
        const def = NODES[key];
        const [x, y] = def.p;
        const g = svgEl("g", { opacity: 0 });
        const halo = svgEl("circle", { cx: x, cy: y, r: 11, fill: "none", stroke: this.P.sage, "stroke-width": 0.85, opacity: 0 });
        const base = def.rank === 1 ? 3.5 : 2.3;
        const dot = svgEl("circle", { cx: x, cy: y, r: base, fill: def.rank === 1 ? this.P.graphite : this.P.dormant });
        g.appendChild(halo);
        g.appendChild(dot);
        g.style.transition = `opacity 520ms ${EASE}`;
        this.net.appendChild(g);
        this.nodeEls[key] = { g, dot, halo, x, y, base };
      }

      // Two ultra-fine rings; the second follows out of phase.
      this.ripples = [];
      for (let i = 0; i < 2; i++) {
        const r = svgEl("circle", {
          cx: NODES.verify.p[0], cy: NODES.verify.p[1], r: 5,
          fill: "none", stroke: this.P.sage, "stroke-width": i === 0 ? 0.9 : 0.6, opacity: 0
        });
        this.net.appendChild(r);
        this.ripples.push(r);
      }

      this.signalLayer = svgEl("g", {});
      this.net.appendChild(this.signalLayer);
      root.appendChild(svg);

      const stack = document.createElement("div");
      stack.style.cssText = "position:relative;display:flex;flex-direction:column;align-items:center;gap:32px;transform:translateY(-16px);";

      const lock = document.createElement("div");
      // Blur is a trace ingredient only — the mark must never look low-resolution.
      lock.style.cssText = `display:flex;align-items:center;gap:14px;opacity:0;filter:blur(1.4px);transition:opacity 560ms ${EASE_SOFT}, filter 620ms ${EASE};`;
      this.lock = lock;

      const markWrap = document.createElement("div");
      markWrap.appendChild(this.markSvg(38));
      this.markWrap = markWrap;

      const word = document.createElement("span");
      word.textContent = "CareLoop";
      word.style.cssText = `font-size:27px;font-weight:600;letter-spacing:-0.03em;color:${this.P.ink};opacity:0;transform:translateY(3px);transition:opacity 680ms ${EASE_SOFT} 220ms, transform 700ms ${EASE} 220ms;`;
      this.word = word;

      lock.appendChild(markWrap);
      lock.appendChild(word);

      const status = document.createElement("span");
      status.textContent = STATUS.entering;
      status.style.cssText = `font-size:13px;letter-spacing:0.01em;color:${this.P.statusInk};opacity:0;transition:opacity 620ms ${EASE_SOFT};`;
      this.status = status;

      stack.appendChild(lock);
      stack.appendChild(status);
      root.appendChild(stack);
      this.stack = stack;

      document.body.appendChild(root);

      if (!REDUCED) {
        this.onPointer = (e) => {
          const dx = (e.clientX / window.innerWidth - 0.5) * 12;
          const dy = (e.clientY / window.innerHeight - 0.5) * 8;
          this.net.style.transform = `translate(${dx}px, ${dy}px)`;
          this.net.style.transition = `transform 950ms ${EASE}`;
        };
        window.addEventListener("pointermove", this.onPointer, { passive: true });
      }
    }

    /* ------------------------------------------------------------- Signals */

    /** Signal particles are tiny and transient — clearly not nodes. */
    signal(pathId, tone, opts = {}) {
      if (this.done || !this.pathEls) return;
      const el = this.pathEls[pathId];
      if (!el) return;
      const dur = opts.dur || 620;
      const r = opts.r || 2.2;
      const dot = svgEl("circle", { r, fill: this.P[tone], opacity: 0.92 });
      this.signalLayer.appendChild(dot);

      const trail = [];
      const trailCount = opts.trail === false ? 0 : 3;
      for (let i = 0; i < trailCount; i++) {
        const t = svgEl("circle", { r: r * (1 - (i + 1) * 0.2), fill: this.P[tone], opacity: 0.24 - i * 0.07 });
        this.signalLayer.appendChild(t);
        trail.push(t);
      }

      const total = el.getTotalLength ? el.getTotalLength() : 400;
      const start = performance.now();
      const step = (now) => {
        if (this.done) {
          dot.remove();
          trail.forEach((t) => t.remove());
          return;
        }
        const raw = Math.min(1, (now - start) / dur);
        // Eases in, carries through the middle, settles — decided, then acting.
        const e = raw < 0.5 ? 2 * raw * raw : 1 - Math.pow(-2 * raw + 2, 2) / 2;
        const place = (node, tt) => {
          const pt = el.getPointAtLength(Math.max(0, Math.min(1, tt)) * total);
          node.setAttribute("cx", pt.x);
          node.setAttribute("cy", pt.y);
        };
        place(dot, e);
        trail.forEach((t, i) => place(t, e - (i + 1) * 0.04));
        if (raw < 1) {
          requestAnimationFrame(step);
        } else {
          dot.style.transition = `opacity 240ms ${EASE_SOFT}`;
          dot.style.opacity = 0;
          trail.forEach((t) => { t.style.transition = `opacity 190ms ${EASE_SOFT}`; t.style.opacity = 0; });
          setTimeout(() => { dot.remove(); trail.forEach((t) => t.remove()); }, 300);
          if (opts.onArrive) opts.onArrive();
        }
      };
      requestAnimationFrame(step);
    }

    drawPath(pathId, dur, opacity) {
      if (!this.pathEls) return;
      const el = this.pathEls[pathId];
      if (!el) return;
      el.style.transition = `stroke-dashoffset ${dur}ms ${EASE}, opacity ${Math.min(dur, 460)}ms ${EASE_SOFT}, stroke 420ms ${EASE_SOFT}`;
      el.style.strokeDashoffset = 0;
      el.setAttribute("opacity", opacity);
    }

    showNode(key, delay) {
      if (!this.nodeEls) return;
      const n = this.nodeEls[key];
      if (!n) return;
      this.at(delay || 0, () => { if (!this.done) n.g.setAttribute("opacity", 1); });
    }

    /**
     * A node acknowledges a signal ~60ms after it lands: a nearly imperceptible
     * contraction first, then the tone resolves and it settles back.
     */
    respond(key, tone, delay) {
      if (this.done || !this.nodeEls) return;
      const n = this.nodeEls[key];
      if (!n) return;
      this.at(delay ?? 60, () => {
        n.dot.style.transition = `r 90ms ${EASE_SOFT}, fill 300ms ${EASE_SOFT}`;
        n.dot.setAttribute("r", n.base * 0.84);
        this.at(90, () => {
          n.dot.style.transition = `r 300ms ${EASE}, fill 320ms ${EASE_SOFT}`;
          n.dot.setAttribute("fill", this.P[tone || "sage"]);
          n.dot.setAttribute("r", n.base * 1.28);
          this.at(300, () => n.dot.setAttribute("r", n.base));
        });
      });
    }

    halo(key) {
      if (this.done || !this.nodeEls) return;
      const n = this.nodeEls[key];
      if (!n) return;
      n.halo.style.transition = `opacity 400ms ${EASE_SOFT}, r 620ms ${EASE}`;
      n.halo.setAttribute("opacity", 0.5);
      n.halo.setAttribute("r", 15);
      this.at(600, () => n.halo.setAttribute("opacity", 0));
    }

    ripple(delay, index, radius, opacity) {
      this.at(delay, () => {
        if (this.done || !this.ripples) return;
        const r = this.ripples[index];
        if (!r) return;
        r.style.transition = "none";
        r.setAttribute("r", 5);
        r.setAttribute("opacity", opacity);
        requestAnimationFrame(() => {
          r.style.transition = `r 1050ms ${EASE}, opacity 1050ms ${EASE_SOFT}`;
          r.setAttribute("r", radius);
          r.setAttribute("opacity", 0);
        });
      });
    }

    /** A barely perceptible synchronized breath through the structure. */
    breath() {
      if (this.done || !this.pathEls) return;
      const lift = ["frame1", "frame2", "reason", "traverse", "closure"];
      for (const id of lift) {
        const el = this.pathEls[id];
        if (!el) continue;
        const at = parseFloat(el.getAttribute("opacity")) || 0.4;
        el.style.transition = `opacity 260ms ${EASE_SOFT}`;
        el.setAttribute("opacity", Math.min(0.72, at + 0.09));
        this.at(280, () => {
          el.style.transition = `opacity 520ms ${EASE_SOFT}`;
          el.setAttribute("opacity", at);
        });
      }
    }

    setStatus(key) {
      if (!STATUS[key]) return;
      // Nothing to cross-fade when the sentence is the same one; see LOCAL DELTA 4. Without this
      // the line would blink at every beat, which is exactly the churn the single line removes.
      if (this.status.textContent === STATUS[key]) return;
      this.status.style.opacity = 0;
      this.at(240, () => {
        this.status.textContent = STATUS[key];
        this.status.style.opacity = 0.88;
      });
    }

    /** Marks the closure complete: the mark reaches perfect optical sharpness. */
    resolveMark() {
      this.lock.style.filter = "none";
      this.lock.style.opacity = 1;
    }

    /* ----------------------------------------------------------- choreography */

    run() {
      if (REDUCED) {
        this.resolveMark();
        this.word.style.opacity = 1;
        this.word.style.transform = "none";
        this.status.style.opacity = 0.88;
        for (const key in this.nodeEls) this.nodeEls[key].g.setAttribute("opacity", 1);
        for (const p of PATHS) this.pathEls[p.id].setAttribute("opacity", p.rest);
        this.state = "waiting";
        this.maybeFinish();
        return;
      }

      /* Phase 01 — Origin. Structure converges, then the mark becomes legible fast. */
      this.showNode("entry", 40);
      this.showNode("gather", 170);
      this.at(80, () => this.drawPath("frame1", 620, 0.34));
      this.at(240, () => {
        // The one path that touches the centre, drawn behind the mark.
        this.drawPath("frame2", 780, 0.42);
        this.lock.style.opacity = 1;
        this.lock.style.filter = "none";
        this.word.style.opacity = 1;
        this.word.style.transform = "none";
      });
      this.at(470, () => { this.status.style.opacity = 0.88; });

      /* Phase 02 — Awakening. Only enough structure to imply more beyond frame. */
      this.at(760, () => {
        this.state = "orchestrating";
        this.setStatus("orchestrating");
        this.showNode("junction", 0);
        this.drawPath("reason", 640, 0.4);
        this.signal("frame1", "graphite", { dur: 560, onArrive: () => this.respond("gather", "graphite") });
      });
      this.at(1180, () => {
        // Passing behind the mark, arriving at the dead end on the right.
        this.signal("frame2", "graphite", { dur: 780, onArrive: () => this.respond("close", "graphite") });
        this.showNode("close", 260);
      });

      /* Phase 03 — Decision. Violet appears only here, and only briefly. */
      this.at(1560, () => {
        this.state = "authorizing";
        this.setStatus("authorizing");
        this.showNode("muted", 0);
        this.drawPath("branchMuted", 520, 0.15);
        this.drawPath("traverse", 660, 0.2);
        this.showNode("verify", 120);
      });
      this.at(1700, () => {
        this.signal("reason", "violet", {
          dur: 520,
          onArrive: () => { this.respond("junction", "violet"); this.halo("junction"); }
        });
      });
      this.at(2140, () => {
        // The branch that is not taken: it moves, then simply stops mattering.
        this.signal("branchMuted", "dormant", { dur: 640, r: 1.8, trail: false });
      });

      /* Phase 04 — Traversal. Sage begins to earn dominance. */
      this.at(2260, () => {
        this.pathEls.traverse.style.stroke = this.P.sage;
        this.drawPath("traverse", 420, 0.46);
        this.signal("traverse", "sage", { dur: 520 });
        this.drawPath("outwardP", 900, 0.18);
        this.showNode("outward", 220);
      });

      /* Phase 05 — The hero: verification into closure, ~800ms end to end. */
      const H = 2820;
      this.at(H, () => {                                   // Beat A — arrival
        this.state = "verifying";
        this.setStatus("verifying");
        this.respond("verify", "sage", 0);
        this.halo("verify");
      });
      this.ripple(H + 90, 0, 46, 0.46);                    // Beat B — confirmation
      this.ripple(H + 225, 1, 68, 0.26);
      this.at(H + 250, () => {                             // Beat C — synchronization
        for (const id of ["reason", "traverse"]) this.pathEls[id].style.stroke = this.P.sage;
        this.respond("junction", "sage", 0);
        this.respond("gather", "sage", 70);
        this.respond("close", "sage", 150);
      });
      this.at(H + 400, () => {                             // Beat D — the gap resolves
        this.pathEls.closure.style.stroke = this.P.sage;
        this.drawPath("closure", 430, 0.5);
      });
      this.at(H + 810, () => {                             // Beat E — the loop closes
        this.closed = true;
        this.resolveMark();
        this.signal("closure", "sage", { dur: 400, r: 2.4 });
        this.breath();
        this.at(300, () => {
          // And continues back toward CareLoop along the path behind the mark.
          this.pathEls.frame2.style.stroke = this.P.sage;
          this.signal("frame2", "sage", { dur: 620, r: 2.2 });
        });
        this.state = "waiting";
        this.maybeFinish();
      });

      this.at(FULL_RUN + 1400, () => this.startAmbient());
    }

    /** Slow load: irregular single signals and drift. No perceptible cycle. */
    startAmbient() {
      if (this.done || this.ambient) return;
      this.setStatus("waiting");
      const ids = ["frame1", "frame2", "reason", "traverse", "outwardP", "closure"];
      const keys = Object.keys(this.nodeEls);
      const tick = () => {
        if (this.done) return;
        const id = ids[Math.floor(Math.random() * ids.length)];
        this.signal(id, Math.random() < 0.35 ? "sage" : "graphite", { dur: 950 + Math.random() * 750, r: 1.9 });
        const n = this.nodeEls[keys[Math.floor(Math.random() * keys.length)]];
        n.g.style.transition = `transform 2800ms ${EASE_SOFT}, opacity 520ms ${EASE}`;
        n.g.style.transform = `translate(${(Math.random() - 0.5) * 6}px, ${(Math.random() - 0.5) * 4}px)`;
        this.ambient = setTimeout(tick, 1700 + Math.random() * 3000);
      };
      this.ambient = setTimeout(tick, 400);
    }

    /* -------------------------------------------------------- BootTransition */

    ready() {
      if (this.appReady) return;
      this.appReady = true;
      this.maybeFinish();
    }

    maybeFinish() {
      if (!this.appReady || this.done) return;
      const elapsed = performance.now() - this.t0;

      // ?boot=full holds for the complete choreography (demo and video use).
      if (FORCE === "full" && !this.closed) {
        this.at(Math.max(0, FULL_RUN + 900 - elapsed), () => this.exit());
        return;
      }

      if (!REDUCED && !this.closed && elapsed < FULL_RUN) {
        this.condense(elapsed);
        return;
      }

      const wait = Math.max(0, MIN_VISIBLE - elapsed);
      this.at(wait + (this.closed ? 300 : 0), () => this.exit());
    }

    /** Every beat, ~1.5s, causality intact. Never a jump straight to closure. */
    condense(elapsed) {
      this.timers.forEach(clearTimeout);
      this.timers = [];
      this.closed = true;

      this.lock.style.opacity = 1;
      this.lock.style.filter = "none";
      this.word.style.opacity = 1;
      this.word.style.transform = "none";
      this.status.style.opacity = 0.88;

      for (const k of ["entry", "gather", "junction", "muted", "verify", "close"]) this.showNode(k, 0);
      this.showNode("outward", 240);
      this.drawPath("frame1", 320, 0.34);
      this.drawPath("frame2", 420, 0.42);

      this.at(160, () => {
        this.setStatus("authorizing");
        this.drawPath("reason", 320, 0.4);
        this.drawPath("branchMuted", 300, 0.15);
        this.drawPath("traverse", 340, 0.2);
        this.signal("reason", "violet", {
          dur: 300, onArrive: () => { this.respond("junction", "violet", 40); this.halo("junction"); }
        });
      });

      this.at(480, () => {
        this.pathEls.traverse.style.stroke = this.P.sage;
        this.drawPath("traverse", 280, 0.46);
        this.drawPath("outwardP", 480, 0.18);
        this.signal("traverse", "sage", { dur: 300 });
      });

      const H = 760;
      this.at(H, () => {
        this.setStatus("verifying");
        this.respond("verify", "sage", 0);
        this.halo("verify");
      });
      this.ripple(H + 70, 0, 44, 0.44);
      this.ripple(H + 190, 1, 64, 0.25);
      this.at(H + 210, () => {
        for (const id of ["reason", "traverse"]) this.pathEls[id].style.stroke = this.P.sage;
        this.respond("junction", "sage", 0);
        this.respond("close", "sage", 90);
      });
      this.at(H + 330, () => {
        this.pathEls.closure.style.stroke = this.P.sage;
        this.drawPath("closure", 340, 0.5);
      });
      this.at(H + 640, () => {
        this.resolveMark();
        this.signal("closure", "sage", { dur: 340, r: 2.4 });
        this.breath();
      });

      this.at(Math.max(CONDENSED - elapsed, 900), () => this.exit());
    }

    /**
     * Hand-off, not a cross-fade. Ordering matters: the wordmark leaves first
     * while the symbol stays fully opaque, the interface surfaces underneath,
     * then the symbol travels to the navbar mark's measured rect and swaps in a
     * single frame — so there is never an ambiguous middle frame.
     */
    exit() {
      if (this.done) return;
      this.done = true;
      this.state = "exiting";
      if (this.ambient) clearTimeout(this.ambient);

      /* LOCAL DELTA 6b — the hand-off cannot be the last thing that happens.
       *
       * Everything below is cosmetic and everything below can throw: a rect from a node React
       * unmounted, a transform on an element somebody moved, a style write on a frozen node. The
       * deadline is armed *first* and outside the try, so it is already running before there is
       * anything to fail, and `finalize` is idempotent so the ordinary path reaching it first
       * costs nothing. `setTimeout` rather than a frame, deliberately: `requestAnimationFrame`
       * does not run in a hidden tab, and a backgrounded boot must still end. */
      const deadline = setTimeout(() => this.finalize(), HARD_EXIT);
      this.timers.push(deadline);

      try {
        this.flip();
      } catch (error) {
        console.warn("[CareLoop] boot hand-off failed:", error && error.message);
        this.finalize();
      }
    }

    /** The hand-off itself. Everything here is allowed to throw; nothing here is allowed to be the
     *  only thing that removes the overlay. */
    flip() {
      const target = document.querySelector('[data-scene-zone="logo"] svg');
      const boot = this.markWrap.querySelector("svg");

      /* LOCAL DELTA 6c — the geometry is guarded before it is trusted.
       *
       * Upstream divides two widths. A target that has not been laid out gives `scale = 0` and the
       * mark shrinks to nothing; a boot svg that has not gives `Infinity`, and a `NaN` anywhere in
       * a transform makes the browser drop the whole declaration without saying so. Neither is a
       * case worth animating, and both degrade to the same clean fade as a missing target. */
      const a = boot ? boot.getBoundingClientRect() : null;
      const b = target ? target.getBoundingClientRect() : null;
      const scale = a && b && a.width > 0 ? b.width / a.width : NaN;

      if (!target || !boot || REDUCED || !b || b.width <= 0 || !isFinite(scale) || scale <= 0) {
        this.fade();
        // Long enough for the 560ms paper fade that `fade()` starts, and no longer. Reduced
        // motion has no fade to wait for: `global.css` collapses every transition to nothing.
        this.at(REDUCED ? 220 : FADE_AT + 620, () => this.finalize());
        return;
      }

      const dx = b.left + b.width / 2 - (a.left + a.width / 2);
      const dy = b.top + b.height / 2 - (a.top + a.height / 2);

      const flier = boot;
      flier.style.position = "fixed";
      flier.style.left = a.left + "px";
      flier.style.top = a.top + "px";
      flier.style.width = a.width + "px";
      flier.style.height = a.height + "px";
      flier.style.margin = "0";
      flier.style.zIndex = "9100";
      flier.style.transformOrigin = "center center";
      /* LOCAL DELTA 6d — both temporary changes to the page announce themselves in the DOM, so a
       * remover that knows nothing about this object can still undo exactly them and nothing
       * else. */
      flier.setAttribute("data-boot-flier", "");
      document.body.appendChild(flier);
      target.style.opacity = "0";
      target.setAttribute("data-boot-hidden", "");

      // Wordmark and status leave; the symbol does not.
      this.word.style.transition = `opacity 240ms ${EASE_SOFT}`;
      this.word.style.opacity = 0;
      this.status.style.transition = `opacity 240ms ${EASE_SOFT}`;
      this.status.style.opacity = 0;

      this.fade();

      this.at(200, () => {
        requestAnimationFrame(() => {
          flier.style.transition = `transform 620ms ${EASE}`;
          flier.style.transform = `translate(${dx}px, ${dy}px) scale(${scale})`;
        });
      });
      // Land and swap in one frame — no cross-fade, no ghost.
      this.at(830, () => this.restoreTarget());
      this.at(880, () => this.finalize());
    }

    /** The overlay leaves. Topology quiets, but the structurally relevant lines survive longest so
     *  the field underneath appears to grow out of them. */
    fade() {
      for (const p of PATHS) {
        const el = this.pathEls && this.pathEls[p.id];
        if (!el) continue;
        const keep = p.id === "closure" || p.id === "frame2";
        el.style.transition = `opacity ${keep ? 620 : 300}ms ${EASE_SOFT} ${keep ? 180 : 0}ms`;
        el.setAttribute("opacity", 0);
      }
      for (const key in this.nodeEls) {
        const n = this.nodeEls[key];
        n.g.style.transition = `opacity 300ms ${EASE_SOFT}`;
        n.g.setAttribute("opacity", 0);
      }
      this.at(FADE_AT, () => {
        if (!this.root) return;
        /* LOCAL DELTA 6e — transparent and inert in the same statement.
         *
         * Upstream drops the opacity at +200ms and removes the overlay at +940ms, and never sets
         * `pointer-events`. For those 740ms the application is fully visible and every click lands
         * on an invisible div at z-index 9000 — which looks exactly like a broken application and
         * is invisible in a screenshot. The two writes are adjacent so that no future edit can
         * move one without the other. */
        this.root.style.pointerEvents = "none";
        this.root.style.opacity = 0;
      });
    }

    /** Give the header its mark back, and take the flier off the page. Safe to call any number of
     *  times, including when neither ever happened. */
    restoreTarget() {
      const hidden = document.querySelectorAll("[data-boot-hidden]");
      for (const el of hidden) {
        el.style.opacity = "";
        el.removeAttribute("data-boot-hidden");
      }
      const flier = document.querySelector("[data-boot-flier]");
      if (flier) flier.remove();
    }

    /**
     * LOCAL DELTA 6f — one teardown, idempotent, and it never depends on how it was reached.
     *
     * Upstream's `destroy(flier)` restores nothing: the navbar mark is un-hidden by a `setTimeout`
     * inside the hand-off, so any path that reaches teardown early or by way of an exception
     * leaves the header's brand permanently invisible. Restoration moved in here, where it is on
     * every path by construction rather than on the happy one by arrangement.
     *
     * Read back out of the DOM rather than held in a field, for the same reason `index.html`'s
     * reaper does it: what has to be undone is a fact about the page, and reading it is what makes
     * this correct even when this object's own state is not.
     */
    finalize() {
      if (this.finalized) return;
      this.finalized = true;
      this.done = true;

      this.timers.forEach(clearTimeout);
      this.timers = [];
      if (this.ambient) clearTimeout(this.ambient);
      this.ambient = null;
      if (this.onPointer) window.removeEventListener("pointermove", this.onPointer);

      try {
        this.restoreTarget();
      } catch (error) {
        /* Nothing useful left to do here, and `index.html`'s reaper covers the same ground. */
      }

      if (this.root && this.root.parentNode) this.root.remove();
      this.svg = this.net = this.pathEls = this.nodeEls = this.ripples = null;
      document.documentElement.removeAttribute("data-booting");
      window.dispatchEvent(new CustomEvent("careloop:booted"));
    }
  }

  /* --------------------------------------------------------------- bootstrap */

  const boot = (() => {
    if (!shouldRun()) return null;
    rememberShown();
    try {
      document.documentElement.setAttribute("data-booting", "");
      return new CareLoopBoot();
    } catch (err) {
      // The loader must never be why the app fails to appear.
      console.warn("[CareLoop] boot animation unavailable:", err && err.message);
      const stale = document.getElementById("careloop-boot");
      if (stale) stale.remove();
      document.documentElement.removeAttribute("data-booting");
      return null;
    }
  })();

  window.CareLoopBootController = {
    ready: () => boot && boot.ready(),
    state: () => (boot ? boot.state : "ready")
  };

  window.addEventListener("careloop:ready", () => boot && boot.ready());

  Promise.all([
    document.fonts && document.fonts.ready ? document.fonts.ready : Promise.resolve(),
    new Promise((r) => {
      if (document.readyState === "complete") return r();
      window.addEventListener("load", r, { once: true });
    })
  ]).then(() => {
    requestAnimationFrame(() => requestAnimationFrame(() => boot && boot.ready()));
  });

  setTimeout(() => boot && boot.ready(), 9000);
})();
