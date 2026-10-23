import { render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { DevicesInfo } from "./DevicesInfo";
import { devicesDoc } from "../suite/testing/fixtures";
import supported from "../compare/supported-devices.json";

describe("DevicesInfo", () => {
  it("shows the fixed supported devices from the real catalogue in release order, read-only", () => {
    const doc = devicesDoc();                                // the real CLI catalogue (alphabetical order)
    doc.devices.push({ ...doc.devices[0], id: "unknown_projector", label: "Unknown projector" });
    render(<DevicesInfo devices={doc} />);
    const cards = screen.getAllByRole("article");
    expect(cards.map((c) => c.getAttribute("data-device"))).toEqual(supported.devices.map((d) => d.id));
    expect(cards.map((c) => c.getAttribute("data-device"))).toEqual(["sony_bravia_8_ii", "vivobook_pro_16x", "galaxy_tab_s9_ultra"]);
    for (const [i, fixed] of supported.devices.entries()) {
      const real = doc.devices.find((d) => d.id === fixed.id)!;
      expect(within(cards[i]).getByRole("heading", { level: 3 })).toHaveTextContent(fixed.label);
      expect(cards[i]).toHaveTextContent(real.label);
      for (const p of real.players) expect(cards[i]).toHaveTextContent(p.label);
    }
    expect(screen.queryByText(/Unknown projector/)).toBeNull();
    const tv = cards[0];
    expect(within(tv).getByText(/Sony USB media player \(built-in\)/).closest("li")).toHaveTextContent("default player");
    expect(tv).toHaveTextContent("official 22");
    expect(tv).toHaveTextContent("owner 0");
    expect(tv).toHaveTextContent("Not yet confirmed by your own tests");
    expect(screen.getByRole("region", { name: "Evidence legend" })).toBeInTheDocument();
    for (const role of ["button", "textbox", "combobox", "checkbox"] as const) expect(screen.queryAllByRole(role)).toHaveLength(0);
    expect(screen.getByText(/The list is fixed in this version/)).toBeInTheDocument();
  });

  it("explains an unavailable catalogue without inventing devices", () => {
    render(<DevicesInfo devices={null} />);
    expect(screen.getByText("The device list is not available. Check that VideoSuite is installed, then reopen this tab.")).toBeInTheDocument();
    expect(screen.queryAllByRole("article")).toHaveLength(0);
  });

  it("renders catalogue text literally", () => {
    const doc = devicesDoc();
    doc.devices[1].players[0].label = "<img src=x onerror=alert(1)>";
    const { container } = render(<DevicesInfo devices={doc} />);
    expect(container.querySelector("img")).toBeNull();
    expect(container.textContent).toContain("<img src=x onerror=alert(1)>");
  });
});
