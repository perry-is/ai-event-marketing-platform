import type { Page } from "@playwright/test";

export async function resetDestinationModes(page: Page) {
  const status = await page.evaluate(async () => {
    const me = await fetch("/api/me", { cache: "no-store" }).then((r) =>
      r.json(),
    );
    return fetch("/api/test/reset-destination-modes", {
      method: "POST",
      headers: { "X-CSRF-Token": me.csrf_token },
    }).then(async (r) => ({ status: r.status, body: await r.text() }));
  });
  if (status.status !== 204)
    throw new Error(
      `Could not reset browser-test modes (HTTP ${status.status}: ${status.body})`,
    );
}
