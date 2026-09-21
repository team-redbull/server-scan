import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { BmcLink } from "@/components/BmcLink";

describe("BmcLink", () => {
  it("opens https://<host> in a new tab", () => {
    render(<BmcLink host="bmc-1.example" serverName="srv-1" />);
    const link = screen.getByRole("link", {
      name: "Open BMC console for srv-1",
    });
    expect(link).toHaveAttribute("href", "https://bmc-1.example");
    expect(link).toHaveAttribute("target", "_blank");
    expect(link).toHaveAttribute("rel", "noopener noreferrer");
    expect(link).not.toHaveTextContent("BMC");
  });

  it("shows the word BMC when labeled", () => {
    render(<BmcLink host="bmc-1.example" serverName="srv-1" labeled />);
    expect(screen.getByRole("link")).toHaveTextContent("BMC");
  });

  it("renders nothing without a host", () => {
    const { container } = render(<BmcLink host={null} serverName="srv-1" />);
    expect(container).toBeEmptyDOMElement();
  });
});
