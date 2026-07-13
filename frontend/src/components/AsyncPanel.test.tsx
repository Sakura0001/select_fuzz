import {render, screen} from "@testing-library/react";
import {describe, expect, it, vi} from "vitest";
import {AsyncPanel} from "./AsyncPanel";

describe("AsyncPanel", () => {
  it.each(["loading", "empty", "data", "stale", "error"] as const)("renders %s locally", (state) => {
    render(<AsyncPanel state={state} onRetry={vi.fn()}>{state === "data" ? "rows" : null}</AsyncPanel>);
    expect(screen.getByTestId(`panel-${state}`)).toBeInTheDocument();
  });
});
