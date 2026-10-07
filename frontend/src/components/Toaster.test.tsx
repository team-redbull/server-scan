import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { CopyButton } from "@/components/CopyButton";
import { Toaster } from "@/components/Toaster";

describe("Toaster", () => {
  it("shows 'Copied <name>' when a CopyButton succeeds", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", { value: { writeText }, configurable: true });
    const { container } = render(
      <>
        <CopyButton text="srv-9" label="Copy server name" />
        <Toaster />
      </>,
    );

    fireEvent.click(screen.getByRole("button", { name: "Copy server name" }));

    const pill = await screen.findByText("srv-9");
    expect(pill.parentElement).toHaveTextContent("Copied srv-9");
    expect(container.querySelector('[data-open="true"]')).not.toBeNull();
  });
});
