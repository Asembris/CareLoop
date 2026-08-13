import { readFileSync, readdirSync, existsSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

/* The three families are self-hosted so the demo cannot be filmed in fallback faces because the
   network hiccuped. That is a property of the shipped files, not of anyone's intention, so it is
   asserted on them: nothing the browser loads may name an external host, and the eight woff2 files
   the stylesheet points at must actually be there.

   The sweep covers `index.html` and everything under `src/` that reaches the bundle. Two things
   are out of scope by that rule rather than by exemption: `public/fonts/LICENSE.md` records the
   upstream URLs on purpose and is never linked from a page, and test files are not imported by
   `main.tsx` so nothing in them is ever served — including this one's own list of hosts. */

const ROOT = join(__dirname, "..", "..");

const EXTERNAL_HOSTS = [
  "fonts.googleapis.com",
  "fonts.gstatic.com",
  "use.typekit.net",
  "fonts.bunny.net",
  "cdn.jsdelivr.net"
];

function everyFileUnder(directory: string): string[] {
  return readdirSync(directory, { withFileTypes: true }).flatMap((entry) => {
    const full = join(directory, entry.name);
    return entry.isDirectory() ? everyFileUnder(full) : [full];
  });
}

describe("the type is self-hosted", () => {
  it("names no external font host in anything the browser loads", () => {
    const loaded = [
      join(ROOT, "index.html"),
      ...everyFileUnder(join(ROOT, "src")).filter((file) => !/[\\/]test[\\/]|\.test\./.test(file))
    ];
    const offenders = loaded.filter((file) => {
      const text = readFileSync(file, "utf-8");
      return EXTERNAL_HOSTS.some((host) => text.includes(host));
    });
    expect(offenders).toEqual([]);
  });

  it("ships every woff2 file the stylesheet asks for", () => {
    const stylesheet = readFileSync(join(ROOT, "src", "styles", "fonts.css"), "utf-8");
    const referenced = [...stylesheet.matchAll(/url\("(\/fonts\/[^"]+)"\)/g)].map((m) => m[1]);

    // Non-vacuity: an empty list would make the missing-file check below pass trivially.
    expect(referenced.length).toBe(8);

    const missing = referenced.filter((path) => !existsSync(join(ROOT, "public", path)));
    expect(missing).toEqual([]);
  });

  it("carries real woff2 payloads rather than an error page saved with the right name", () => {
    const files = everyFileUnder(join(ROOT, "public", "fonts")).filter((f) => f.endsWith(".woff2"));
    expect(files.length).toBe(8);
    for (const file of files) {
      // wOF2 — the woff2 signature. A 404 body or an HTML redirect saved under a .woff2 name
      // would render as fallback faces and look exactly like the problem self-hosting fixes.
      expect(readFileSync(file).subarray(0, 4).toString("latin1")).toBe("wOF2");
    }
  });
});
