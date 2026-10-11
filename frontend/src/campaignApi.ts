/** Same-origin JSON client for campaign commands; errors carry the server code only. */

export class CampaignError extends Error {
  code: string;

  constructor(code: string) {
    super(code);
    this.code = code;
  }
}

export async function api<T>(
  path: string,
  csrf: string,
  method = "GET",
  body?: unknown,
): Promise<T> {
  const response = await fetch(path, {
    method,
    credentials: "same-origin",
    cache: "no-store",
    headers:
      method === "GET"
        ? undefined
        : { "Content-Type": "application/json", "X-CSRF-Token": csrf },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (response.status === 401) throw new CampaignError("AUTH_REQUIRED");
  if (!response.ok) {
    let code = "REQUEST_FAILED";
    try {
      code = (await response.json()).error?.code ?? code;
    } catch {
      // Keep a safe generic message if the server did not return an API error.
    }
    throw new CampaignError(code);
  }
  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}
