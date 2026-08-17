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
 * The one place the UI says the backend cannot be reached.
 *
 * Rendered on every screen, because every screen otherwise keeps showing its last good state
 * through an outage — which is the right behaviour only as long as something says it is stale.
 *
 * The live region is a *persistent* element whose text changes, rather than a `role="status"` on
 * the banner itself: a region that mounts with its message is unreliable across screen readers,
 * and an unmounting banner cannot announce "Reconnected." at all.
 */
export default function ConnectionBanner() {
  const { connection } = useCareLoop();
  const wasDown = useRef(false);
  const [announcement, setAnnouncement] = useState("");

  useEffect(() => {
    if (connection === "unreachable") {
      wasDown.current = true;
      setAnnouncement("CareLoop can't be reached. Retrying automatically.");
    } else if (connection === "live" && wasDown.current) {
      wasDown.current = false;
      setAnnouncement("Reconnected.");
    }
  }, [connection]);

  return (
    <>
      <span role="status" style={visuallyHidden}>
        {announcement}
      </span>
      {connection === "unreachable" ? (
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
          <span style={{ fontSize: 13, color: "var(--muted-foreground)" }}>
            CareLoop can&apos;t be reached — retrying automatically.
          </span>
        </div>
      ) : null}
    </>
  );
}
