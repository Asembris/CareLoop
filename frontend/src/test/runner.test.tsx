import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";

/* The runner's own smoke test. It asserts the three things every other test file
   assumes and none of them would report clearly if broken: the `@` alias resolves,
   JSX renders into a DOM, and the jest-dom matchers are registered. A suite whose
   harness is wrong fails in ways that read as product bugs. */
describe("the test runner", () => {
  it("renders JSX into a DOM and has the jest-dom matchers", () => {
    render(<p>rendered</p>);
    expect(screen.getByText("rendered")).toBeInTheDocument();
  });

  it("resolves the @ alias the app's own imports use", async () => {
    const { greeting } = await import("@/lib/tone");
    expect(greeting(new Date(2026, 7, 13, 9))).toBe("Good morning.");
  });
});
