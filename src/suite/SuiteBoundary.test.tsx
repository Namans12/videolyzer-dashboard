import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { SuiteBoundary } from "./SuiteBoundary";

afterEach(() => vi.restoreAllMocks());

it("contains render errors and retries a healthy child without exposing error text", () => {
  vi.spyOn(console, "error").mockImplementation(() => {});
  let broken = true;
  function Child() {
    if (broken) throw new Error("private backend details");
    return <p>Healthy suite</p>;
  }
  render(<SuiteBoundary><Child /></SuiteBoundary>);
  expect(screen.getByText("The Suite screen hit an unexpected error. Use the Analyze tab, or reload the page.")).toBeVisible();
  expect(document.body.textContent).not.toContain("private backend details");
  broken = false;
  fireEvent.click(screen.getByRole("button", { name: "Try again" }));
  expect(screen.getByText("Healthy suite")).toBeVisible();
});

it("names the screen given by the optional label", () => {
  vi.spyOn(console, "error").mockImplementation(() => {});
  function Broken(): never { throw new Error("private calibration details"); }
  render(<SuiteBoundary label="The Calibration screen"><Broken /></SuiteBoundary>);
  expect(screen.getByRole("alert")).toHaveTextContent("The Calibration screen hit an unexpected error. Use the Analyze tab, or reload the page.");
  expect(document.body.textContent).not.toContain("private calibration details");
  expect(document.body.textContent).not.toContain("The Suite screen");
});
