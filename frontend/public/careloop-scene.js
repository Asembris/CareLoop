/* CareLoop — "The Living Care Loop"
 *
 * An orchestration field rendered behind the interface: sparse anchor nodes,
 * hairline spline paths, traveling signals, and verification ripples.
 *
 * Ink and graphite embedded into paper — not light on black. At rest the field
 * is nearly invisible; interaction reveals how much structure is actually there.
 *
 * Modules below, in order:
 *   Palette        theme-derived colors
 *   Layout         normalized graph topology + world projection (keep-out band)
 *   PathField      LineSegments, one draw call, shader wobble + activation
 *   NodeField      Points, core dot + discovery halo
 *   SignalField    Points with trails, walk sampled curves
 *   RippleField    InstancedMesh rings, incl. the mismatch arc with a gap
 *   Pointer        weak local influence field, damped
 *   Sequencer      semantic per-scenario choreography
 *   Scene          camera, scroll, entrance, RAF loop, disposal
 *
 * ---------------------------------------------------------------------------
 * Vendored from `careloop-motion` (docs/MOTION.md records the version). Kept
 * byte-identical to upstream except for the single line marked LOCAL DELTA
 * below, so the next upgrade is a copy rather than a merge.
 * ---------------------------------------------------------------------------
 */

/* LOCAL DELTA — self-hosted rather than fetched from unpkg, which is what the
 * package documents as the way to self-host (docs/BACKGROUND.md, README.md).
 * Upstream: "https://unpkg.com/three@0.160.0/build/three.module.js".
 * The version is in the filename, so these bytes can never change under this
 * name and `careloop/api/static.py` may serve them immutable truthfully. */
const CDN = "/assets/three-0.160.0.module.min.js";

const REDUCED = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

/* ---------------------------------------------------------------- Palette */

const PALETTES = {
  light: {
    neutral: [0.42, 0.40, 0.36],
    dormant: [0.66, 0.64, 0.59],
    green: [0.29, 0.49, 0.42],
    violet: [0.49, 0.42, 0.56],
    terracotta: [0.63, 0.35, 0.27],
    lineAlpha: 0.16,
    nodeAlpha: 0.30,
    signalAlpha: 0.62
  },
  dark: {
    neutral: [0.62, 0.60, 0.55],
    dormant: [0.34, 0.33, 0.30],
    green: [0.57, 0.71, 0.65],
    violet: [0.71, 0.62, 0.77],
    terracotta: [0.81, 0.54, 0.45],
    lineAlpha: 0.22,
    nodeAlpha: 0.36,
    signalAlpha: 0.78
  }
};

const TONE_FOR_MODE = {
  ambient: "neutral",
  referral: "green",
  injection: "violet",
  mismatch: "terracotta",
  home: "neutral",
  trust: "green",
  hood: "neutral"
};

/* ----------------------------------------------------------------- Layout */

/** Density tier from viewport. Every node must earn its place. */
function tier() {
  const w = window.innerWidth;
  if (w < 620) return { nodes: 38, ambient: 10, signals: 20, dpr: 1.25, depth: 0.55 };
  if (w < 1080) return { nodes: 64, ambient: 17, signals: 30, dpr: 1.5, depth: 0.8 };
  return { nodes: 92, ambient: 24, signals: 44, dpr: 1.75, depth: 1 };
}

/** World-space exclusion boxes derived from the real DOM layout. */
const MAX_ZONES = 6;
const ZONE_MARGIN = 2.8;      // interaction perimeter, in world units
const ZONE_FLOOR = 0.12;      // focus zones: almost nothing survives at rest

function zoneUniform(THREE) {
  const arr = [];
  for (let i = 0; i < MAX_ZONES; i++) arr.push(new THREE.Vector4(0, 0, 0, 0));
  return arr;
}

/** Deterministic PRNG so the composition is art-directed, not re-rolled per load. */
function rng(seed) {
  let s = seed >>> 0;
  return () => {
    s = (s * 1664525 + 1013904223) >>> 0;
    return s / 4294967296;
  };
}

/**
 * The three workflow chains, authored as normalized control arcs.
 * Each sweeps the page's outer field rather than crossing the text column head-on:
 * referral runs upper-left → lower-right, injection branches through the left
 * midground, mismatch closes (almost) in the lower right.
 */
const CHAINS = {
  referral: [
    [-1.16, 0.62, -0.30], [-0.86, 0.44, -0.12], [-0.64, 0.20, 0.16], [-0.72, -0.08, -0.05],
    [-0.52, -0.34, 0.22], [-0.18, -0.52, -0.42], [0.28, -0.60, -0.30], [0.66, -0.44, 0.10],
    [0.92, -0.18, 0.26], [1.04, 0.10, -0.08]
  ],
  injection: [
    [-1.10, -0.34, 0.30], [-0.90, -0.12, 0.44], [-0.68, 0.10, 0.28], [-0.44, 0.30, 0.40],
    [-0.58, 0.56, 0.52], [-0.30, 0.70, 0.18], [0.58, 0.88, -0.20]
  ],
  mismatch: [
    [1.14, 0.44, 0.14], [0.94, 0.20, 0.32], [0.78, -0.06, 0.20], [0.86, -0.34, 0.38],
    [0.62, -0.56, 0.16], [0.34, -0.72, 0.30]
  ]
};

/** Branch that fractures and is rejected during the injection sequence. */
const INJECTION_BRANCH = [[-0.44, 0.30, 0.40], [-0.30, 0.14, 0.62], [-0.12, -0.04, 0.74]];

class Layout {
  constructor(t) {
    this.t = t;
    this.nodes = [];
    this.paths = [];
    this.chains = {};
    this.build();
  }

  build() {
    const rand = rng(20260817);
    const nodes = this.nodes;
    const paths = this.paths;

    const addNode = (x, y, z, role, layer) => {
      nodes.push({ nx: x, ny: y, nz: z, role, layer, pos: null, drift: 0, active: 0, seed: rand() });
      return nodes.length - 1;
    };

    // Chain nodes first — they carry semantic meaning.
    for (const key of Object.keys(CHAINS)) {
      const ids = CHAINS[key].map((p, i) =>
        addNode(p[0], p[1], p[2], i === CHAINS[key].length - 1 ? "verify" : "stage", key)
      );
      this.chains[key] = { nodes: ids, paths: [] };
      for (let i = 0; i < ids.length - 1; i++) {
        this.chains[key].paths.push(paths.length);
        paths.push({ a: ids[i], b: ids[i + 1], kind: key, bow: (rand() - 0.5) * 0.5, gap: 0 });
      }
    }

    // The rejected branch.
    const branchIds = INJECTION_BRANCH.slice(1).map((p) => addNode(p[0], p[1], p[2], "stage", "branch"));
    this.branchPaths = [];
    let prev = this.chains.injection.nodes[3];
    for (const id of branchIds) {
      this.branchPaths.push(paths.length);
      paths.push({ a: prev, b: id, kind: "branch", bow: 0.3, gap: 0 });
      prev = id;
    }

    // The mismatch chain's closure never completes: the last hop keeps a gap.
    const mm = this.chains.mismatch;
    this.mismatchClosure = paths.length;
    paths.push({
      a: mm.nodes[mm.nodes.length - 1],
      b: mm.nodes[0],
      kind: "closure",
      bow: 1.15,
      gap: 0.22
    });
    mm.paths.push(this.mismatchClosure);

    // Ambient field. Biased outside the text column, spread across depth layers.
    const ambientStart = nodes.length;
    const count = this.t.nodes - nodes.length;
    for (let i = 0; i < count; i++) {
      const side = rand() < 0.5 ? -1 : 1;
      const edge = 0.55 + rand() * 0.72;
      const deep = rand() < 0.34;
      const x = deep ? (rand() * 2 - 1) * 1.15 : side * edge;
      const y = (rand() * 2 - 1) * 1.05;
      const z = deep ? -0.55 - rand() * 0.6 : (rand() * 2 - 1) * 0.75;
      addNode(x, y, z, rand() < 0.12 ? "verify" : "anchor", deep ? "back" : "field");
    }

    // Ambient connections: nearest-neighbour, partial loops, never a full circle.
    const ambientNodes = [];
    for (let i = ambientStart; i < nodes.length; i++) ambientNodes.push(i);
    let made = 0;
    for (const i of ambientNodes) {
      if (made >= this.t.ambient) break;
      let best = -1;
      let bestD = Infinity;
      for (const j of ambientNodes) {
        if (j === i) continue;
        const a = nodes[i];
        const b = nodes[j];
        const d = (a.nx - b.nx) ** 2 + (a.ny - b.ny) ** 2 + (a.nz - b.nz) ** 2;
        if (d < bestD && d > 0.02 && !paths.some((p) => (p.a === i && p.b === j) || (p.a === j && p.b === i))) {
          bestD = d;
          best = j;
        }
      }
      if (best >= 0 && bestD < 0.5) {
        paths.push({ a: i, b: best, kind: "ambient", bow: (rand() - 0.5) * 0.85, gap: 0 });
        made++;
      }
    }

    this.pathCount = paths.length;
  }

  /** Project normalized topology into world space for the current frustum. */
  project(camera) {
    const dist = camera.position.z;
    const halfH = Math.tan((camera.fov * Math.PI) / 360) * dist;
    const halfW = halfH * camera.aspect;
    // Keep-out band around the 820px text column, in normalized x.
    const keep = Math.min(0.62, 470 / Math.max(window.innerWidth, 1));
    this.keep = keep;
    this.halfW = halfW;
    this.halfH = halfH;

    for (const n of this.nodes) {
      const x = n.nx * halfW * 1.02;
      const y = n.ny * halfH * 1.04;
      const z = n.nz * 7.5 * this.t.depth;
      n.pos = n.pos || { x: 0, y: 0, z: 0 };
      n.pos.x = x;
      n.pos.y = y;
      n.pos.z = z;
      n.veil = 1;
    }
  }

  /**
   * Attenuation from the DOM-derived exclusion zones: near-zero inside a focus
   * zone (hero copy, card interiors, CTA), ramping back to full out in the
   * ambient margins. The layout is part of the composition.
   */
  veilAt(x, y) {
    const zones = this.zones;
    if (!zones || !zones.length) return 1;
    let a = 1;
    for (let i = 0; i < zones.length; i++) {
      const z = zones[i];
      const dx = Math.max(z[0] - x, x - z[2], 0);
      const dy = Math.max(z[1] - y, y - z[3], 0);
      const d = Math.sqrt(dx * dx + dy * dy);
      const t = Math.min(1, d / ZONE_MARGIN);
      const smooth = t * t * (3 - 2 * t);
      a = Math.min(a, ZONE_FLOOR + (1 - ZONE_FLOOR) * smooth);
    }
    return a;
  }
}

/* -------------------------------------------------------------- PathField */

const PATH_SAMPLES = 46;

const ZONE_GLSL = `
uniform vec4 uZones[6];
uniform float uZoneMargin;
uniform float uZoneFloor;
float zoneAtten(vec2 p) {
  float a = 1.0;
  for (int i = 0; i < 6; i++) {
    vec4 z = uZones[i];
    float valid = step(0.001, z.z - z.x);
    vec2 d = max(max(z.xy - p, p - z.zw), vec2(0.0));
    float att = mix(uZoneFloor, 1.0, smoothstep(0.0, uZoneMargin, length(d)));
    a = min(a, mix(1.0, att, valid));
  }
  return a;
}`;

const PATH_VERT = `
uniform float uTime;
uniform vec3 uPointer;
uniform float uPointerAmt;
uniform float uReveal;
uniform sampler2D uActive;
attribute vec3 aColor;
attribute float aAlpha;
attribute float aSeed;
attribute float aVeil;
attribute float aPath;
attribute float aOrder;
varying vec3 vColor;
varying float vAlpha;
` + ZONE_GLSL + `

void main() {
  vec3 p = position;

  // Low-frequency procedural drift so lines feel alive, not plotted.
  float w = 0.055 + aSeed * 0.05;
  p.x += sin(uTime * 0.17 + aSeed * 12.5 + p.y * 0.13) * w;
  p.y += cos(uTime * 0.13 + aSeed * 8.3 + p.x * 0.11) * w;
  p.z += sin(uTime * 0.09 + aSeed * 4.1) * w * 1.4;

  // Weak local influence field: nearby curves bend around the cursor.
  vec2 d = p.xy - uPointer.xy;
  float dist = length(d);
  float infl = smoothstep(5.2, 0.0, dist) * uPointerAmt;
  p.xy += normalize(d + 0.0001) * infl * 0.34;

  float act = texture2D(uActive, vec2(aPath / 64.0 + 0.0078, 0.5)).r;

  // Progressive draw-in during the entrance.
  float drawn = smoothstep(aOrder - 0.14, aOrder + 0.10, uReveal);

  vColor = mix(aColor, aColor * 1.25 + 0.06, act);
  // Zones suppress resting topology, but an excited path must still read.
  float za = zoneAtten(p.xy);
  float live = max(act, infl);
  vAlpha = aAlpha * aVeil * mix(za, min(1.0, za + 0.55), live) * drawn * (1.0 + act * 2.6 + infl * 1.5);

  vec4 mv = modelViewMatrix * vec4(p, 1.0);
  // Depth-sensitive falloff — far structures read thinner and quieter.
  vAlpha *= clamp(1.0 - (-mv.z - 14.0) * 0.028, 0.28, 1.0);
  gl_Position = projectionMatrix * mv;
}`;

const PATH_FRAG = `
varying vec3 vColor;
varying float vAlpha;
void main() {
  gl_FragColor = vec4(vColor, clamp(vAlpha, 0.0, 1.0));
}`;

class PathField {
  constructor(THREE, layout, palette) {
    this.THREE = THREE;
    this.layout = layout;
    this.samples = [];
    this.activation = new Float32Array(64);
    this.activeTarget = new Float32Array(64);

    const paths = layout.paths;
    const segs = PATH_SAMPLES - 1;
    const verts = paths.length * segs * 2;

    const position = new Float32Array(verts * 3);
    const color = new Float32Array(verts * 3);
    const alpha = new Float32Array(verts);
    const seed = new Float32Array(verts);
    const veil = new Float32Array(verts);
    const pathId = new Float32Array(verts);
    const order = new Float32Array(verts);

    this.arrays = { position, color, alpha, seed, veil, pathId, order };

    const g = new THREE.BufferGeometry();
    g.setAttribute("position", new THREE.BufferAttribute(position, 3));
    g.setAttribute("aColor", new THREE.BufferAttribute(color, 3));
    g.setAttribute("aAlpha", new THREE.BufferAttribute(alpha, 1));
    g.setAttribute("aSeed", new THREE.BufferAttribute(seed, 1));
    g.setAttribute("aVeil", new THREE.BufferAttribute(veil, 1));
    g.setAttribute("aPath", new THREE.BufferAttribute(pathId, 1));
    g.setAttribute("aOrder", new THREE.BufferAttribute(order, 1));

    const tex = new THREE.DataTexture(new Uint8Array(64 * 4), 64, 1, THREE.RGBAFormat);
    tex.needsUpdate = true;
    this.tex = tex;
    this.texData = tex.image.data;

    this.material = new THREE.ShaderMaterial({
      vertexShader: PATH_VERT,
      fragmentShader: PATH_FRAG,
      transparent: true,
      depthWrite: false,
      uniforms: {
        uTime: { value: 0 },
        uPointer: { value: new THREE.Vector3(999, 999, 0) },
        uPointerAmt: { value: 0 },
        uReveal: { value: REDUCED ? 1 : 0 },
        uActive: { value: tex },
        uZones: { value: zoneUniform(THREE) },
        uZoneMargin: { value: ZONE_MARGIN },
        uZoneFloor: { value: ZONE_FLOOR }
      }
    });

    this.object = new THREE.LineSegments(g, this.material);
    this.object.frustumCulled = false;
    this.palette = palette;
  }

  /** Resample every curve after a layout change. */
  refresh(palette) {
    const { THREE, layout } = this;
    this.palette = palette || this.palette;
    const P = this.palette;
    const paths = layout.paths;
    const segs = PATH_SAMPLES - 1;
    const A = this.arrays;
    const tmp = new THREE.Vector3();
    this.samples.length = 0;

    let v = 0;
    for (let pi = 0; pi < paths.length; pi++) {
      const p = paths[pi];
      const a = layout.nodes[p.a].pos;
      const b = layout.nodes[p.b].pos;

      // Bowed quadratic — partial arcs, never straight wire.
      const mid = new THREE.Vector3(
        (a.x + b.x) / 2 - (b.y - a.y) * p.bow * 0.42,
        (a.y + b.y) / 2 + (b.x - a.x) * p.bow * 0.42,
        (a.z + b.z) / 2 + p.bow * 2.2
      );
      const curve = new THREE.QuadraticBezierCurve3(
        new THREE.Vector3(a.x, a.y, a.z),
        mid,
        new THREE.Vector3(b.x, b.y, b.z)
      );

      const pts = [];
      for (let s = 0; s < PATH_SAMPLES; s++) pts.push(curve.getPoint(s / (PATH_SAMPLES - 1)));
      this.samples.push(pts);

      const tone =
        p.kind === "referral" ? P.green :
        p.kind === "injection" ? P.violet :
        p.kind === "branch" ? P.violet :
        p.kind === "mismatch" || p.kind === "closure" ? P.terracotta :
        P.dormant;
      const base =
        p.kind === "ambient" ? P.lineAlpha * 0.72 :
        p.kind === "branch" ? P.lineAlpha * 0.5 :
        p.kind === "closure" ? P.lineAlpha * 0.55 :
        P.lineAlpha;

      const seedVal = (pi * 0.618) % 1;
      const orderVal = pi / paths.length;

      for (let s = 0; s < segs; s++) {
        const t0 = s / (PATH_SAMPLES - 1);
        const t1 = (s + 1) / (PATH_SAMPLES - 1);
        // A gap means the loop refuses to close: drop the segments that would seal it.
        const cut = p.gap > 0 && t0 > 0.5 - p.gap / 2 && t1 < 0.5 + p.gap / 2;

        for (let k = 0; k < 2; k++) {
          const pt = pts[s + k];
          A.position[v * 3] = pt.x;
          A.position[v * 3 + 1] = pt.y;
          A.position[v * 3 + 2] = pt.z;
          A.color[v * 3] = tone[0];
          A.color[v * 3 + 1] = tone[1];
          A.color[v * 3 + 2] = tone[2];
          A.alpha[v] = cut ? 0 : base;
          A.seed[v] = seedVal;
          A.veil[v] = 1;
          A.pathId[v] = pi;
          A.order[v] = orderVal + (s / segs) * (1 / paths.length);
          v++;
        }
      }
      tmp.set(0, 0, 0);
    }

    const g = this.object.geometry;
    for (const key of ["position", "aColor", "aAlpha", "aSeed", "aVeil", "aPath", "aOrder"]) {
      g.getAttribute(key).needsUpdate = true;
    }
  }

  /** Ask a path to light up; decays on its own. */
  excite(pathIndex, amount) {
    if (pathIndex < 64) this.activeTarget[pathIndex] = Math.min(1, this.activeTarget[pathIndex] + amount);
  }

  update(dt) {
    for (let i = 0; i < 64; i++) {
      this.activeTarget[i] *= 1 - Math.min(1, dt * 1.5);
      this.activation[i] += (this.activeTarget[i] - this.activation[i]) * Math.min(1, dt * 6);
      this.texData[i * 4] = Math.max(0, Math.min(255, this.activation[i] * 255));
    }
    this.tex.needsUpdate = true;
  }

  dispose() {
    this.object.geometry.dispose();
    this.material.dispose();
    this.tex.dispose();
  }
}

/* -------------------------------------------------------------- NodeField */

const NODE_VERT = `
uniform float uTime;
uniform vec3 uPointer;
uniform float uPointerAmt;
uniform float uReveal;
uniform float uPixelRatio;
attribute vec3 aColor;
attribute float aAlpha;
attribute float aSize;
attribute float aSeed;
attribute float aVeil;
attribute float aActive;
attribute float aOrder;
varying vec3 vColor;
varying float vAlpha;
varying float vActive;
` + ZONE_GLSL + `

void main() {
  vec3 p = position;
  float w = 0.07 + aSeed * 0.06;
  p.x += sin(uTime * 0.15 + aSeed * 11.0) * w;
  p.y += cos(uTime * 0.12 + aSeed * 7.0) * w;

  vec2 d = p.xy - uPointer.xy;
  float dist = length(d);
  float infl = smoothstep(4.6, 0.0, dist) * uPointerAmt;
  p.xy += normalize(d + 0.0001) * infl * 0.42;

  float drawn = smoothstep(aOrder - 0.10, aOrder + 0.06, uReveal);
  float disc = max(aActive, infl);

  vColor = aColor;
  vActive = disc;
  vAlpha = aAlpha * aVeil * mix(zoneAtten(p.xy), min(1.0, zoneAtten(p.xy) + 0.5), disc) * drawn * (1.0 + disc * 2.2);

  vec4 mv = modelViewMatrix * vec4(p, 1.0);
  vAlpha *= clamp(1.0 - (-mv.z - 14.0) * 0.03, 0.25, 1.0);
  gl_PointSize = aSize * (1.0 + disc * 0.85) * uPixelRatio * (170.0 / -mv.z);
  gl_Position = projectionMatrix * mv;
}`;

const NODE_FRAG = `
varying vec3 vColor;
varying float vAlpha;
varying float vActive;
void main() {
  float r = length(gl_PointCoord - vec2(0.5)) * 2.0;
  float core = 1.0 - smoothstep(0.20, 0.46, r);
  // A very fine halo forms only on discovery.
  float ring = (1.0 - smoothstep(0.055, 0.0, abs(r - 0.80))) * 0.0;
  ring = (1.0 - smoothstep(0.0, 0.09, abs(r - 0.80))) * vActive * 0.34;
  float a = (core + ring) * vAlpha;
  if (a < 0.004) discard;
  gl_FragColor = vec4(vColor, a);
}`;

class NodeField {
  constructor(THREE, layout, palette) {
    this.THREE = THREE;
    this.layout = layout;
    const n = layout.nodes.length;
    this.arrays = {
      position: new Float32Array(n * 3),
      aColor: new Float32Array(n * 3),
      aAlpha: new Float32Array(n),
      aSize: new Float32Array(n),
      aSeed: new Float32Array(n),
      aVeil: new Float32Array(n),
      aActive: new Float32Array(n),
      aOrder: new Float32Array(n)
    };

    const g = new THREE.BufferGeometry();
    g.setAttribute("position", new THREE.BufferAttribute(this.arrays.position, 3));
    g.setAttribute("aColor", new THREE.BufferAttribute(this.arrays.aColor, 3));
    for (const k of ["aAlpha", "aSize", "aSeed", "aVeil", "aActive", "aOrder"]) {
      g.setAttribute(k, new THREE.BufferAttribute(this.arrays[k], 1));
    }

    this.material = new THREE.ShaderMaterial({
      vertexShader: NODE_VERT,
      fragmentShader: NODE_FRAG,
      transparent: true,
      depthWrite: false,
      uniforms: {
        uTime: { value: 0 },
        uPointer: { value: new THREE.Vector3(999, 999, 0) },
        uPointerAmt: { value: 0 },
        uReveal: { value: REDUCED ? 1 : 0 },
        uPixelRatio: { value: 1 },
        uZones: { value: zoneUniform(THREE) },
        uZoneMargin: { value: ZONE_MARGIN },
        uZoneFloor: { value: ZONE_FLOOR }
      }
    });

    this.object = new THREE.Points(g, this.material);
    this.object.frustumCulled = false;
    this.palette = palette;
  }

  refresh(palette) {
    this.palette = palette || this.palette;
    const P = this.palette;
    const A = this.arrays;
    const nodes = this.layout.nodes;
    for (let i = 0; i < nodes.length; i++) {
      const n = nodes[i];
      A.position[i * 3] = n.pos.x;
      A.position[i * 3 + 1] = n.pos.y;
      A.position[i * 3 + 2] = n.pos.z;
      const tone =
        n.layer === "referral" ? P.green :
        n.layer === "injection" || n.layer === "branch" ? P.violet :
        n.layer === "mismatch" ? P.terracotta :
        P.neutral;
      A.aColor[i * 3] = tone[0];
      A.aColor[i * 3 + 1] = tone[1];
      A.aColor[i * 3 + 2] = tone[2];
      const chained = n.layer !== "field" && n.layer !== "back";
      A.aAlpha[i] = P.nodeAlpha * (chained ? 1 : n.layer === "back" ? 0.5 : 0.72);
      A.aSize[i] = n.role === "verify" ? 5.2 : chained ? 4.2 : 3.1;
      A.aSeed[i] = n.seed;
      A.aVeil[i] = n.veil;
      A.aOrder[i] = i / nodes.length;
    }
    const g = this.object.geometry;
    for (const k of Object.keys(A)) g.getAttribute(k === "position" ? "position" : k).needsUpdate = true;
  }

  /** Nodes respond with delayed micro-movement, so excitement ripples outward. */
  excite(index, amount) {
    const n = this.layout.nodes[index];
    if (n) n.active = Math.min(1.4, n.active + amount);
  }

  update(dt) {
    const A = this.arrays;
    const nodes = this.layout.nodes;
    for (let i = 0; i < nodes.length; i++) {
      const n = nodes[i];
      if (n.active > 0.001) {
        n.active *= 1 - Math.min(1, dt * 1.15);
        A.aActive[i] = n.active;
      } else if (A.aActive[i] !== 0) {
        n.active = 0;
        A.aActive[i] = 0;
      }
    }
    this.object.geometry.getAttribute("aActive").needsUpdate = true;
  }

  dispose() {
    this.object.geometry.dispose();
    this.material.dispose();
  }
}

/* ------------------------------------------------------------ SignalField */

const TRAIL = 5;

const SIGNAL_VERT = `
uniform float uPixelRatio;
attribute vec3 aColor;
attribute float aAlpha;
attribute float aSize;
varying vec3 vColor;
varying float vAlpha;
void main() {
  vColor = aColor;
  vAlpha = aAlpha;
  vec4 mv = modelViewMatrix * vec4(position, 1.0);
  gl_PointSize = aSize * uPixelRatio * (170.0 / -mv.z);
  gl_Position = projectionMatrix * mv;
}`;

const SIGNAL_FRAG = `
varying vec3 vColor;
varying float vAlpha;
void main() {
  float r = length(gl_PointCoord - vec2(0.5)) * 2.0;
  float core = 1.0 - smoothstep(0.0, 0.85, r);
  float a = core * vAlpha;
  if (a < 0.004) discard;
  gl_FragColor = vec4(vColor, a);
}`;

class SignalField {
  constructor(THREE, layout, paths, palette) {
    this.THREE = THREE;
    this.layout = layout;
    this.pathField = paths;
    this.palette = palette;
    this.max = tier().signals;
    this.live = [];

    const count = this.max * TRAIL;
    this.arrays = {
      position: new Float32Array(count * 3),
      aColor: new Float32Array(count * 3),
      aAlpha: new Float32Array(count),
      aSize: new Float32Array(count)
    };
    // Park unused points far behind the camera.
    for (let i = 0; i < count; i++) this.arrays.position[i * 3 + 2] = -9999;

    const g = new THREE.BufferGeometry();
    g.setAttribute("position", new THREE.BufferAttribute(this.arrays.position, 3));
    g.setAttribute("aColor", new THREE.BufferAttribute(this.arrays.aColor, 3));
    g.setAttribute("aAlpha", new THREE.BufferAttribute(this.arrays.aAlpha, 1));
    g.setAttribute("aSize", new THREE.BufferAttribute(this.arrays.aSize, 1));

    this.material = new THREE.ShaderMaterial({
      vertexShader: SIGNAL_VERT,
      fragmentShader: SIGNAL_FRAG,
      transparent: true,
      depthWrite: false,
      blending: THREE.NormalBlending,
      uniforms: { uPixelRatio: { value: 1 } }
    });

    this.object = new THREE.Points(g, this.material);
    this.object.frustumCulled = false;
  }

  emit(pathIndex, tone, opts = {}) {
    if (this.live.length >= this.max) return null;
    const path = this.layout.paths[pathIndex];
    if (!path) return null;
    const sig = {
      path: pathIndex,
      t: opts.from ?? 0,
      speed: opts.speed ?? 0.34 + Math.random() * 0.16,
      tone,
      alpha: opts.alpha ?? 1,
      fade: opts.fade ?? 0,
      onArrive: opts.onArrive || null,
      dying: false
    };
    this.live.push(sig);
    return sig;
  }

  update(dt, time) {
    const A = this.arrays;
    const P = this.palette;
    const samples = this.pathField.samples;
    let slot = 0;

    for (let i = this.live.length - 1; i >= 0; i--) {
      const s = this.live[i];
      s.t += s.speed * dt;

      if (s.fade > 0) s.alpha -= dt / s.fade;
      if (s.alpha <= 0.02) {
        this.live.splice(i, 1);
        continue;
      }
      if (s.t >= 1) {
        const path = this.layout.paths[s.path];
        if (s.onArrive) s.onArrive(path ? path.b : -1);
        this.live.splice(i, 1);
        continue;
      }

      this.pathField.excite(s.path, dt * 3.4 * s.alpha);
      const pts = samples[s.path];
      if (!pts) continue;
      const tone = P[s.tone] || P.neutral;
      const seedVal = (s.path * 0.618) % 1;

      for (let k = 0; k < TRAIL; k++) {
        if (slot >= this.max * TRAIL) break;
        const tt = Math.max(0, s.t - k * 0.028);
        const f = tt * (PATH_SAMPLES - 1);
        const i0 = Math.min(PATH_SAMPLES - 1, Math.floor(f));
        const i1 = Math.min(PATH_SAMPLES - 1, i0 + 1);
        const m = f - i0;
        const p0 = pts[i0];
        const p1 = pts[i1];
        let x = p0.x + (p1.x - p0.x) * m;
        let y = p0.y + (p1.y - p0.y) * m;
        let z = p0.z + (p1.z - p0.z) * m;
        // Match the shader's drift so signals stay glued to their path.
        const w = 0.055 + seedVal * 0.05;
        x += Math.sin(time * 0.17 + seedVal * 12.5 + y * 0.13) * w;
        y += Math.cos(time * 0.13 + seedVal * 8.3 + x * 0.11) * w;

        A.position[slot * 3] = x;
        A.position[slot * 3 + 1] = y;
        A.position[slot * 3 + 2] = z;
        A.aColor[slot * 3] = tone[0];
        A.aColor[slot * 3 + 1] = tone[1];
        A.aColor[slot * 3 + 2] = tone[2];
        const decay = 1 - k / TRAIL;
        // Signals are the interaction payload: softened suppression, not silence.
        A.aAlpha[slot] = P.signalAlpha * s.alpha * decay * decay * Math.pow(this.layout.veilAt(x, y), 0.55);
        A.aSize[slot] = 3.4 * decay + 0.6;
        slot++;
      }
    }

    for (let i = slot; i < this.max * TRAIL; i++) {
      if (A.aAlpha[i] === 0) continue;
      A.aAlpha[i] = 0;
      A.position[i * 3 + 2] = -9999;
    }

    const g = this.object.geometry;
    g.getAttribute("position").needsUpdate = true;
    g.getAttribute("aColor").needsUpdate = true;
    g.getAttribute("aAlpha").needsUpdate = true;
    g.getAttribute("aSize").needsUpdate = true;
  }

  dispose() {
    this.object.geometry.dispose();
    this.material.dispose();
  }
}

/* ------------------------------------------------------------ RippleField */

const RIPPLE_VERT = `
attribute vec3 aColor;
attribute float aProgress;
attribute float aScale;
attribute float aGap;
attribute float aRings;
varying vec3 vColor;
varying float vProgress;
varying float vGap;
varying float vRings;
void main() {
  vColor = aColor;
  vProgress = aProgress;
  vGap = aGap;
  vRings = aRings;
  vec3 p = position * aScale;
  vec4 mv = modelViewMatrix * vec4(instanceOffset + p, 1.0);
  gl_Position = projectionMatrix * mv;
}`;

const RIPPLE_FRAG = `
varying vec3 vColor;
varying float vProgress;
varying float vGap;
varying float vRings;
void main() {
  vec2 c = gl_PointCoord;
  gl_FragColor = vec4(vColor, 0.0);
}`;

/**
 * Verification geometry. Concentric rings expand and fade; when a loop refuses
 * to close, the outer ring is drawn with an angular gap left open.
 */
class RippleField {
  constructor(THREE, palette) {
    this.THREE = THREE;
    this.palette = palette;
    this.max = 10;
    this.live = [];

    const geo = new THREE.PlaneGeometry(1, 1);
    const mat = new THREE.ShaderMaterial({
      transparent: true,
      depthWrite: false,
      uniforms: { uPalette: { value: 0 } },
      vertexShader: `
        attribute vec3 aColor;
        attribute float aProgress;
        attribute float aRadius;
        attribute float aGap;
        attribute float aSecond;
        attribute float aAlpha;
        varying vec2 vUv;
        varying vec3 vColor;
        varying float vProgress;
        varying float vGap;
        varying float vSecond;
        varying float vAlpha;
        void main() {
          vUv = uv - 0.5;
          vColor = aColor;
          vProgress = aProgress;
          vGap = aGap;
          vSecond = aSecond;
          vAlpha = aAlpha;
          vec3 p = vec3(position.xy * aRadius, 0.0);
          gl_Position = projectionMatrix * modelViewMatrix * instanceMatrix * vec4(p, 1.0);
        }`,
      fragmentShader: `
        varying vec2 vUv;
        varying vec3 vColor;
        varying float vProgress;
        varying float vGap;
        varying float vSecond;
        varying float vAlpha;

        float ringAt(float r, float target, float width) {
          return 1.0 - smoothstep(0.0, width, abs(r - target));
        }

        void main() {
          float r = length(vUv) * 2.0;
          float ang = atan(vUv.y, vUv.x);

          float a = 0.0;
          a += ringAt(r, vProgress, 0.045);
          // A second, slightly asynchronous ring — the read-back arriving late.
          a += ringAt(r, vSecond, 0.05) * 0.7;

          // Leave the loop open where the outside world disagreed.
          if (vGap > 0.001) {
            float g = smoothstep(vGap, vGap * 0.4, abs(ang - 1.05));
            a *= 1.0 - g;
          }

          float fade = 1.0 - smoothstep(0.55, 1.0, vProgress);
          float out_ = a * vAlpha * fade;
          if (out_ < 0.004) discard;
          gl_FragColor = vec4(vColor, out_);
        }`
    });

    this.mesh = new THREE.InstancedMesh(geo, mat, this.max);
    this.mesh.frustumCulled = false;
    this.mesh.count = this.max;

    const c = new Float32Array(this.max * 3);
    const p = new Float32Array(this.max);
    const rad = new Float32Array(this.max);
    const gap = new Float32Array(this.max);
    const sec = new Float32Array(this.max);
    const al = new Float32Array(this.max);
    geo.setAttribute("aColor", new THREE.InstancedBufferAttribute(c, 3));
    geo.setAttribute("aProgress", new THREE.InstancedBufferAttribute(p, 1));
    geo.setAttribute("aRadius", new THREE.InstancedBufferAttribute(rad, 1));
    geo.setAttribute("aGap", new THREE.InstancedBufferAttribute(gap, 1));
    geo.setAttribute("aSecond", new THREE.InstancedBufferAttribute(sec, 1));
    geo.setAttribute("aAlpha", new THREE.InstancedBufferAttribute(al, 1));
    this.attrs = { c, p, rad, gap, sec, al };

    this.dummy = new THREE.Object3D();
    for (let i = 0; i < this.max; i++) {
      this.dummy.position.set(0, 0, -9999);
      this.dummy.updateMatrix();
      this.mesh.setMatrixAt(i, this.dummy.matrix);
    }
    this.mesh.instanceMatrix.needsUpdate = true;
    this.object = this.mesh;
  }

  /** Injected by the scene so ripples recede over the reading column too. */
  veil(x, y) {
    return this.veilFn ? this.veilFn(x, y) : 1;
  }

  spawn(pos, tone, opts = {}) {
    if (this.live.length >= this.max) this.live.shift();
    this.live.push({
      x: pos.x, y: pos.y, z: pos.z,
      tone,
      progress: 0,
      speed: opts.speed ?? 0.9,
      radius: opts.radius ?? 3.4,
      gap: opts.gap ?? 0,
      lag: opts.lag ?? 0.16,
      alpha: opts.alpha ?? 0.7
    });
  }

  update(dt) {
    const A = this.attrs;
    const P = this.palette;
    for (let i = this.live.length - 1; i >= 0; i--) {
      const r = this.live[i];
      r.progress += r.speed * dt;
      if (r.progress > 1.05) this.live.splice(i, 1);
    }
    for (let i = 0; i < this.max; i++) {
      const r = this.live[i];
      if (!r) {
        if (A.al[i] !== 0) {
          A.al[i] = 0;
          this.dummy.position.set(0, 0, -9999);
          this.dummy.scale.setScalar(1);
          this.dummy.updateMatrix();
          this.mesh.setMatrixAt(i, this.dummy.matrix);
        }
        continue;
      }
      const tone = P[r.tone] || P.neutral;
      A.c[i * 3] = tone[0];
      A.c[i * 3 + 1] = tone[1];
      A.c[i * 3 + 2] = tone[2];
      A.p[i] = r.progress;
      A.sec[i] = Math.max(0, r.progress - r.lag);
      A.rad[i] = r.radius * 2;
      A.gap[i] = r.gap;
      A.al[i] = r.alpha * Math.pow(this.veil(r.x, r.y), 0.6);
      this.dummy.position.set(r.x, r.y, r.z);
      this.dummy.scale.setScalar(1);
      this.dummy.updateMatrix();
      this.mesh.setMatrixAt(i, this.dummy.matrix);
    }
    this.mesh.instanceMatrix.needsUpdate = true;
    const g = this.mesh.geometry;
    for (const k of ["aColor", "aProgress", "aRadius", "aGap", "aSecond", "aAlpha"]) {
      g.getAttribute(k).needsUpdate = true;
    }
  }

  dispose() {
    this.mesh.geometry.dispose();
    this.mesh.material.dispose();
  }
}

/* ------------------------------------------------------------- Sequencer */

/** Tiny scheduler: semantic choreography per scenario, no per-frame React state. */
class Sequencer {
  constructor() {
    this.queue = [];
    this.clock = 0;
  }
  at(delay, fn) {
    this.queue.push({ t: this.clock + delay, fn });
  }
  clear() {
    this.queue.length = 0;
  }
  update(dt) {
    this.clock += dt;
    for (let i = this.queue.length - 1; i >= 0; i--) {
      if (this.clock >= this.queue[i].t) {
        const fn = this.queue[i].fn;
        this.queue.splice(i, 1);
        try { fn(); } catch (_) { /* a sequence step must never break the frame */ }
      }
    }
  }
}

/* ----------------------------------------------------------------- Scene */

class CareLoopScene {
  constructor(THREE, canvas) {
    this.THREE = THREE;
    this.canvas = canvas;
    this.t = tier();
    this.mode = "ambient";
    this.themeName = "light";
    this.palette = PALETTES.light;

    this.renderer = new THREE.WebGLRenderer({
      canvas,
      alpha: true,
      antialias: true,
      powerPreference: "high-performance"
    });
    this.renderer.setClearColor(0x000000, 0);
    this.dpr = Math.min(window.devicePixelRatio || 1, this.t.dpr);
    this.renderer.setPixelRatio(this.dpr);

    this.scene = new THREE.Scene();
    this.camera = new THREE.PerspectiveCamera(30, 1, 0.1, 120);
    this.camera.position.set(0, 0, 26);

    this.group = new THREE.Group();
    this.scene.add(this.group);

    this.layout = new Layout(this.t);
    this.paths = new PathField(THREE, this.layout, this.palette);
    this.nodes = new NodeField(THREE, this.layout, this.palette);
    this.signals = new SignalField(THREE, this.layout, this.paths, this.palette);
    this.ripples = new RippleField(THREE, this.palette);
    this.ripples.veilFn = (x, y) => this.layout.veilAt(x, y);
    this.group.add(this.paths.object, this.nodes.object, this.signals.object, this.ripples.object);

    this.seq = new Sequencer();

    // Pointer influence, damped toward the target so it feels viscous.
    this.pointer = { x: 999, y: 999, tx: 999, ty: 999, amt: 0, tAmt: 0, active: false };
    this.scroll = { v: 0, target: 0 };
    this.reveal = REDUCED ? 1 : 0;
    this.time = 0;
    this.nextAmbient = 1.4;
    this.visible = true;
    this.running = false;
    this.zoneAnchor = null;

    this.plane = new THREE.Plane(new THREE.Vector3(0, 0, 1), 0);
    this.ray = new THREE.Raycaster();
    this.ndc = new THREE.Vector2();
    this.hit = new THREE.Vector3();
    this.zoneFrame = 0;
    this.refreshZones();

    this.resize();
    this.bind();
    this.entrance();
    this.start();
  }

  /* -------------------- layout -------------------- */

  resize() {
    const w = window.innerWidth;
    const h = window.innerHeight;
    this.camera.aspect = w / h;
    this.camera.updateProjectionMatrix();
    this.renderer.setSize(w, h, false);
    this.layout.project(this.camera);
    this.paths.refresh(this.palette);
    this.nodes.refresh(this.palette);
    this.nodes.material.uniforms.uPixelRatio.value = this.dpr;
    this.signals.material.uniforms.uPixelRatio.value = this.dpr;
    this.refreshZones();
  }

  setTheme(name) {
    if (name === this.themeName) return;
    this.themeName = name;
    this.palette = PALETTES[name] || PALETTES.light;
    this.signals.palette = this.palette;
    this.ripples.palette = this.palette;
    this.paths.refresh(this.palette);
    this.nodes.refresh(this.palette);
  }

  /* -------------------- interaction -------------------- */

  /**
   * Read the real layout and turn it into world-space exclusion boxes, so the
   * field frames the interface instead of wallpapering it. Recomputed cheaply,
   * which also covers scroll and route changes without hardcoded coordinates.
   */
  refreshZones() {
    const els = document.querySelectorAll("[data-scene-exclude]");
    const vw = window.innerWidth;
    const vh = window.innerHeight;
    const halfH = this.layout.halfH || 1;
    const halfW = this.layout.halfW || 1;
    const gy = this.group ? this.group.position.y : 0;

    const rects = [];
    for (const el of els) {
      const r = el.getBoundingClientRect();
      if (r.width < 4 || r.height < 4) continue;
      if (r.bottom < -80 || r.top > vh + 80) continue;   // offscreen: ignore
      rects.push(r);
    }
    // Six slots: keep the largest, they carry the most reading.
    rects.sort((a, b) => b.width * b.height - a.width * a.height);
    rects.length = Math.min(rects.length, MAX_ZONES);

    const zones = [];
    for (const r of rects) {
      const x0 = (r.left / vw) * 2 - 1;
      const x1 = (r.right / vw) * 2 - 1;
      const y0 = -((r.bottom / vh) * 2 - 1);
      const y1 = -((r.top / vh) * 2 - 1);
      // Compare in the network's local space: the group drifts with scroll.
      zones.push([x0 * halfW, y0 * halfH - gy, x1 * halfW, y1 * halfH - gy]);
    }
    this.layout.zones = zones;

    for (const mat of [this.paths && this.paths.material, this.nodes && this.nodes.material]) {
      if (!mat || !mat.uniforms.uZones) continue;
      const v = mat.uniforms.uZones.value;
      for (let i = 0; i < MAX_ZONES; i++) {
        const b = zones[i];
        if (b) v[i].set(b[0], b[1], b[2], b[3]);
        else v[i].set(0, 0, 0, 0);
      }
    }
  }

  /* -------------------- interaction -------------------- */

  worldAt(clientX, clientY) {
    this.ndc.set((clientX / window.innerWidth) * 2 - 1, -(clientY / window.innerHeight) * 2 + 1);
    this.ray.setFromCamera(this.ndc, this.camera);
    this.ray.ray.intersectPlane(this.plane, this.hit);
    return this.hit;
  }

  bind() {
    const onMove = (e) => {
      const p = this.worldAt(e.clientX, e.clientY);
      this.pointer.tx = p.x;
      this.pointer.ty = p.y;
      this.pointer.tAmt = REDUCED ? 0 : 1;
      this.pointer.active = true;
      if (!REDUCED && Math.random() < 0.014) this.discover(p);
    };
    const onLeave = () => {
      this.pointer.tAmt = 0;
      this.pointer.active = false;
    };
    const onScroll = () => {
      const max = Math.max(1, document.body.scrollHeight - window.innerHeight);
      this.scroll.target = Math.min(1, window.scrollY / max);
    };
    const onResize = () => this.resize();
    const onVisibility = () => {
      this.visible = !document.hidden;
      if (this.visible) this.start();
    };
    const onDown = (e) => {
      if (REDUCED) return;
      const zone = e.target && e.target.closest ? e.target.closest("[data-scene-zone]") : null;
      if (zone) {
        this.runSequence(zone.getAttribute("data-scene-zone"), zone, true);
      } else {
        this.localPulse(this.worldAt(e.clientX, e.clientY));
      }
    };
    const onOver = (e) => {
      const zone = e.target && e.target.closest ? e.target.closest("[data-scene-zone]") : null;
      const key = zone ? zone.getAttribute("data-scene-zone") : null;
      if (key === this.hoverKey) return;
      this.hoverKey = key;
      if (key) this.runSequence(key, zone, false);
      else {
        this.mode = "ambient";
        this.zoneAnchor = null;
      }
    };

    window.addEventListener("pointermove", onMove, { passive: true });
    window.addEventListener("pointerdown", onDown, { passive: true });
    window.addEventListener("pointerover", onOver, { passive: true });
    window.addEventListener("pointerleave", onLeave, { passive: true });
    window.addEventListener("scroll", onScroll, { passive: true });
    window.addEventListener("resize", onResize);
    document.addEventListener("visibilitychange", onVisibility);

    this.unbind = () => {
      window.removeEventListener("pointermove", onMove);
      window.removeEventListener("pointerdown", onDown);
      window.removeEventListener("pointerover", onOver);
      window.removeEventListener("pointerleave", onLeave);
      window.removeEventListener("scroll", onScroll);
      window.removeEventListener("resize", onResize);
      document.removeEventListener("visibilitychange", onVisibility);
    };
  }

  /** Nearest node gains a halo and wakes its neighbours a beat later. */
  discover(world) {
    let best = -1;
    let bestD = 9;
    const nodes = this.layout.nodes;
    for (let i = 0; i < nodes.length; i++) {
      const p = nodes[i].pos;
      const d = (p.x - world.x) ** 2 + (p.y - world.y) ** 2;
      if (d < bestD) { bestD = d; best = i; }
    }
    if (best < 0) return;
    this.nodes.excite(best, 0.7);
    const touching = this.layout.paths
      .map((p, i) => ({ p, i }))
      .filter(({ p }) => p.a === best || p.b === best)
      .slice(0, 3);
    touching.forEach(({ p, i }, k) => {
      this.seq.at(0.05 + k * 0.07, () => {
        this.paths.excite(i, 0.5);
        this.nodes.excite(p.a === best ? p.b : p.a, 0.45);
      });
    });
  }

  /** Restrained response to a click on empty ground. */
  localPulse(world) {
    this.ripples.spawn(world, TONE_FOR_MODE[this.mode] || "neutral", {
      radius: 2.2, speed: 1.5, alpha: 0.26, lag: 0.3
    });
    let best = -1;
    let bestD = 24;
    const nodes = this.layout.nodes;
    for (let i = 0; i < nodes.length; i++) {
      const p = nodes[i].pos;
      const d = (p.x - world.x) ** 2 + (p.y - world.y) ** 2;
      if (d < bestD) { bestD = d; best = i; }
    }
    if (best >= 0) {
      this.nodes.excite(best, 0.8);
      const near = this.layout.paths.findIndex((p) => p.a === best || p.b === best);
      if (near >= 0) this.signals.emit(near, TONE_FOR_MODE[this.mode] || "neutral", { speed: 0.6, alpha: 0.7 });
    }
  }

  chainWalk(key, tone, opts = {}) {
    const chain = this.layout.chains[key];
    if (!chain) return;
    const hops = chain.paths.filter((i) => this.layout.paths[i].kind === key);
    const step = opts.step ?? 0.19;
    hops.forEach((pathIndex, k) => {
      this.seq.at(k * step, () => {
        const path = this.layout.paths[pathIndex];
        this.nodes.excite(path.a, 0.9);
        this.signals.emit(pathIndex, tone, {
          speed: opts.speed ?? 1.5,
          alpha: opts.alpha ?? 1,
          onArrive: (nodeIndex) => {
            if (nodeIndex >= 0) this.nodes.excite(nodeIndex, 1);
            if (k === hops.length - 1 && opts.onEnd) opts.onEnd(nodeIndex);
          }
        });
      });
    });
    return hops.length * step;
  }

  /**
   * Per-scenario semantics:
   *   referral  — the workflow reaches closure, and a clean ripple confirms it
   *   injection — one branch fractures and is rejected; the trusted route continues
   *   mismatch  — two asynchronous rings, and the closing arc stays open
   */
  runSequence(key, el, clicked) {
    if (el) {
      const r = el.getBoundingClientRect();
      const p = this.worldAt(r.left + r.width / 2, r.top + r.height / 2);
      this.zoneAnchor = { x: p.x, y: p.y };
    }

    const strength = clicked ? 1 : 0.72;

    if (key === "referral") {
      this.mode = "referral";
      const span = this.chainWalk("referral", "green", { speed: clicked ? 2.1 : 1.6, alpha: strength });
      this.seq.at(span + 0.34, () => {
        const chain = this.layout.chains.referral;
        const last = this.layout.nodes[chain.nodes[chain.nodes.length - 1]];
        this.ripples.spawn(last.pos, "green", { radius: 3.6, speed: 0.85, alpha: 0.34 * strength + 0.08, lag: 0.14 });
        this.nodes.excite(chain.nodes[chain.nodes.length - 1], 1.3);
      });
      return;
    }

    if (key === "injection") {
      this.mode = "injection";
      const chain = this.layout.chains.injection;
      const hops = chain.paths;
      hops.slice(0, 3).forEach((pathIndex, k) => {
        this.seq.at(k * 0.2, () => {
          this.signals.emit(pathIndex, "violet", { speed: 1.5, alpha: strength });
          this.nodes.excite(this.layout.paths[pathIndex].a, 0.9);
        });
      });
      // The unsafe branch diverges, then fades out rather than erroring.
      this.seq.at(0.62, () => {
        this.branchPathsActive = true;
        this.layout.branchPaths.forEach((pathIndex, k) => {
          this.seq.at(k * 0.11, () => {
            this.paths.excite(pathIndex, 1);
            this.signals.emit(pathIndex, "violet", { speed: 1.9, alpha: 0.95 * strength, fade: 0.6 });
          });
        });
      });
      // Meanwhile the legitimate route keeps going and completes.
      this.seq.at(0.72, () => {
        hops.slice(3).forEach((pathIndex, k) => {
          this.seq.at(k * 0.2, () => {
            this.signals.emit(pathIndex, "green", { speed: 1.6, alpha: strength });
            this.nodes.excite(this.layout.paths[pathIndex].b, 0.95);
          });
        });
      });
      this.seq.at(1.6, () => {
        const last = this.layout.nodes[chain.nodes[chain.nodes.length - 1]];
        this.ripples.spawn(last.pos, "green", { radius: 3.0, speed: 0.95, alpha: 0.30 * strength + 0.07 });
      });
      return;
    }

    if (key === "mismatch") {
      this.mode = "mismatch";
      const chain = this.layout.chains.mismatch;
      const hops = chain.paths.filter((i) => this.layout.paths[i].kind === "mismatch");
      hops.forEach((pathIndex, k) => {
        this.seq.at(k * 0.2, () => {
          this.signals.emit(pathIndex, "terracotta", { speed: 1.55, alpha: strength });
          this.nodes.excite(this.layout.paths[pathIndex].a, 0.9);
        });
      });
      const verifyNode = this.layout.nodes[chain.nodes[chain.nodes.length - 1]];
      this.seq.at(hops.length * 0.2 + 0.26, () => {
        this.nodes.excite(chain.nodes[chain.nodes.length - 1], 1.4);
        // Authorized and observed, arriving out of step.
        this.ripples.spawn(verifyNode.pos, "terracotta", { radius: 3.3, speed: 0.8, alpha: 0.32 * strength + 0.08, lag: 0.3 });
      });
      // The closure is attempted and refuses to complete — the gap remains.
      this.seq.at(hops.length * 0.2 + 0.72, () => {
        this.paths.excite(this.layout.mismatchClosure, 1);
        this.signals.emit(this.layout.mismatchClosure, "terracotta", { speed: 1.1, alpha: 0.85 * strength, fade: 0.75 });
        this.ripples.spawn(verifyNode.pos, "terracotta", {
          radius: 4.2, speed: 0.55, alpha: 0.26 * strength + 0.06, gap: 0.55, lag: 0.22
        });
      });
      return;
    }

    // Navbar zones: region-scoped activity, subliminal by design.
    if (key === "home") {
      this.mode = "home";
      this.layout.nodes.forEach((_, i) => {
        if (i % 4 === 0) this.seq.at(Math.random() * 0.4, () => this.nodes.excite(i, 0.42));
      });
      return;
    }
    if (key === "trust") {
      this.mode = "trust";
      const chain = this.layout.chains.referral;
      chain.paths.slice(-3).forEach((pathIndex, k) =>
        this.seq.at(k * 0.14, () => {
          this.paths.excite(pathIndex, 0.8);
          this.signals.emit(pathIndex, "green", { speed: 1.4, alpha: 0.65 });
        })
      );
      return;
    }
    if (key === "hood") {
      this.mode = "hood";
      // Briefly reveal topology that is normally almost invisible.
      this.layout.paths.forEach((p, i) => {
        if (p.kind === "ambient" || p.kind === "branch") {
          this.seq.at(Math.random() * 0.5, () => this.paths.excite(i, 0.72));
        }
      });
      return;
    }
    if (key === "logo") {
      this.mode = "ambient";
      const first = this.layout.chains.injection.paths[0];
      this.signals.emit(first, "neutral", { speed: 1.2, alpha: 0.6 });
      return;
    }
  }

  /* -------------------- entrance + ambient -------------------- */

  entrance() {
    if (REDUCED) {
      this.reveal = 1;
      return;
    }
    // Nodes, then hairline connections drawn progressively, then two signals.
    this.seq.at(1.5, () => {
      const first = this.layout.chains.injection.paths[0];
      this.signals.emit(first, "neutral", { speed: 0.9, alpha: 0.55 });
    });
    this.seq.at(2.0, () => {
      this.chainWalk("referral", "green", { speed: 1.3, alpha: 0.5, step: 0.16 });
    });
  }

  ambientTick(dt) {
    this.nextAmbient -= dt;
    if (this.nextAmbient > 0) return;
    // Asymmetric intervals so the field never reads as a fixed loop.
    this.nextAmbient = 1.1 + Math.random() * 4.6;
    const paths = this.layout.paths;
    const idx = Math.floor(Math.random() * paths.length);
    const kind = paths[idx].kind;
    const tone =
      kind === "ambient" ? (Math.random() < 0.22 ? "green" : "neutral") :
      kind === "branch" ? "violet" :
      kind === "closure" ? "terracotta" : TONE_FOR_MODE[kind] || "neutral";
    const sig = this.signals.emit(idx, tone, { speed: 0.28 + Math.random() * 0.3, alpha: 0.5 });
    if (sig && Math.random() < 0.2) {
      // Occasional bifurcation: a second signal leaves the same node.
      const branch = paths.findIndex((p, i) => i !== idx && p.a === paths[idx].b);
      if (branch >= 0) this.seq.at(0.5, () => this.signals.emit(branch, tone, { speed: 0.4, alpha: 0.4 }));
    }
    if (Math.random() < 0.14) {
      const n = this.layout.nodes[paths[idx].b];
      this.seq.at(0.9, () => this.ripples.spawn(n.pos, tone, { radius: 2.4, speed: 1.1, alpha: 0.16 }));
    }
  }

  /* -------------------- loop -------------------- */

  start() {
    if (this.running) return;
    this.running = true;
    this.last = performance.now();
    const frame = (now) => {
      if (!this.running) return;
      if (!this.visible) {
        this.running = false;
        return;
      }
      const dt = Math.min(0.05, (now - this.last) / 1000);
      this.last = now;
      this.tick(dt);
      this.raf = requestAnimationFrame(frame);
    };
    this.raf = requestAnimationFrame(frame);
  }

  tick(dt) {
    this.time += dt;
    this.seq.update(dt);

    if (!REDUCED) {
      this.reveal = Math.min(1, this.reveal + dt / 2.4);
      this.ambientTick(dt);
    }

    // Damped pointer: viscous, never snapping.
    const P = this.pointer;
    const k = Math.min(1, dt * 4.5);
    if (P.tx !== 999) {
      P.x = P.x === 999 ? P.tx : P.x + (P.tx - P.x) * k;
      P.y = P.y === 999 ? P.ty : P.y + (P.ty - P.y) * k;
    }
    P.amt += (P.tAmt - P.amt) * Math.min(1, dt * 3);

    // Scroll: damped travel through the depth field, a few degrees of rotation.
    this.scroll.v += (this.scroll.target - this.scroll.v) * Math.min(1, dt * 2.2);
    const s = this.scroll.v;
    if (!REDUCED) {
      this.group.rotation.y = -0.10 + s * 0.20 + Math.sin(this.time * 0.045) * 0.022;
      this.group.rotation.x = 0.03 - s * 0.09;
      this.group.position.y = s * 5.2;
      this.group.position.z = s * 3.4;
      // Parallax bias toward whichever zone is being explored.
      const ax = this.zoneAnchor ? this.zoneAnchor.x * 0.035 : 0;
      const ay = this.zoneAnchor ? this.zoneAnchor.y * 0.02 : 0;
      this.camera.position.x += (P.amt * (P.x || 0) * 0.02 + ax - this.camera.position.x) * Math.min(1, dt * 1.6);
      this.camera.position.y += (P.amt * (P.y || 0) * 0.014 + ay - this.camera.position.y) * Math.min(1, dt * 1.6);
      this.camera.lookAt(0, 0, 0);
    }

    for (const m of [this.paths.material, this.nodes.material]) {
      m.uniforms.uTime.value = this.time;
      m.uniforms.uReveal.value = this.reveal;
      m.uniforms.uPointerAmt.value = P.amt;
      m.uniforms.uPointer.value.set(P.x === 999 ? 9999 : P.x, P.y === 999 ? 9999 : P.y, 0);
    }

    this.paths.update(dt);
    this.nodes.update(dt);
    this.signals.update(dt, this.time);
    this.ripples.update(dt);

    // Zones follow the live layout; a few rect reads every twelfth frame.
    this.zoneFrame = (this.zoneFrame + 1) % 12;
    if (this.zoneFrame === 0) this.refreshZones();

    this.renderer.render(this.scene, this.camera);
  }

  destroy() {
    this.running = false;
    if (this.raf) cancelAnimationFrame(this.raf);
    if (this.unbind) this.unbind();
    this.paths.dispose();
    this.nodes.dispose();
    this.signals.dispose();
    this.ripples.dispose();
    this.renderer.dispose();
  }
}

/* --------------------------------------------------------------- bootstrap */

async function boot() {
  const canvas = await waitFor("#careloop-field");
  const root = document.querySelector("[data-theme]");

  let THREE;
  try {
    THREE = await import(CDN);
  } catch (err) {
    // WebGL/CDN unavailable: the page must remain entirely usable.
    console.warn("[CareLoop] orchestration field unavailable:", err && err.message);
    canvas.style.display = "none";
    return;
  }

  let scene;
  try {
    scene = new CareLoopScene(THREE, canvas);
  } catch (err) {
    console.warn("[CareLoop] orchestration field failed to start:", err && err.message);
    canvas.style.display = "none";
    return;
  }

  window.__careLoopScene = scene;

  if (root) {
    const sync = () => scene.setTheme(root.getAttribute("data-theme") === "dark" ? "dark" : "light");
    sync();
    new MutationObserver(sync).observe(root, { attributes: true, attributeFilter: ["data-theme"] });
  }

  // The canvas element is re-created when the DC re-renders its root; re-attach.
  const guard = new MutationObserver(() => {
    if (!document.body.contains(canvas)) {
      guard.disconnect();
      scene.destroy();
      boot();
    }
  });
  guard.observe(document.body, { childList: true, subtree: true });
}

function waitFor(selector) {
  return new Promise((resolve) => {
    const found = document.querySelector(selector);
    if (found) return resolve(found);
    const obs = new MutationObserver(() => {
      const el = document.querySelector(selector);
      if (el) {
        obs.disconnect();
        resolve(el);
      }
    });
    obs.observe(document.documentElement, { childList: true, subtree: true });
  });
}

boot();
