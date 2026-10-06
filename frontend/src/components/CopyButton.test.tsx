import { act, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { CopyButton } from "@/components/CopyButton";

describe("CopyButton", () => {
  const writeText = vi.fn<(text: string) => Promise<void>>();

  beforeEach(() => {
    vi.useFakeTimers();
    writeText.mockReset().mockResolvedValue();
    Object.defineProperty(navigator, "clipboard", {
      value: { writeText },
      configurable: true,
    });
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("copies the text and announces it, then resets", async () => {
    render(<CopyButton text="srv-1" label="Copy server name" />);
    fireEvent.click(screen.getByRole("button", { name: "Copy server name" }));
    await act(async () => {
      await Promise.resolve();
    });
    expect(writeText).toHaveBeenCalledWith("srv-1");
    expect(screen.getByRole("status")).toHaveTextContent("Copied");

    act(() => {
      vi.advanceTimersByTime(2000);
    });
    expect(screen.getByRole("status")).toBeEmptyDOMElement();
  });

  it("reports a failure when the clipboard is refused and no fallback works", async () => {
    writeText.mockRejectedValue(new Error("denied"));
    document.execCommand = vi.fn(() => false);
    render(<CopyButton text="srv-1" label="Copy server name" />);
    fireEvent.click(screen.getByRole("button"));
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(screen.getByRole("status")).toHaveTextContent("Copy failed");
  });

  it("keeps the click from reaching a clickable row", async () => {
    const onRowClick = vi.fn();
    render(
      <div onClick={onRowClick}>
        <CopyButton
          text="srv-1"
          label="Copy name of srv-1"
          revealOnRowHover
          compact
        />
      </div>,
    );
    fireEvent.click(screen.getByRole("button", { name: "Copy name of srv-1" }));
    await act(async () => {
      await Promise.resolve();
    });
    expect(onRowClick).not.toHaveBeenCalled();
    expect(writeText).toHaveBeenCalledWith("srv-1");
  });
});
