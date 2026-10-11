import { expect, test } from "@playwright/test";

test("protected shell, responsive navigation and server-revoked logout", async ({
  page,
  context,
}, testInfo) => {
  await page.goto("/events");
  await expect(page).toHaveURL(/\/login$/);
  await expect(
    page.getByRole("link", { name: "Continue with Google" }),
  ).toBeVisible();
  expect((await context.request.get("/api/me")).status()).toBe(401);
  await page.screenshot({
    path: testInfo.outputPath("login.png"),
    fullPage: true,
  });
  await page.getByRole("link", { name: "Continue with Google" }).click();
  await expect(
    page.getByRole("heading", { name: "Tell me about the event." }),
  ).toBeVisible();
  const cookies = await context.cookies();
  const session = cookies.find((cookie) => cookie.name === "emp_session")!;
  expect(session.httpOnly).toBe(true);
  expect(session.sameSite).toBe("Lax");
  expect(session.value).toHaveLength(43);
  const info = await (await context.request.get("/api/me")).json();
  expect(Object.keys(info).sort()).toEqual(["csrf_token", "display_name"]);
  for (const title of ["Events", "Groups", "Settings", "New Event"]) {
    await page.getByRole("link", { name: title, exact: true }).click();
    await expect(
      page.getByRole("heading", {
        name: title === "New Event" ? "Tell me about the event." : title,
        exact: true,
      }),
    ).toBeVisible();
  }
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth,
    ),
  ).toBe(true);
  await page.screenshot({
    path: testInfo.outputPath("shell.png"),
    fullPage: true,
  });
  await page.getByRole("button", { name: "Sign out" }).click();
  await expect(page).toHaveURL(/\/login$/);
  expect((await context.request.get("/api/me")).status()).toBe(401);
  await context.addCookies([session]);
  expect((await context.request.get("/api/me")).status()).toBe(401);
  await page.reload();
  await expect(
    page.getByRole("link", { name: "Continue with Google" }),
  ).toBeVisible();
  await expect(page.getByRole("navigation")).toHaveCount(0);
});
