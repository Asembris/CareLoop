# three.js — vendored

`three-0.160.0.module.min.js` is the official minified ES module build of **three.js r160**,
committed rather than fetched at page load. The reason is the one already written into
`index.html` for the fonts: the CareLoop demo is filmed once, and a page whose visual field
depends on a CDN reachable at that moment is a page that can lose it. It is also what keeps the
deployment single-origin — `careloop/` mounts no CORS middleware anywhere, and a runtime import
from a third-party host would be the one cross-origin request the whole arrangement avoids.

**The version is in the filename on purpose.** `careloop/api/static.py` serves everything under
`assets/` as `public, max-age=31536000, immutable`, which is only honest for bytes that can never
change under a name. Upgrading three means a new filename and a new `CDN` constant in
`public/careloop-scene.js` — never an edit in place.

| | |
|---|---|
| Version | `three@0.160.0` |
| Upstream file | `package/build/three.module.min.js` |
| Source | `npm pack three@0.160.0` (registry.npmjs.org), 2026-08-18 |
| Tarball SHA-256 | `1ee2f935c4f555814b388e87b5ef78a44856bd2e9d0feb88643a6e193fb42856` |
| File SHA-256 | `3e690ac7d180b0aadf0891bea39eec643e29e2d3e75c99b18689518665f69ba6` |
| Size | 670 681 bytes |

The minified build is carried rather than the 1.27 MB unminified one; they are the same release,
built by the same publish. The file is unmodified, carries its own `@license` banner, and
references no source map. r160 is the version `careloop-motion` was authored and verified against
— see `frontend/docs/MOTION.md`.

The motion field is the only consumer, it is loaded lazily by `public/careloop-scene.js`, and
nothing in the React application imports three. `three` is deliberately **not** an `npm`
dependency: the scene is a drop-in vendored file with no build step, and adding a bundler path for
it would make it one.

## Licence

three.js is **MIT**. CareLoop's own code is Apache-2.0; this file is not, and this note is what
keeps the two from being confused.

```
The MIT License

Copyright © 2010-2023 three.js authors

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in
all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
THE SOFTWARE.
```
