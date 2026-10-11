import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { expect, test, vi } from "vitest";
import {
  CampaignReview,
  type ApprovalPreview,
  type Campaign,
  type DestinationExecutionView,
  type Distribution,
  type Surface,
  type Version,
} from "./campaigns";

const event = {
  id: "event-1",
  title: "Lunch & Learn",
  starts_at: "2026-10-22T11:30:00-06:00",
  timezone: "America/Denver",
  delivery: "IN_PERSON",
  facts: {
    location: { venue: "Example Venue" },
    people: {},
    registration: { price_kind: "FREE" },
  },
  readiness: { ready: true, missing_fields: [], open_question_count: 0 },
};
const content = {
  marketing_headline: "Lunch & Learn",
  short_summary: "An Event Marketing gathering focused on small business.",
  general_description: "Join us for Lunch & Learn.\n\nWhere: Example Venue",
  attendee_experience: null,
  eventbrite_description: "Join us for Lunch & Learn.",
  meetup_description: "Join the Event Marketing gathering.",
  linkedin_organization_post: "Event Marketing invites you to Lunch & Learn.",
  linkedin_personal_post: "Sharing an invitation to Lunch & Learn.",
  instagram_caption: "You're invited: Lunch & Learn.",
  facebook_groups_post: "Hello, community. Join us for Lunch & Learn.",
  creative_direction: "A clean text-led layout.",
};
const makeVersion = (
  number: number,
  current: boolean,
  caption = content.linkedin_organization_post,
): Version => ({
  id: `version-${number}`,
  version_number: number,
  parent_version_id: number > 1 ? `version-${number - 1}` : null,
  source_event_revision: 4,
  event_snapshot: {
    title: "Lunch & Learn",
    starts_at: event.starts_at,
    ends_at: null,
    timezone: event.timezone,
    delivery: event.delivery,
    facts: event.facts,
  },
  selected_surfaces: [
    "EVENTBRITE",
    "MEETUP",
    "LINKEDIN_ORGANIZATION",
    "LINKEDIN_PERSONAL",
    "INSTAGRAM",
    "FACEBOOK_GROUPS",
  ] as Surface[],
  content: { ...content, linkedin_organization_post: caption },
  revision_request: number > 1 ? "Make LinkedIn warmer." : null,
  revision_targets: number > 1 ? ["linkedin_organization_post"] : [],
  content_hash: "hash-is-not-user-facing",
  created_at: "2026-10-05T10:00:00Z",
  is_current: current,
});
const makeCampaign = (
  currentNumber: number,
  options: { stale?: boolean } = {},
): Campaign => {
  const first = makeVersion(1, currentNumber === 1);
  const current =
    currentNumber === 1
      ? first
      : makeVersion(
          2,
          true,
          "Join us for Lunch & Learn.\n\nWe’d love to see you there.",
        );
  return {
    id: "campaign-1",
    event_id: event.id,
    revision: currentNumber,
    state: "REVIEW",
    current_version_id: current.id,
    current_version_number: currentNumber,
    is_stale: options.stale ?? false,
    stale_message: options.stale
      ? "Event details have changed since this campaign was generated."
      : null,
    event_readiness: event.readiness,
    current_version: current,
    versions:
      currentNumber === 1
        ? [
            {
              id: first.id,
              version_number: 1,
              revision_request: null,
              revision_targets: [],
              created_at: first.created_at,
              is_current: true,
            },
          ]
        : [
            {
              id: current.id,
              version_number: 2,
              revision_request: current.revision_request,
              revision_targets: current.revision_targets,
              created_at: current.created_at,
              is_current: true,
            },
            {
              id: first.id,
              version_number: 1,
              revision_request: null,
              revision_targets: [],
              created_at: first.created_at,
              is_current: false,
            },
          ],
  };
};
const graphicVersion = (number: number): Version => {
  const base = makeCampaign(number).current_version!;
  return {
    ...base,
    id: `version-${number}`,
    version_number: number,
    parent_version_id: number > 1 ? `version-${number - 1}` : null,
    revision_request:
      number === 1
        ? null
        : number === 2
          ? "Generated campaign graphic."
          : "Use less orange.",
    revision_targets: number === 1 ? [] : ["primary_graphic"],
    primary_graphic:
      number === 1
        ? null
        : {
            asset_id: `asset-${number}`,
            role: "PRIMARY_GRAPHIC",
            sha256: String(number).repeat(64),
            composition_version: "campaign-square-1080-v1",
            layout_variant: "STANDARD",
            mime_type: "image/png",
            width: 1080,
            height: 1080,
            preview_url: `/api/campaign-versions/version-${number}/graphic`,
          },
  };
};
const withGraphic = (number: number): Campaign => {
  const base = makeCampaign(number);
  return {
    ...base,
    revision: number,
    current_version: graphicVersion(number),
    current_version_id: `version-${number}`,
    current_version_number: number,
    versions: Array.from({ length: number }, (_, index) => {
      const versionNumber = number - index;
      const viewedVersion = graphicVersion(versionNumber);
      return {
        id: viewedVersion.id,
        version_number: versionNumber,
        revision_request: viewedVersion.revision_request,
        revision_targets: viewedVersion.revision_targets,
        created_at: viewedVersion.created_at,
        is_current: versionNumber === number,
      };
    }),
  };
};
const response = (value: unknown, status = 200) =>
  new Response(JSON.stringify(value), {
    status,
    headers: { "Content-Type": "application/json" },
  });

const target = (destination: string, kind: "AUTO" | "MANUAL" = "AUTO") => ({
  destination_key: destination,
  ui_label:
    destination === "EVENTBRITE" ? "Event Marketing Organization" : undefined,
  target_key:
    kind === "MANUAL" ? "manual-group-selection-deferred" : "simulated-default",
  execution_kind: kind,
  ...(kind === "AUTO"
    ? { runtime_mode: "SIMULATED" as "SIMULATED" | "LIVE" }
    : {}),
  campaign_version_id: "version-1",
  event_title: event.title,
  approved_content: content.facebook_groups_post,
  content_field: "facebook_groups_post",
  creative_direction: content.creative_direction,
  group_selection_status:
    kind === "MANUAL" ? "DEFERRED_NO_GROUPS_SELECTED" : null,
});

const previewFor = (version = makeVersion(1, true)): ApprovalPreview => ({
  campaign: makeCampaign(version.version_number),
  version_id: version.id,
  version_number: version.version_number,
  version_hash: version.content_hash,
  destination_plan_hash: "plan-hash-1",
  destination_plan: {
    schema_version: 2,
    targets: [target("EVENTBRITE"), target("FACEBOOK_GROUPS", "MANUAL")],
    exclusions: [],
    content_only: [
      {
        destination_key: "LINKEDIN_PERSONAL",
        reason: "Event Marketing access is unverified.",
      },
    ],
  },
  destination_readiness: [
    {
      destination_key: "EVENTBRITE",
      target_key: "simulated-default",
      execution_kind: "AUTO" as const,
      runtime_mode: "SIMULATED" as const,
      status: "READY" as const,
      reason: null,
    },
    {
      destination_key: "FACEBOOK_GROUPS",
      target_key: "manual-group-selection-deferred",
      execution_kind: "MANUAL" as const,
      runtime_mode: null,
      status: "READY" as const,
      reason: null,
    },
  ],
  replacement_requirements: [],
  uncertain_risk_requirements: [],
  blocking_unknowns: [],
  can_approve: true,
});

const emptyDistribution = (campaign = makeCampaign(1)): Distribution => ({
  campaign,
  active_approval: null,
  approvals: [],
  executions: [],
  summary: {
    state: "REVIEW",
    outcome: "NEEDS_APPROVAL",
    complete: false,
    completed_at: null,
    target_count: 0,
    succeeded_count: 0,
    failed_count: 0,
    unknown_count: 0,
    manual_pending_count: 0,
    manual_done_count: 0,
    accounted_count: 0,
  },
});

const executionRow = (
  destination: string,
  status: string,
  options: {
    id?: string;
    retryable?: boolean;
    attempts?: number;
    kind?: "AUTO" | "MANUAL";
  } = {},
): DestinationExecutionView => ({
  id: options.id ?? `execution-${destination.toLowerCase()}`,
  campaign_version_id: "version-1",
  approval_id: "approval-1",
  destination_key: destination,
  target_key:
    options.kind === "MANUAL"
      ? "manual-group-selection-deferred"
      : "simulated-default",
  execution_kind: options.kind ?? "AUTO",
  runtime_mode: options.kind === "MANUAL" ? null : "SIMULATED",
  destination_plan_hash: "plan-hash-1",
  status,
  retryable: options.retryable ?? false,
  verification_status: "SIMULATED",
  result_summary:
    status === "SUCCEEDED"
      ? "Simulated result verified; no external platform was contacted."
      : null,
  human_error: null,
  manual_package:
    options.kind === "MANUAL"
      ? {
          approved_copy: content.facebook_groups_post,
          event_title: event.title,
          destination: "Facebook Groups",
          group_selection:
            "No group was selected; choose a group in Facebook manually.",
          creative_status: "No graphic generated",
          creative_direction: content.creative_direction,
          instructions:
            "Review the copy, choose appropriate Groups, and post manually.",
          simulation_label:
            "Manual action package; nothing was posted by this app.",
        }
      : null,
  disposition: {},
  attempts: Array.from({ length: options.attempts ?? 1 }, (_, index) => ({
    attempt_number: index + 1,
    operation: options.kind === "MANUAL" ? "PREPARE_MANUAL" : "PUBLISH",
    outcome:
      status === "FAILED"
        ? "NOT_CREATED"
        : status === "UNKNOWN"
          ? "UNCERTAIN"
          : "PUBLISHED",
    finished_at: "2026-10-05T10:00:00Z",
    human_error: null,
  })),
});

function mount(
  fetcher: (url: string, init?: RequestInit) => Promise<Response>,
) {
  let latestCampaign = makeCampaign(1);
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init?: RequestInit) => {
      const result = await fetcher(url, init);
      const body =
        result.status === 200
          ? await result
              .clone()
              .json()
              .catch(() => null)
          : null;
      if (body?.current_version && body?.versions)
        latestCampaign = body as Campaign;
      if (url.endsWith("/distribution")) {
        return Array.isArray(body?.executions) && body?.summary
          ? result
          : response(emptyDistribution(latestCampaign));
      }
      if (url.endsWith("/approval-preview")) {
        return body?.destination_plan && body?.version_hash
          ? result
          : response({
              ...previewFor(
                latestCampaign.current_version ?? makeVersion(1, true),
              ),
              campaign: latestCampaign,
              can_approve: !latestCampaign.is_stale,
            });
      }
      return result;
    }),
  );
  render(
    <MemoryRouter initialEntries={[`/events/${event.id}/campaign`]}>
      <Routes>
        <Route
          path="/events/:eventId/campaign"
          element={<CampaignReview csrf="test-csrf" onUnauthorized={vi.fn()} />}
        />
      </Routes>
    </MemoryRouter>,
  );
}

test("ready Event generates reviewed content surfaces and a focused successor version", async () => {
  let campaign: ReturnType<typeof makeCampaign> | undefined;
  const fetcher = vi.fn(async (url: string, init?: RequestInit) => {
    if (url === `/api/events/${event.id}/campaign` && init?.method === "POST") {
      campaign = makeCampaign(1);
      return response(campaign);
    }
    if (url === `/api/events/${event.id}`) return response(event);
    if (url === `/api/events/${event.id}/campaign`)
      return campaign
        ? response(campaign)
        : response({ error: { code: "CAMPAIGN_NOT_FOUND" } }, 404);
    if (url.endsWith("/revisions")) {
      expect(JSON.parse(String(init?.body))).toEqual({
        expected_revision: 1,
        revision_request: "Make LinkedIn warmer.",
      });
      campaign = makeCampaign(2);
      return response(campaign);
    }
    return response({ error: { code: "REQUEST_FAILED" } }, 500);
  });
  mount(fetcher);
  expect(
    await screen.findByRole("heading", { name: "Choose the copy to prepare." }),
  ).toBeVisible();
  expect(screen.getByLabelText("Facebook Groups · manual copy")).toBeChecked();
  await userEvent.click(
    screen.getByRole("button", { name: "Create campaign draft" }),
  );
  expect(
    await screen.findByRole("heading", { name: "Review each voice." }),
  ).toBeVisible();
  expect(
    screen.getByRole("navigation", { name: "Campaign sections" }),
  ).toBeVisible();
  expect(
    screen.getByRole("group", {
      name: "No artwork is included in this campaign version",
    }),
  ).toBeVisible();
  expect(
    screen.getByRole("link", { name: "Approval & distribution" }),
  ).toHaveAttribute("href", "#execution-heading");
  expect(screen.getAllByText(/Version 1 · Current/).length).toBeGreaterThan(0);
  expect(screen.getByText("Facebook Groups · manual copy")).toBeVisible();
  expect(screen.getByText("A clean text-led layout.")).toBeVisible();
  await userEvent.type(
    screen.getByLabelText("What would you like to change?"),
    "Make LinkedIn warmer.",
  );
  await userEvent.click(
    screen.getByRole("button", { name: "Create revised version" }),
  );
  await waitFor(() =>
    expect(screen.getAllByText(/Version 2 · Current/).length).toBeGreaterThan(
      0,
    ),
  );
  expect(screen.getByText(/We’d love to see you there\./)).toBeVisible();
  expect(screen.getByRole("button", { name: /Version 1/ })).toBeVisible();
  expect(
    screen.getByText(/Version 2 is ready — “Make LinkedIn warmer\.”/),
  ).toBeVisible();
  const changedBlock = screen
    .getByRole("heading", { name: "LinkedIn · organization" })
    .closest("article");
  expect(within(changedBlock!).getByText("Updated in Version 2")).toBeVisible();
  expect(screen.getAllByText("Updated in Version 2")).toHaveLength(1);
  const progress = screen.getByRole("list", { name: "Event progress" });
  expect(within(progress).getByText("Campaign").closest("li")).toHaveAttribute(
    "aria-current",
    "step",
  );
});

test("campaign graphics create visible historical successor versions", async () => {
  let campaign = makeCampaign(1);
  const fetcher = vi.fn(async (url: string, init?: RequestInit) => {
    if (url === `/api/events/${event.id}`) return response(event);
    if (url === `/api/events/${event.id}/campaign`) return response(campaign);
    if (url.endsWith("/graphics") && init?.method === "POST") {
      const body = JSON.parse(String(init.body));
      if (body.instruction) {
        expect(body).toMatchObject({
          expected_revision: 2,
          instruction: "Use less orange.",
        });
        campaign = withGraphic(3);
      } else {
        expect(body).toMatchObject({ expected_revision: 1 });
        expect(body).not.toHaveProperty("instruction");
        campaign = withGraphic(2);
      }
      return response(campaign);
    }
    if (url.endsWith("/versions/1"))
      return response({
        ...campaign,
        viewed_version: { ...graphicVersion(1), is_current: false },
      });
    return response({ error: { code: "CAMPAIGN_NOT_FOUND" } }, 404);
  });
  mount(fetcher);
  await userEvent.click(
    await screen.findByRole("button", { name: "Generate graphic" }),
  );
  expect(
    await screen.findByRole("img", {
      name: "Campaign graphic for Lunch & Learn",
    }),
  ).toHaveAttribute("src", "/api/campaign-versions/version-2/graphic");
  expect(screen.getAllByText("Graphic added in Version 2.").length).toBe(2);
  expect(
    screen.getByRole("button", { name: /Version 2 · Currents*Needs approval/ }),
  ).toHaveTextContent("Graphic added");

  await userEvent.click(screen.getByRole("button", { name: /Version 1/ }));
  expect(
    await screen.findByRole("group", {
      name: "No artwork is included in this campaign version",
    }),
  ).toBeVisible();
  expect(
    screen.queryByRole("button", { name: "Generate graphic" }),
  ).not.toBeInTheDocument();
  await userEvent.click(
    screen.getByRole("button", { name: "Return to current version" }),
  );
  await userEvent.type(
    screen.getByLabelText("What would you like to change about the graphic?"),
    "Use less orange.",
  );
  await userEvent.click(screen.getByRole("button", { name: "Revise graphic" }));
  expect(
    await screen.findByRole("img", {
      name: "Campaign graphic for Lunch & Learn",
    }),
  ).toHaveAttribute("src", "/api/campaign-versions/version-3/graphic");
  expect(screen.getAllByText("Graphic updated in Version 3.").length).toBe(2);
  expect(
    screen.getByRole("button", { name: /Version 3 · Current/ }),
  ).toHaveTextContent("Graphic change: “Use less orange.”");
  expect(
    fetcher.mock.calls.some(
      ([url, init]) =>
        String(url).endsWith("/graphics") && init?.method === "POST",
    ),
  ).toBe(true);
});

test("failed graphic generation leaves the reviewed CampaignVersion visible", async () => {
  const current = makeCampaign(1);
  const fetcher = async (url: string, init?: RequestInit) => {
    if (url === `/api/events/${event.id}`) return response(event);
    if (url === `/api/events/${event.id}/campaign`) return response(current);
    if (url.endsWith("/graphics") && init?.method === "POST")
      return response({ error: { code: "GRAPHIC_GENERATION_FAILED" } }, 503);
    return response({ error: { code: "CAMPAIGN_NOT_FOUND" } }, 404);
  };
  mount(fetcher);
  await userEvent.click(
    await screen.findByRole("button", { name: "Generate graphic" }),
  );
  const alert = await screen.findByRole("alert");
  expect(alert).toHaveTextContent(
    "I couldn’t finish the graphic. Your current campaign version and its approval are unchanged",
  );
  expect(alert.closest("section")).toHaveAccessibleName("Campaign graphic");
  expect(screen.getAllByText(/Version 1 · Current/)[0]).toBeVisible();
  expect(
    screen.getByRole("button", { name: "Generate graphic" }),
  ).toBeEnabled();
});

test("graphic generation exposes a clear pending state", async () => {
  let finish: ((value: Response) => void) | undefined;
  const fetcher = async (url: string, init?: RequestInit) => {
    if (url === `/api/events/${event.id}`) return response(event);
    if (url === `/api/events/${event.id}/campaign`)
      return response(makeCampaign(1));
    if (url.endsWith("/graphics") && init?.method === "POST")
      return new Promise<Response>((resolve) => {
        finish = resolve;
      });
    return response({ error: { code: "CAMPAIGN_NOT_FOUND" } }, 404);
  };
  mount(fetcher);
  await userEvent.click(
    await screen.findByRole("button", { name: "Generate graphic" }),
  );
  const pending = screen.getByRole("button", {
    name: "Creating your graphic…",
  });
  expect(pending).toBeDisabled();
  expect(
    screen.getByText(/Creating your graphic… The campaign stays as it is/),
  ).toBeVisible();
  await act(async () => {
    finish?.(response(withGraphic(2)));
  });
  expect(
    await screen.findByRole("img", {
      name: "Campaign graphic for Lunch & Learn",
    }),
  ).toHaveAttribute("src", "/api/campaign-versions/version-2/graphic");
});

test("a graphic request that changes Event facts points back to Event details", async () => {
  const current = withGraphic(2);
  const fetcher = async (url: string, init?: RequestInit) => {
    if (url === `/api/events/${event.id}`) return response(event);
    if (url === `/api/events/${event.id}/campaign`) return response(current);
    if (url.endsWith("/graphics") && init?.method === "POST")
      return response({ error: { code: "EVENT_CORRECTION_REQUIRED" } }, 409);
    return response({ error: { code: "CAMPAIGN_NOT_FOUND" } }, 404);
  };
  mount(fetcher);
  await userEvent.type(
    await screen.findByLabelText(
      "What would you like to change about the graphic?",
    ),
    "Change the date to October 24.",
  );
  await userEvent.click(screen.getByRole("button", { name: "Revise graphic" }));
  const alert = await screen.findByRole("alert");
  expect(alert).toHaveTextContent(
    "That changes the event details, not just the graphic.",
  );
  expect(
    within(alert).getByRole("link", { name: "Update Event details" }),
  ).toHaveAttribute("href", `/events/${event.id}`);
  expect(
    screen.getByLabelText("What would you like to change about the graphic?"),
  ).toHaveValue("Change the date to October 24.");
});

test("campaign generation failure is clear and can be retried without losing the draft", async () => {
  let attempts = 0;
  const fetcher = vi.fn(async (url: string, init?: RequestInit) => {
    if (url === `/api/events/${event.id}`) return response(event);
    if (url === `/api/events/${event.id}/campaign` && init?.method === "POST") {
      attempts += 1;
      return attempts === 1
        ? response({ error: { code: "CAMPAIGN_GENERATION_FAILED" } }, 503)
        : response(makeCampaign(1));
    }
    if (url === `/api/events/${event.id}/campaign`)
      return response({ error: { code: "CAMPAIGN_NOT_FOUND" } }, 404);
    return response({ error: { code: "REQUEST_FAILED" } }, 500);
  });
  mount(fetcher);
  await userEvent.click(
    await screen.findByRole("button", { name: "Create campaign draft" }),
  );
  expect(await screen.findByRole("alert")).toHaveTextContent(
    "Campaign generation didn’t finish.",
  );
  expect(
    screen.getByRole("button", { name: "Create campaign draft" }),
  ).toBeEnabled();
  await userEvent.click(
    screen.getByRole("button", { name: "Create campaign draft" }),
  );
  expect(
    await screen.findByRole("heading", { name: "Review each voice." }),
  ).toBeVisible();
  expect(attempts).toBe(2);
});

test("historical version is read-only and can return to current", async () => {
  const campaign = makeCampaign(2);
  const fetcher = async (url: string) => {
    if (url === `/api/events/${event.id}`) return response(event);
    if (url.endsWith("/versions/1"))
      return response({ ...campaign, viewed_version: makeVersion(1, false) });
    return response(campaign);
  };
  mount(fetcher);
  expect(
    await screen.findByRole("heading", { name: "Review each voice." }),
  ).toBeVisible();
  await userEvent.click(screen.getByRole("button", { name: /Version 1/ }));
  await waitFor(() =>
    expect(
      screen.getByRole("button", { name: "Return to current version" }),
    ).toBeVisible(),
  );
  expect(screen.getByText(/Version 1 · History/)).toBeVisible();
  expect(
    screen.queryByRole("button", { name: "Create revised version" }),
  ).not.toBeInTheDocument();
  await userEvent.click(
    screen.getByRole("button", { name: "Return to current version" }),
  );
  expect(
    screen.getByRole("button", { name: "Create revised version" }),
  ).toBeVisible();
});

test("stale campaign requires refresh and does not offer a copy revision", async () => {
  const current = makeCampaign(1, { stale: true });
  const refreshed = makeCampaign(2);
  const fetcher = vi.fn(async (url: string, init?: RequestInit) => {
    if (url === `/api/events/${event.id}`) return response(event);
    if (url.endsWith("/regenerate") && init?.method === "POST")
      return response(refreshed);
    return response(current);
  });
  mount(fetcher);
  expect(await screen.findByText("Event details changed")).toBeVisible();
  expect(
    screen.queryByLabelText("What would you like to change?"),
  ).not.toBeInTheDocument();
  await userEvent.click(
    screen.getByRole("button", { name: "Create an updated version" }),
  );
  await waitFor(() =>
    expect(screen.getAllByText(/Version 2 · Current/).length).toBeGreaterThan(
      0,
    ),
  );
});

test("unready Event cannot generate copy", async () => {
  mount(async (url: string) => {
    if (url === `/api/events/${event.id}`)
      return response({
        ...event,
        readiness: {
          ...event.readiness,
          ready: false,
          missing_fields: ["delivery"],
        },
      });
    return response({ error: { code: "CAMPAIGN_NOT_FOUND" } }, 404);
  });
  expect(await screen.findByText(/Finish the Event details/)).toBeVisible();
  expect(
    screen.getByRole("button", { name: "Create campaign draft" }),
  ).toBeDisabled();
});

test("approval preview shows mixed treatments from the server plan", async () => {
  const preview = previewFor();
  preview.destination_plan.targets = [
    ...preview.destination_plan.targets.map((item) =>
      item.destination_key === "EVENTBRITE"
        ? { ...item, runtime_mode: "LIVE" as const }
        : item,
    ),
    target("MEETUP"),
    target("LINKEDIN_ORGANIZATION"),
    target("INSTAGRAM"),
  ];
  preview.destination_readiness = [
    ...preview.destination_readiness.map((item) =>
      item.destination_key === "EVENTBRITE"
        ? { ...item, runtime_mode: "LIVE" as const }
        : item,
    ),
    ...["MEETUP", "LINKEDIN_ORGANIZATION", "INSTAGRAM"].map(
      (destination_key) => ({
        destination_key,
        target_key: "simulated-default",
        execution_kind: "AUTO" as const,
        runtime_mode: "SIMULATED" as const,
        status: "READY" as const,
        reason: null,
      }),
    ),
  ];
  const fetcher = async (url: string) => {
    if (url === `/api/events/${event.id}`) return response(event);
    if (url === `/api/events/${event.id}/campaign`)
      return response(makeCampaign(1));
    if (url.endsWith("/approval-preview")) return response(preview);
    if (url.endsWith("/distribution")) return response(emptyDistribution());
    return response({ error: { code: "REQUEST_FAILED" } }, 500);
  };
  mount(fetcher);

  expect(await screen.findByText("Will publish live")).toBeVisible();
  expect(screen.getByText("Event Marketing Organization")).toBeVisible();
  expect(screen.getAllByText("Practice mode")).toHaveLength(3);
  expect(
    screen.getByText("You post it · groups chosen in Facebook"),
  ).toBeVisible();
});

test("exact version approval stays separate from execution and manual completion", async () => {
  let distribution = emptyDistribution();
  const preview = previewFor();
  const fetcher = vi.fn(async (url: string, init?: RequestInit) => {
    if (url === `/api/events/${event.id}`) return response(event);
    if (url === `/api/events/${event.id}/campaign`)
      return response(makeCampaign(1));
    if (url.endsWith("/approval-preview")) return response(preview);
    if (url.endsWith("/distribution")) return response(distribution);
    if (url.endsWith("/approve")) {
      expect(JSON.parse(String(init?.body))).toMatchObject({
        campaign_version_id: "version-1",
        version_hash: "hash-is-not-user-facing",
        expected_destination_plan_hash: "plan-hash-1",
      });
      expect(JSON.parse(String(init?.body))).not.toHaveProperty("runtime_mode");
      expect(JSON.parse(String(init?.body))).not.toHaveProperty(
        "destination_plan_hash",
      );
      distribution = {
        ...distribution,
        active_approval: {
          id: "approval-1",
          campaign_version_id: "version-1",
          approved_at: "2026-10-05T10:00:00Z",
        },
        summary: {
          ...distribution.summary,
          state: "APPROVED",
          outcome: "APPROVED_NOT_STARTED",
        },
      };
      return response({ id: "approval-1" });
    }
    if (url.endsWith("/start")) {
      const automatic = executionRow("EVENTBRITE", "SUCCEEDED");
      const manual = executionRow("FACEBOOK_GROUPS", "MANUAL_PENDING", {
        kind: "MANUAL",
      });
      distribution = {
        ...distribution,
        executions: [automatic, manual],
        summary: {
          ...distribution.summary,
          state: "EXECUTING",
          outcome: "ACTION_REQUIRED",
          target_count: 2,
          succeeded_count: 1,
          manual_pending_count: 1,
        },
      };
      return response(distribution);
    }
    if (url.endsWith("/manual-complete")) {
      const completed = distribution.executions.map((row) =>
        row.destination_key === "FACEBOOK_GROUPS"
          ? {
              ...row,
              status: "MANUAL_DONE",
              disposition: { type: "MANUAL_COMPLETION" },
            }
          : row,
      );
      distribution = {
        ...distribution,
        executions: completed,
        summary: {
          ...distribution.summary,
          state: "COMPLETE",
          outcome: "COMPLETED_WITH_MANUAL_ACTIONS",
          complete: true,
          manual_pending_count: 0,
          manual_done_count: 1,
        },
      };
      return response(distribution);
    }
    return response({ error: { code: "REQUEST_FAILED" } }, 500);
  });
  mount(fetcher);

  expect(await screen.findByText("Practice mode")).toBeVisible();
  expect(
    screen.getByText(/LinkedIn personal copy remains available/),
  ).toBeVisible();
  await userEvent.click(
    screen.getByRole("button", { name: "Approve this version" }),
  );
  expect(
    await screen.findByText(/This exact version is approved/),
  ).toBeVisible();
  expect(
    screen.getByRole("button", { name: "Start simulated distribution" }),
  ).toBeVisible();
  expect(
    fetcher.mock.calls.some(([url]) => String(url).endsWith("/start")),
  ).toBe(false);

  await userEvent.click(
    screen.getByRole("button", { name: "Start simulated distribution" }),
  );
  expect(await screen.findByText("Published (Simulated)")).toBeVisible();
  const manualCard = screen
    .getByRole("heading", { name: "Facebook Groups" })
    .closest("article");
  expect(manualCard).not.toBeNull();
  expect(
    within(manualCard!).getByLabelText("Approved Facebook Groups copy"),
  ).toHaveValue(content.facebook_groups_post);
  expect(within(manualCard!).getByText(/No group was selected/)).toBeVisible();
  const writeText = vi.fn().mockResolvedValue(undefined);
  Object.defineProperty(navigator, "clipboard", {
    configurable: true,
    value: { writeText },
  });
  await userEvent.click(
    within(manualCard!).getByRole("button", { name: "Copy approved post" }),
  );
  expect(writeText).toHaveBeenCalledWith(content.facebook_groups_post);
  expect(
    within(manualCard!).getByRole("button", { name: "Copied" }),
  ).toBeVisible();
  await userEvent.click(
    within(manualCard!).getByRole("button", {
      name: "Mark Facebook Groups complete",
    }),
  );
  expect(
    await screen.findByText("Completed with manual actions"),
  ).toBeVisible();
});

test("changed approval plan refreshes and requires a second explicit approval", async () => {
  let distribution = emptyDistribution();
  let preview = previewFor();
  const approvalRequests: Record<string, unknown>[] = [];
  const fetcher = vi.fn(async (url: string, init?: RequestInit) => {
    if (url.endsWith("/api/events/event-1")) return response(event);
    if (url.endsWith("/api/events/event-1/campaign"))
      return response(makeCampaign(1));
    if (url.endsWith("/approval-preview")) return response(preview);
    if (url.endsWith("/distribution")) return response(distribution);
    if (url.endsWith("/approve")) {
      const body = JSON.parse(String(init?.body)) as Record<string, unknown>;
      approvalRequests.push(body);
      if (approvalRequests.length === 1) {
        preview = { ...preview, destination_plan_hash: "plan-hash-2" };
        return response({ error: { code: "APPROVAL_PLAN_CHANGED" } }, 409);
      }
      distribution = {
        ...distribution,
        active_approval: {
          id: "approval-1",
          campaign_version_id: "version-1",
          approved_at: "2026-10-05T10:00:00Z",
        },
      };
      return response({ id: "approval-1" });
    }
    return response({ error: { code: "REQUEST_FAILED" } }, 500);
  });
  mount(fetcher);

  await screen.findByRole("button", { name: "Approve this version" });
  await userEvent.click(
    screen.getByRole("button", { name: "Approve this version" }),
  );
  expect(
    await screen.findByText(
      "The distribution plan changed. Review the updated destinations, then approve again.",
    ),
  ).toBeVisible();
  expect(approvalRequests).toHaveLength(1);
  expect(approvalRequests[0].expected_destination_plan_hash).toBe(
    "plan-hash-1",
  );
  expect(
    screen.queryByText(/This exact version is approved/),
  ).not.toBeInTheDocument();

  await userEvent.click(
    screen.getByRole("button", { name: "Approve this version" }),
  );
  expect(
    await screen.findByText(/This exact version is approved/),
  ).toBeVisible();
  expect(approvalRequests).toHaveLength(2);
  expect(approvalRequests[1].expected_destination_plan_hash).toBe(
    "plan-hash-2",
  );
});

test("a destination retry targets only the retryable failure and preserves successful siblings", async () => {
  const campaign = makeCampaign(1);
  const preview = { ...previewFor(), can_approve: false };
  const active = emptyDistribution(campaign);
  active.active_approval = {
    id: "approval-1",
    campaign_version_id: "version-1",
    approved_at: "2026-10-05T10:00:00Z",
  };
  active.summary = {
    ...active.summary,
    state: "EXECUTING",
    outcome: "ACTION_REQUIRED",
    target_count: 5,
    succeeded_count: 3,
    failed_count: 1,
    manual_pending_count: 1,
  };
  active.executions = [
    executionRow("EVENTBRITE", "SUCCEEDED"),
    executionRow("MEETUP", "SUCCEEDED"),
    executionRow("LINKEDIN_ORGANIZATION", "FAILED", { retryable: true }),
    executionRow("INSTAGRAM", "SUCCEEDED"),
    executionRow("FACEBOOK_GROUPS", "MANUAL_PENDING", { kind: "MANUAL" }),
  ];
  const calls: string[] = [];
  const fetcher = vi.fn(async (url: string) => {
    if (url === `/api/events/${event.id}`) return response(event);
    if (url === `/api/events/${event.id}/campaign`) return response(campaign);
    if (url.endsWith("/distribution")) return response(active);
    if (url.endsWith("/approval-preview")) return response(preview);
    if (url.endsWith("/retry")) {
      calls.push(String(url));
      active.executions = active.executions.map((row) =>
        row.destination_key === "LINKEDIN_ORGANIZATION"
          ? {
              ...row,
              status: "SUCCEEDED",
              retryable: false,
              attempts: [
                ...row.attempts,
                {
                  attempt_number: 2,
                  operation: "PUBLISH",
                  outcome: "PUBLISHED",
                  finished_at: "2026-10-05T10:02:00Z",
                  human_error: null,
                },
              ],
            }
          : row,
      );
      active.summary = {
        ...active.summary,
        outcome: "ACTION_REQUIRED",
        failed_count: 0,
        succeeded_count: 4,
      };
      return response(active);
    }
    return response({ error: { code: "REQUEST_FAILED" } }, 500);
  });
  mount(fetcher);
  expect(await screen.findByText("Action required")).toBeVisible();
  await userEvent.click(
    screen.getByRole("button", { name: "Retry LinkedIn organization" }),
  );
  await waitFor(() =>
    expect(calls).toEqual([
      "/api/destination-executions/execution-linkedin_organization/retry",
    ]),
  );
  expect(await screen.findAllByText("Published (Simulated)")).toHaveLength(4);
  expect(screen.getByText(/History · 2 actions/)).toBeVisible();
  expect(calls).toHaveLength(1);
});

test("replacement approval requires a conscious acknowledgement", async () => {
  const campaign = makeCampaign(2);
  const preview: ApprovalPreview = {
    ...previewFor(makeVersion(2, true)),
    version_id: "version-2",
    version_number: 2,
    replacement_requirements: [
      {
        execution_id: "prior-instagram-execution",
        destination_key: "MEETUP",
        version_number: 1,
        status: "SUCCEEDED",
        runtime_mode: "LIVE",
      },
    ],
  };
  const distribution = emptyDistribution(campaign);
  const fetcher = vi.fn(async (url: string, init?: RequestInit) => {
    if (url === `/api/events/${event.id}`) return response(event);
    if (url === `/api/events/${event.id}/campaign`) return response(campaign);
    if (url.endsWith("/distribution")) return response(distribution);
    if (url.endsWith("/approval-preview")) return response(preview);
    if (url.endsWith("/approve")) {
      expect(JSON.parse(String(init?.body))).toMatchObject({
        campaign_version_id: "version-2",
        replacement_acknowledgements: ["prior-instagram-execution"],
      });
      distribution.active_approval = {
        id: "approval-2",
        campaign_version_id: "version-2",
        approved_at: "2026-10-05T10:00:00Z",
      };
      return response({ id: "approval-2" });
    }
    return response({ error: { code: "REQUEST_FAILED" } }, 500);
  });
  mount(fetcher);

  const approveButton = await screen.findByRole("button", {
    name: "Approve this version",
  });
  expect(screen.getByText(/Meetup · Version 1 \(Published\)/)).toBeVisible();
  expect(approveButton).toBeDisabled();
  await userEvent.click(
    screen.getByRole("checkbox", { name: /Meetup · Version 1/ }),
  );
  expect(approveButton).toBeEnabled();
  await userEvent.click(approveButton);
  expect(
    await screen.findByText(/This exact version is approved/),
  ).toBeVisible();
});

test("UNKNOWN is distinct from failure and can be reconciled without a retry button", async () => {
  const active = emptyDistribution();
  active.active_approval = {
    id: "approval-1",
    campaign_version_id: "version-1",
    approved_at: "2026-10-05T10:00:00Z",
  };
  active.summary = {
    ...active.summary,
    state: "EXECUTING",
    outcome: "UNRESOLVED_UNKNOWN",
    target_count: 1,
    unknown_count: 1,
  };
  active.executions = [executionRow("INSTAGRAM", "UNKNOWN")];
  const fetcher = vi.fn(async (url: string) => {
    if (url === `/api/events/${event.id}`) return response(event);
    if (url === `/api/events/${event.id}/campaign`)
      return response(makeCampaign(1));
    if (url.endsWith("/distribution")) return response(active);
    if (url.endsWith("/approval-preview"))
      return response({ ...previewFor(), can_approve: false });
    if (url.endsWith("/reconcile")) {
      active.executions[0] = { ...active.executions[0], status: "SUCCEEDED" };
      active.summary = {
        ...active.summary,
        outcome: "COMPLETED_SUCCESSFULLY",
        complete: true,
        unknown_count: 0,
        succeeded_count: 1,
      };
      return response(active);
    }
    return response({ error: { code: "REQUEST_FAILED" } }, 500);
  });
  mount(fetcher);
  expect(
    await screen.findByText("Result uncertain — verify before retrying"),
  ).toBeVisible();
  expect(
    screen.queryByRole("button", { name: /Retry Instagram/ }),
  ).not.toBeInTheDocument();
  await userEvent.click(
    screen.getByRole("button", { name: "Verify destination result" }),
  );
  expect(await screen.findByText("Completed successfully")).toBeVisible();
  expect(
    fetcher.mock.calls.some(([url]) => String(url).endsWith("/retry")),
  ).toBe(false);
});

test("material Event change preserves historical approval but blocks current approval", async () => {
  const staleCampaign = makeCampaign(2, { stale: true });
  const distribution = emptyDistribution(staleCampaign);
  distribution.approvals = [
    {
      id: "approval-1",
      campaign_version_id: "version-1",
      version_number: 1,
      approved_at: "2026-10-05T10:00:00Z",
      active: false,
      destination_plan_hash: "plan-hash-1",
      destination_plan: previewFor().destination_plan,
    },
  ];
  const fetcher = async (url: string) => {
    if (url === `/api/events/${event.id}`) return response(event);
    if (url === `/api/events/${event.id}/campaign`)
      return response(staleCampaign);
    if (url.endsWith("/distribution")) return response(distribution);
    if (url.endsWith("/approval-preview"))
      return response({
        ...previewFor(makeVersion(2, true)),
        can_approve: false,
      });
    return response({ error: { code: "REQUEST_FAILED" } }, 500);
  };
  mount(fetcher);
  expect(
    await screen.findByText(/earlier approval is retained in history/),
  ).toBeVisible();
  await userEvent.click(screen.getByText(/Approval history/));
  expect(screen.getByText("Version 1 · Historical approval")).toBeVisible();
  expect(
    screen.queryByRole("button", { name: "Approve this version" }),
  ).not.toBeInTheDocument();
});

test("approval summary and manual package show exactly the approved Facebook Groups", async () => {
  const campaign = makeCampaign(1);
  const approvedGroups = [
    {
      group_record_id: "group-a",
      display_name: "Denver Business Network",
      url: "https://www.facebook.com/groups/denverbc",
      geography_label: "Colorado",
      member_count: 900,
      cautions: [],
    },
    {
      group_record_id: "group-b",
      display_name: "Aurora Business Network",
      url: "https://www.facebook.com/groups/aurorabc",
      geography_label: "Colorado",
      member_count: null,
      cautions: [],
    },
  ];
  const pendingPreview = {
    ...previewFor(),
    can_approve: false,
    group_selection_pending: true,
    destination_plan: {
      ...previewFor().destination_plan,
      targets: [
        target("EVENTBRITE"),
        {
          ...target("FACEBOOK_GROUPS", "MANUAL"),
          selected_groups: approvedGroups,
        },
      ],
    },
  };
  const fetcher = vi.fn(async (url: string) => {
    if (url === `/api/events/${event.id}`) return response(event);
    if (url === `/api/events/${event.id}/campaign`) return response(campaign);
    if (url.endsWith("/approval-preview")) return response(pendingPreview);
    if (url.endsWith("/distribution"))
      return response(emptyDistribution(campaign));
    return response({ error: { code: "REQUEST_FAILED" } }, 500);
  });
  mount(fetcher);
  expect(await screen.findByText("You post it · 2 groups")).toBeVisible();
  expect(
    screen.getByText("Denver Business Network · Aurora Business Network"),
  ).toBeVisible();
  expect(
    screen.getByText(/Facebook Group choices aren’t saved yet/),
  ).toBeVisible();
  expect(
    screen.getByRole("button", { name: "Approve this version" }),
  ).toBeDisabled();
});

test("the manual Facebook package lists the approved groups with their links", async () => {
  const campaign = makeCampaign(1);
  const active = emptyDistribution(campaign);
  active.active_approval = {
    id: "approval-1",
    campaign_version_id: "version-1",
    approved_at: "2026-10-05T10:00:00Z",
  };
  active.summary = {
    ...active.summary,
    state: "EXECUTING",
    outcome: "ACTION_REQUIRED",
    target_count: 1,
    manual_pending_count: 1,
  };
  const manual = executionRow("FACEBOOK_GROUPS", "MANUAL_PENDING", {
    kind: "MANUAL",
  });
  manual.manual_package = {
    ...manual.manual_package!,
    group_selection: "Share in the 1 approved group below.",
    selected_groups: [
      {
        group_record_id: "group-a",
        display_name: "Denver Business Network",
        url: "https://www.facebook.com/groups/denverbc",
        geography_label: "Colorado",
        member_count: 900,
        cautions: [],
      },
    ],
  };
  active.executions = [manual];
  const fetcher = vi.fn(async (url: string) => {
    if (url === `/api/events/${event.id}`) return response(event);
    if (url === `/api/events/${event.id}/campaign`) return response(campaign);
    if (url.endsWith("/distribution")) return response(active);
    if (url.endsWith("/approval-preview"))
      return response({ ...previewFor(), can_approve: false });
    return response({ error: { code: "REQUEST_FAILED" } }, 500);
  });
  mount(fetcher);
  const card = (
    await screen.findByRole("heading", { name: "Facebook Groups" })
  ).closest("article")!;
  expect(
    within(card).getByText("Share in the 1 approved group below."),
  ).toBeVisible();
  expect(
    within(card).getByRole("link", {
      name: "Open group: Denver Business Network on Facebook (opens in a new tab)",
    }),
  ).toHaveAttribute("href", "https://www.facebook.com/groups/denverbc");
  expect(
    within(card).getAllByRole("button", {
      name: "Mark Facebook Groups complete",
    }),
  ).toHaveLength(1);
});

function liveDistribution(
  rows: DestinationExecutionView[],
  outcome = "ACTION_REQUIRED",
) {
  const campaign = makeCampaign(1);
  const active = emptyDistribution(campaign);
  active.active_approval = {
    id: "approval-1",
    campaign_version_id: "version-1",
    approved_at: "2026-10-05T10:00:00Z",
  };
  active.summary = { ...active.summary, state: "EXECUTING", outcome };
  active.executions = rows;
  const fetcher = vi.fn(async (url: string) => {
    if (url.endsWith("/acknowledge-failure")) {
      active.executions = active.executions.map((row) =>
        row.status === "BLOCKED"
          ? {
              ...row,
              disposition: { type: "ACKNOWLEDGED_FAILURE" },
              result_summary: "Failure accounted for by an authorized user.",
            }
          : row,
      );
      active.summary = {
        ...active.summary,
        state: "COMPLETE",
        outcome: "COMPLETED_WITH_ACKNOWLEDGED_FAILURES",
        complete: true,
        accounted_count: 1,
      };
      return response(active);
    }
    if (url === `/api/events/${event.id}`) return response(event);
    if (url === `/api/events/${event.id}/campaign`) return response(campaign);
    if (url.endsWith("/distribution")) return response(active);
    if (url.endsWith("/approval-preview"))
      return response({ ...previewFor(), can_approve: false });
    return response({ error: { code: "REQUEST_FAILED" } }, 500);
  });
  mount(fetcher);
}

const card = async (name: string) =>
  (await screen.findByRole("heading", { name })).closest("article")!;

test("mixed results show live Eventbrite truthfully beside practice and manual work", async () => {
  liveDistribution([
    {
      ...executionRow("EVENTBRITE", "SUCCEEDED"),
      runtime_mode: "LIVE",
      verification_status: "VERIFIED",
      external_id: "7000123",
      external_url: "https://www.eventbrite.com/e/event-marketing-7000123",
      result_summary: "Published on Eventbrite and verified.",
    },
    {
      ...executionRow("INSTAGRAM", "SUCCEEDED"),
      runtime_mode: "LIVE",
      verification_status: "VERIFIED",
      external_id: "17841400000000001_7000123",
      external_url: "https://www.instagram.com/p/EventMarketingPost01/",
      result_summary: "Published on Instagram and verified.",
    },
    { ...executionRow("MEETUP", "SUCCEEDED"), runtime_mode: "SIMULATED" },
    {
      ...executionRow("FACEBOOK_GROUPS", "MANUAL_DONE", { kind: "MANUAL" }),
      runtime_mode: null,
    },
  ]);
  const eventbrite = await card("Eventbrite");
  expect(within(eventbrite).getByText("Published")).toBeVisible();
  expect(
    within(eventbrite).getByText("Live on Eventbrite · verified"),
  ).toBeVisible();
  expect(
    within(eventbrite).getByRole("link", {
      name: "View on Eventbrite (opens in a new tab)",
    }),
  ).toHaveAttribute(
    "href",
    "https://www.eventbrite.com/e/event-marketing-7000123",
  );
  const instagram = screen.getAllByTestId("destination-card").find((element) =>
    within(element).queryByRole("heading", {
      name: /^Instagram$/,
    }),
  )!;
  expect(
    within(instagram).getByText("Live on Instagram · verified"),
  ).toBeVisible();
  expect(
    within(instagram).getByRole("link", {
      name: "View on Instagram (opens in a new tab)",
    }),
  ).toHaveAttribute(
    "href",
    "https://www.instagram.com/p/EventMarketingPost01/",
  );
  const meetup = await card("Meetup");
  expect(within(meetup).getByText("Published (Simulated)")).toBeVisible();
  expect(within(meetup).getByText("Practice mode")).toBeVisible();
  expect(within(meetup).queryByRole("link")).not.toBeInTheDocument();
  const facebook = await card("Facebook Groups");
  expect(
    within(facebook).getByText("Marked complete by the operator"),
  ).toBeVisible();
  expect(document.body.textContent).not.toMatch(/Bearer|private token/i);
});

test("live failure, uncertain create and blocked live mode are explained honestly", async () => {
  liveDistribution([
    {
      ...executionRow("EVENTBRITE", "UNKNOWN"),
      runtime_mode: "LIVE",
      verification_status: "INCONCLUSIVE",
      human_error:
        "Eventbrite may have created the event, but the result could not be verified.",
    },
  ]);
  const eventbrite = await card("Eventbrite");
  expect(
    within(eventbrite).getByText(/may have created the event/),
  ).toBeVisible();
  expect(
    within(eventbrite).queryByRole("button", { name: /Retry/ }),
  ).not.toBeInTheDocument();
  expect(
    within(eventbrite).queryByRole("link", { name: /View on Eventbrite/ }),
  ).not.toBeInTheDocument();
});

test("an approved live entry blocked by configuration is not shown as practice", async () => {
  liveDistribution([
    {
      ...executionRow("EVENTBRITE", "BLOCKED"),
      runtime_mode: "LIVE",
      error_category: "APPROVED_CONNECTOR_MODE_UNAVAILABLE",
      human_error:
        "The approved destination mode is unavailable. No alternate mode was used.",
    },
    {
      ...executionRow("MEETUP", "FAILED", { retryable: true }),
      runtime_mode: "SIMULATED",
    },
  ]);
  const eventbrite = await card("Eventbrite");
  expect(within(eventbrite).getByText("Blocked")).toBeVisible();
  expect(
    within(eventbrite).getByText(/wasn’t switched to practice mode/),
  ).toBeVisible();
  expect(
    within(eventbrite).queryByText("Practice mode"),
  ).not.toBeInTheDocument();
  const meetup = await card("Meetup");
  expect(
    within(meetup).getByRole("button", { name: "Retry Meetup" }),
  ).toBeVisible();
});

test("a provider-blocked destination can be accounted for from the Campaign", async () => {
  liveDistribution([
    {
      ...executionRow("INSTAGRAM", "BLOCKED"),
      runtime_mode: "LIVE",
      error_category: "CONFIGURATION",
      human_error:
        "This Instagram post is already published. Updating it automatically is not supported.",
    },
  ]);
  await screen.findAllByTestId("destination-card");
  const instagram = screen
    .getAllByTestId("destination-card")
    .find((element) =>
      within(element).queryByRole("heading", { name: /^Instagram$/ }),
    )!;
  expect(within(instagram).getByText("Blocked")).toBeVisible();
  expect(within(instagram).getByText(/already published/)).toBeVisible();
  await userEvent.click(
    within(instagram).getByRole("button", { name: "Account for this" }),
  );
  expect(
    await within(instagram).findByText("Blocked · accounted for"),
  ).toBeVisible();
  expect(
    await screen.findByText("Completed with acknowledged failures"),
  ).toBeVisible();
  expect(screen.getByText(/1 accounted for/)).toBeVisible();
});
