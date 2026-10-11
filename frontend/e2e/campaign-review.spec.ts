import { resetDestinationModes } from "./reset-modes";
import { expect, test } from "@playwright/test";

test("campaign generation, targeted revision, stale handling and version history", async ({
  page,
  context,
}, testInfo) => {
  await page.goto("/new");
  await expect(page).toHaveURL(/\/login$/);
  await page.getByRole("link", { name: "Continue with Google" }).click();
  await expect(
    page.getByRole("heading", { name: "Tell me about the event." }),
  ).toBeVisible();
  await resetDestinationModes(page);
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
  await expect(
    page.getByRole("heading", { name: "Choose the copy to prepare." }),
  ).toBeVisible();
  await expect(page.getByLabel("Facebook Groups · manual copy")).toBeChecked();
  await page.getByRole("button", { name: "Create campaign draft" }).click();
  await expect(
    page.getByRole("heading", { name: "Review each voice." }),
  ).toBeVisible();
  await expect(page.getByText("Facebook Groups · manual copy")).toBeVisible();
  await expect(
    page.getByRole("heading", { name: "Campaign graphic" }),
  ).toBeVisible();
  await expect(
    page.getByRole("heading", { name: "Every version is kept." }),
  ).toBeVisible();
  const listingBefore = await page
    .getByRole("heading", { name: "Eventbrite description" })
    .locator("..")
    .textContent();

  await page
    .getByLabel("What would you like to change?")
    .fill("Make LinkedIn warmer.");
  await page.getByRole("button", { name: "Create revised version" }).click();
  await expect(page.getByText(/Version 2 · Current/).first()).toBeVisible();
  await expect(
    page.getByText("We’d love to see you there.").first(),
  ).toBeVisible();
  const listingAfter = await page
    .getByRole("heading", { name: "Eventbrite description" })
    .locator("..")
    .textContent();
  expect(listingAfter).toBe(listingBefore);

  await page.getByRole("button", { name: /Version 1/ }).click();
  await expect(page.getByText(/Version 1 · History/)).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Create revised version" }),
  ).toHaveCount(0);
  await page.getByRole("button", { name: "Return to current version" }).click();
  await page
    .getByLabel("What would you like to change?")
    .fill("Change the date to October 24.");
  await page.getByRole("button", { name: "Create revised version" }).click();
  await expect(page.getByRole("alert")).toContainText(
    "That changes an Event fact.",
  );

  const me = await (await context.request.get("/api/me")).json();
  const eventId = eventPath.split("/").at(-1)!;
  const updated = await page.evaluate(
    async ({ eventId, csrf }) => {
      const current = await fetch(`/api/events/${eventId}`, {
        cache: "no-store",
      }).then((r) => r.json());
      return fetch(`/api/events/${eventId}/facts`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf },
        body: JSON.stringify({
          expected_revision: current.revision,
          changes: [
            { field_path: "title", value: "Lunch & Learn: autumn connection" },
          ],
        }),
      }).then((r) => r.json());
    },
    { eventId, csrf: me.csrf_token },
  );
  expect(updated.title).toBe("Lunch & Learn: autumn connection");
  await page.reload();
  await expect(page.getByText("Event details changed")).toBeVisible();
  await page.getByRole("button", { name: "Create an updated version" }).click();
  await expect(page.getByText(/Version 3 · Current/).first()).toBeVisible();
  await expect(
    page.getByText(/Event details have changed since this campaign/),
  ).toHaveCount(0);

  await page.getByRole("link", { name: "← Event details" }).click();
  await page.getByRole("link", { name: "Events", exact: true }).click();
  await page.locator(`a[href="${eventPath}"]`).click();
  await expect(
    page.getByRole("heading", {
      level: 1,
      name: "Lunch & Learn: autumn connection",
    }),
  ).toBeVisible();
  await page.getByRole("link", { name: "Open campaign review" }).click();
  await expect(page.getByText(/Version 3 · Current/).first()).toBeVisible();
  await expect(page.getByRole("button", { name: /Version 1/ })).toBeVisible();
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth,
    ),
  ).toBe(true);
  await page.screenshot({
    path: testInfo.outputPath("campaign-review.png"),
    fullPage: true,
  });
});
