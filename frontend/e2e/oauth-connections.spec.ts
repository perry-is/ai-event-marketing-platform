import {
  expect,
  test,
  type BrowserContextOptions,
  type Page,
} from "@playwright/test";

async function signIn(page: Page) {
  await page.goto("/new");
  await expect(page).toHaveURL(/\/login$/);
  await page.getByRole("link", { name: "Continue with Google" }).click();
  await expect(
    page.getByRole("heading", { name: "Tell me about the event." }),
  ).toBeVisible();
  await page.goto("/settings");
  await expect(page.getByRole("heading", { name: "Settings" })).toBeVisible();
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

async function fakeState(page: Page, provider: "linkedin" | "instagram") {
  return page.evaluate(
    async (provider) =>
      fetch(`/api/test/fake-oauth/${provider}`, { cache: "no-store" }).then(
        (r) => r.json(),
      ),
    provider,
  );
}

async function connectThroughFake(
  page: Page,
  provider: "linkedin" | "instagram",
  tamperState = false,
  exchangeFailure = false,
) {
  let originalState = "";
  const label = provider === "linkedin" ? "LinkedIn" : "Instagram";
  const action = page.getByRole("button", {
    name: new RegExp(`^(Connect|Reconnect) ${label}$`),
  });
  await page.route(`https://oauth.${provider}.test/**`, async (route) => {
    const authorization = new URL(route.request().url());
    originalState = authorization.searchParams.get("state") ?? "";
    const callback = new URL(
      `/auth/${provider}/callback`,
      `http://localhost:${process.env.EMP_E2E_FRONTEND_PORT ?? "5174"}`,
    );
    callback.searchParams.set(
      "state",
      tamperState ? `${originalState.slice(0, -1)}x` : originalState,
    );
    callback.searchParams.set("code", "synthetic-browser-authorization-code");
    await route.fulfill({
      status: 302,
      headers: { location: callback.toString() },
    });
  });
  await expect(action).toBeVisible();
  await action.click();
  await expect(page).toHaveURL(
    new RegExp(
      `/settings\\?connection=${provider}_${tamperState || exchangeFailure ? "failed" : "connected"}`,
    ),
  );
  await page.unroute(`https://oauth.${provider}.test/**`);
  return originalState;
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

test("LinkedIn OAuth rejects tampering and replay, preserves failed reconnect, and disconnects locally", async () => {
  await testPost(page, "/api/test/fake-oauth/linkedin/reset");
  await page.goto("/settings");
  await expect(
    page.getByRole("button", { name: "Connect LinkedIn" }),
  ).toBeVisible();

  const tamperedState = await connectThroughFake(page, "linkedin", true);
  expect(tamperedState).toMatch(/^[A-Za-z0-9_-]{43}$/);
  expect((await fakeState(page, "linkedin")).status).toBe("NOT_CONNECTED");

  await page.goto("/settings");
  const state = await connectThroughFake(page, "linkedin");
  await expect(
    page.getByText("Connected account: Browser Test Member"),
  ).toBeVisible({ timeout: 15000 });
  await expect(
    page.getByRole("button", { name: "Reconnect LinkedIn" }),
  ).toBeVisible();
  const before = await fakeState(page, "linkedin");
  expect(before.status).toBe("CONNECTED");
  expect(before.envelope_present).toBe(true);
  expect(before.database_plaintext_leak).toBe(false);
  expect(before.provider_credential_usable).toBe(true);
  expect(before.granted_scopes).toContain("w_organization_social");

  await page.goto(
    `/auth/linkedin/callback?state=${encodeURIComponent(state)}&code=synthetic-browser-authorization-code`,
  );
  await expect(page).toHaveURL(/\/settings\?connection=linkedin_failed$/);
  const replay = await fakeState(page, "linkedin");
  expect(replay.status).toBe("CONNECTED");
  expect(replay.envelope_digest).toBe(before.envelope_digest);

  await testPost(page, "/api/test/fake-oauth/linkedin/fail-next-exchange");
  await page.goto("/settings");
  await connectThroughFake(page, "linkedin", false, true);
  await expect(
    page.getByText(
      "The connection could not be completed. Any existing connection is unchanged.",
      { exact: true },
    ),
  ).toBeVisible();
  const preserved = await fakeState(page, "linkedin");
  expect(preserved.status).toBe("CONNECTED");
  expect(preserved.envelope_digest).toBe(before.envelope_digest);

  page.once("dialog", (dialog) => dialog.accept());
  await page
    .getByRole("region", { name: "LinkedIn organization" })
    .getByRole("button", { name: "Disconnect LinkedIn" })
    .click();
  await expect(
    page.getByText("Disconnected. Provider-side revocation was confirmed."),
  ).toBeVisible();
  const disconnected = await fakeState(page, "linkedin");
  expect(disconnected.status).toBe("DISCONNECTED");
  expect(disconnected.envelope_present).toBe(false);
  expect(disconnected.provider_credential_usable).toBe(false);
  const settingsPayload = await page.evaluate(() =>
    fetch("/api/settings/oauth-connections", { cache: "no-store" }).then((r) =>
      r.json(),
    ),
  );
  expect(JSON.stringify(settingsPayload)).not.toMatch(
    /BROWSER_.*(ACCESS|REFRESH)_SYNTHETIC|ciphertext|nonce|key_id/i,
  );
  expect(
    await page.evaluate(() => document.documentElement.scrollWidth),
  ).toBeLessThanOrEqual(await page.evaluate(() => window.innerWidth));
});

test("Instagram Login connects an eligible Professional account without exposing credentials", async () => {
  await testPost(page, "/api/test/fake-oauth/instagram/reset");
  await page.goto("/settings");
  await expect(
    page.getByRole("button", { name: "Connect Instagram" }),
  ).toBeVisible();
  await connectThroughFake(page, "instagram");
  await expect(
    page.getByText("Connected account: eventmarketing_test"),
  ).toBeVisible();
  const state = await fakeState(page, "instagram");
  expect(state.status).toBe("CONNECTED");
  expect(state.account_type).toBe("Business");
  expect(state.database_plaintext_leak).toBe(false);
  expect(state.envelope_present).toBe(true);
  expect(state.provider_credential_usable).toBe(true);
  expect(state.granted_scopes).toEqual(
    expect.arrayContaining([
      "instagram_business_basic",
      "instagram_business_content_publish",
    ]),
  );
  const settingsPayload = await page.evaluate(() =>
    fetch("/api/settings/oauth-connections", { cache: "no-store" }).then((r) =>
      r.json(),
    ),
  );
  expect(JSON.stringify(settingsPayload)).not.toMatch(
    /BROWSER_.*(ACCESS|REFRESH)_SYNTHETIC|ciphertext|nonce|key_id/i,
  );
});
