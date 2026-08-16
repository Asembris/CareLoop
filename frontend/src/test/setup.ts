/* Vitest setup. Loaded once per test file, before anything under test.
   `jest-dom/vitest` registers the DOM matchers and their types; importing it here
   rather than per-file is what makes `toBeInTheDocument` resolve under `tsc --noEmit`. */
import "@testing-library/jest-dom/vitest";
import { afterEach, beforeEach } from "vitest";
import { cleanup } from "@testing-library/react";
import { forgetSession } from "@/services/realApi";

afterEach(cleanup);

/* The adapter's session is per page, so it is module state — which in a test file is state one
   test can leave lying around for the next. Cleared before each, so every test starts as a fresh
   visitor and none of them depends on the order they run in. */
beforeEach(forgetSession);
