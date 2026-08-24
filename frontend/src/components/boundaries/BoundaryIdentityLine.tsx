import { useEffect, useState } from "react";
import { authMode, establishIdentity, type AuthMode } from "@/services/auth";

/**
 * Whose boundaries these are, said once and truthfully.
 *
 * The demo says the caregiver is invented **and that the enforcement is not**, because both halves
 * are true and only saying the first would undersell the page while only saying the second would be
 * the small lie `IdentityStrip` already refuses. Production says the identity was verified and that
 * access is checked server-side, and names nothing about the token, the issuer or the subject.
 *
 * It renders nothing until `GET /api/auth` has answered, for the reason the identity strip does: a
 * line that guessed would occasionally tell a production visitor their identity was synthetic.
 */
export default function BoundaryIdentityLine() {
  const [mode, setMode] = useState<AuthMode | null>(authMode());

  useEffect(() => {
    let live = true;
    void establishIdentity().then((found) => live && setMode(found));
    return () => {
      live = false;
    };
  }, []);

  if (mode === null) return null;

  return (
    <p
      data-testid="boundary-identity"
      style={{
        margin: 0,
        fontSize: 15,
        lineHeight: 1.6,
        color: "var(--muted-foreground)",
        maxWidth: "62ch"
      }}
    >
      {mode === "DEMO" ? (
        <>
          These are the boundaries of a <strong>synthetic caregiver in a synthetic world</strong>.
          The enforcement is real: everything below is read from the same policy the agent is
          actually held to.
        </>
      ) : (
        <>
          These are <strong>your</strong> boundaries, read from the policy in force and the
          destinations configured for your account. Access is checked against server-side grants
          every time CareLoop acts.
        </>
      )}
    </p>
  );
}
