import { resetDestinationModes } from "./reset-modes";
import {
  expect,
  test,
  type BrowserContextOptions,
  type Page,
} from "@playwright/test";

// Runs the real Meetup LIVE adapter through a stateful fake GraphQL transport.
// This suite never sends a request to Meetup.

async function signIn(page: Page) {
  await page.goto("/new");
  await expect(page).toHaveURL(/\/login$/);
  await page.getByRole("link", { name: "Continue with Google" }).click();
  await expect(
    page.getByRole("heading", { name: "Tell me about the event." }),
  ).toBeVisible();
  await resetDestinationModes(page);
}

async function testPost(page: Page, path: string) {
  const status = await page.evaluate(async (path) => {
    const me = await fetch("/api/me", { cache: "no-store" }).then((r) =>
      r.json(),
    );
    return fetch(path, {
      method: "POST",
      headers: { "X-CSRF-Token": me.csrf_token },
    }).then((r) => r.status);
  }, path);
  expect(status).toBe(204);
}

async function fakeState(page: Page) {
  return page.evaluate(async () =>
    fetch("/api/test/fake-meetup", { cache: "no-store" }).then((r) => r.json()),
  );
}

test.describe.configure({ mode: "serial" });
let page: Page;

test.beforeAll(async ({ browser }, testInfo) => {
  const options = { ...testInfo.project.use } as BrowserContextOptions &
    Record<string, unknown>;
  delete options.defaultBrowserType;
  const context = await browser.newContext({
    ...options,
    baseURL: `http://localhost:${process.env.EMP_E2E_FRONTEND_PORT ?? "5174"}`,
  });
  page = await context.newPage();
  await signIn(page);
});

test.afterAll(async () => {
  await page.context().close();
});

test.afterEach(async () => {
  await page.evaluate(async () => {
    const me = await fetch("/api/me", { cache: "no-store" }).then((r) =>
      r.json(),
    );
    await fetch("/api/test/meetup-simulated-mode", {
      method: "POST",
      headers: { "X-CSRF-Token": me.csrf_token },
    });
    await fetch("/api/test/eventbrite-simulated-mode", {
      method: "POST",
      headers: { "X-CSRF-Token": me.csrf_token },
    });
  });
});

async function readyLiveCampaign(
  page: Page,
  { eventbrite = false, meetupOnly = true } = {},
) {
  await testPost(page, "/api/test/fake-meetup/reset");
  await testPost(page, "/api/test/meetup-live-mode");
  if (eventbrite) {
    await testPost(page, "/api/test/fake-eventbrite/reset");
    await testPost(page, "/api/test/eventbrite-live-connector");
  }
  await page.goto("/new");
  await page
    .getByLabel("What do you know so far?")
    .fill(
      "Lunch & Learn on October 22, 2026 from 11:30 AM to 1:00 PM at Example Venue. Timezone America/Denver.",
    );
  await page.getByRole("button", { name: "Create Event" }).click();
  await expect(page).toHaveURL(/\/events\/[0-9a-f-]+$/);
  await page.getByRole("button", { name: "Use these details" }).click();
  await page.getByRole("checkbox", { name: "In person" }).check();
  await page.getByRole("button", { name: "Save format" }).click();
  await expect(
    page.getByText(/essential event details are ready/),
  ).toBeVisible();
  await page.getByRole("button", { name: "Edit details" }).click();
  await page.getByLabel("Venue or location").fill("Aurora Event Center");
  await page.getByLabel("Street address").fill("123 Main St, Aurora, CO 80012");
  await page.getByLabel("Admission").selectOption("FREE");
  await page.getByLabel("Capacity (number of attendees)").fill("120");
  await page.getByRole("button", { name: "Save corrections" }).click();
  await expect(page.getByText("120", { exact: true })).toBeVisible();
  await page.getByRole("link", { name: "Open campaign review" }).click();
  if (meetupOnly) {
    for (const label of [
      "Event listing copy",
      "LinkedIn organization",
      "LinkedIn personal",
      "Instagram",
      "Facebook Groups · manual copy",
    ]) {
      await page.getByLabel(label).uncheck();
    }
  }
  await page.getByRole("button", { name: "Create campaign draft" }).click();
  await expect(page.getByText("Will publish live")).toHaveCount(
    eventbrite ? 2 : 1,
  );
}

function card(page: Page, name: string) {
  return page
    .getByTestId("destination-card")
    .filter({ has: page.getByRole("heading", { name, exact: true }) });
}

async function approveAndStart(page: Page) {
  const approveButton = page.getByRole("button", {
    name: "Approve this version",
  });
  const replacementPrompt = page.getByText(
    "Earlier versions already affected these destinations. Confirm that this new version is an intentional replacement.",
  );
  await expect
    .poll(
      async () =>
        (await replacementPrompt.isVisible().catch(() => false)) ||
        (await approveButton.isEnabled()),
    )
    .toBe(true);
  if (await replacementPrompt.isVisible().catch(() => false)) {
    await expect(
      page.getByRole("checkbox", { name: /Version \d/ }).first(),
    ).toBeVisible();
  }
  for (const box of await page
    .getByRole("checkbox", { name: /Version \d/ })
    .all()) {
    await box.check();
  }
  await expect(approveButton).toBeEnabled();
  await approveButton.click();
  const startButton = page.getByRole("button", {
    name: "Start distribution",
  });
  await expect(startButton).toBeVisible();
  await startButton.click();
}

test("Meetup creates a draft, publishes after verification, and updates the same Event", async () => {
  await readyLiveCampaign(page);
  await approveAndStart(page);
  let meetup = card(page, "Meetup");
  await expect(meetup.getByText("Published", { exact: true })).toBeVisible();
  await expect(meetup.getByText("Live on Meetup · verified")).toBeVisible();
  await expect(
    meetup.getByRole("link", { name: "View on Meetup (opens in a new tab)" }),
  ).toHaveAttribute(
    "href",
    /^https:\/\/www\.meetup\.com\/example-business-network\/events\//,
  );
  let state = await fakeState(page);
  expect(state.creates).toBe(1);
  expect(state.events).toHaveLength(1);
  expect(state.events[0].status).toBe("ACTIVE");
  expect(state.photo_calls).toBe(0);
  const created = state.calls.find(
    (call: { operation: string }) => call.operation === "MeetupEventCreate",
  );
  expect(created.variables.input.publishStatus).toBe("DRAFT");
  expect(created.variables.input.duration).toBe("PT1H30M");
  expect(created.variables.input.selfRsvp).toBe(false);

  await page
    .getByLabel("What would you like to change?")
    .fill("Make Meetup warmer.");
  await page.getByRole("button", { name: "Create revised version" }).click();
  await expect(page.getByText(/Version 2 · Current/).first()).toBeVisible();
  await approveAndStart(page);
  meetup = card(page, "Meetup");
  await expect(meetup.getByText("Live on Meetup · verified")).toBeVisible();
  state = await fakeState(page);
  expect(state.creates).toBe(1);
  expect(state.updates).toBeGreaterThan(0);
  expect(state.events).toHaveLength(1);
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth,
    ),
  ).toBe(true);
});

test("an uncertain create is not retried or duplicated", async () => {
  await readyLiveCampaign(page);
  await testPost(page, "/api/test/fake-meetup/timeout-next-create");
  await approveAndStart(page);
  const meetup = card(page, "Meetup");
  await expect(
    meetup.getByText("Result uncertain — verify before retrying"),
  ).toBeVisible();
  await expect(meetup.getByRole("button", { name: /Retry/ })).toHaveCount(0);
  await meetup
    .getByRole("button", { name: "Verify destination result" })
    .click();
  await expect(
    meetup.getByText("Result uncertain — verify before retrying"),
  ).toBeVisible();
  const state = await fakeState(page);
  expect(state.creates).toBe(1);
  expect(state.events).toHaveLength(1);
});

test("an approved graphic blocks Meetup before any mutation", async () => {
  await readyLiveCampaign(page);
  await page.getByRole("button", { name: "Generate graphic" }).click();
  await expect(
    page.getByText("Graphic added in Version 2.").first(),
  ).toBeVisible();
  await expect(
    page.getByText(
      "Meetup graphic upload is not yet verified for live publishing. This approved graphic can’t be left out.",
    ),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Approve this version" }),
  ).toBeDisabled();
  const state = await fakeState(page);
  expect(state.creates).toBe(0);
  expect(state.write_operations).toEqual([]);
  expect(state.photo_calls).toBe(0);
});

test("server mode drift blocks an already approved Meetup LIVE plan", async () => {
  await readyLiveCampaign(page);
  for (const box of await page
    .getByRole("checkbox", { name: /Version \d/ })
    .all()) {
    await box.check();
  }
  await page.getByRole("button", { name: "Approve this version" }).click();
  await testPost(page, "/api/test/meetup-simulated-mode");
  await page.getByRole("button", { name: "Start distribution" }).click();
  const meetup = card(page, "Meetup");
  await expect(meetup.getByText("Blocked", { exact: true })).toBeVisible();
  await expect(meetup.getByText("Live plan blocked")).toBeVisible();
  await expect(meetup.getByText("Published (Simulated)")).toHaveCount(0);
  const state = await fakeState(page);
  expect(state.write_operations).toEqual([]);
});

test("Eventbrite LIVE and Meetup LIVE coexist with simulated and manual destinations", async () => {
  await readyLiveCampaign(page, { eventbrite: true, meetupOnly: false });
  await expect(
    page
      .getByRole("listitem")
      .filter({ hasText: /^Eventbrite/ })
      .getByText("Will publish live"),
  ).toBeVisible();
  await expect(
    page
      .getByRole("listitem")
      .filter({ hasText: /^Meetup/ })
      .getByText("Will publish live"),
  ).toBeVisible();
  await expect(page.getByText("Practice mode").first()).toBeVisible();
  await approveAndStart(page);
  await expect(
    card(page, "Eventbrite").getByText("Live on Eventbrite · verified"),
  ).toBeVisible();
  await expect(
    card(page, "Meetup").getByText("Live on Meetup · verified"),
  ).toBeVisible();
  await expect(
    card(page, "LinkedIn organization").getByText("Published (Simulated)"),
  ).toBeVisible();
  await expect(
    card(page, "Instagram").getByText("Published (Simulated)"),
  ).toBeVisible();
  await expect(
    card(page, "Facebook Groups").getByText("Manual action required"),
  ).toBeVisible();
  const state = await fakeState(page);
  expect(state.creates).toBe(1);
  expect(state.photo_calls).toBe(0);
});
