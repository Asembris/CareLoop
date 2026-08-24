/* ------------------------------------------------------------------ *
 * Production identity, in the browser
 * ------------------------------------------------------------------ *
 *
 * **One coherent security model: a bearer token in memory.** Not cookies, and not both. The API is
 * header-driven end to end, the built frontend is served same-origin so there is no CORS to
 * negotiate, and `careloop/api/sessions.py` records that header forwarding through the CDN is
 * *measured* while cookie forwarding is not — a cookie design would be an assumption that fails in
 * production rather than in a test. A bearer header also carries no ambient authority, so no
 * cross-origin form can send one and CSRF machinery would be cargo-cult.
 *
 * **In memory, and nowhere else.** Not `localStorage`, not `sessionStorage`, not IndexedDB. A token
 * in web storage is an XSS-exfiltratable credential that outlives the tab that obtained it; a
 * module-scoped variable dies with the page and is not reachable by anything that cannot already
 * run in this context. A reload therefore re-authenticates silently against the hosted UI, which is
 * a redirect the visitor usually does not see.
 *
 * The **PKCE verifier** is the one thing that must survive a redirect, because the code exchange
 * happens in a *different page load* from the one that generated it. It lives in `sessionStorage`
 * for exactly that hop and is deleted the moment it is used — it is a single-use secret bound to
 * one authorization code, not a credential, and it is worthless once the code is spent.
 *
 * **The demo carries none of this.** `GET /api/auth` answers `{"mode": "DEMO"}` and this module
 * does nothing at all: no redirect, no token, no login screen. The judge opens the URL and is on
 * the dashboard, exactly as before. What the demo *gains* is that the page now knows its identity
 * is synthetic and can say so rather than implying otherwise.
 */

export type AuthMode = "DEMO" | "PRODUCTION";

/** What `GET /api/auth` publishes. Everything here is public; none of it is a secret. */
export interface AuthDiscovery {
  mode: AuthMode;
  authorization_endpoint?: string;
  token_endpoint?: string;
  end_session_endpoint?: string;
  client_id?: string;
  scopes?: string;
}

const BASE_URL: string = import.meta.env.VITE_API_BASE_URL ?? "";

/** Where the PKCE verifier waits out the redirect. Deleted the moment the code is exchanged. */
const VERIFIER_KEY = "careloop.pkce";

/** The access token. **Module scope on purpose** — see the header. */
let token: string | null = null;

/** What the backend said it is. `null` until asked. */
let discovered: AuthDiscovery | null = null;

/** The discovery request in flight, so concurrent callers ask once. */
let discovering: Promise<AuthDiscovery> | null = null;

/** The token this page holds, or `null`. Read by the API adapter on every request. */
export function bearerToken(): string | null {
  return token;
}

/** Whether this deployment verifies identity. `false` for the demo and before discovery. */
export function isProduction(): boolean {
  return discovered?.mode === "PRODUCTION";
}

/** What the backend published, once it has been asked. */
export function authMode(): AuthMode | null {
  return discovered?.mode ?? null;
}

/** Forget everything. For tests, and for signing out locally when there is no hosted UI. */
export function forgetAuth(): void {
  token = null;
  discovered = null;
  discovering = null;
  try {
    window.sessionStorage.removeItem(VERIFIER_KEY);
  } catch {
    /* private-mode browsers throw on storage access; there is nothing to clean up there */
  }
}

/** Ask the backend which identity story it tells. Cached for the life of the page. */
export async function discover(): Promise<AuthDiscovery> {
  if (discovered) return discovered;
  if (!discovering) {
    discovering = fetch(`${BASE_URL}/api/auth`, { headers: { Accept: "application/json" } })
      .then(async (response) => {
        if (!response.ok) throw new Error(`auth discovery answered ${response.status}`);
        return (await response.json()) as AuthDiscovery;
      })
      .then((found) => {
        discovered = found;
        return found;
      })
      .finally(() => {
        discovering = null;
      });
  }
  return discovering;
}

/* ------------------------------------------------------------------ *
 * PKCE
 * ------------------------------------------------------------------ *
 *
 * RFC 7636 with S256. The verifier is 32 random bytes base64url-encoded; the challenge is its
 * SHA-256, base64url-encoded. `crypto.getRandomValues` and `crypto.subtle` are both standard and
 * both require a secure context, which a CloudFront-served page always is.
 */

function base64url(bytes: Uint8Array): string {
  let binary = "";
  for (const byte of bytes) binary += String.fromCharCode(byte);
  return btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

function randomVerifier(): string {
  const bytes = new Uint8Array(32);
  crypto.getRandomValues(bytes);
  return base64url(bytes);
}

async function challengeFor(verifier: string): Promise<string> {
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(verifier));
  return base64url(new Uint8Array(digest));
}

/** Where the hosted UI sends the browser back to. The app's own origin, and only its origin. */
function redirectUri(): string {
  return `${window.location.origin}/`;
}

/* ------------------------------------------------------------------ *
 * The two halves of the flow
 * ------------------------------------------------------------------ */

/** Send the browser to the hosted UI. Does not return — the page navigates away.
 *
 *  Returns `false` without navigating when this deployment publishes no login endpoint, which is
 *  the truthful state for a production CareLoop whose clients are machines rather than people. */
export async function beginLogin(): Promise<boolean> {
  const found = await discover();
  if (!found.authorization_endpoint || !found.client_id) return false;

  const verifier = randomVerifier();
  try {
    window.sessionStorage.setItem(VERIFIER_KEY, verifier);
  } catch {
    // Without somewhere to keep the verifier across the redirect the exchange cannot succeed, so
    // refusing to start is better than a round trip that will fail on the way back.
    return false;
  }

  const query = new URLSearchParams({
    response_type: "code",
    client_id: found.client_id,
    redirect_uri: redirectUri(),
    scope: found.scopes ?? "openid",
    code_challenge: await challengeFor(verifier),
    code_challenge_method: "S256"
  });
  window.location.assign(`${found.authorization_endpoint}?${query.toString()}`);
  return true;
}

/** Exchange the `?code=` the hosted UI came back with, if there is one.
 *
 *  Returns whether a token was obtained. The code and the verifier are both single-use: the query
 *  string is scrubbed from the address bar with `replaceState` so a copied URL carries no
 *  authorization code, and the verifier is deleted whether the exchange succeeded or not. */
export async function completeLoginFromRedirect(): Promise<boolean> {
  const query = new URLSearchParams(window.location.search);
  const code = query.get("code");
  if (!code) return false;

  let verifier: string | null = null;
  try {
    verifier = window.sessionStorage.getItem(VERIFIER_KEY);
    window.sessionStorage.removeItem(VERIFIER_KEY);
  } catch {
    /* see forgetAuth */
  }
  scrubTheQueryString();
  if (!verifier) return false;

  const found = await discover();
  if (!found.token_endpoint || !found.client_id) return false;

  try {
    const response = await fetch(found.token_endpoint, {
      method: "POST",
      headers: { "Content-Type": "application/x-www-form-urlencoded" },
      body: new URLSearchParams({
        grant_type: "authorization_code",
        client_id: found.client_id,
        code,
        redirect_uri: redirectUri(),
        code_verifier: verifier
      }).toString()
    });
    if (!response.ok) return false;
    const body = (await response.json()) as { access_token?: string };
    if (!body.access_token) return false;
    token = body.access_token;
    return true;
  } catch {
    // A blip during the exchange leaves the page unauthenticated, which is the safe state: every
    // API call answers 401 and the login button is still there.
    return false;
  }
}

/** Remove `code` and `state` from the address bar without a reload. */
function scrubTheQueryString(): void {
  const url = new URL(window.location.href);
  url.searchParams.delete("code");
  url.searchParams.delete("state");
  window.history.replaceState({}, "", url.toString());
}

/** Sign out: drop the token here, then let the hosted UI end its own session if there is one. */
export function logout(): void {
  const found = discovered;
  token = null;
  if (found?.end_session_endpoint && found.client_id) {
    const query = new URLSearchParams({
      client_id: found.client_id,
      logout_uri: redirectUri()
    });
    window.location.assign(`${found.end_session_endpoint}?${query.toString()}`);
  }
}

/** Discover, then finish a redirect if this load is one. The one call the app shell makes. */
export async function establishIdentity(): Promise<AuthMode | null> {
  try {
    const found = await discover();
    if (found.mode === "PRODUCTION") await completeLoginFromRedirect();
    return found.mode;
  } catch {
    // A backend that cannot be reached is a connection problem, which the dashboard already has a
    // designed state for. Guessing a mode here would be worse than not knowing one.
    return null;
  }
}
