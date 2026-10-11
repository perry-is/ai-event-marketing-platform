import { resetDestinationModes } from "./reset-modes";
import {
  expect,
  test,
  type BrowserContextOptions,
  type Page,
} from "@playwright/test";

// The real Eventbrite LIVE adapter runs against the test server's in-memory Eventbrite
// double (mocked HTTP transport). No request ever reaches Eventbrite.

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
    fetch("/api/test/fake-eventbrite", { cache: "no-store" }).then((r) =>
      r.json(),
    ),
  );
}

// One sign-in per device project keeps the suite under the production login rate limit.
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
    await fetch("/api/test/eventbrite-simulated-mode", {
      method: "POST",
      headers: { "X-CSRF-Token": me.csrf_token },
    });
  });
});

async function readyLiveCampaign(page: Page) {
  await testPost(page, "/api/test/fake-eventbrite/reset");
  await testPost(page, "/api/test/eventbrite-live-connector");
  await page.goto("/new");
  await page
    .getByLabel("What do you know so far?")
    .fill(
      "Lunch & Learn on October 22, 2026 from 11:30 AM to 1:00 PM at Example Venue. Timezone America/Denver.",
    );
  await page.getByRole("button", { name: "Create Event" }).click();
  await expect(page).toHaveURL(/\/events\/[0-9a-f-]+$/);
  const eventId = new URL(page.url()).pathname.split("/").at(-1)!;
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
  await page.getByRole("button", { name: "Create campaign draft" }).click();
  await expect(page.getByText("Will publish live")).toBeVisible();
  return eventId;
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
  const replacements = page.getByRole("checkbox", { name: /Version \d/ });
  for (const box of await replacements.all()) await box.check();
  await expect(approveButton).toBeEnabled();
  await approveButton.click();
  const startButton = page.getByRole("button", {
    name: "Start distribution",
  });
  await expect(startButton).toBeVisible();
  await startButton.click();
}

test("live create publishes once, then a successor version updates the same listing", async () => {
  const testInfo = test.info();
  await readyLiveCampaign(page);

  // Flow A: live create with simulated and manual siblings.
  await expect(page.getByText("Practice mode").first()).toBeVisible();
  await approveAndStart(page);
  const eventbrite = card(page, "Eventbrite");
  await expect(
    eventbrite.getByText("Published", { exact: true }),
  ).toBeVisible();
  await expect(
    eventbrite.getByText("Live on Eventbrite · verified"),
  ).toBeVisible();
  await expect(
    eventbrite.getByRole("link", {
      name: "View on Eventbrite (opens in a new tab)",
    }),
  ).toHaveAttribute("href", /^https:\/\/www\.eventbrite\.com\/e\//);
  await expect(
    card(page, "Meetup").getByText("Published (Simulated)"),
  ).toBeVisible();
  await expect(
    card(page, "Meetup").getByText("Practice mode", { exact: true }),
  ).toBeVisible();
  await expect(
    card(page, "Facebook Groups").getByText("Manual action required"),
  ).toBeVisible();
  let state = await fakeState(page);
  expect(state.creates).toBe(1);
  expect(state.events[0].status).toBe("live");
  await eventbrite.screenshot({
    path: testInfo.outputPath("eventbrite-live-card.png"),
  });

  // Flow B: approve a successor version; Eventbrite is updated, never re-created.
  await page
    .getByLabel("What would you like to change?")
    .fill("Make Eventbrite shorter.");
  await page.getByRole("button", { name: "Create revised version" }).click();
  await expect(page.getByText(/Version 2 · Current/).first()).toBeVisible();
  await approveAndStart(page);
  await expect(
    card(page, "Eventbrite").getByText("Live on Eventbrite · verified"),
  ).toBeVisible();
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

test("an uncertain create is reconciled, never retried into a duplicate", async () => {
  await readyLiveCampaign(page);
  await testPost(page, "/api/test/fake-eventbrite/timeout-next-create");
  await approveAndStart(page);
  const eventbrite = card(page, "Eventbrite");
  await expect(
    eventbrite.getByText("Result uncertain — verify before retrying"),
  ).toBeVisible();
  await expect(
    eventbrite.getByText(/may have created the event/).first(),
  ).toBeVisible();
  await expect(eventbrite.getByRole("button", { name: /Retry/ })).toHaveCount(
    0,
  );
  await eventbrite
    .getByRole("button", { name: "Verify destination result" })
    .click();
  await expect(
    eventbrite.getByText("Result uncertain — verify before retrying"),
  ).toBeVisible();
  const state = await fakeState(page);
  expect(state.creates).toBe(1);
});

test("Eventbrite receives only the approved version's graphic", async () => {
  const eventId = await readyLiveCampaign(page);
  await page.getByRole("button", { name: "Generate graphic" }).click();
  await expect(
    page.getByText("Graphic added in Version 2.").first(),
  ).toBeVisible();
  await approveAndStart(page);
  await expect(
    card(page, "Eventbrite").getByText("Live on Eventbrite · verified"),
  ).toBeVisible();
  const approvedDigest = await page.evaluate(async (eventId) => {
    const campaign = await fetch(`/api/events/${eventId}/campaign`).then((r) =>
      r.json(),
    );
    return campaign.current_version.primary_graphic.sha256;
  }, eventId);
  let state = await fakeState(page);
  expect(state.upload_digests).toEqual([approvedDigest]);

  // A newer unapproved graphic version reaches Eventbrite only after its own approval.
  await page
    .getByLabel("What would you like to change about the graphic?")
    .fill("Use less orange.");
  await page.getByRole("button", { name: "Revise graphic" }).click();
  await expect(
    page.getByText(/Graphic updated in Version 3\./).first(),
  ).toBeVisible();
  const before = state.call_count;
  state = await fakeState(page);
  expect(state.call_count).toBe(before);
  expect(state.upload_digests).toEqual([approvedDigest]);
});

test("config drift blocks an approved live entry instead of simulating it", async () => {
  await readyLiveCampaign(page);
  const replacements = page.getByRole("checkbox", { name: /Version \d/ });
  for (const box of await replacements.all()) await box.check();
  await page.getByRole("button", { name: "Approve this version" }).click();
  await expect(
    page.getByRole("button", { name: "Start distribution" }),
  ).toBeVisible();
  await testPost(page, "/api/test/eventbrite-simulated-mode");
  await page.getByRole("button", { name: "Start distribution" }).click();
  const eventbrite = card(page, "Eventbrite");
  await expect(eventbrite.getByText("Blocked", { exact: true })).toBeVisible();
  await expect(
    eventbrite.getByText(/wasn’t switched to practice mode/),
  ).toBeVisible();
  await expect(
    eventbrite.getByText("Practice mode", { exact: true }),
  ).toHaveCount(0);
  await expect(
    card(page, "Meetup").getByText("Published (Simulated)"),
  ).toBeVisible();
  const state = await fakeState(page);
  expect(state.creates).toBe(0);
  expect(state.call_count).toBe(0);
});
