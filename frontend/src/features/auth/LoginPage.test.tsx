import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { LoginPage } from "@/features/auth/LoginPage";

function jsonErrorResponse(status: number, code: string) {
  return Promise.resolve({
    ok: false,
    status,
    json: () =>
      Promise.resolve({
        type: `/problems/${code.toLowerCase()}`,
        title: code,
        status,
        detail: "irrelevant — the page maps by status code",
        instance: "/api/v1/auth/login",
        code,
        request_id: null,
        details: {},
      }),
  });
}

function renderLoginPage() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={queryClient}>
      <LoginPage />
    </QueryClientProvider>,
  );
}

async function submit(username: string, password: string) {
  fireEvent.change(screen.getByLabelText("Username"), { target: { value: username } });
  fireEvent.change(screen.getByLabelText("Password"), { target: { value: password } });
  fireEvent.click(screen.getByRole("button", { name: /log in/i }));
}

describe("LoginPage", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it.each([
    [401, "Wrong username or password."],
    [403, "Your account has no permission for this app."],
    [503, "The directory is unreachable right now — try again."],
  ])("maps a %i response to its message", async (status, message) => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockImplementation(() => jsonErrorResponse(status, "X")),
    );
    renderLoginPage();

    await submit("jdoe", "wrong");

    await waitFor(() => {
      expect(screen.getByRole("alert")).toHaveTextContent(message);
    });
  });

  it("shows the server's own message when the login is throttled (429)", async () => {
    const detail = "Too many failed login attempts. Try again later.";
    vi.stubGlobal(
      "fetch",
      vi.fn().mockImplementation(() =>
        Promise.resolve({
          ok: false,
          status: 429,
          json: () =>
            Promise.resolve({
              type: "/problems/rate-limited",
              title: "Rate Limited",
              status: 429,
              detail,
              instance: "/api/v1/auth/login",
              code: "RATE_LIMITED",
              request_id: null,
              details: { retry_after_seconds: 900 },
            }),
        }),
      ),
    );
    renderLoginPage();

    await submit("jdoe", "wrong");

    await waitFor(() => {
      expect(screen.getByRole("alert")).toHaveTextContent(detail);
    });
  });
});
