import { resetDestinationModes } from "./reset-modes";
import { expect, test } from "@playwright/test";

test("graphic is reviewed, approved and executed by exact CampaignVersion", async ({
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
  const eventId = eventPath.split("/").at(-1)!;
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

  const graphicSection = page.getByRole("region", { name: "Campaign graphic" });
  await expect(
    graphicSection.getByText("This campaign doesn’t have a graphic yet."),
  ).toBeVisible();
  await graphicSection.screenshot({
    path: testInfo.outputPath("graphic-empty.png"),
  });
  await page.getByRole("button", { name: "Generate graphic" }).click();
  await expect(
    page.getByText("Graphic added in Version 2.").first(),
  ).toBeVisible();
  const graphic = page.getByRole("img", {
    name: "Campaign graphic for Lunch & Learn",
  });
  await expect(graphic).toBeVisible();
  await expect(graphic).toHaveJSProperty("complete", true);
  const graphicUrl = await graphic.getAttribute("src");
  expect(graphicUrl).toMatch(/\/api\/campaign-versions\/[0-9a-f-]+\/graphic$/);

  const beforeApproval = await page.evaluate(async (eventId) => {
    const campaign = await fetch(`/api/events/${eventId}/campaign`).then((r) =>
      r.json(),
    );
    const preview = await fetch(
      `/api/campaigns/${campaign.id}/approval-preview`,
    ).then((r) => r.json());
    return { campaign, preview };
  }, eventId);
  expect(beforeApproval.campaign.current_version_number).toBe(2);
  expect(
    beforeApproval.campaign.current_version.primary_graphic.asset_id,
  ).toBeTruthy();
  expect(
    beforeApproval.preview.destination_plan.targets.every(
      (target: { approved_graphic?: { asset_id: string } }) =>
        target.approved_graphic?.asset_id ===
        beforeApproval.campaign.current_version.primary_graphic.asset_id,
    ),
  ).toBe(true);

  await expect(
    page.getByRole("img", { name: "Graphic included in Version 2" }),
  ).toBeVisible();
  await graphicSection.screenshot({
    path: testInfo.outputPath("graphic-generated.png"),
  });
  await page
    .locator("section", {
      has: page.getByRole("heading", { name: "Ready to send Version 2 out?" }),
    })
    .screenshot({ path: testInfo.outputPath("graphic-approval.png") });
  await page.getByRole("button", { name: "Approve this version" }).click();
  await expect(
    graphicSection.getByText("Included in approved Version 2"),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Start simulated distribution" }),
  ).toBeVisible();
  await page
    .getByRole("button", { name: "Start simulated distribution" })
    .click();
  const manual = page
    .getByTestId("destination-card")
    .filter({ has: page.getByRole("heading", { name: "Facebook Groups" }) });
  await expect(
    manual.getByRole("img", { name: "Approved graphic for Lunch & Learn" }),
  ).toBeVisible();
  await expect(
    manual.getByRole("link", { name: "Download graphic" }),
  ).toBeVisible();
  await manual.screenshot({
    path: testInfo.outputPath("graphic-manual-package.png"),
  });
  const afterExecution = await page.evaluate(async (eventId) => {
    const campaign = await fetch(`/api/events/${eventId}/campaign`).then((r) =>
      r.json(),
    );
    return fetch(`/api/campaigns/${campaign.id}/distribution`).then((r) =>
      r.json(),
    );
  }, eventId);
  const manualRecord = afterExecution.executions.find(
    (row: { destination_key: string }) =>
      row.destination_key === "FACEBOOK_GROUPS",
  );
  expect(manualRecord.campaign_version_id).toBe(
    beforeApproval.campaign.current_version.id,
  );
  expect(manualRecord.manual_package.graphic_asset_id).toBe(
    beforeApproval.campaign.current_version.primary_graphic.asset_id,
  );

  await page
    .getByLabel("What would you like to change?")
    .fill("Make LinkedIn warmer.");
  await page.getByRole("button", { name: "Create revised version" }).click();
  await expect(
    page.getByRole("heading", { name: "Ready to send Version 3 out?" }),
  ).toBeVisible();
  const afterCopyRevision = await page.evaluate(async (eventId) => {
    const campaign = await fetch(`/api/events/${eventId}/campaign`).then((r) =>
      r.json(),
    );
    const distribution = await fetch(
      `/api/campaigns/${campaign.id}/distribution`,
    ).then((r) => r.json());
    return { campaign, distribution };
  }, eventId);
  const copiedGraphicVersion = afterCopyRevision.campaign.current_version;
  expect(copiedGraphicVersion.version_number).toBe(3);
  expect(copiedGraphicVersion.primary_graphic.asset_id).toBe(
    beforeApproval.campaign.current_version.primary_graphic.asset_id,
  );
  expect(copiedGraphicVersion.primary_graphic.sha256).toBe(
    beforeApproval.campaign.current_version.primary_graphic.sha256,
  );
  expect(copiedGraphicVersion.content_hash).not.toBe(
    beforeApproval.campaign.current_version.content_hash,
  );
  expect(afterCopyRevision.distribution.active_approval).toBeNull();
  expect(afterCopyRevision.distribution.approvals[0].campaign_version_id).toBe(
    beforeApproval.campaign.current_version.id,
  );
  await expect(
    page.getByRole("button", { name: "Approve this version" }),
  ).toBeVisible();
  const copiedGraphicPreview = page.getByRole("img", {
    name: "Campaign graphic for Lunch & Learn",
  });
  await expect(copiedGraphicPreview).toBeVisible();
  await expect(copiedGraphicPreview).toHaveAttribute(
    "src",
    `/api/campaign-versions/${copiedGraphicVersion.id}/graphic`,
  );

  await page
    .getByLabel("What would you like to change about the graphic?")
    .fill("Use less orange and make the background feel more professional.");
  await page.getByRole("button", { name: "Revise graphic" }).click();
  await expect(
    page.getByText("Graphic updated in Version 4.").first(),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: /Version 4 · Current Needs approval/ }),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: /Version 2 Previously approved/ }),
  ).toBeVisible();
  await page
    .getByRole("region", { name: "Every version is kept." })
    .screenshot({ path: testInfo.outputPath("graphic-history.png") });
  const afterRevision = await page.evaluate(async (eventId) => {
    const campaign = await fetch(`/api/events/${eventId}/campaign`).then((r) =>
      r.json(),
    );
    const distribution = await fetch(
      `/api/campaigns/${campaign.id}/distribution`,
    ).then((r) => r.json());
    return { campaign, distribution };
  }, eventId);
  const v2 = beforeApproval.campaign.current_version;
  const v3 = afterCopyRevision.campaign.current_version;
  const v4 = afterRevision.campaign.current_version;
  expect(v4.version_number).toBe(4);
  expect(v4.primary_graphic.asset_id).not.toBe(v3.primary_graphic.asset_id);
  expect(v4.content_hash).not.toBe(v3.content_hash);
  expect(afterRevision.distribution.active_approval).toBeNull();
  expect(afterRevision.distribution.approvals[0].campaign_version_id).toBe(
    v2.id,
  );
  expect(
    afterRevision.distribution.executions.find(
      (row: { destination_key: string }) =>
        row.destination_key === "FACEBOOK_GROUPS",
    ).manual_package.graphic_asset_id,
  ).toBe(v2.primary_graphic.asset_id);

  await page.getByRole("button", { name: /Version 2/ }).click();
  await expect(page.getByText("Version 2 · History").first()).toBeVisible();
  const historicalImage = page
    .getByRole("img", {
      name: "Campaign graphic for Lunch & Learn",
    })
    .first();
  await expect(historicalImage).toHaveAttribute("src", graphicUrl!);
  await expect(
    graphicSection.getByText("Saved with Version 2 · read-only"),
  ).toBeVisible();
  await graphicSection.screenshot({
    path: testInfo.outputPath("graphic-historical.png"),
  });
  await page.getByRole("button", { name: "Return to current version" }).click();

  await page
    .getByLabel("What would you like to change about the graphic?")
    .fill("Change the date on the graphic to October 24.");
  await page.getByRole("button", { name: "Revise graphic" }).click();
  await expect(page.getByRole("alert")).toContainText(
    "That changes the event details, not just the graphic.",
  );
  await expect(
    page.getByRole("alert").getByRole("link", { name: "Update Event details" }),
  ).toBeVisible();
  expect(
    await page.evaluate(async (eventId) => {
      const campaign = await fetch(`/api/events/${eventId}/campaign`).then(
        (r) => r.json(),
      );
      return campaign.current_version_number;
    }, eventId),
  ).toBe(4);

  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth,
    ),
  ).toBe(true);
  await graphicSection.screenshot({
    path: testInfo.outputPath("graphic-fact-change.png"),
  });
  if (testInfo.project.name === "mobile") {
    await page.screenshot({
      path: testInfo.outputPath("campaign-graphic-mobile.png"),
      fullPage: true,
    });
  }
  // The endpoint is private: the already-authenticated context can load it, but anonymous cannot.
  const anonymousPage = await context.browser()?.newPage();
  if (anonymousPage) {
    await anonymousPage.goto(
      `http://localhost:${process.env.EMP_E2E_FRONTEND_PORT ?? "5174"}/login`,
    );
    const anonymousImageStatus = await anonymousPage.evaluate(
      async (url) =>
        fetch(url, { credentials: "omit" }).then((response) => response.status),
      graphicUrl!,
    );
    expect(anonymousImageStatus).toBe(401);
    await anonymousPage.close();
  }
});
