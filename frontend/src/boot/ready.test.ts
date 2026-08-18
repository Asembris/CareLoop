import { describe, expect, it, vi, beforeEach, afterEach } from "vitest";
import { renderHook } from "@testing-library/react";
import { READY_EVENT, resetApplicationReadyForTests, useApplicationReady } from "./ready";

/* Frames are driven by hand rather than waited for.
 *
 * The property under test is *ordering* — nothing is announced until two frames after the commit —
 * and a real `requestAnimationFrame` turns that into a question about how long jsdom's timer takes,
 * which is a different question and a slower one. The queue below runs callbacks in registration
 * order, which is what the platform guarantees and all this depends on. */
function frameQueue() {
  const queued = new Map<number, FrameRequestCallback>();
  let next = 0;
  vi.stubGlobal("requestAnimationFrame", (callback: FrameRequestCallback) => {
    next += 1;
    queued.set(next, callback);
    return next;
  });
  // Cancellation is implemented rather than stubbed away: a no-op here would make the teardown
  // test pass against a hook that never cleans up, which is the one thing it exists to catch.
  vi.stubGlobal("cancelAnimationFrame", (handle: number) => {
    queued.delete(handle);
  });
  return () => {
    const due = [...queued.entries()];
    queued.clear();
    for (const [, callback] of due) callback(0);
  };
}

describe("announcing that CareLoop is usable", () => {
  let announcements: number;
  let listener: () => void;

  beforeEach(() => {
    resetApplicationReadyForTests();
    announcements = 0;
    listener = () => {
      announcements += 1;
    };
    window.addEventListener(READY_EVENT, listener);
  });

  afterEach(() => {
    window.removeEventListener(READY_EVENT, listener);
    vi.unstubAllGlobals();
  });

  it("says nothing until the first committed frame has been painted", () => {
    const frame = frameQueue();
    renderHook(() => useApplicationReady());

    // Committed, but nothing has reached the screen yet.
    expect(announcements).toBe(0);
    frame();
    // One frame is the frame the commit's work lands in; it is not yet a painted frame.
    expect(announcements).toBe(0);
    frame();
    expect(announcements).toBe(1);
  });

  it("still announces when StrictMode mounts, tears down and mounts again", () => {
    const frame = frameQueue();
    // What StrictMode does in development, without needing StrictMode to do it: the first
    // instance's frames are cancelled by its own cleanup, so only the second can announce.
    const first = renderHook(() => useApplicationReady());
    first.unmount();
    renderHook(() => useApplicationReady());
    frame();
    frame();

    expect(announcements).toBe(1);
  });

  it("announces the arrival once, not once per mount", () => {
    const frame = frameQueue();
    const first = renderHook(() => useApplicationReady());
    frame();
    frame();
    first.unmount();

    renderHook(() => useApplicationReady());
    frame();
    frame();

    expect(announcements).toBe(1);
  });

  it("says nothing at all if the tree is torn down first", () => {
    const frame = frameQueue();
    const { unmount } = renderHook(() => useApplicationReady());
    unmount();
    frame();
    frame();

    expect(announcements).toBe(0);
  });
});
