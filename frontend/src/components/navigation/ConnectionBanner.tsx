import { useEffect, useRef, useState } from "react";
import { useCareLoop } from "@/hooks/useCareLoop";

/** Screen-reader-only. The app has no utility CSS, so the clip pattern lives inline. */
const visuallyHidden: React.CSSProperties = {
  position: "absolute",
  width: 1,
  height: 1,
  padding: 0,
  margin: -1,
  overflow: "hidden",
  clip: "rect(0 0 0 0)",
  whiteSpace: "nowrap",
  border: 0
};

/**
 * The one place the UI says the page has no world to read, and which of the two reasons it is.
 *
 * Rendered on every screen, because every screen otherwise keeps showing its last good state
 * through an outage — which is the right behaviour only as long as something says it is stale.
 *
 * **Two states, two sentences, deliberately.** `unreachable` is nobody answering. `at_capacity` is
 * the backend answering, in full health, that every world it holds is in use — each visitor gets
 * their own case store and their own clinic, and it refuses rather than taking one from somebody
 * who is still using it. They look identical from here (no session, every read fails) and they are
 * not the same fact: one is fixed by the service coming back, the other by somebody finishing.
 * Saying "CareLoop can't be reached" over a healthy service would be this product telling a small
 * lie about its own state, and it would send the reader to refresh, which does not help.
 *
 * The live region is a *persistent* element whose text changes, rather than a `role="status"` on
 * the banner itself: a region that mounts with its message is unreliable across screen readers,
 * and an unmounting banner cannot announce "Reconnected." at all. Each state gets its own
 * announcement for the same reason it gets its own banner.
 */
export default function ConnectionBanner() {
  const { connection } = useCareLoop();
  const wasDown = useRef(false);
  const [announcement, setAnnouncement] = useState("");

  useEffect(() => {
    if (connection === "unreachable") {
      wasDown.current = true;
      setAnnouncement("CareLoop can't be reached. Retrying automatically.");
    } else if (connection === "at_capacity") {
      wasDown.current = true;
      setAnnouncement("The demo is full right now. Every world is in use; trying again shortly.");
    } else if (connection === "live" && wasDown.current) {
      wasDown.current = false;
      setAnnouncement("Reconnected.");
    }
  }, [connection]);

  const message =
    connection === "unreachable"
      ? "CareLoop can't be reached — retrying automatically."
      : connection === "at_capacity"
        ? "The demo is full right now — every world is in use. Trying again shortly."
        : null;

  return (
    <>
      <span role="status" style={visuallyHidden}>
        {announcement}
      </span>
      {message !== null ? (
        <div
          style={{
            display: "flex",
            justifyContent: "center",
            alignItems: "center",
            gap: 8,
            padding: "9px var(--page-gutter)",
            borderBottom: "1px solid color-mix(in oklab, currentColor 12%, transparent)",
            background: "color-mix(in oklab, var(--state-decision) 6%, var(--background))"
          }}
        >
          <span
            aria-hidden
            style={{
              width: 6,
              height: 6,
              borderRadius: "50%",
              background: "var(--state-decision)",
              flexShrink: 0
            }}
          />
          <span style={{ fontSize: 13, color: "var(--muted-foreground)" }}>{message}</span>
        </div>
      ) : null}
    </>
  );
}
