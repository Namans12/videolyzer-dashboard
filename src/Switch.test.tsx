import { useState } from "react";
import { fireEvent, render, screen } from "@testing-library/react";
import { expect, it, vi } from "vitest";
import Switch from "./Switch";

function Theme() {
  const [light, setLight] = useState(true);
  return <Switch isLightMode={light} setIsLightMode={setLight} />;
}

it("exposes a focusable native theme button with a stable name and dark-mode state", () => {
  render(<Theme />);
  const control = screen.getByRole("button", { name: "Dark mode", pressed: false });
  expect(control).toHaveAttribute("type", "button");
  expect(control).not.toHaveAttribute("tabindex", "-1");
  control.focus();
  expect(control).toHaveFocus();
  // Native buttons translate both Space and Enter activation into click.
  fireEvent.click(control);
  expect(screen.getByRole("button", { name: "Dark mode", pressed: true })).toHaveFocus();
  fireEvent.click(control);
  expect(control).toHaveAttribute("aria-pressed", "false");
});

it("keeps the native control in layout and paints focus with the Plan 3 token", async () => {
  const { readFileSync } = await vi.importActual<{ readFileSync: (path: string, encoding: "utf8") => string }>("node:fs");
  const css = readFileSync("src/theme-toggle.css", "utf8");
  const rule = css.match(/\.theme-switch__checkbox\s*\{([^}]+)\}/)?.[1];
  expect(rule).not.toMatch(/display:\s*none|visibility:\s*hidden/);
  expect(css).toMatch(/\.theme-switch__checkbox:focus-visible\s*\+\s*\.theme-switch__container\s*\{[^}]*outline:\s*2px solid var\(--accent-bright\)/);
});
