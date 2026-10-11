import { render, screen } from "@testing-library/react";
import { expect, test, vi } from "vitest";
import {
  SettingsPage,
  type EventbriteStatus,
  type LinkedInStatus,
  type InstagramStatus,
  type MeetupStatus,
  type OAuthConnections,
} from "./settings";

const json = (value: unknown, status = 200) =>
  new Response(JSON.stringify(value), {
    status,
    headers: { "Content-Type": "application/json" },
  });

const defaultMeetup: MeetupStatus = {
  mode: "SIMULATED",
  status: "NEEDS_SETUP",
  reasons: ["Meetup private key not configured"],
  member_name: null,
  network_name: null,
  group_name: null,
  checked_live: false,
  photo_capability: "DEFERRED",
  photo_note: "Live image upload awaiting verification",
};

const defaultLinkedIn: LinkedInStatus = {
  mode: "SIMULATED",
  status: "NEEDS_SETUP",
  reasons: [
    "LinkedIn Community Management access must be verified in the Developer Portal",
  ],
  organization_name: null,
  connected_member: null,
  community_access_tier: "UNVERIFIED",
  checked_live: false,
  connector_implemented: true,
};

const defaultInstagram: InstagramStatus = {
  mode: "SIMULATED",
  status: "SIMULATED",
  reasons: [],
  username: null,
  checked_live: false,
  connector_implemented: true,
  account_eligible: false,
  media_delivery_ready: false,
};

const defaultConnections: OAuthConnections = {
  linkedin: {
    provider: "linkedin",
    status: "NOT_CONNECTED",
    account_name: null,
    account_id: null,
    granted_scopes: [],
    missing_scopes: ["w_organization_social"],
    access_expires_at: null,
    vault_ready: true,
  },
  instagram: {
    provider: "instagram",
    status: "NOT_CONNECTED",
    account_name: null,
    account_id: null,
    account_type: null,
    granted_scopes: [],
    missing_scopes: ["instagram_business_content_publish"],
    access_expires_at: null,
    vault_ready: true,
  },
};

function show(
  value: EventbriteStatus,
  meetup = defaultMeetup,
  linkedin = defaultLinkedIn,
  instagram = defaultInstagram,
  connections = defaultConnections,
) {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) =>
      json(
        String(input).endsWith("/oauth-connections")
          ? connections
          : String(input).endsWith("/linkedin-organization")
            ? linkedin
            : String(input).endsWith("/meetup")
              ? meetup
              : String(input).endsWith("/instagram")
                ? instagram
                : value,
      ),
    ),
  );
  render(<SettingsPage csrf="csrf" onUnauthorized={vi.fn()} />);
}

test("OAuth connection controls show connect/reconnect state without credential material", async () => {
  show(
    {
      mode: "LIVE",
      status: "NEEDS_SETUP",
      reasons: [],
      organization_name: "Event Marketing Page",
      organizer_name: null,
      checked_live: false,
    },
    defaultMeetup,
    defaultLinkedIn,
    defaultInstagram,
    {
      ...defaultConnections,
      linkedin: {
        ...defaultConnections.linkedin,
        status: "CONNECTED",
        account_name: "Workspace Member",
        account_id: "urn:li:person:synthetic-user",
        granted_scopes: ["w_organization_social"],
        missing_scopes: [],
        access_expires_at: "2026-11-01T00:00:00Z",
      },
    },
  );

  expect(
    await screen.findByText("Connected account: Workspace Member"),
  ).toBeVisible();
  expect(
    screen.getByRole("button", { name: "Reconnect LinkedIn" }),
  ).toBeVisible();
  expect(
    screen.getByRole("button", { name: "Disconnect LinkedIn" }),
  ).toBeVisible();
  expect(document.body.textContent).not.toMatch(
    /synthetic-user|access token|refresh token|ciphertext/i,
  );
});

test("not configured shows practice mode and the missing setup", async () => {
  show({
    mode: "SIMULATED",
    status: "NEEDS_SETUP",
    reasons: ["Access token not configured", "Organizer not configured"],
    organization_name: null,
    organizer_name: null,
    checked_live: false,
  });
  expect(await screen.findAllByText("Needs setup")).toHaveLength(3);
  expect(
    screen.getAllByText("Practice mode · not connected for live publishing"),
  ).toHaveLength(2);
  expect(
    screen.getByText("Practice mode · live account not connected"),
  ).toBeVisible();
  expect(screen.getByText("Organizer not configured")).toBeVisible();
});

test("connected and ready shows the organization without credentials", async () => {
  show({
    mode: "LIVE",
    status: "READY",
    reasons: [],
    organization_name: "Event Marketing",
    organizer_name: "Event Marketing Events",
    checked_live: true,
  });
  expect(await screen.findByText("Connected")).toBeVisible();
  expect(screen.getByText("Ready for live publishing")).toBeVisible();
  expect(
    screen.getByText(/Organization: Event Marketing · Organizer:/),
  ).toBeVisible();
  expect(document.body.textContent).not.toMatch(/token|Bearer|\d{6,}/i);
});

test("needs setup in live mode lists what to fix", async () => {
  show({
    mode: "LIVE",
    status: "NEEDS_SETUP",
    reasons: ["Configured organizer isn't part of this organization"],
    organization_name: "Event Marketing",
    organizer_name: null,
    checked_live: false,
  });
  expect(
    await screen.findByText("Needs setup before live publishing"),
  ).toBeVisible();
  expect(
    screen.getByText("Configured organizer isn't part of this organization"),
  ).toBeVisible();
});

test("an unreachable Eventbrite is not shown as connected", async () => {
  show({
    mode: "LIVE",
    status: "UNAVAILABLE",
    reasons: ["Eventbrite couldn't be reached"],
    organization_name: null,
    organizer_name: null,
    checked_live: false,
  });
  expect(await screen.findByText("Unavailable")).toBeVisible();
  expect(screen.queryByText("Connected")).not.toBeInTheDocument();
});

test("Meetup account readiness is separate from deferred image upload", async () => {
  show(
    {
      mode: "SIMULATED",
      status: "NEEDS_SETUP",
      reasons: ["Access token not configured"],
      organization_name: null,
      organizer_name: null,
      checked_live: false,
    },
    {
      ...defaultMeetup,
      mode: "LIVE",
      status: "READY",
      reasons: [],
      member_name: "Event Marketing",
      network_name: "Example Network",
      group_name: "Example Business Network",
      checked_live: true,
    },
  );
  expect(await screen.findByText("Connected")).toBeVisible();
  expect(
    screen.getByText("Account and group ready for live publishing"),
  ).toBeVisible();
  expect(
    screen.getByText(/Group: Example Business Network · Network:/),
  ).toBeVisible();
  expect(
    screen.getByText("Live image upload awaiting verification"),
  ).toBeVisible();
  expect(document.body.textContent).not.toMatch(/Bearer|private key path|JWT/i);
});

test("Meetup live setup blocker is shown without exposing key configuration", async () => {
  show(
    {
      mode: "SIMULATED",
      status: "READY",
      reasons: [],
      organization_name: "Event Marketing",
      organizer_name: "Event Marketing Events",
      checked_live: true,
    },
    {
      ...defaultMeetup,
      mode: "LIVE",
      reasons: ["Meetup signing key is not readable"],
    },
  );
  expect(
    await screen.findByText("Needs setup before live publishing"),
  ).toBeVisible();
  expect(screen.getByText("Meetup signing key is not readable")).toBeVisible();
  expect(document.body.textContent).not.toMatch(
    /\.pem|private key path|Bearer|JWT/i,
  );
});

test("Meetup owner mismatch is shown as a connection issue", async () => {
  show(
    {
      mode: "SIMULATED",
      status: "READY",
      reasons: [],
      organization_name: "Event Marketing",
      organizer_name: "Event Marketing Events",
      checked_live: true,
    },
    {
      ...defaultMeetup,
      mode: "LIVE",
      status: "UNAVAILABLE",
      reasons: ["Connected as an unexpected Meetup account"],
    },
  );
  expect(await screen.findByText("Connection needs attention")).toBeVisible();
  expect(screen.getByText("Meetup connection needs attention")).toBeVisible();
  expect(
    screen.getByText("Connected as an unexpected Meetup account"),
  ).toBeVisible();
  expect(
    screen.queryByText("Account and group ready for live publishing"),
  ).not.toBeInTheDocument();
});

test("LinkedIn setup shows the access gate without implying an account is connected", async () => {
  show({
    mode: "SIMULATED",
    status: "NEEDS_SETUP",
    reasons: [],
    organization_name: null,
    organizer_name: null,
    checked_live: false,
  });
  expect(
    await screen.findByRole("heading", { name: "LinkedIn organization" }),
  ).toBeVisible();
  expect(screen.getAllByText("Needs setup")).toHaveLength(3);
  expect(
    screen.getByText("Community Management access tier has not been verified."),
  ).toBeVisible();
  expect(
    screen.getByText(
      "LinkedIn Community Management access must be verified in the Developer Portal",
    ),
  ).toBeVisible();
  expect(
    screen.getByText("Organization posting connector implemented."),
  ).toBeVisible();
  expect(document.body.textContent).not.toMatch(
    /client secret|access token|refresh token|Bearer/i,
  );
});

test("LinkedIn shows connected Page and Development access without calling it production ready", async () => {
  show(
    {
      mode: "SIMULATED",
      status: "NEEDS_SETUP",
      reasons: [],
      organization_name: null,
      organizer_name: null,
      checked_live: false,
    },
    defaultMeetup,
    {
      ...defaultLinkedIn,
      status: "NEEDS_SETUP",
      reasons: [
        "Standard Community Management access is required for live publishing",
      ],
      organization_name: "Event Marketing Page",
      connected_member: "Connected member",
      community_access_tier: "DEVELOPMENT",
      checked_live: true,
    },
  );
  expect(
    await screen.findByText("Page connected · new campaigns use practice mode"),
  ).toBeVisible();
  expect(screen.getByText("Page: Event Marketing Page")).toBeVisible();
  expect(
    screen.getByText(
      "Development access is configured; production publishing needs Standard access.",
    ),
  ).toBeVisible();
  expect(
    screen.getByText(
      "Standard Community Management access is required for live publishing",
    ),
  ).toBeVisible();
});

test("LinkedIn connected Page shows live readiness only after role checks pass", async () => {
  show(
    {
      mode: "SIMULATED",
      status: "NEEDS_SETUP",
      reasons: [],
      organization_name: null,
      organizer_name: null,
      checked_live: false,
    },
    defaultMeetup,
    {
      ...defaultLinkedIn,
      mode: "LIVE",
      status: "READY",
      reasons: [],
      organization_name: "Event Marketing Page",
      connected_member: "Connected member",
      community_access_tier: "STANDARD",
      checked_live: true,
    },
  );
  expect(
    await screen.findByText("Page ready for live publishing"),
  ).toBeVisible();
  expect(
    screen.getByText("Standard Community Management access is configured."),
  ).toBeVisible();
});

test("Instagram practice mode is explicit and does not imply a connected account", async () => {
  show({
    mode: "SIMULATED",
    status: "READY",
    reasons: [],
    organization_name: "Event Marketing",
    organizer_name: "Event Marketing Events",
    checked_live: true,
  });
  expect(
    await screen.findByRole("heading", { name: "Instagram" }),
  ).toBeVisible();
  expect(
    screen.getByText("Practice mode · live account not connected"),
  ).toBeVisible();
  expect(screen.getByText("Practice mode")).toBeVisible();
});

test("Instagram live setup identifies access and media delivery blockers safely", async () => {
  show(
    {
      mode: "SIMULATED",
      status: "NEEDS_SETUP",
      reasons: [],
      organization_name: null,
      organizer_name: null,
      checked_live: false,
    },
    defaultMeetup,
    defaultLinkedIn,
    {
      ...defaultInstagram,
      mode: "LIVE",
      status: "NEEDS_SETUP",
      reasons: [
        "Instagram account has not connected this app",
        "Secure media delivery is not configured",
      ],
      media_delivery_ready: false,
    },
  );
  expect(
    await screen.findByText("Needs setup before live publishing"),
  ).toBeVisible();
  expect(
    screen.getByText("Instagram account has not connected this app"),
  ).toBeVisible();
  expect(
    screen.getByText("Secure media delivery is not configured"),
  ).toBeVisible();
  expect(screen.getByText("Secure media delivery: Needs setup")).toBeVisible();
  expect(document.body.textContent).not.toMatch(
    /Bearer\s+\S{12,}|access_token=\S+|[?&]token=\S+/i,
  );
});

test("Instagram readiness shows the professional account without a token", async () => {
  show(
    {
      mode: "SIMULATED",
      status: "NEEDS_SETUP",
      reasons: [],
      organization_name: null,
      organizer_name: null,
      checked_live: false,
    },
    defaultMeetup,
    defaultLinkedIn,
    {
      ...defaultInstagram,
      mode: "LIVE",
      status: "READY",
      reasons: [],
      username: "eventmarketing",
      checked_live: true,
      account_eligible: true,
      media_delivery_ready: true,
    },
  );
  expect(await screen.findByText("Connected")).toBeVisible();
  expect(
    screen.getByText("Professional account ready for live publishing"),
  ).toBeVisible();
  expect(screen.getByText("Account: @eventmarketing")).toBeVisible();
  expect(screen.getByText("Secure media delivery: Ready")).toBeVisible();
});
