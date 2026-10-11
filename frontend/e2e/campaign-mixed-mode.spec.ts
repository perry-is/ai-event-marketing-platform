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

async function configureTestMode(page: Page, mode: "live" | "simulated") {
  const status = await page.evaluate(async (mode) => {
    const me = await fetch("/api/me", { cache: "no-store" }).then((response) =>
      response.json(),
    );
    return fetch(`/api/test/eventbrite-${mode}-mode`, {
      method: "POST",
      headers: { "X-CSRF-Token": me.csrf_token },
    }).then((response) => response.status);
  }, mode);
  expect(status).toBe(204);
}

test.afterEach(async ({ page }) => {
  if (
    !page
      .url()
      .startsWith(
        `http://localhost:${process.env.EMP_E2E_FRONTEND_PORT ?? "5174"}`,
      )
  )
    return;
  await page.evaluate(async () => {
    const response = await fetch("/api/me", { cache: "no-store" });
    if (!response.ok) return;
    const me = await response.json();
    await fetch("/api/test/eventbrite-simulated-mode", {
      method: "POST",
      headers: { "X-CSRF-Token": me.csrf_token },
    });
  });
});

async function createReadyCampaign(page: Page, title: string) {
  await page.goto("/new");
  await page
    .getByLabel("What do you know so far?")
    .fill(
      `${title} on October 22, 2026 from 11:30 AM to 1:00 PM at Example Venue. Timezone America/Denver.`,
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
  await expect(
    page.getByRole("heading", { name: "Ready to send Version 1 out?" }),
  ).toBeVisible();
  return eventId;
}

async function approve(page: Page, startButtonName = "Start distribution") {
  const approvalRequests: string[] = [];
  page.on("request", (request) => {
    if (request.url().endsWith("/approve") && request.method() === "POST") {
      approvalRequests.push(request.postData() ?? "{}");
    }
  });
  await page.getByRole("button", { name: "Approve this version" }).click();
  await expect(
    page.getByRole("button", { name: startButtonName }),
  ).toBeVisible();
  expect(approvalRequests).toHaveLength(1);
  const body = JSON.parse(approvalRequests[0]) as Record<string, unknown>;
  expect(body).not.toHaveProperty("runtime_mode");
  expect(body).not.toHaveProperty("destination_plan_hash");
}

async function planState(page: Page, eventId: string) {
  return page.evaluate(async (eventId) => {
    const campaign = await fetch(`/api/events/${eventId}/campaign`).then((r) =>
      r.json(),
    );
    const distribution = await fetch(
      `/api/campaigns/${campaign.id}/distribution`,
    ).then((r) => r.json());
    return { campaign, distribution };
  }, eventId);
}

test("mixed approval freezes per-destination modes and honors later config drift", async ({
  page,
}) => {
  await signIn(page);

  // Flow A: the browser previews and approves the server-derived mixed plan.
  await configureTestMode(page, "live");
  const firstEventId = await createReadyCampaign(
    page,
    "Mixed-mode Lunch & Learn",
  );
  await expect(page.getByText("Will publish live")).toBeVisible();
  await expect(page.getByText("Practice mode")).toHaveCount(3);
  await expect(
    page.getByText("You post it · groups chosen in Facebook"),
  ).toBeVisible();
  await approve(page);

  const approved = await planState(page, firstEventId);
  const approvedPlan = approved.distribution.active_approval.destination_plan;
  const approvedModes = Object.fromEntries(
    approvedPlan.targets.map(
      (target: {
        destination_key: string;
        execution_kind: string;
        runtime_mode?: string;
      }) => [
        target.destination_key,
        target.runtime_mode ?? target.execution_kind,
      ],
    ),
  );
  expect(approvedModes).toEqual({
    EVENTBRITE: "LIVE",
    MEETUP: "SIMULATED",
    LINKEDIN_ORGANIZATION: "SIMULATED",
    INSTAGRAM: "SIMULATED",
    FACEBOOK_GROUPS: "MANUAL",
  });

  await page.getByRole("button", { name: "Start distribution" }).click();
  await expect(page.getByText("Published", { exact: true })).toBeVisible();
  await expect(
    page.getByText("Mocked LIVE path · no network call"),
  ).toBeVisible();
  await expect(page.getByText("Published (Simulated)").first()).toBeVisible();
  await expect(page.getByText("Manual action required")).toBeVisible();
  const executed = await planState(page, firstEventId);
  const outcomes = Object.fromEntries(
    executed.distribution.executions.map(
      (execution: {
        destination_key: string;
        runtime_mode: string | null;
        status: string;
        verification_status: string;
      }) => [execution.destination_key, execution],
    ),
  );
  expect(outcomes.EVENTBRITE.runtime_mode).toBe("LIVE");
  expect(outcomes.EVENTBRITE.verification_status).toBe("TEST_MOCKED");
  expect(outcomes.MEETUP.runtime_mode).toBe("SIMULATED");
  expect(outcomes.FACEBOOK_GROUPS.runtime_mode).toBeNull();
  expect(outcomes.FACEBOOK_GROUPS.status).toBe("MANUAL_PENDING");

  // Flow B: a second LIVE approval is blocked after server configuration changes.
  await configureTestMode(page, "live");
  const secondEventId = await createReadyCampaign(
    page,
    "Configuration-drift Mixer",
  );
  await approve(page);
  await configureTestMode(page, "simulated");
  await page.getByRole("button", { name: "Start distribution" }).click();
  // Wait for the start request to finish before reading persisted execution state.
  await expect(
    page
      .getByText(
        "The approved destination mode is unavailable. No alternate mode was used.",
      )
      .first(),
  ).toBeVisible();
  const drifted = await planState(page, secondEventId);
  const eventbrite = drifted.distribution.executions.find(
    (execution: { destination_key: string }) =>
      execution.destination_key === "EVENTBRITE",
  );
  expect(eventbrite.runtime_mode).toBe("LIVE");
  expect(eventbrite.status).toBe("BLOCKED");
  expect(eventbrite.attempts).toHaveLength(0);
  await expect(
    page
      .getByText(
        "The approved destination mode is unavailable. No alternate mode was used.",
      )
      .first(),
  ).toBeVisible();

  // Flow C: an existing all-SIMULATED approval stays simulated after LIVE config appears.
  await configureTestMode(page, "simulated");
  const thirdEventId = await createReadyCampaign(
    page,
    "Historical practice event",
  );
  await expect(page.getByText("Practice mode").first()).toBeVisible();
  await approve(page, "Start simulated distribution");
  const historical = await planState(page, thirdEventId);
  expect(
    historical.distribution.active_approval.destination_plan.targets
      .filter(
        (target: { execution_kind: string }) =>
          target.execution_kind === "AUTO",
      )
      .every(
        (target: { runtime_mode: string }) =>
          target.runtime_mode === "SIMULATED",
      ),
  ).toBe(true);
  await configureTestMode(page, "live");
  await page
    .getByRole("button", { name: "Start simulated distribution" })
    .click();
  const afterConfigChange = await planState(page, thirdEventId);
  expect(
    afterConfigChange.distribution.executions
      .filter(
        (execution: { execution_kind: string }) =>
          execution.execution_kind === "AUTO",
      )
      .every(
        (execution: { runtime_mode: string }) =>
          execution.runtime_mode === "SIMULATED",
      ),
  ).toBe(true);
  await expect(page.getByText("Published (Simulated)").first()).toBeVisible();
});
