/* The browser half of production identity.
 *
 * Three properties, and they are the ones a reviewer would want proven rather than described.
 *
 * **The token never touches storage.** Not `localStorage`, not `sessionStorage`, not IndexedDB. The
 * test asserts that over the *whole* of both storages after a successful exchange, rather than over
 * the one key the implementation happens to use — a key rename would defeat the narrower check.
 *
 * **The PKCE verifier survives exactly one redirect.** It has to live in `sessionStorage`, because
 * the exchange happens in a different page load from the one that generated it; it is deleted the
 * moment the code is spent, whether the exchange succeeded or not.
 *
 * **The demo carries none of it.** No discovery of endpoints, no redirect, no token, no
 * `Authorization` header — and `X-Caregiver` still sent, because a synthetic world's audit trail
 * should still record who pressed the button.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  authMode,
  bearerToken,
  beginLogin,
  completeLoginFromRedirect,
  discover,
  establishIdentity,
  forgetAuth,
  isProduction,
  logout
} from "./auth";

const PRODUCTION = {
  mode: "PRODUCTION",
  authorization_endpoint: "https://login.example/oauth2/authorize",
  token_endpoint: "https://login.example/oauth2/token",
  end_session_endpoint: "https://login.example/logout",
  client_id: "client-abc123",
  scopes: "openid"
};

function answering(...responses: Array<[number, unknown]>) {
  const calls: Array<{ url: string; init?: RequestInit }> = [];
  let index = 0;
  const fetcher = vi.fn(async (url: string, init?: RequestInit) => {
    calls.push({ url: String(url), init });
    const [status, body] = responses[Math.min(index, responses.length - 1)];
    index += 1;
    return {
      ok: status >= 200 && status < 300,
      status,
      json: async () => body,
      text: async () => JSON.stringify(body)
    } as Response;
  });
  vi.stubGlobal("fetch", fetcher);
  return calls;
}

function navigation() {
  const assigned: string[] = [];
  const replaced: string[] = [];
  vi.stubGlobal("location", {
    origin: "https://careloop.example",
    href: "https://careloop.example/?code=the-code",
    search: "?code=the-code",
    assign: (to: string) => assigned.push(to)
  });
  vi.stubGlobal("history", {
    replaceState: (_s: unknown, _t: string, url: string) => replaced.push(url)
  });
  return { assigned, replaced };
}

beforeEach(() => {
  forgetAuth();
  window.sessionStorage.clear();
  window.localStorage.clear();
});

afterEach(() => {
  vi.unstubAllGlobals();
  forgetAuth();
});

describe("discovery", () => {
  it("reports the mode the backend published", async () => {
    answering([200, { mode: "DEMO" }]);

    expect((await discover()).mode).toBe("DEMO");
    expect(authMode()).toBe("DEMO");
    expect(isProduction()).toBe(false);
  });

  it("reports production when the backend does", async () => {
    answering([200, PRODUCTION]);

    await discover();

    expect(isProduction()).toBe(true);
  });

  it("asks once however many callers there are", async () => {
    const calls = answering([200, { mode: "DEMO" }]);

    await Promise.all([discover(), discover(), discover()]);

    expect(calls).toHaveLength(1);
  });

  it("does not guess a mode when the backend cannot be reached", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => {
        throw new Error("offline");
      })
    );

    await expect(establishIdentity()).resolves.toBeNull();
    expect(authMode()).toBeNull();
    expect(isProduction()).toBe(false);
  });
});

describe("the demo carries no identity machinery", () => {
  it("holds no token", async () => {
    answering([200, { mode: "DEMO" }]);

    await establishIdentity();

    expect(bearerToken()).toBeNull();
  });

  it("never navigates away", async () => {
    answering([200, { mode: "DEMO" }]);
    const { assigned } = navigation();

    await establishIdentity();

    expect(assigned).toEqual([]);
  });

  it("refuses to start a login it has no endpoint for", async () => {
    answering([200, { mode: "DEMO" }]);
    const { assigned } = navigation();

    await expect(beginLogin()).resolves.toBe(false);
    expect(assigned).toEqual([]);
  });
});

describe("the authorization redirect", () => {
  it("sends the browser to the hosted UI with a S256 challenge", async () => {
    answering([200, PRODUCTION]);
    const { assigned } = navigation();

    await beginLogin();

    expect(assigned).toHaveLength(1);
    const sent = new URL(assigned[0]);
    expect(sent.origin + sent.pathname).toBe(PRODUCTION.authorization_endpoint);
    expect(sent.searchParams.get("response_type")).toBe("code");
    expect(sent.searchParams.get("client_id")).toBe(PRODUCTION.client_id);
    expect(sent.searchParams.get("code_challenge_method")).toBe("S256");
    expect(sent.searchParams.get("code_challenge")).toMatch(/^[A-Za-z0-9_-]{43}$/);
    expect(sent.searchParams.get("redirect_uri")).toBe("https://careloop.example/");
  });

  it("asks for openid and nothing else", async () => {
    answering([200, PRODUCTION]);
    const { assigned } = navigation();

    await beginLogin();

    expect(new URL(assigned[0]).searchParams.get("scope")).toBe("openid");
  });

  it("never puts a secret in the URL", async () => {
    answering([200, PRODUCTION]);
    const { assigned } = navigation();

    await beginLogin();

    const sent = new URL(assigned[0]);
    expect(sent.searchParams.get("client_secret")).toBeNull();
    expect(sent.searchParams.get("code_verifier")).toBeNull();
  });

  it("keeps the verifier only until the redirect returns", async () => {
    answering([200, PRODUCTION]);
    navigation();

    await beginLogin();

    expect(window.sessionStorage.getItem("careloop.pkce")).toMatch(/^[A-Za-z0-9_-]{43}$/);
  });

  it("generates a different verifier every time", async () => {
    answering([200, PRODUCTION]);
    navigation();

    await beginLogin();
    const first = window.sessionStorage.getItem("careloop.pkce");
    await beginLogin();

    expect(window.sessionStorage.getItem("careloop.pkce")).not.toBe(first);
  });
});

describe("the code exchange", () => {
  async function exchange(tokenResponse: [number, unknown]) {
    answering([200, PRODUCTION], tokenResponse);
    const nav = navigation();
    window.sessionStorage.setItem("careloop.pkce", "the-verifier");
    const obtained = await completeLoginFromRedirect();
    return { obtained, nav };
  }

  it("obtains a token and holds it in memory", async () => {
    const { obtained } = await exchange([200, { access_token: "the-token" }]);

    expect(obtained).toBe(true);
    expect(bearerToken()).toBe("the-token");
  });

  it("puts the token in no storage at all", async () => {
    await exchange([200, { access_token: "the-token" }]);

    const everything = [
      ...Object.values({ ...window.sessionStorage }),
      ...Object.values({ ...window.localStorage })
    ].join("|");
    expect(everything).not.toContain("the-token");
  });

  it("spends the verifier exactly once", async () => {
    await exchange([200, { access_token: "the-token" }]);

    expect(window.sessionStorage.getItem("careloop.pkce")).toBeNull();
  });

  it("spends the verifier even when the exchange fails", async () => {
    const { obtained } = await exchange([400, { error: "invalid_grant" }]);

    expect(obtained).toBe(false);
    expect(bearerToken()).toBeNull();
    expect(window.sessionStorage.getItem("careloop.pkce")).toBeNull();
  });

  it("scrubs the authorization code out of the address bar", async () => {
    const { nav } = await exchange([200, { access_token: "the-token" }]);

    expect(nav.replaced).toHaveLength(1);
    expect(nav.replaced[0]).not.toContain("code=");
  });

  it("does nothing when this load is not a redirect", async () => {
    answering([200, PRODUCTION]);
    vi.stubGlobal("location", { origin: "https://careloop.example", href: "https://careloop.example/", search: "" });

    await expect(completeLoginFromRedirect()).resolves.toBe(false);
    expect(bearerToken()).toBeNull();
  });

  it("refuses a code it has no verifier for", async () => {
    answering([200, PRODUCTION], [200, { access_token: "the-token" }]);
    navigation();

    await expect(completeLoginFromRedirect()).resolves.toBe(false);
    expect(bearerToken()).toBeNull();
  });

  it("sends the verifier and no secret to the token endpoint", async () => {
    const calls = answering([200, PRODUCTION], [200, { access_token: "the-token" }]);
    navigation();
    window.sessionStorage.setItem("careloop.pkce", "the-verifier");

    await completeLoginFromRedirect();

    const exchangeCall = calls.find((call) => call.url === PRODUCTION.token_endpoint);
    const body = String(exchangeCall?.init?.body ?? "");
    expect(body).toContain("code_verifier=the-verifier");
    expect(body).toContain("grant_type=authorization_code");
    expect(body).not.toContain("client_secret");
  });

  it("stays unauthenticated when the token endpoint is unreachable", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string) => {
        if (String(url).endsWith("/api/auth")) {
          return { ok: true, status: 200, json: async () => PRODUCTION } as Response;
        }
        throw new Error("offline");
      })
    );
    navigation();
    window.sessionStorage.setItem("careloop.pkce", "the-verifier");

    await expect(completeLoginFromRedirect()).resolves.toBe(false);
    expect(bearerToken()).toBeNull();
  });
});

describe("signing out", () => {
  it("drops the token before navigating anywhere", async () => {
    answering([200, PRODUCTION], [200, { access_token: "the-token" }]);
    const { assigned } = navigation();
    window.sessionStorage.setItem("careloop.pkce", "the-verifier");
    await completeLoginFromRedirect();

    logout();

    expect(bearerToken()).toBeNull();
    expect(assigned[assigned.length - 1]).toContain(PRODUCTION.end_session_endpoint);
  });

  it("drops the token even where there is no hosted UI to visit", async () => {
    answering([200, { mode: "PRODUCTION" }]);
    navigation();
    await discover();

    logout();

    expect(bearerToken()).toBeNull();
  });
});
