import { QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { App } from "./App";
import { createQueryClient } from "./queryClient";
import { json, me, mockFetch } from "./test/utils";

afterEach(() => vi.unstubAllGlobals());

const signedOut = json(401, {
  error: { code: "unauthenticated", message: "Sign in to continue.", correlation_id: "c401",
           retryable: false, details: {} },
});

// A session can end while the page is open: idle or absolute expiry, a sign-out elsewhere, or
// the identity provider signing the person out (back-channel logout). Every request then fails
// with 401. The page must say the session ended and offer sign-in, not show error panels while
// the cached session still looks valid.
test("a session that ends while the page is open leads to sign-in", async () => {
  let sessionEnded = false;
  mockFetch((url) => {
    if (url.includes("/api/v1/me")) return sessionEnded ? signedOut.clone() : json(200, me);
    sessionEnded = true; // the case list is the first request after the session ended
    return signedOut.clone();
  });
  render(
    <QueryClientProvider client={createQueryClient()}>
      <MemoryRouter initialEntries={["/t/demo-furniture/cases"]}>
        <App />
      </MemoryRouter>
    </QueryClientProvider>,
  );
  expect(await screen.findByText("Your session has ended. Sign in again to continue.")).toBeInTheDocument();
  expect(screen.getByRole("link", { name: "Sign in with your organization" })).toBeInTheDocument();
});

test("someone never signed in sees sign-in without being told a session ended", async () => {
  const fetchMock = mockFetch(() => signedOut.clone());
  render(
    <QueryClientProvider client={createQueryClient()}>
      <MemoryRouter initialEntries={["/"]}>
        <App />
      </MemoryRouter>
    </QueryClientProvider>,
  );
  expect(await screen.findByRole("link", { name: "Sign in with your organization" })).toBeInTheDocument();
  expect(screen.queryByText(/Your session has ended/)).not.toBeInTheDocument();
  // The session check's own 401 does not ask for the session again (no refetch loop).
  await new Promise((resolve) => setTimeout(resolve, 200));
  expect(fetchMock.mock.calls.filter(([url]) => String(url).includes("/api/v1/me"))).toHaveLength(1);
});
