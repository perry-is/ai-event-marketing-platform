import {
  act,
  fireEvent,
  render,
  screen,
  within,
  waitFor,
} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { expect, test, vi } from "vitest";
import App from "./App";

const me = {
  display_name: "Test Member",
  csrf_token: "test-antiforgery-token",
};
const response = (value: unknown, status = 200) =>
  new Response(JSON.stringify(value), { status });
type TestFetch = (url: string, init?: RequestInit) => Promise<Response>;
function mount(fetcher: TestFetch, path = "/events") {
  vi.stubGlobal(
    "fetch",
    vi.fn((url: string, init?: RequestInit) =>
      url === "/api/events"
        ? Promise.resolve(response([]))
        : fetcher(url, init),
    ),
  );
  render(
    <MemoryRouter initialEntries={[path]}>
      <App />
    </MemoryRouter>,
  );
}

test("private route and a browser login flag cannot bypass server rejection", async () => {
  localStorage.setItem("isLoggedIn", "true");
  mount(
    vi.fn(async (url: string) =>
      url === "/api/me"
        ? response({}, 401)
        : response({ login_available: true }),
    ),
  );
  expect(
    await screen.findByRole("link", { name: "Continue with Google" }),
  ).toHaveAttribute("href", "/auth/login");
  expect(screen.queryByRole("navigation")).not.toBeInTheDocument();
  expect(
    screen.queryByRole("heading", { name: "Events" }),
  ).not.toBeInTheDocument();
  localStorage.clear();
});

test("server-authenticated identity sees only shell destinations", async () => {
  mount(vi.fn(async () => response(me)));
  expect(await screen.findByRole("heading", { name: "Events" })).toBeVisible();
  expect(screen.getByRole("navigation")).toBeVisible();
  expect(screen.getByText("Test Member")).toBeVisible();
  for (const name of ["New Event", "Events", "Groups", "Settings"])
    expect(
      within(screen.getByRole("navigation")).getByRole("link", { name }),
    ).toBeVisible();
  expect(screen.queryByText("test-antiforgery-token")).not.toBeInTheDocument();
});

test("logout sends CSRF token and returns to public state", async () => {
  const fetcher = vi.fn(async (url: string) =>
    url === "/api/logout"
      ? new Response(null, { status: 204 })
      : url === "/auth/status"
        ? response({ login_available: true })
        : response(me),
  );
  mount(fetcher);
  await userEvent.click(
    await screen.findByRole("button", { name: "Sign out" }),
  );
  expect(
    await screen.findByRole("link", { name: "Continue with Google" }),
  ).toBeVisible();
  expect(fetcher).toHaveBeenCalledWith(
    "/api/logout",
    expect.objectContaining({
      method: "POST",
      headers: { "X-CSRF-Token": me.csrf_token },
    }),
  );
  expect(screen.queryByRole("navigation")).not.toBeInTheDocument();
});

test("session revocation on focus removes private shell", async () => {
  let authorized = true;
  mount(
    vi.fn(async (url: string) =>
      url === "/auth/status"
        ? response({ login_available: true })
        : response(me, authorized ? 200 : 401),
    ),
  );
  await screen.findByRole("navigation");
  authorized = false;
  await act(async () => {
    fireEvent(window, new Event("focus"));
  });
  expect(
    await screen.findByRole("link", { name: "Continue with Google" }),
  ).toBeVisible();
  expect(screen.queryByRole("navigation")).not.toBeInTheDocument();
});

test("network failure hides private content and permits retry", async () => {
  const fetcher = vi
    .fn()
    .mockRejectedValueOnce(new Error("offline"))
    .mockResolvedValue(response(me));
  mount(fetcher);
  expect(
    await screen.findByRole("heading", { name: "We can’t connect right now." }),
  ).toBeVisible();
  expect(screen.queryByRole("navigation")).not.toBeInTheDocument();
  await userEvent.click(screen.getByRole("button", { name: "Try again" }));
  await waitFor(() => expect(screen.getByRole("navigation")).toBeVisible());
});

test("unconfigured login is honest and displays no bypass", async () => {
  mount(
    vi.fn(async (url: string) =>
      url === "/api/me"
        ? response({}, 401)
        : response({ login_available: false }),
    ),
  );
  expect(await screen.findByText(/Sign-in is not available yet/)).toBeVisible();
  expect(
    screen.queryByRole("link", { name: "Continue with Google" }),
  ).not.toBeInTheDocument();
});

test("failed logout keeps session visible rather than claiming success", async () => {
  mount(
    vi.fn(async (url: string) =>
      url === "/api/logout" ? response({}, 503) : response(me),
    ),
  );
  await userEvent.click(
    await screen.findByRole("button", { name: "Sign out" }),
  );
  expect(await screen.findByRole("alert")).toHaveTextContent(
    "We couldn’t sign you out.",
  );
  expect(screen.getByRole("navigation")).toBeVisible();
});
