import { resetDestinationModes } from "./reset-modes";
import {
  expect,
  test,
  type BrowserContextOptions,
  type Page,
} from "@playwright/test";

// Uses the real Instagram LIVE adapter over an in-memory fake Graph API. The fake
// fetches the app's signed media URL through the public media endpoint; no Meta call
// is made by this suite.

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
    fetch("/api/test/fake-instagram", { cache: "no-store" }).then((r) =>
      r.json(),
    ),
  );
}

async function readyCampaign(page: Page) {
  // The desktop OAuth E2E stores a synthetic token in the shared browser-test
  // database. Keep live-adapter scenarios on their dedicated fake credential
  // provider so project order cannot change readiness behavior.
  await testPost(page, "/api/test/fake-oauth/instagram/reset");
  await testPost(page, "/api/test/fake-instagram/reset");
  await testPost(page, "/api/test/fake-instagram/live-mode");
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
  await page.getByRole("link", { name: "Open campaign review" }).click();
  await page.getByRole("button", { name: "Create campaign draft" }).click();
  return eventId;
}

function instagramCard(page: Page) {
  return page.getByTestId("destination-card").filter({
    has: page.getByRole("heading", { name: "Instagram", exact: true }),
  });
}

async function approveAndStart(page: Page) {
  const approvalButton = page.getByRole("button", {
    name: "Approve this version",
  });
  const replacementPrompt = page.getByText(
    "Earlier versions already affected these destinations. Confirm that this new version is an intentional replacement.",
  );
  await expect
    .poll(
      async () =>
        (await replacementPrompt.isVisible().catch(() => false)) ||
        (await approvalButton.isEnabled()),
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
  await expect(approvalButton).toBeEnabled();
  const requests: string[] = [];
  page.on("request", (request) => {
    if (request.url().endsWith("/approve") && request.method() === "POST") {
      requests.push(request.postData() ?? "{}");
    }
  });
  await page.getByRole("button", { name: "Approve this version" }).click();
  await page.getByRole("button", { name: "Start distribution" }).click();
  expect(requests).toHaveLength(1);
  const body = JSON.parse(requests[0]) as Record<string, unknown>;
  expect(body).not.toHaveProperty("runtime_mode");
  expect(body).not.toHaveProperty("instagram_account_id");
}

async function approvedInstagramTarget(page: Page, eventId: string) {
  return page.evaluate(async (eventId) => {
    const campaign = await fetch(`/api/events/${eventId}/campaign`).then((r) =>
      r.json(),
    );
    const preview = await fetch(
      `/api/campaigns/${campaign.id}/approval-preview`,
    ).then((r) => r.json());
    const target = preview.destination_plan.targets.find(
      (item: { destination_key: string }) =>
        item.destination_key === "INSTAGRAM",
    );
    return { campaign, preview, target };
  }, eventId);
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
  if (
    !page
      .url()
      .startsWith(
        `http://localhost:${process.env.EMP_E2E_FRONTEND_PORT ?? "5174"}`,
      )
  )
    return;
  await page.evaluate(async () => {
    const me = await fetch("/api/me", { cache: "no-store" }).then((r) =>
      r.json(),
    );
    await fetch("/api/test/fake-instagram/simulated-mode", {
      method: "POST",
      headers: { "X-CSRF-Token": me.csrf_token },
    });
  });
});

test("approved graphic is fetched, published once, verified, and successor is blocked", async () => {
  const eventId = await readyCampaign(page);
  await page.getByRole("button", { name: "Generate graphic" }).click();
  await expect(
    page.getByText("Graphic added in Version 2.").first(),
  ).toBeVisible();
  const { campaign, target } = await approvedInstagramTarget(page, eventId);
  expect(target.runtime_mode).toBe("LIVE");
  expect(target.instagram_account.account_id).toBe("17841400000000001");
  expect(target.approved_graphic.asset_id).toBe(
    campaign.current_version.primary_graphic.asset_id,
  );
  const caption = target.approved_content as string;

  await approveAndStart(page);
  const instagram = instagramCard(page);
  await expect(
    instagram.getByText("Live on Instagram · verified"),
  ).toBeVisible();
  await expect(
    instagram.getByRole("link", { name: "View on Instagram" }),
  ).toHaveAttribute("href", /^https:\/\/www\.instagram\.com\/p\//);

  let fake = await fakeState(page);
  expect(fake.container_creates).toBe(1);
  expect(fake.publish_calls).toBe(1);
  expect(fake.containers[0].caption).toBe(caption);
  expect(fake.media[0].caption).toBe(caption);
  expect(fake.fetched_image_digests).toHaveLength(1);
  expect(fake.fetched_image_digests[0]).toMatch(/^[0-9a-f]{64}$/);
  expect(fake.write_operations).toEqual([
    "/v26.0/17841400000000001/media",
    "/v26.0/17841400000000001/media_publish",
  ]);

  await page
    .getByLabel("What would you like to change?")
    .fill("Make Instagram warmer.");
  await page.getByRole("button", { name: "Create revised version" }).click();
  await expect(page.getByText(/Version 3 · Current/).first()).toBeVisible();
  await expect(page.getByText("Cannot publish automatically")).toBeVisible();
  await expect(
    page.getByText(
      "This Event already has a published Instagram post. Automatic replacement isn’t supported.",
    ),
  ).toBeVisible();
  await approveAndStart(page);
  await expect(instagram.getByText("Blocked", { exact: true })).toBeVisible();
  await expect(instagram).toContainText(
    "already has a published Instagram post",
  );
  await expect(
    instagram.getByRole("button", { name: "Account for this" }),
  ).toBeVisible();
  await instagram.getByRole("button", { name: "Account for this" }).click();
  await expect(instagram.getByText("Blocked · accounted for")).toBeVisible();
  await page
    .getByRole("button", { name: "Mark Facebook Groups complete" })
    .click();
  await expect(
    page.getByText("Completed with acknowledged failures"),
  ).toBeVisible();
  fake = await fakeState(page);
  expect(fake.container_creates).toBe(1);
  expect(fake.publish_calls).toBe(1);
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth,
    ),
  ).toBe(true);
});

test("uncertain publish is not retried and remains safely unresolved", async () => {
  await readyCampaign(page);
  await page.getByRole("button", { name: "Generate graphic" }).click();
  await expect(
    page.getByText("Graphic added in Version 2.").first(),
  ).toBeVisible();
  await testPost(page, "/api/test/fake-instagram/timeout-next-publish");
  await approveAndStart(page);
  const instagram = instagramCard(page);
  await expect(
    instagram.getByText("Result uncertain — verify before retrying"),
  ).toBeVisible();
  await expect(instagram.getByRole("button", { name: /Retry/ })).toHaveCount(0);
  await instagram
    .getByRole("button", { name: "Verify destination result" })
    .click();
  await expect(
    instagram.getByText("Result uncertain — verify before retrying"),
  ).toBeVisible();
  const fake = await fakeState(page);
  expect(fake.container_creates).toBe(1);
  expect(fake.publish_calls).toBe(1);
});

test("approved Instagram account cannot drift to a different server target", async () => {
  await readyCampaign(page);
  await page.getByRole("button", { name: "Generate graphic" }).click();
  await expect(
    page.getByText("Graphic added in Version 2.").first(),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Approve this version" }),
  ).toBeEnabled();
  await page.getByRole("button", { name: "Approve this version" }).click();
  await expect(
    page.getByRole("button", { name: "Start distribution" }),
  ).toBeVisible();
  await testPost(page, "/api/test/fake-instagram/other-account");
  await page.getByRole("button", { name: "Start distribution" }).click();
  const instagram = instagramCard(page);
  await expect(instagram.getByText("Blocked", { exact: true })).toBeVisible();
  const fake = await fakeState(page);
  expect(fake.container_creates).toBe(0);
  expect(fake.publish_calls).toBe(0);
});
