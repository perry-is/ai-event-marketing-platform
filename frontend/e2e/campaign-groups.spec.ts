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

async function rankerScenario(page: Page, mode: "NORMAL" | "FAIL") {
  const status = await page.evaluate(async (mode) => {
    const me = await fetch("/api/me", { cache: "no-store" }).then((r) =>
      r.json(),
    );
    return fetch("/api/test/group-ranker-scenario", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-CSRF-Token": me.csrf_token,
      },
      body: JSON.stringify({ mode }),
    }).then((r) => r.status);
  }, mode);
  expect(status).toBe(204);
}

async function readyAuroraCampaign(page: Page) {
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
  await page
    .getByLabel("Venue or location")
    .fill("Aurora Event Center, Aurora, CO");
  await page.getByRole("button", { name: "Save corrections" }).click();
  await expect(page.getByText("Aurora Event Center, Aurora, CO")).toBeVisible();
  await page.getByRole("link", { name: "Open campaign review" }).click();
  await page.getByRole("button", { name: "Create campaign draft" }).click();
  await expect(
    page.getByRole("heading", { name: "Where to share on Facebook" }),
  ).toBeVisible();
  return eventId;
}

async function campaignState(page: Page, eventId: string) {
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

test("recommend, select, approve and post manually to exactly the approved groups", async ({
  page,
}, testInfo) => {
  await signIn(page);
  await rankerScenario(page, "NORMAL");
  const eventId = await readyAuroraCampaign(page);

  // Flow A: recommend and select.
  await page.getByRole("button", { name: "Generate graphic" }).click();
  await expect(
    page.getByText("Graphic added in Version 2.").first(),
  ).toBeVisible();
  const section = page.getByRole("region", {
    name: "Where to share on Facebook",
  });
  await section.getByRole("button", { name: "Suggest groups" }).click();
  const list = section.getByRole("list", {
    name: "Recommended Facebook Groups",
  });
  await expect(list.getByText("Best fit").first()).toBeVisible();
  await expect(
    list.getByText(/Colorado audience matches the event location/).first(),
  ).toBeVisible();
  await expect(section.getByText(/0\.\d{2}/)).toHaveCount(0);
  const boxes = list.getByRole("checkbox");
  const names: string[] = [];
  for (let index = 0; index < 4; index += 1) {
    const label = boxes.nth(index).locator("xpath=ancestor::label");
    names.push((await label.locator("strong").textContent())!.trim());
    await boxes.nth(index).check();
    await expect(
      section.getByText(
        `${index + 1} ${index === 0 ? "group" : "groups"} selected`,
      ),
    ).toBeVisible();
  }
  await boxes.nth(3).uncheck();
  await expect(section.getByText("3 groups selected")).toBeVisible();
  await expect(
    section.getByText(/not saved to the campaign yet/),
  ).toBeVisible();
  await page.reload();
  await expect(section.getByText("3 groups selected")).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Approve this version" }),
  ).toBeDisabled();
  await expect(
    page.getByText(/Facebook Group choices aren’t saved yet/),
  ).toBeVisible();
  await section.screenshot({
    path: testInfo.outputPath("groups-selection.png"),
  });
  await section.getByRole("button", { name: "Save group selection" }).click();
  await expect(
    page.getByText(/Facebook Groups saved in Version 3/),
  ).toBeVisible();
  const saved = (await campaignState(page, eventId)).campaign.current_version;
  expect(
    saved.facebook_group_targets
      .map((g: { display_name: string }) => g.display_name)
      .sort(),
  ).toEqual(names.slice(0, 3).sort());

  // Flow B: approval and the manual package.
  await expect(page.getByText("You post it · 3 groups")).toBeVisible();
  await page.getByRole("button", { name: "Approve this version" }).click();
  await page
    .getByRole("button", { name: "Start simulated distribution" })
    .click();
  const manual = page
    .getByTestId("destination-card")
    .filter({ has: page.getByRole("heading", { name: "Facebook Groups" }) });
  await expect(
    manual.getByText("Share in the 3 approved groups below."),
  ).toBeVisible();
  for (const name of names.slice(0, 3)) {
    await expect(
      manual.getByRole("link", {
        name: `Open group: ${name} on Facebook (opens in a new tab)`,
      }),
    ).toHaveAttribute("href", /^https:\/\/www\.facebook\.com\/groups\//);
  }
  await expect(manual.getByText(names[3], { exact: true })).toHaveCount(0);
  await expect(
    manual.getByRole("img", { name: "Approved graphic for Lunch & Learn" }),
  ).toBeVisible();
  await expect(manual.getByLabel("Approved Facebook Groups copy")).toHaveValue(
    /Lunch & Learn/,
  );
  await manual.screenshot({
    path: testInfo.outputPath("groups-manual-package.png"),
  });
  await manual
    .getByRole("button", { name: "Mark Facebook Groups complete" })
    .click();
  await expect(
    manual.getByText("Marked complete by the operator"),
  ).toBeVisible();

  // Flow C: a fourth group after approval needs a fresh approval-bound version.
  const approvedState = await campaignState(page, eventId);
  const approvalId = approvedState.distribution.active_approval.id;
  await section.getByRole("button", { name: "Change groups" }).click();
  await section.getByRole("button", { name: "Change groups" }).first().click();
  await list.getByRole("checkbox", { name: new RegExp(names[3]) }).check();
  await expect(section.getByText("4 groups selected")).toBeVisible();
  const withdrawn = await campaignState(page, eventId);
  expect(withdrawn.distribution.active_approval).toBeNull();
  const historical = withdrawn.distribution.approvals.find(
    (a: { id: string }) => a.id === approvalId,
  );
  const historicalGroups = historical.destination_plan.targets
    .find(
      (t: { destination_key: string }) =>
        t.destination_key === "FACEBOOK_GROUPS",
    )
    .selected_groups.map((g: { display_name: string }) => g.display_name)
    .sort();
  expect(historicalGroups).toEqual(names.slice(0, 3).sort());
  await section.getByRole("button", { name: "Save group selection" }).click();
  await expect(
    page.getByText(/Facebook Groups saved in Version 4/),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: /Version 4 · Current\s*Needs approval/ }),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: /Version 3\s*Previously approved/ }),
  ).toBeVisible();
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth,
    ),
  ).toBe(true);
});

test("AI ranking failure falls back and eligible groups can still be browsed", async ({
  page,
}) => {
  await signIn(page);
  await readyAuroraCampaign(page);
  await rankerScenario(page, "FAIL");
  try {
    const section = page.getByRole("region", {
      name: "Where to share on Facebook",
    });
    await section.getByRole("button", { name: "Suggest groups" }).click();
    await expect(
      section.getByText(/AI suggestions aren’t available right now/),
    ).toBeVisible();
    await expect(
      section.getByRole("list", { name: "Recommended Facebook Groups" }),
    ).toBeVisible();
    await section.getByText("Browse other eligible groups").click();
    await section.getByLabel("Search by name or state").fill("Denver");
    await section.getByRole("button", { name: "Search" }).click();
    const eligible = section.getByRole("list", {
      name: "Eligible Facebook Groups",
    });
    const denver = eligible.getByRole("checkbox", {
      name: /Denver Business Network/,
    });
    await denver.check();
    await expect(section.getByText(/1 group selected/)).toBeVisible();
    await expect(
      page.getByRole("heading", { name: "Ready to send Version 1 out?" }),
    ).toBeVisible();
  } finally {
    await rankerScenario(page, "NORMAL");
  }
});
