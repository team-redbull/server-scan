import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { McpSection } from "@/features/skill/McpSection";

describe("McpSection", () => {
  it("points the command at this server and asks for a token until one exists", () => {
    render(<McpSection origin="https://scan.example" token={null} />);

    expect(
      screen.getByText(/claude mcp add --transport http server-scan https:\/\/scan\.example\/mcp/),
    ).toBeInTheDocument();
    expect(screen.getByText(/<your token>/)).toBeInTheDocument();
  });

  it("fills in a freshly created token", () => {
    render(<McpSection origin="https://scan.example" token="ssk_abc" />);

    expect(screen.getByText(/Bearer ssk_abc/)).toBeInTheDocument();
  });
});
