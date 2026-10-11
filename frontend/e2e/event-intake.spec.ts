import { expect, test } from "@playwright/test";

test("new Event intake, clarification, persistence and responsive summary", async ({
  page,
  context,
}, testInfo) => {
  await page.goto("/new");
  await expect(page).toHaveURL(/\/login$/);
  expect(
    (
      await context.request.post("/api/events", { data: { text: "anonymous" } })
    ).status(),
  ).toBe(401);

  await page.getByRole("link", { name: "Continue with Google" }).click();
  await expect(
    page.getByRole("heading", { name: "Tell me about the event." }),
  ).toBeVisible();
  await page
    .getByLabel("What do you know so far?")
    .fill(
      "Lunch & Learn on October 22, 2026 from 11:30 AM to 1:00 PM at Example Venue. Timezone America/Denver.",
    );
  await page.getByRole("button", { name: "Create Event" }).click();
  await expect(page).toHaveURL(/\/events\/[0-9a-f-]+$/);
  const eventPath = new URL(page.url()).pathname;
  await expect(
    page.getByRole("heading", { name: "Your new Event" }),
  ).toBeVisible();
  await expect(page.getByText("Lunch & Learn", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "Use these details" }).click();
  await expect(page.getByText("How will people attend?")).toBeVisible();
  await page.getByRole("checkbox", { name: "In person" }).check();
  await page.getByRole("button", { name: "Save format" }).click();
  await expect(
    page.getByText(/essential event details are ready/),
  ).toBeVisible();
  await expect(page.getByText("Example Venue", { exact: true })).toBeVisible();
  await expect(page.getByText(/October 22, 2026/)).toBeVisible();
  await page.getByRole("button", { name: "Suggest with AI" }).click();
  await expect(page.getByText(/Join us for Lunch & Learn/)).toBeVisible();
  await page.getByRole("button", { name: "Use suggestion" }).click();
  await expect(page.getByText("Description · source of truth")).toBeVisible();
  expect((await context.request.get("/api/events")).status()).toBe(200);
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth,
    ),
  ).toBe(true);
  await page.screenshot({
    path: testInfo.outputPath("event-detail.png"),
    fullPage: true,
  });

  await page.getByRole("link", { name: "Events", exact: true }).click();
  const eventLink = page.locator(`a[href="${eventPath}"]`);
  await expect(eventLink).toBeVisible();
  await eventLink.click();
  await expect(page).toHaveURL(eventPath);
  await expect(
    page.getByRole("heading", { name: "Lunch & Learn", level: 1 }),
  ).toBeVisible();
  await expect(
    page.getByText(/essential event details are ready/),
  ).toBeVisible();
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth,
    ),
  ).toBe(true);
});
