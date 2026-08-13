/* Vitest setup. Loaded once per test file, before anything under test.
   `jest-dom/vitest` registers the DOM matchers and their types; importing it here
   rather than per-file is what makes `toBeInTheDocument` resolve under `tsc --noEmit`. */
import "@testing-library/jest-dom/vitest";
import { afterEach } from "vitest";
import { cleanup } from "@testing-library/react";

afterEach(cleanup);
