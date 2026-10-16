import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import version from "../../tests/fixtures/suite/version.json";

describe("test tooling", () => {
  it("renders React through the build plugins with jest-dom matchers", () => {
    render(<p>hello suite</p>);
    expect(screen.getByText("hello suite")).toBeInTheDocument();
  });

  it("imports the real suite fixtures as JSON", () => {
    expect(version.tool_version).toBe("0.4.0");
    expect(version.capabilities).toContain("score_partial");
  });
});
