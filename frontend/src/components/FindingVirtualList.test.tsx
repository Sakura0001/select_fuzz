import {fireEvent, render, screen} from "@testing-library/react";
import {describe, expect, it} from "vitest";
import {FindingVirtualList} from "./FindingVirtualList";

describe("FindingVirtualList", () => {
  it("exposes every row by scrolling its bounded virtual window", () => {
    const items = Array.from({length: 1000}, (_, index) => ({id: `case-${index}`}));
    render(<FindingVirtualList items={items}/>);
    const region = screen.getByRole("grid", {name: "Findings"});
    expect(region).toHaveAttribute("aria-rowcount", "1000");
    expect(screen.getByText("case-0")).toBeVisible();
    Object.defineProperty(region, "scrollTop", {value: 999 * 48, configurable: true});
    fireEvent.scroll(region);
    expect(screen.getByText("case-999")).toBeVisible();
  });
});
