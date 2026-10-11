import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, test, vi } from "vitest";
import {
  FacebookGroupsSection,
  FrozenGroupList,
  type GroupView,
  type RecommendationsView,
} from "./facebookGroups";

const group = (
  id: string,
  name: string,
  extra: Partial<GroupView> = {},
): GroupView => ({
  id,
  display_name: name,
  url: `https://www.facebook.com/groups/${id}`,
  geography_label: "Colorado",
  member_count: 2100,
  category: null,
  cautions: [],
  selectable: true,
  unavailable_reason: null,
  ...extra,
});
const denver = group("denverbc", "Denver Business Network");
const springs = group("springsbc", "Springfield Business Network", {
  cautions: [
    "Has posting rules in the Group Master — check them before sharing.",
  ],
});
const browseOnly = group("puebloshop", "Pueblo Small Business");

const baseView = (
  overrides: Partial<RecommendationsView> = {},
): RecommendationsView => ({
  campaign_id: "campaign-1",
  campaign_revision: 3,
  facebook_groups_enabled: true,
  run: {
    run_id: "run-1",
    created_at: "2026-10-06T10:00:00Z",
    ranking_source: "MODEL",
    candidate_count: 44,
    excluded_counts: { GEOGRAPHY_MISMATCH: 119, UNRESOLVED_IDENTITY: 12 },
    run_notes: [],
    stale: false,
  },
  recommendations: [
    {
      recommendation_id: "rec-1",
      rank: 1,
      tier: "BEST_FIT",
      reason: "Colorado business community and a strong local match.",
      signals: ["LOCAL_GEOGRAPHY", "BUSINESS_COMMUNITY"],
      caution: null,
      explanation_source: "MODEL",
      group: denver,
    },
    {
      recommendation_id: "rec-2",
      rank: 2,
      tier: "GOOD_FIT",
      reason: "Colorado audience with a business focus.",
      signals: ["LOCAL_GEOGRAPHY"],
      caution: springs.cautions[0],
      explanation_source: "MODEL",
      group: springs,
    },
  ],
  selection: {
    selected: [],
    pending: false,
    saved_group_ids: [],
    version_number: 1,
    approval_active: false,
  },
  ...overrides,
});

const json = (value: unknown, status = 200) =>
  new Response(JSON.stringify(value), {
    status,
    headers: { "Content-Type": "application/json" },
  });

function renderSection(
  fetcher: (url: string, init?: RequestInit) => Promise<Response>,
  props: Partial<Parameters<typeof FacebookGroupsSection>[0]> = {},
) {
  const fetchMock = vi.fn(fetcher);
  vi.stubGlobal("fetch", fetchMock);
  const handlers = { onDecision: vi.fn(), onSaved: vi.fn() };
  render(
    <FacebookGroupsSection
      campaignId="campaign-1"
      campaignRevision={3}
      versionNumber={1}
      frozenGroups={[]}
      historical={false}
      stale={false}
      csrf="csrf"
      onUnauthorized={vi.fn()}
      {...handlers}
      {...props}
    />,
  );
  return { fetchMock, ...handlers };
}

test("empty state offers suggestions without loading them automatically", async () => {
  let view = baseView({ run: null, recommendations: [] });
  const { fetchMock } = renderSection(async (url, init) => {
    if (init?.method === "POST" && url.endsWith("/group-recommendations")) {
      view = baseView();
      return json(view);
    }
    return json(view);
  });
  const suggest = await screen.findByRole("button", { name: "Suggest groups" });
  expect(
    fetchMock.mock.calls.every(([, init]) => init?.method !== "POST"),
  ).toBe(true);
  await userEvent.click(suggest);
  const list = await screen.findByRole("list", {
    name: "Recommended Facebook Groups",
  });
  expect(within(list).getByText("Best fit")).toBeVisible();
  expect(within(list).getByText("Good fit")).toBeVisible();
  expect(
    within(list).getByText(
      "Colorado business community and a strong local match.",
    ),
  ).toBeVisible();
  expect(within(list).getAllByText(/about 2,100 members/)[0]).toBeVisible();
  expect(within(list).getByText(/check them before sharing/)).toBeVisible();
  expect(
    within(list).getAllByRole("link", { name: /Open group/ })[0],
  ).toHaveAttribute("href", "https://www.facebook.com/groups/denverbc");
  expect(within(list).queryByText(/0\.\d/)).not.toBeInTheDocument();
});

test("select and deselect are explicit decisions and saving creates a new version", async () => {
  let view = baseView();
  const decisions: unknown[] = [];
  const { onDecision, onSaved } = renderSection(async (url, init) => {
    if (url.endsWith("/group-decisions")) {
      const body = JSON.parse(String(init?.body));
      decisions.push(body);
      const selected =
        body.decision === "SELECT"
          ? [
              ...view.selection.selected,
              body.group_record_id === denver.id ? denver : springs,
            ]
          : view.selection.selected.filter(
              (item) => item.id !== body.group_record_id,
            );
      view = {
        ...view,
        campaign_revision: view.campaign_revision + 1,
        selection: {
          ...view.selection,
          selected,
          pending: selected.length > 0,
        },
      };
      return json(view);
    }
    if (url.endsWith("/group-selection"))
      return json({ current_version_number: 2 });
    return json(view);
  });
  const denverBox = await screen.findByRole("checkbox", {
    name: /Denver Business Network/,
  });
  expect(denverBox).not.toBeChecked();
  await userEvent.click(denverBox);
  await userEvent.click(screen.getByRole("checkbox", { name: /Springfield/ }));
  await waitFor(() =>
    expect(screen.getByText("2 groups selected")).toBeVisible(),
  );
  expect(screen.getByText(/not saved to the campaign yet/)).toBeVisible();
  await userEvent.click(screen.getByRole("checkbox", { name: /Springfield/ }));
  await waitFor(() =>
    expect(screen.getByText("1 group selected")).toBeVisible(),
  );
  expect(decisions).toEqual([
    expect.objectContaining({
      decision: "SELECT",
      group_record_id: denver.id,
      expected_revision: 3,
      recommendation_id: "rec-1",
    }),
    expect.objectContaining({
      decision: "SELECT",
      group_record_id: springs.id,
      expected_revision: 4,
    }),
    expect.objectContaining({
      decision: "REJECT",
      group_record_id: springs.id,
      expected_revision: 5,
    }),
  ]);
  expect(onDecision).toHaveBeenCalledTimes(3);
  await userEvent.click(
    screen.getByRole("button", { name: "Save group selection" }),
  );
  await waitFor(() =>
    expect(onSaved).toHaveBeenCalledWith({ current_version_number: 2 }),
  );
});

test("ranker fallback is explained and other eligible groups can be browsed", async () => {
  let view = baseView({
    run: {
      ...baseView().run!,
      ranking_source: "DETERMINISTIC_FALLBACK",
      run_notes: ["EVENT_STATE_UNKNOWN"],
    },
  });
  renderSection(async (url, init) => {
    if (url.includes("/eligible-groups"))
      return json({
        eligible_count: 44,
        groups: [{ ...browseOnly, tier: "POSSIBLE_FIT" }],
      });
    if (url.endsWith("/group-decisions")) {
      view = {
        ...view,
        selection: { ...view.selection, selected: [browseOnly], pending: true },
      };
      return json(view);
    }
    void init;
    return json(view);
  });
  expect(
    await screen.findByText(/AI suggestions aren’t available right now/),
  ).toBeVisible();
  expect(screen.getByText(/weren’t filtered by location/)).toBeVisible();
  await userEvent.click(screen.getByText("Browse other eligible groups"));
  const eligible = await screen.findByRole("list", {
    name: "Eligible Facebook Groups",
  });
  expect(screen.getByText(/1 of 44 eligible groups shown/)).toBeVisible();
  await userEvent.click(
    within(eligible).getByRole("checkbox", { name: /Pueblo Small Business/ }),
  );
  expect(await screen.findByText("Also chosen")).toBeVisible();
});

test("approved groups are locked until the operator confirms a change that needs a new approval", async () => {
  const view = baseView({
    selection: {
      selected: [denver],
      pending: false,
      saved_group_ids: [denver.id],
      version_number: 2,
      approval_active: true,
    },
  });
  const decisions: { confirm_withdraw_approval: boolean }[] = [];
  renderSection(
    async (url, init) => {
      if (url.endsWith("/group-decisions")) {
        decisions.push(JSON.parse(String(init?.body)));
        return json({
          ...view,
          selection: {
            ...view.selection,
            approval_active: false,
            pending: true,
          },
        });
      }
      return json(view);
    },
    {
      versionNumber: 2,
      frozenGroups: [
        {
          group_record_id: denver.id,
          display_name: denver.display_name,
          chosen_url: denver.url,
          geography_label: "Colorado",
          member_count: 2100,
          cautions: [],
        },
      ],
    },
  );
  expect(await screen.findByText("Approved with Version 2")).toBeVisible();
  expect(screen.queryByRole("checkbox")).not.toBeInTheDocument();
  await userEvent.click(screen.getByRole("button", { name: "Change groups" }));
  expect(
    screen.getByText(/Changing groups ends the current approval/),
  ).toBeVisible();
  await userEvent.click(
    screen.getAllByRole("button", { name: "Change groups" })[0],
  );
  await userEvent.click(
    await screen.findByRole("checkbox", { name: /Springfield/ }),
  );
  await waitFor(() => expect(decisions).toHaveLength(1));
  expect(decisions[0].confirm_withdraw_approval).toBe(true);
});

test("historical versions show their own saved groups read-only", async () => {
  renderSection(async () => json(baseView()), {
    historical: true,
    versionNumber: 1,
    frozenGroups: [
      {
        group_record_id: "old",
        display_name: "Old Approved Group",
        chosen_url: "https://www.facebook.com/groups/old",
        geography_label: null,
        member_count: null,
        cautions: [],
      },
    ],
  });
  expect(
    screen.getByText(/Groups saved with Version 1 · read-only/),
  ).toBeVisible();
  expect(screen.getByText("Old Approved Group")).toBeVisible();
  expect(screen.queryByRole("checkbox")).not.toBeInTheDocument();
  expect(
    screen.queryByRole("button", { name: "Suggest groups" }),
  ).not.toBeInTheDocument();
});

test("unavailable suggestions do not raise an alert or block review", async () => {
  renderSection(async () => json({ error: { code: "REQUEST_FAILED" } }, 500));
  expect(
    await screen.findByText(/suggestions aren’t available right now/),
  ).toBeVisible();
  expect(screen.queryByRole("alert")).not.toBeInTheDocument();
});

test("approved group list names each link accessibly", () => {
  render(
    <FrozenGroupList
      groups={[
        {
          group_record_id: "a",
          display_name: "Denver Business Network",
          chosen_url: "https://www.facebook.com/groups/denverbc",
          geography_label: "Colorado",
          member_count: 900,
          cautions: [],
        },
      ]}
    />,
  );
  expect(
    screen.getByRole("link", {
      name: "Open group: Denver Business Network on Facebook (opens in a new tab)",
    }),
  ).toHaveAttribute("target", "_blank");
});
