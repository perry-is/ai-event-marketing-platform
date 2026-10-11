import { resetDestinationModes } from "./reset-modes";
import {
  expect,
  test,
  type BrowserContextOptions,
  type Page,
} from "@playwright/test";

// Exercises the real LinkedIn LIVE adapter against the browser server's stateful
// fake transport. No request from this suite reaches LinkedIn.

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
    fetch("/api/test/fake-linkedin", { cache: "no-store" }).then((r) =>
      r.json(),
    ),
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
    await fetch("/api/test/fake-linkedin/simulated-mode", {
      method: "POST",
      headers: { "X-CSRF-Token": me.csrf_token },
    });
  });
});

async function readyCampaign(page: Page) {
  // OAuth E2E runs before the mobile project and shares the browser-test DB.
  // Reset its persisted fake connection so readiness starts from the same state.
  await testPost(page, "/api/test/fake-oauth/linkedin/reset");
  await testPost(page, "/api/test/fake-linkedin/reset");
  await testPost(page, "/api/test/fake-linkedin/live-mode");
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
  await expect(page.getByText("Will publish live")).toBeVisible();
  return eventId;
}

function card(page: Page, name: string) {
  return page
    .getByTestId("destination-card")
    .filter({ has: page.getByRole("heading", { name, exact: true }) });
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
  await approvalButton.click();
  await page.getByRole("button", { name: "Start distribution" }).click();
  expect(requests).toHaveLength(1);
  const body = JSON.parse(requests[0]) as Record<string, unknown>;
  expect(body).not.toHaveProperty("runtime_mode");
  expect(body).not.toHaveProperty("destination_plan_hash");
}

async function currentPlan(page: Page, eventId: string) {
  return page.evaluate(async (eventId) => {
    const campaign = await fetch(`/api/events/${eventId}/campaign`).then((r) =>
      r.json(),
    );
    const preview = await fetch(
      `/api/campaigns/${campaign.id}/approval-preview`,
    ).then((r) => r.json());
    if (!preview.destination_plan) {
      throw new Error(
        `Approval preview unavailable for campaign ${campaign.id}: ${JSON.stringify(preview)}`,
      );
    }
    return { campaign, preview };
  }, eventId);
}

test("text post is exact, verified, and a copy successor updates the same Post", async () => {
  const eventId = await readyCampaign(page);
  const state = await currentPlan(page, eventId);
  const target = state.preview.destination_plan.targets.find(
    (item: { destination_key: string }) =>
      item.destination_key === "LINKEDIN_ORGANIZATION",
  );
  expect(target.runtime_mode).toBe("LIVE");
  expect(target.linkedin_organization.organization_urn).toBe(
    "urn:li:organization:81234567",
  );
  await approveAndStart(page);

  const linkedin = card(page, "LinkedIn organization");
  await expect(
    linkedin.getByText("Live on LinkedIn organization · verified"),
  ).toBeVisible();
  await expect(
    card(page, "Meetup").getByText("Published (Simulated)"),
  ).toBeVisible();
  await expect(
    card(page, "Instagram").getByText("Published (Simulated)"),
  ).toBeVisible();
  await expect(
    card(page, "Facebook Groups").getByText("Manual action required"),
  ).toBeVisible();
  let fake = await fakeState(page);
  expect(fake.post_creates).toBe(1);
  expect(fake.image_uploads).toBe(0);
  expect(fake.posts[0].commentary).toBe(target.approved_content);
  expect(fake.posts[0].author).toBe("urn:li:organization:81234567");
  expect(fake.posts[0].lifecycleState).toBe("PUBLISHED");
  expect(fake.version_headers).toEqual(["202609"]);
  const postId = fake.posts[0].id;

  await page
    .getByLabel("What would you like to change?")
    .fill("Make LinkedIn warmer.");
  await page.getByRole("button", { name: "Create revised version" }).click();
  await expect(page.getByText(/Version 2 · Current/).first()).toBeVisible();
  await approveAndStart(page);
  await expect(
    linkedin.getByText("Live on LinkedIn organization · verified"),
  ).toBeVisible();
  fake = await fakeState(page);
  expect(fake.post_creates).toBe(1);
  expect(fake.post_updates).toBe(1);
  expect(fake.posts).toHaveLength(1);
  expect(fake.posts[0].id).toBe(postId);
  expect(fake.posts[0].commentary).not.toBe(target.approved_content);
  expect(fake.posts[0].commentary).toContain("Lunch & Learn");
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth,
    ),
  ).toBe(true);
});

test("approved image is exact; graphic successor blocks before LinkedIn mutation", async () => {
  const eventId = await readyCampaign(page);
  await page.getByRole("button", { name: "Generate graphic" }).click();
  await expect(
    page.getByText("Graphic added in Version 2.").first(),
  ).toBeVisible();
  const version = await currentPlan(page, eventId);
  const approvedAsset = version.campaign.current_version.primary_graphic;
  await approveAndStart(page);
  await expect(
    card(page, "LinkedIn organization").getByText(
      "Live on LinkedIn organization · verified",
    ),
  ).toBeVisible();
  let fake = await fakeState(page);
  expect(fake.post_creates).toBe(1);
  expect(fake.image_uploads).toBe(1);
  expect(fake.upload_digests).toEqual([approvedAsset.sha256]);
  expect(fake.posts[0].image_id).toMatch(/^urn:li:image:/);

  await page
    .getByLabel("What would you like to change about the graphic?")
    .fill("Use less orange.");
  await page.getByRole("button", { name: "Revise graphic" }).click();
  await expect(
    page.getByText(/Graphic updated in Version 3\./).first(),
  ).toBeVisible();
  const revised = await currentPlan(page, eventId);
  expect(revised.campaign.current_version.version_number).toBe(3);
  await expect(
    page.getByRole("button", { name: "Approve this version" }),
  ).toBeVisible();
  const createCount = fake.post_creates;
  const uploadCount = fake.image_uploads;
  const beforeWrites = fake.write_paths.length;
  await approveAndStart(page);
  const linkedin = card(page, "LinkedIn organization");
  await expect(linkedin.getByText("Blocked", { exact: true })).toBeVisible();
  await expect(linkedin).toContainText("cannot replace media");
  fake = await fakeState(page);
  expect(fake.post_creates).toBe(createCount);
  expect(fake.image_uploads).toBe(uploadCount);
  expect(fake.write_paths).toHaveLength(beforeWrites);
  expect(fake.posts[0].image_id).toBeTruthy();
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth,
    ),
  ).toBe(true);
});

test("uncertain create stays UNKNOWN and config drift cannot redirect an approved Page", async () => {
  await readyCampaign(page);
  await testPost(page, "/api/test/fake-linkedin/timeout-next-create");
  await approveAndStart(page);
  const linkedin = card(page, "LinkedIn organization");
  await expect(
    linkedin.getByText("Result uncertain — verify before retrying"),
  ).toBeVisible();
  await expect(linkedin.getByRole("button", { name: /Retry/ })).toHaveCount(0);
  let fake = await fakeState(page);
  expect(fake.post_creates).toBe(1);
  expect(fake.posts).toHaveLength(1);
  await linkedin
    .getByRole("button", { name: "Verify destination result" })
    .click();
  await expect(
    linkedin.getByText("Result uncertain — verify before retrying"),
  ).toBeVisible();
  fake = await fakeState(page);
  expect(fake.post_creates).toBe(1);

  // A separate fresh campaign freezes the original Page before configuration drift.
  const eventId = await readyCampaign(page);
  const beforeApproval = await currentPlan(page, eventId);
  const approvalTarget = beforeApproval.preview.destination_plan.targets.find(
    (item: { destination_key: string }) =>
      item.destination_key === "LINKEDIN_ORGANIZATION",
  );
  const approvalResponse = page.waitForResponse(
    (response) =>
      response.url().endsWith("/approve") &&
      response.request().method() === "POST",
  );
  await page.getByRole("button", { name: "Approve this version" }).click();
  expect((await approvalResponse).ok()).toBe(true);
  await expect(
    page.getByRole("button", { name: "Start distribution" }),
  ).toBeVisible();
  await testPost(page, "/api/test/fake-linkedin/other-page");
  await page.getByRole("button", { name: "Start distribution" }).click();
  await expect(
    card(page, "LinkedIn organization").getByText("Blocked", { exact: true }),
  ).toBeVisible();
  fake = await fakeState(page);
  expect(fake.post_creates).toBe(0);
  expect(approvalTarget.linkedin_organization.organization_urn).toBe(
    "urn:li:organization:81234567",
  );
});

test("Settings reports access status without revealing credentials", async () => {
  await testPost(page, "/api/test/fake-linkedin/reset");
  await testPost(page, "/api/test/fake-linkedin/live-mode");
  await page.goto("/settings");
  const section = page.getByRole("region", { name: "LinkedIn organization" });
  await expect(section.getByText("Connected", { exact: true })).toBeVisible();
  await expect(section).toContainText("Event Marketing Page");
  await expect(section).toContainText(
    "Standard Community Management access is configured.",
  );
  const text = await section.innerText();
  expect(text).not.toContain("synthetic-linkedin-access-token");
  expect(text).not.toContain("synthetic-linkedin-client-secret");
});
