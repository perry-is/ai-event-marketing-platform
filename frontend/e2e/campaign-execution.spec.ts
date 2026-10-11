import { resetDestinationModes } from "./reset-modes";
import { expect, test, type Page } from "@playwright/test";

async function signIn(page: Page) {
  await page.goto("/new");
  await expect(page).toHaveURL(/\/login$/);
  await page.getByRole("link", { name: "Continue with Google" }).click();
  await expect(
    page.getByRole("heading", { name: "Tell me about the event." }),
  ).toBeVisible();
  await resetDestinationModes(page);
}

async function configureSimulation(
  page: Page,
  outcomes: Record<string, string[]> = {},
  verifications: Record<string, string[]> = {},
) {
  const status = await page.evaluate(
    async ({ outcomes, verifications }) => {
      const me = await fetch("/api/me", { cache: "no-store" }).then(
        (response) => response.json(),
      );
      return fetch("/api/test/simulation-scenario", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-CSRF-Token": me.csrf_token,
        },
        body: JSON.stringify({ outcomes, verifications }),
      }).then((response) => response.status);
    },
    { outcomes, verifications },
  );
  expect(status).toBe(204);
}

async function createReadyEventAndCampaign(page: Page) {
  await page
    .getByLabel("What do you know so far?")
    .fill(
      "Lunch & Learn on October 22, 2026 from 11:30 AM to 1:00 PM at Example Venue. Timezone America/Denver.",
    );
  await page.getByRole("button", { name: "Create Event" }).click();
  await expect(page).toHaveURL(/\/events\/[0-9a-f-]+$/);
  const eventPath = new URL(page.url()).pathname;
  await page.getByRole("button", { name: "Use these details" }).click();
  await page.getByRole("checkbox", { name: "In person" }).check();
  await page.getByRole("button", { name: "Save format" }).click();
  await expect(
    page.getByText(/essential event details are ready/),
  ).toBeVisible();
  await page.getByRole("link", { name: "Open campaign review" }).click();
  await page.getByRole("button", { name: "Create campaign draft" }).click();
  await expect(
    page.getByRole("heading", { name: "Ready to send Version 1 out?" }),
  ).toBeVisible();
  return eventPath;
}

async function approveCurrentVersion(page: Page) {
  await page.getByRole("button", { name: "Approve this version" }).click();
  await expect(
    page.getByRole("button", { name: "Start simulated distribution" }),
  ).toBeVisible();
}

function destinationCard(page: Page, name: string) {
  return page
    .getByTestId("destination-card")
    .filter({ has: page.getByRole("heading", { name }) });
}

test("happy path approves one exact version, completes a manual action, and preserves history", async ({
  page,
}) => {
  await signIn(page);
  await configureSimulation(page);
  const eventPath = await createReadyEventAndCampaign(page);
  await expect(
    page.getByText(/LinkedIn personal copy remains available/),
  ).toBeVisible();
  await approveCurrentVersion(page);
  await expect(
    page.getByRole("button", {
      name: /Version 1 · Current Approved for distribution/,
    }),
  ).toBeVisible();
  await page
    .getByRole("button", { name: "Start simulated distribution" })
    .click();
  await expect(
    page.getByText("Action required", { exact: true }),
  ).toBeVisible();
  await expect(page.getByText("Published (Simulated)").first()).toBeVisible();
  const manual = destinationCard(page, "Facebook Groups");
  await expect(manual.getByLabel("Approved Facebook Groups copy")).toHaveValue(
    /Lunch & Learn/,
  );
  await expect(manual.getByText(/No group was selected/)).toBeVisible();
  await manual
    .getByRole("button", { name: "Mark Facebook Groups complete" })
    .click();
  await expect(page.getByText("Completed with manual actions")).toBeVisible();
  await expect(page.getByText(/Completion recorded/)).toBeVisible();
  await page.reload();
  await expect(page.getByText("Completed with manual actions")).toBeVisible();
  await expect(page.getByText(/Completion recorded/)).toBeVisible();
  await expect(page.getByText(/Approval history · 1 version/)).toBeVisible();

  await page.getByRole("link", { name: "Events", exact: true }).click();
  await expect(page).toHaveURL(/\/events$/);
  await expect(
    page.getByRole("heading", { name: "Events", exact: true }),
  ).toBeVisible();
  await page.locator(`a[href="${eventPath}"]`).click();
  await page.getByRole("link", { name: "Open campaign review" }).click();
  await expect(page.getByText("Completed with manual actions")).toBeVisible();
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth,
    ),
  ).toBe(true);
});

test("partial failure retries only the failed destination", async ({
  page,
}) => {
  await signIn(page);
  await configureSimulation(page, {
    LINKEDIN_ORGANIZATION: ["FAILED_RETRYABLE", "SUCCESS"],
  });
  await createReadyEventAndCampaign(page);
  await approveCurrentVersion(page);
  await page
    .getByRole("button", { name: "Start simulated distribution" })
    .click();
  const linkedin = destinationCard(page, "LinkedIn organization");
  await expect(
    linkedin.getByText("Needs retry", { exact: true }),
  ).toBeVisible();
  await expect(
    page.getByText("Action required", { exact: true }),
  ).toBeVisible();
  await linkedin
    .getByRole("button", { name: "Retry LinkedIn organization" })
    .click();
  await expect(linkedin.getByText("Published (Simulated)")).toBeVisible();
  await expect(linkedin.getByText(/History · 2 actions/)).toBeVisible();
  for (const name of ["Eventbrite", "Meetup", "Instagram"]) {
    const card = destinationCard(page, name);
    await expect(card.getByText("Published (Simulated)")).toBeVisible();
    await expect(card.getByText(/History · 1 action/)).toBeVisible();
  }
  await expect(
    page.getByRole("button", { name: /Retry Eventbrite/ }),
  ).toHaveCount(0);
});

test("UNKNOWN waits for reconciliation and is never offered as a blind retry", async ({
  page,
}) => {
  await signIn(page);
  await configureSimulation(
    page,
    { INSTAGRAM: ["UNKNOWN"] },
    { INSTAGRAM: ["FOUND_MATCHING"] },
  );
  await createReadyEventAndCampaign(page);
  await approveCurrentVersion(page);
  await page
    .getByRole("button", { name: "Start simulated distribution" })
    .click();
  const instagram = destinationCard(page, "Instagram");
  await expect(
    instagram.getByText("Result uncertain — verify before retrying"),
  ).toBeVisible();
  await expect(
    page.getByText("Unresolved · a destination result is uncertain"),
  ).toBeVisible();
  await expect(instagram.getByRole("button", { name: /Retry/ })).toHaveCount(0);
  await instagram
    .getByRole("button", { name: "Verify destination result" })
    .click();
  await expect(instagram.getByText("Published (Simulated)")).toBeVisible();
  await expect(
    page.getByText("Action required", { exact: true }),
  ).toBeVisible();
  await expect(instagram.getByRole("button", { name: /Retry/ })).toHaveCount(0);
});

test("successor version needs its own approval and material Event changes preserve old approval", async ({
  page,
}) => {
  await signIn(page);
  await configureSimulation(page);
  const eventPath = await createReadyEventAndCampaign(page);
  await approveCurrentVersion(page);
  const eventId = eventPath.split("/").at(-1)!;
  const before = await page.evaluate(async (eventId) => {
    const campaign = await fetch(`/api/events/${eventId}/campaign`).then(
      (response) => response.json(),
    );
    const distribution = await fetch(
      `/api/campaigns/${campaign.id}/distribution`,
    ).then((response) => response.json());
    return { campaign, approvalId: distribution.active_approval.id };
  }, eventId);

  await page
    .getByLabel("What would you like to change?")
    .fill("Make LinkedIn warmer.");
  await page.getByRole("button", { name: "Create revised version" }).click();
  await expect(page.getByText(/Version 2 · Current/).first()).toBeVisible();
  const successor = await page.evaluate(async (eventId) => {
    const campaign = await fetch(`/api/events/${eventId}/campaign`).then(
      (response) => response.json(),
    );
    const distribution = await fetch(
      `/api/campaigns/${campaign.id}/distribution`,
    ).then((response) => response.json());
    const oldStart = await fetch(
      `/api/approvals/${distribution.approvals[0].id}/start`,
      {
        method: "POST",
        headers: {
          "X-CSRF-Token": (
            await fetch("/api/me").then((response) => response.json())
          ).csrf_token,
        },
      },
    );
    return { campaign, distribution, oldStartStatus: oldStart.status };
  }, eventId);
  expect(successor.campaign.current_version_id).not.toBe(
    before.campaign.current_version_id,
  );
  expect(successor.distribution.active_approval).toBeNull();
  expect(successor.distribution.approvals[0].campaign_version_id).toBe(
    before.campaign.current_version_id,
  );
  expect(successor.oldStartStatus).toBe(409);
  await page.getByText(/Approval history/).click();
  await expect(page.getByText("Version 1 · Historical approval")).toBeVisible();
  await approveCurrentVersion(page);
  const approvedSuccessor = await page.evaluate(async (eventId) => {
    const campaign = await fetch(`/api/events/${eventId}/campaign`).then(
      (response) => response.json(),
    );
    const distribution = await fetch(
      `/api/campaigns/${campaign.id}/distribution`,
    ).then((response) => response.json());
    return {
      versionId: campaign.current_version_id,
      approvalVersionId: distribution.active_approval.campaign_version_id,
      approvalId: distribution.active_approval.id,
    };
  }, eventId);
  expect(approvedSuccessor.approvalVersionId).toBe(approvedSuccessor.versionId);

  const changed = await page.evaluate(
    async ({ eventId }) => {
      const current = await fetch(`/api/events/${eventId}`).then((response) =>
        response.json(),
      );
      const me = await fetch("/api/me").then((response) => response.json());
      const response = await fetch(`/api/events/${eventId}/facts`, {
        method: "PATCH",
        headers: {
          "Content-Type": "application/json",
          "X-CSRF-Token": me.csrf_token,
        },
        body: JSON.stringify({
          expected_revision: current.revision,
          changes: [
            { field_path: "title", value: "Lunch & Learn: autumn connection" },
          ],
        }),
      });
      return { status: response.status, title: (await response.json()).title };
    },
    { eventId },
  );
  expect(changed.status).toBe(200);
  await page.reload();
  await expect(page.getByText("Event details changed")).toBeVisible();
  await expect(
    page.getByText(/earlier approval is retained in history/),
  ).toBeVisible();
  const oldVersionStart = await page.evaluate(async (approvalId) => {
    const me = await fetch("/api/me").then((response) => response.json());
    return fetch(`/api/approvals/${approvalId}/start`, {
      method: "POST",
      headers: { "X-CSRF-Token": me.csrf_token },
    }).then((response) => response.status);
  }, approvedSuccessor.approvalId);
  expect(oldVersionStart).toBe(409);
  await page.getByRole("button", { name: "Create an updated version" }).click();
  await expect(page.getByText(/Version 3 · Current/).first()).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Approve this version" }),
  ).toBeVisible();
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth,
    ),
  ).toBe(true);
});
