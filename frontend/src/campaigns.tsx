import { FormEvent, useCallback, useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import styles from "./App.module.css";
import { api, CampaignError } from "./campaignApi";
import {
  FacebookGroupsSection,
  FrozenGroupList,
  type FrozenGroup,
} from "./facebookGroups";
import { ProgressSteps, StatusLabel } from "./ui";
import { CampaignGraphicSection, GraphicIncluded } from "./campaignGraphic";
import {
  graphicChange,
  graphicChangeText,
  graphicErrorMessage,
  type GraphicAction,
} from "./graphicText";

const surfaceOptions = [
  ["EVENTBRITE", "Event listing copy"],
  ["MEETUP", "Meetup description"],
  ["LINKEDIN_ORGANIZATION", "LinkedIn organization"],
  ["LINKEDIN_PERSONAL", "LinkedIn personal"],
  ["INSTAGRAM", "Instagram"],
  ["FACEBOOK_GROUPS", "Facebook Groups · manual copy"],
] as const;
export type Surface = (typeof surfaceOptions)[number][0];
type Content = {
  marketing_headline: string;
  short_summary: string;
  general_description: string;
  attendee_experience: string | null;
  eventbrite_description: string | null;
  meetup_description: string | null;
  linkedin_organization_post: string | null;
  linkedin_personal_post: string | null;
  instagram_caption: string | null;
  facebook_groups_post: string | null;
  creative_direction: string;
};
export type Version = {
  id: string;
  version_number: number;
  parent_version_id: string | null;
  source_event_revision: number;
  event_snapshot: {
    title: string;
    starts_at: string | null;
    ends_at: string | null;
    timezone: string | null;
    delivery: string | null;
    facts: Record<string, unknown>;
  };
  selected_surfaces: Surface[];
  facebook_group_targets?: FrozenGroup[];
  content: Content;
  revision_request: string | null;
  revision_targets: string[];
  content_hash: string;
  primary_graphic?: {
    asset_id: string;
    role: "PRIMARY_GRAPHIC";
    sha256: string;
    composition_version: string;
    layout_variant: "STANDARD" | "TITLE_LARGER" | "DETAILS_HIGHER";
    mime_type: string;
    width: number;
    height: number;
    preview_url: string;
  } | null;
  created_at: string;
  is_current: boolean;
};
export type Campaign = {
  id: string;
  event_id: string;
  revision: number;
  state:
    | "DRAFT"
    | "GENERATING"
    | "REVIEW"
    | "APPROVED"
    | "EXECUTING"
    | "COMPLETE";
  current_version_id: string | null;
  current_version_number: number | null;
  is_stale: boolean;
  stale_message: string | null;
  event_readiness: {
    ready: boolean;
    missing_fields: string[];
    open_question_count: number;
  };
  current_version: Version | null;
  viewed_version?: Version;
  versions: {
    id: string;
    version_number: number;
    revision_request: string | null;
    revision_targets: string[];
    created_at: string;
    is_current: boolean;
  }[];
};
type PlanEntry = {
  destination_key: string;
  target_key: string;
  ui_label?: string;
  execution_kind: "AUTO" | "MANUAL";
  runtime_mode?: "SIMULATED" | "LIVE";
  approved_content: string;
  creative_direction: string | null;
  group_selection_status: string | null;
  selected_groups?: {
    group_record_id: string;
    display_name: string;
    url: string;
    geography_label: string | null;
    member_count: number | null;
    cautions: string[];
  }[];
  approved_graphic?: {
    asset_id: string;
    sha256: string;
    mime_type: string;
    width: number;
    height: number;
    preview_url: string;
  } | null;
};
type DestinationPlan = {
  schema_version: number;
  targets: PlanEntry[];
  exclusions: { destination_key: string; reason: string }[];
  content_only: { destination_key: string; reason: string }[];
};
export type ApprovalPreview = {
  campaign: Campaign;
  version_id: string;
  version_number: number;
  version_hash: string;
  destination_plan_hash: string;
  destination_plan: DestinationPlan;
  destination_readiness: {
    destination_key: string;
    target_key: string;
    execution_kind: "AUTO" | "MANUAL";
    runtime_mode: "SIMULATED" | "LIVE" | null;
    status: "READY" | "BLOCKED";
    reason: string | null;
  }[];
  replacement_requirements: {
    execution_id: string;
    destination_key: string;
    version_number: number;
    status: string;
    runtime_mode?: "SIMULATED" | "LIVE" | null;
  }[];
  uncertain_risk_requirements: {
    execution_id: string;
    destination_key: string;
    version_number: number;
    status: string;
  }[];
  blocking_unknowns: { execution_id: string; destination_key: string }[];
  can_approve: boolean;
  group_selection_pending?: boolean;
};
type ExecutionAttemptView = {
  attempt_number: number;
  operation: string;
  outcome: string | null;
  finished_at: string | null;
  human_error: string | null;
};
export type DestinationExecutionView = {
  id: string;
  campaign_version_id: string;
  approval_id: string;
  destination_key: string;
  target_key: string;
  execution_kind: "AUTO" | "MANUAL";
  runtime_mode: "SIMULATED" | "LIVE" | null;
  destination_plan_hash: string;
  status: string;
  retryable: boolean;
  verification_status: string;
  result_summary: string | null;
  human_error: string | null;
  external_id?: string | null;
  external_url?: string | null;
  error_category?: string | null;
  manual_package: {
    approved_copy: string;
    event_title: string;
    destination: string;
    group_selection: string;
    selected_groups?: {
      group_record_id: string;
      display_name: string;
      url: string;
      geography_label: string | null;
      member_count: number | null;
      cautions: string[];
    }[];
    creative_status: string;
    graphic_asset_id?: string | null;
    graphic_sha256?: string | null;
    graphic_preview_url?: string | null;
    creative_direction: string | null;
    instructions: string;
    simulation_label: string;
  } | null;
  disposition: { type?: string; occurred_at?: string; note?: string | null };
  attempts: ExecutionAttemptView[];
};
type ApprovalHistory = {
  id: string;
  campaign_version_id: string;
  version_number: number | null;
  approved_at: string;
  active: boolean;
  destination_plan_hash: string;
  destination_plan: DestinationPlan;
};
export type Distribution = {
  campaign: Campaign;
  active_approval: {
    id: string;
    campaign_version_id: string;
    approved_at: string;
  } | null;
  approvals: ApprovalHistory[];
  executions: DestinationExecutionView[];
  summary: {
    state: string;
    outcome: string;
    complete: boolean;
    completed_at: string | null;
    target_count: number;
    succeeded_count: number;
    failed_count: number;
    accounted_count: number;
    unknown_count: number;
    manual_pending_count: number;
    manual_done_count: number;
  };
};
type EventSummary = {
  id: string;
  title: string | null;
  starts_at: string | null;
  timezone: string | null;
  delivery: string | null;
  facts: {
    location: Record<string, string | null>;
    people: Record<string, string | null>;
    registration: Record<string, string | null>;
  };
  readiness: {
    ready: boolean;
    missing_fields: string[];
    open_question_count: number;
  };
};

function messageFor(code: string) {
  const messages: Record<string, string> = {
    AUTH_REQUIRED: "Your session ended. Please sign in again.",
    EVENT_NOT_READY:
      "Finish the open Event details before creating campaign copy.",
    CAMPAIGN_NOT_FOUND: "This Event does not have a campaign yet.",
    CAMPAIGN_GENERATION_FAILED:
      "Campaign generation didn’t finish. Your Event and current campaign version are unchanged. Check the Event details and try again.",
    CAMPAIGN_COPY_NEEDS_EVENT_REVIEW:
      "The draft included wording that didn’t match confirmed Event details. Review the Event facts, then try again.",
    CAMPAIGN_AI_RESPONSE_INVALID:
      "The draft couldn’t be checked safely. Your current version is unchanged; try generating it once more.",
    CAMPAIGN_AI_RATE_LIMITED:
      "OpenAI is temporarily rate-limiting campaign requests. Wait a little, then try again.",
    CAMPAIGN_AI_TIMEOUT:
      "Campaign generation took too long. Your current version is unchanged; try again in a moment.",
    CAMPAIGN_REVISION_FAILED:
      "Campaign revision didn’t finish. Your current version is unchanged.",
    REVISION_NOT_SUPPORTED:
      "That revision request isn’t supported in this demonstration. Try a focused tone or length change.",
    EVENT_CORRECTION_REQUIRED:
      "That changes an Event fact. Update the Event first, then refresh this campaign.",
    CAMPAIGN_STALE_REGENERATE_FIRST:
      "Refresh the campaign from the updated Event before revising its copy.",
    CAMPAIGN_REVISION_CONFLICT:
      "The campaign changed in another view. Refresh and try again.",
    GRAPHIC_GENERATION_FAILED:
      "The graphic couldn’t be generated. Your current campaign version and approval are unchanged.",
    GRAPHIC_OUTPUT_INVALID:
      "The graphic result could not be safely opened. Your current version is unchanged.",
    GRAPHIC_EVENT_DETAILS_REQUIRED:
      "Finish the Event title, schedule, and delivery details before adding a graphic.",
    GRAPHIC_REVISION_REQUEST_REQUIRED:
      "Tell me what visual change you would like before revising this graphic.",
    GRAPHIC_REVISION_NOT_AVAILABLE:
      "Generate the first graphic before asking for a visual revision.",
    GRAPHIC_LAYOUT_REVISION_NOT_SUPPORTED:
      "That layout adjustment isn’t supported yet. Try a visual change such as warmer colors or a calmer background.",
    GRAPHIC_TEXT_DOES_NOT_FIT:
      "The Event text is too long for this graphic layout. Shorten the Event title or use a different format.",
    CAMPAIGN_NOT_STALE:
      "The Event has not changed since this campaign was created.",
    CAMPAIGN_INPUTS_CHANGED:
      "The Event changed while the campaign was being prepared. Refresh and try again.",
    CAMPAIGN_VERSION_NOT_CURRENT:
      "Only the current campaign version can be approved.",
    APPROVAL_PLAN_CHANGED:
      "The distribution plan changed. Review the updated destinations and approve again.",
    APPROVAL_SUPERSEDED_BY_EVENT_CHANGE:
      "This approval was superseded by a material Event change. Create a new version before continuing.",
    PREVIOUS_UNKNOWN_REQUIRES_RECONCILIATION:
      "Resolve the earlier uncertain result before approving another version.",
    REPLACEMENT_CONFIRMATION_REQUIRED:
      "Confirm the listed earlier destination actions before approving this replacement version.",
    EXECUTION_UNRESOLVED_BEFORE_REVISION:
      "Resolve the uncertain or in-progress destination action before creating another version.",
  };
  return messages[code] ?? "We couldn’t save that change. Please try again.";
}

function sentenceCase(value: string | null | undefined) {
  if (!value) return "Not set";
  const text = value.replaceAll("_", " ").toLowerCase();
  return text.charAt(0).toUpperCase() + text.slice(1);
}

function dateLabel(value: string | null, timezone: string | null) {
  if (!value) return "Not set";
  const date = new Date(value);
  return new Intl.DateTimeFormat("en-US", {
    dateStyle: "full",
    timeStyle: "short",
    timeZone: timezone || "UTC",
  }).format(date);
}

export function CampaignReview({
  csrf,
  onUnauthorized,
}: {
  csrf: string;
  onUnauthorized: () => void;
}) {
  const { eventId = "" } = useParams();
  const [event, setEvent] = useState<EventSummary>();
  const [campaign, setCampaign] = useState<Campaign>();
  const [viewed, setViewed] = useState<Version>();
  const [distribution, setDistribution] = useState<Distribution>();
  const [notice, setNotice] = useState("");
  const [selected, setSelected] = useState<Surface[]>(
    surfaceOptions.map(([key]) => key),
  );
  const [revisionRequest, setRevisionRequest] = useState("");
  const [graphicRequest, setGraphicRequest] = useState("");
  const [graphicPending, setGraphicPending] = useState<GraphicAction | null>(
    null,
  );
  const [graphicError, setGraphicError] = useState({ message: "", code: "" });
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  const load = useCallback(async () => {
    try {
      const loadedEvent = await api<EventSummary>(
        `/api/events/${eventId}`,
        csrf,
      );
      setEvent(loadedEvent);
      try {
        const loadedCampaign = await api<Campaign>(
          `/api/events/${eventId}/campaign`,
          csrf,
        );
        setCampaign(loadedCampaign);
        setViewed(loadedCampaign.current_version ?? undefined);
      } catch (reason) {
        if (
          reason instanceof CampaignError &&
          reason.code === "CAMPAIGN_NOT_FOUND"
        ) {
          setCampaign(undefined);
          setViewed(undefined);
        } else throw reason;
      }
      setError("");
    } catch (reason) {
      if (reason instanceof CampaignError && reason.code === "AUTH_REQUIRED")
        onUnauthorized();
      setError(
        messageFor(
          reason instanceof CampaignError ? reason.code : "REQUEST_FAILED",
        ),
      );
    } finally {
      setLoading(false);
    }
  }, [csrf, eventId, onUnauthorized]);

  useEffect(() => {
    const timer = window.setTimeout(() => {
      void load();
    }, 0);
    return () => window.clearTimeout(timer);
  }, [load]);

  const perform = async (
    action: () => Promise<Campaign>,
    graphicAction?: GraphicAction,
  ) => {
    setBusy(true);
    setError("");
    setNotice("");
    setGraphicError({ message: "", code: "" });
    setGraphicPending(graphicAction ?? null);
    const previousNumber = campaign?.current_version_number ?? 0;
    const previousWasApproved = Boolean(
      campaign &&
      distribution?.active_approval?.campaign_version_id ===
        campaign.current_version_id,
    );
    try {
      const result = await action();
      setCampaign(result);
      setViewed(result.current_version ?? undefined);
      const next = result.current_version;
      if (graphicAction) setGraphicRequest("");
      else setRevisionRequest("");
      if (next && previousNumber > 0 && next.version_number > previousNumber) {
        const change = graphicChange(next);
        const approvalNote = previousWasApproved
          ? ` Version ${previousNumber} stays approved; this new version needs its own approval.`
          : "";
        setNotice(
          change
            ? `${graphicChangeText(change, next.version_number)}${approvalNote}`
            : `Version ${next.version_number} is ready${
                next.revision_request ? ` — “${next.revision_request}”` : ""
              }. Updated sections are marked below.${approvalNote}`,
        );
      }
    } catch (reason) {
      if (reason instanceof CampaignError && reason.code === "AUTH_REQUIRED")
        onUnauthorized();
      const code =
        reason instanceof CampaignError ? reason.code : "REQUEST_FAILED";
      if (graphicAction && code !== "AUTH_REQUIRED")
        setGraphicError({
          code,
          message: graphicErrorMessage(code) ?? messageFor(code),
        });
      else setError(messageFor(code));
    } finally {
      setBusy(false);
      setGraphicPending(null);
    }
  };

  const toggleSurface = (surface: Surface) => {
    setSelected((previous) =>
      previous.includes(surface)
        ? previous.filter((item) => item !== surface)
        : surfaceOptions
            .map(([key]) => key)
            .filter((item) => previous.includes(item) || item === surface),
    );
  };

  const refreshCampaign = async () => {
    if (!campaign) return;
    try {
      setCampaign(await api<Campaign>(`/api/campaigns/${campaign.id}`, csrf));
    } catch (reason) {
      if (reason instanceof CampaignError && reason.code === "AUTH_REQUIRED")
        onUnauthorized();
    }
  };

  const submitRevision = (formEvent: FormEvent) => {
    formEvent.preventDefault();
    if (!campaign || !revisionRequest.trim()) return;
    void perform(() =>
      api<Campaign>(`/api/campaigns/${campaign.id}/revisions`, csrf, "POST", {
        expected_revision: campaign.revision,
        revision_request: revisionRequest,
      }),
    );
  };

  const submitGraphic = (formEvent: FormEvent) => {
    formEvent.preventDefault();
    const shownVersion = viewed ?? campaign?.current_version;
    if (!campaign || !shownVersion || !shownVersion.is_current) return;
    void perform(
      () =>
        api<Campaign>(`/api/campaigns/${campaign.id}/graphics`, csrf, "POST", {
          expected_revision: campaign.revision,
          ...(shownVersion.primary_graphic
            ? { instruction: graphicRequest.trim() }
            : {}),
        }),
      shownVersion.primary_graphic ? "revise" : "generate",
    );
  };

  const openVersion = async (number: number) => {
    if (!campaign) return;
    setBusy(true);
    setError("");
    setNotice("");
    setGraphicError({ message: "", code: "" });
    try {
      const result = await api<Campaign>(
        `/api/campaigns/${campaign.id}/versions/${number}`,
        csrf,
      );
      setViewed(result.viewed_version);
    } catch (reason) {
      if (reason instanceof CampaignError && reason.code === "AUTH_REQUIRED")
        onUnauthorized();
      setError(
        messageFor(
          reason instanceof CampaignError ? reason.code : "REQUEST_FAILED",
        ),
      );
    } finally {
      setBusy(false);
    }
  };

  if (loading)
    return (
      <p role="status" className={styles.status}>
        Opening the campaign review…
      </p>
    );
  if (!event)
    return (
      <div className={styles.eventPage}>
        <p role="alert" className={styles.inlineError}>
          {error || "This Event could not be found."}
        </p>
        <Link to="/events" className={styles.backLink}>
          ← All Events
        </Link>
      </div>
    );

  const version = viewed ?? campaign?.current_version ?? undefined;
  const historical = Boolean(version && !version.is_current);
  const facts = version?.event_snapshot.facts ?? {};
  const location = facts.location as Record<string, string | null> | undefined;
  const people = facts.people as Record<string, string | null> | undefined;
  const registration = facts.registration as
    | Record<string, string | null>
    | undefined;
  const details = [
    [
      "When",
      dateLabel(
        version?.event_snapshot.starts_at ?? event.starts_at,
        version?.event_snapshot.timezone ?? event.timezone,
      ),
    ],
    [
      "Format",
      sentenceCase(version?.event_snapshot.delivery ?? event.delivery),
    ],
    [
      "Where",
      location?.venue ||
        location?.address ||
        location?.join_instructions ||
        location?.join_url ||
        "Not set",
    ],
    ["Host", people?.host || "Not set"],
    ["Admission", sentenceCase(registration?.price_kind)],
  ].filter(([, value]) => value !== "Not set");

  const approvals = distribution?.approvals ?? [];
  const activeApproval = distribution?.active_approval;
  const approvedCurrent = Boolean(
    campaign &&
    !campaign.is_stale &&
    activeApproval?.campaign_version_id === campaign.current_version_id,
  );
  const distributing = Boolean(
    approvedCurrent &&
    distribution?.executions.some(
      (execution) => execution.approval_id === activeApproval?.id,
    ),
  );
  const changed = new Set(
    version && version.version_number > 1 ? version.revision_targets : [],
  );
  const changedLabel = version
    ? `Updated in Version ${version.version_number}`
    : "";
  const viewedApproval = version
    ? approvals.find((approval) => approval.campaign_version_id === version.id)
    : undefined;

  return (
    <div className={`${styles.eventPage} ${styles.campaignPage}`}>
      <Link to={`/events/${eventId}`} className={styles.backLink}>
        ← Event details
      </Link>
      <p className={styles.eyebrow}>Campaign review</p>
      <div className={styles.pageHeadingRow}>
        <div>
          <h1>{event.title || "Event campaign"}</h1>
          <p className={styles.eventLead}>
            Is this campaign right? Read what each destination will say, ask for
            changes, then approve the exact version you want sent.
          </p>
        </div>
        {version && (
          <span className={styles.eventBadge}>
            Version {version.version_number}
            {version.is_current ? " · Current" : " · History"}
          </span>
        )}
      </div>
      <ProgressSteps
        current={approvedCurrent ? 3 : 1}
        complete={distributing && Boolean(distribution?.summary.complete)}
      />
      {error && (
        <p role="alert" className={styles.inlineError}>
          {error}
        </p>
      )}
      {notice && (
        <p role="status" className={styles.executionNotice}>
          {notice}
        </p>
      )}
      {campaign && version && historical && (
        <div className={styles.historyBanner} role="status">
          <p>
            <strong>
              You’re viewing Version {version.version_number} (read-only).
            </strong>{" "}
            Earlier versions can’t be changed or approved.
          </p>
          <button
            className={styles.secondaryButton}
            type="button"
            onClick={() => setViewed(campaign.current_version ?? undefined)}
          >
            Return to current version
          </button>
        </div>
      )}
      {campaign && version && (
        <nav className={styles.sectionNav} aria-label="Campaign sections">
          <a href="#event-facts-heading">Event facts</a>
          <a href="#creative-direction-heading">Graphic</a>
          <a href="#listing-copy-heading">Event listing</a>
          <a href="#social-copy-heading">Social copy</a>
          {version.selected_surfaces.includes("FACEBOOK_GROUPS") && (
            <a href="#facebook-groups-heading">Facebook Groups</a>
          )}
          {!campaign.is_stale && !historical && (
            <a href="#campaign-revision-section">Change wording</a>
          )}
          <a href="#execution-heading">Approval & distribution</a>
          <a href="#history-heading">Version history</a>
        </nav>
      )}
      {campaign?.is_stale && (
        <section className={styles.campaignStale} role="status">
          <strong>Event details changed</strong>
          <p>
            {campaign.stale_message ||
              "Refresh this campaign before using it as current review content."}
          </p>
          <button
            className={styles.eventPrimary}
            disabled={busy || !campaign.event_readiness.ready}
            onClick={() =>
              void perform(() =>
                api<Campaign>(
                  `/api/campaigns/${campaign.id}/regenerate`,
                  csrf,
                  "POST",
                  { expected_revision: campaign.revision },
                ),
              )
            }
          >
            {busy
              ? "Creating the updated version…"
              : "Create an updated version"}
          </button>
          {!campaign.event_readiness.ready && (
            <p className={styles.helperText}>
              Resolve the open Event details first.
            </p>
          )}
        </section>
      )}

      {!campaign && (
        <section
          className={styles.intakeCard}
          aria-labelledby="generate-heading"
        >
          <p className={styles.eyebrow}>Start a review draft</p>
          <h2 id="generate-heading">Choose the copy to prepare.</h2>
          <p className={styles.helperText}>
            I’ll write copy for each place you choose, using the Event facts.
            Facebook Groups stay manual, and nothing is sent until you approve.
          </p>
          <div className={styles.surfaceChoices}>
            {surfaceOptions.map(([key, label]) => (
              <label key={key}>
                <input
                  type="checkbox"
                  checked={selected.includes(key)}
                  onChange={() => toggleSurface(key)}
                />
                <span>{label}</span>
              </label>
            ))}
          </div>
          {!event.readiness.ready && (
            <p className={styles.inlineError} role="status">
              Finish the Event details before creating campaign copy.
              <Link to={`/events/${eventId}`}> Return to Event details.</Link>
            </p>
          )}
          <button
            className={styles.eventPrimary}
            disabled={busy || !event.readiness.ready || selected.length === 0}
            onClick={() =>
              void perform(() =>
                api<Campaign>(`/api/events/${eventId}/campaign`, csrf, "POST", {
                  selected_surfaces: selected,
                }),
              )
            }
          >
            {busy ? "Creating your campaign…" : "Create campaign draft"}
          </button>
        </section>
      )}

      {campaign && version && (
        <>
          <section
            className={styles.summaryCard}
            aria-labelledby="event-facts-heading"
          >
            <div className={styles.summaryHeading}>
              <div>
                <p className={styles.eyebrow}>Shared Event facts</p>
                <h2 id="event-facts-heading">The facts behind this copy.</h2>
              </div>
              <Link
                className={styles.secondaryButton}
                to={`/events/${eventId}`}
              >
                Update Event details
              </Link>
            </div>
            <dl className={styles.factGrid}>
              <div>
                <dt>Event name</dt>
                <dd>{version.event_snapshot.title}</dd>
              </div>
              {details.map(([label, value]) => (
                <div key={label}>
                  <dt>{label}</dt>
                  <dd>{value}</dd>
                </div>
              ))}
              {registration?.amount_minor != null && (
                <div>
                  <dt>Price</dt>
                  <dd>
                    {registration.currency || ""}{" "}
                    {Number(registration.amount_minor) / 100}
                  </dd>
                </div>
              )}
            </dl>
            {historical && (
              <p className={styles.helperText}>
                This saved version is read-only. Return to the current version
                to request a revision.
              </p>
            )}
          </section>

          <CampaignGraphicSection
            eventId={eventId}
            title={version.event_snapshot.title}
            versionNumber={version.version_number}
            graphic={version.primary_graphic}
            change={version.version_number > 1 ? graphicChange(version) : null}
            approval={
              viewedApproval
                ? viewedApproval.active && !campaign.is_stale
                  ? "active"
                  : "previous"
                : null
            }
            historical={historical}
            stale={campaign.is_stale}
            pending={graphicPending}
            busy={busy}
            error={graphicError.message}
            showEventLink={graphicError.code === "EVENT_CORRECTION_REQUIRED"}
            request={graphicRequest}
            onRequestChange={setGraphicRequest}
            onSubmit={submitGraphic}
            brief={
              <ContentBlock
                title="Visual brief"
                text={version.content.creative_direction}
                changed={
                  changed.has("creative_direction") ? changedLabel : undefined
                }
              />
            }
          />

          <section
            className={styles.reviewCard}
            aria-labelledby="listing-copy-heading"
          >
            <p className={styles.eyebrow}>Event listing</p>
            <h2 id="listing-copy-heading">Shared description</h2>
            <div className={styles.campaignContentGrid}>
              <ContentBlock
                title="Headline"
                text={version.content.marketing_headline}
                changed={
                  changed.has("marketing_headline") ? changedLabel : undefined
                }
              />
              <ContentBlock
                title="Short summary"
                text={version.content.short_summary}
                changed={
                  changed.has("short_summary") ? changedLabel : undefined
                }
              />
              <ContentBlock
                title="General description"
                text={version.content.general_description}
                changed={
                  changed.has("general_description") ? changedLabel : undefined
                }
                wide
              />
              {version.content.attendee_experience && (
                <ContentBlock
                  title="Attendee experience"
                  text={version.content.attendee_experience}
                  changed={
                    changed.has("attendee_experience")
                      ? changedLabel
                      : undefined
                  }
                  wide
                />
              )}
              {version.content.eventbrite_description && (
                <ContentBlock
                  title="Eventbrite description"
                  text={version.content.eventbrite_description}
                  changed={
                    changed.has("eventbrite_description")
                      ? changedLabel
                      : undefined
                  }
                />
              )}
              {version.content.meetup_description && (
                <ContentBlock
                  title="Meetup description"
                  text={version.content.meetup_description}
                  changed={
                    changed.has("meetup_description") ? changedLabel : undefined
                  }
                />
              )}
            </div>
          </section>

          <section
            className={styles.reviewCard}
            aria-labelledby="social-copy-heading"
          >
            <p className={styles.eyebrow}>Social copy</p>
            <h2 id="social-copy-heading">Review each voice.</h2>
            <div className={styles.campaignContentGrid}>
              {version.content.linkedin_organization_post && (
                <ContentBlock
                  title="LinkedIn · organization"
                  text={version.content.linkedin_organization_post}
                  changed={
                    changed.has("linkedin_organization_post")
                      ? changedLabel
                      : undefined
                  }
                />
              )}
              {version.content.linkedin_personal_post && (
                <ContentBlock
                  title="LinkedIn · personal"
                  text={version.content.linkedin_personal_post}
                  changed={
                    changed.has("linkedin_personal_post")
                      ? changedLabel
                      : undefined
                  }
                />
              )}
              {version.content.instagram_caption && (
                <ContentBlock
                  title="Instagram"
                  text={version.content.instagram_caption}
                  changed={
                    changed.has("instagram_caption") ? changedLabel : undefined
                  }
                />
              )}
              {version.content.facebook_groups_post && (
                <ContentBlock
                  title="Facebook Groups · manual copy"
                  text={version.content.facebook_groups_post}
                  changed={
                    changed.has("facebook_groups_post")
                      ? changedLabel
                      : undefined
                  }
                />
              )}
            </div>
          </section>

          {version.selected_surfaces.includes("FACEBOOK_GROUPS") && (
            <FacebookGroupsSection
              key={version.id}
              campaignId={campaign.id}
              campaignRevision={campaign.revision}
              versionNumber={version.version_number}
              frozenGroups={version.facebook_group_targets ?? []}
              historical={historical}
              stale={campaign.is_stale}
              changedLabel={
                changed.has("facebook_group_targets") ? changedLabel : undefined
              }
              csrf={csrf}
              onUnauthorized={onUnauthorized}
              onDecision={() => void refreshCampaign()}
              onSaved={(result) => {
                const saved = result as Campaign;
                setCampaign(saved);
                setViewed(saved.current_version ?? undefined);
                setNotice(
                  `Facebook Groups saved in Version ${saved.current_version_number}. Approve this version when it looks right.`,
                );
              }}
            />
          )}

          {campaign.is_stale && (
            <p className={styles.helperText}>
              Changes are paused until the campaign is refreshed from the
              updated Event.
            </p>
          )}
          {!campaign.is_stale && !historical && (
            <form
              className={styles.revisionCard}
              id="campaign-revision-section"
              onSubmit={submitRevision}
            >
              <p className={styles.eyebrow}>
                Change the wording · Version {version.version_number} stays
                saved
              </p>
              <label htmlFor="campaign-revision">
                What would you like to change?
              </label>
              <textarea
                id="campaign-revision"
                value={revisionRequest}
                onChange={(eventChange) =>
                  setRevisionRequest(eventChange.target.value)
                }
                maxLength={500}
                placeholder="For example: Make LinkedIn warmer, or shorten the Eventbrite description."
              />
              <p className={styles.helperText}>
                This changes campaign text. Each change creates a new version.
                To change the picture, use the graphic section above; to change
                the date, place, or price, update Event details.
              </p>
              <button
                className={styles.eventPrimary}
                type="submit"
                disabled={busy || revisionRequest.trim().length < 3}
              >
                {busy && !graphicPending
                  ? "Updating the campaign…"
                  : "Create revised version"}
              </button>
            </form>
          )}

          <ExecutionPanel
            campaign={campaign}
            selectedVersionId={version.id}
            csrf={csrf}
            onUnauthorized={onUnauthorized}
            onCampaignUpdate={setCampaign}
            onDistributionChange={setDistribution}
          />

          <section
            className={styles.historyCard}
            aria-labelledby="history-heading"
          >
            <p className={styles.eyebrow}>Version history</p>
            <h2 id="history-heading">Every version is kept.</h2>
            <ol className={styles.versionHistory}>
              {campaign.versions
                .slice()
                .reverse()
                .map((item) => {
                  const versionApproval = approvals.find(
                    (approval) => approval.campaign_version_id === item.id,
                  );
                  return (
                    <li key={item.id}>
                      <button
                        type="button"
                        className={styles.versionButton}
                        onClick={() => void openVersion(item.version_number)}
                        disabled={busy}
                        aria-current={
                          version.version_number === item.version_number
                            ? "true"
                            : undefined
                        }
                      >
                        <strong>
                          Version {item.version_number}
                          {item.is_current ? " · Current" : ""}
                        </strong>
                        {versionApproval ? (
                          <span className={styles.versionApproval}>
                            {versionApproval.active
                              ? "Approved for distribution"
                              : "Previously approved"}
                          </span>
                        ) : (
                          item.is_current &&
                          !campaign.is_stale && (
                            <span className={styles.versionNeedsApproval}>
                              Needs approval
                            </span>
                          )
                        )}
                        <span>
                          {graphicChange(item) === "added"
                            ? "Graphic added"
                            : graphicChange(item) === "updated"
                              ? `Graphic change: “${item.revision_request}”`
                              : item.revision_request || "Initial campaign"}
                        </span>
                        <span className={styles.versionMeta}>
                          {readableTime(item.created_at)}
                          {version.version_number === item.version_number
                            ? " · Viewing now"
                            : ""}
                        </span>
                      </button>
                    </li>
                  );
                })}
            </ol>
            <p className={styles.helperText}>
              Older versions stay available to read and cannot be changed.
              Approval belongs to one exact version; a newer version needs its
              own approval.
            </p>
          </section>
          <p className={styles.revisionNote}>
            Version {version.version_number} · written from Event details saved
            as revision {version.source_event_revision}.
          </p>
        </>
      )}
    </div>
  );
}

function ContentBlock({
  title,
  text,
  wide = false,
  changed,
}: {
  title: string;
  text: string;
  wide?: boolean;
  changed?: string;
}) {
  return (
    <article
      className={`${styles.contentBlock} ${wide ? styles.contentBlockWide : ""}`}
      data-changed={changed ? true : undefined}
    >
      {changed && <span className={styles.changedTag}>{changed}</span>}
      <h3>{title}</h3>
      <p>{text}</p>
    </article>
  );
}

const destinationLabels: Record<string, string> = {
  EVENTBRITE: "Eventbrite",
  MEETUP: "Meetup",
  LINKEDIN_ORGANIZATION: "LinkedIn organization",
  LINKEDIN_PERSONAL: "LinkedIn personal",
  INSTAGRAM: "Instagram",
  FACEBOOK_GROUPS: "Facebook Groups",
};

function destinationStatus(
  status: string,
  disposition?: string,
  retryable?: boolean,
  runtimeMode?: "SIMULATED" | "LIVE" | null,
) {
  if (status === "SUCCEEDED")
    return runtimeMode === "LIVE" ? "Published" : "Published (Simulated)";
  if (status === "FAILED" && disposition === "ACKNOWLEDGED_FAILURE")
    return "Didn’t complete · accounted for";
  if (status === "BLOCKED" && disposition === "ACKNOWLEDGED_FAILURE")
    return "Blocked · accounted for";
  if (status === "FAILED") return retryable ? "Needs retry" : "Didn’t complete";
  if (status === "UNKNOWN") return "Result uncertain — verify before retrying";
  if (status === "MANUAL_PENDING" && disposition === "MANUAL_HANDOFF")
    return "Package handed off — posting remains unverified";
  if (status === "MANUAL_PENDING") return "Manual action required";
  if (status === "MANUAL_DONE") return "Marked complete by the operator";
  if (status === "SKIPPED") return "Intentionally skipped";
  if (status === "BLOCKED") return "Blocked";
  if (status === "RUNNING") return "In progress";
  return "Ready to start";
}

const readinessText: Record<string, string> = {
  EVENTBRITE_TOKEN_MISSING: "Eventbrite isn’t connected yet. See Settings.",
  EVENTBRITE_ORGANIZATION_MISSING:
    "Eventbrite organization isn’t set up. See Settings.",
  EVENTBRITE_ORGANIZER_MISSING:
    "Eventbrite organizer isn’t set up. See Settings.",
  EVENTBRITE_CURRENCY_MISSING:
    "Eventbrite account currency isn’t set up. See Settings.",
  EVENTBRITE_ACCOUNT_CHANGED:
    "The connected Eventbrite account changed. Review and approve again.",
  EVENTBRITE_ADDRESS_REQUIRED:
    "Eventbrite needs a street address like “123 Main St, Aurora, CO 80012”. Add it in Event details.",
  EVENTBRITE_VENUE_NAME_REQUIRED:
    "Eventbrite needs the venue name. Add it in Event details.",
  EVENTBRITE_CAPACITY_REQUIRED:
    "Eventbrite tickets need an attendee capacity. Add it in Event details.",
  EVENTBRITE_PAID_TICKETS_UNSUPPORTED:
    "Paid Eventbrite tickets need an approved price and payout setup, which aren’t available yet.",
  EVENTBRITE_PRICE_UNKNOWN:
    "Eventbrite needs to know whether the Event is free or paid. Add it in Event details.",
  EVENTBRITE_SCHEDULE_REQUIRED:
    "Eventbrite needs a start time, end time and time zone. Add them in Event details.",
  EVENTBRITE_END_BEFORE_START:
    "The Event ends before it starts. Fix it in Event details.",
  MEETUP_CLIENT_KEY_MISSING: "Meetup isn’t set up yet. See Settings.",
  MEETUP_MEMBER_ID_MISSING:
    "The authorized Meetup account isn’t set up. See Settings.",
  MEETUP_SIGNING_KEY_ID_MISSING:
    "The Meetup signing key ID isn’t set up. See Settings.",
  MEETUP_PRIVATE_KEY_MISSING:
    "The Meetup signing key isn’t set up. See Settings.",
  MEETUP_SIGNING_KEY_UNREADABLE:
    "The Meetup signing key needs attention. See Settings.",
  MEETUP_NETWORK_MISSING: "The Meetup Pro Network isn’t set up. See Settings.",
  MEETUP_GROUP_MISSING: "The Meetup Group isn’t set up. See Settings.",
  MEETUP_ACCOUNT_CHANGED:
    "The authorized Meetup account changed. Review the destination plan.",
  MEETUP_ACCOUNT_MISMATCH:
    "Meetup is connected as an unexpected account. See Settings.",
  MEETUP_NETWORK_UNAVAILABLE:
    "The approved Meetup Pro Network isn’t available.",
  MEETUP_GROUP_UNAVAILABLE: "The approved Meetup Group isn’t available.",
  MEETUP_GROUP_NETWORK_MISMATCH:
    "The approved Meetup Group isn’t in the selected Network.",
  MEETUP_GRAPHIC_UPLOAD_UNVERIFIED:
    "Meetup graphic upload is not yet verified for live publishing. This approved graphic can’t be left out.",
  MEETUP_TITLE_TOO_LONG: "Meetup Event titles must be 80 characters or fewer.",
  MEETUP_DESCRIPTION_TOO_LONG:
    "Meetup’s description limit was reached. Revise the approved copy before publishing.",
  MEETUP_SCHEDULE_INVALID:
    "The Event date and time couldn’t be converted safely for Meetup. Check the time zone.",
  MEETUP_EVENT_TYPE_UNVERIFIED:
    "Meetup live publishing currently supports in-person Events only.",
  MEETUP_VENUE_UNAVAILABLE:
    "A matching Meetup Group venue couldn’t be verified. Nothing was changed.",
  MEETUP_LOCATION_REQUIRED:
    "Meetup needs a confirmed in-person venue and address.",
  MEETUP_SCHEDULE_REQUIRED:
    "Meetup needs a start and end time with a time zone.",
  MEETUP_DURATION_REQUIRED:
    "Meetup needs a known end time to calculate duration.",
  MEETUP_PAID_EVENT_UNSUPPORTED:
    "Paid Meetup events need verified payment setup. Nothing was changed to free.",
  MEETUP_PRICE_UNKNOWN:
    "Confirm whether this Event is free before Meetup publishing.",
  MEETUP_GROUP_CHANGE_UNSUPPORTED:
    "This Event is already linked to a different Meetup Group and can’t be moved automatically.",
  INSTAGRAM_ALREADY_PUBLISHED_IMMUTABLE:
    "This Event already has a published Instagram post. Automatic replacement isn’t supported.",
  INSTAGRAM_TARGET_CHANGED:
    "The published Instagram post belongs to a different account. Review the destination setup.",
  LINKEDIN_MEDIA_REPLACEMENT_UNSUPPORTED:
    "LinkedIn can’t replace the graphic on this published post. This destination needs to be accounted for.",
  LINKEDIN_ORGANIZATION_CHANGED:
    "The published LinkedIn post belongs to a different Page. Review the destination setup.",
  MEETUP_BOUND_EVENT_NOT_FOUND:
    "The linked Meetup Event wasn’t found. A duplicate was not created.",
  MEETUP_EVENT_STATE_UNSUPPORTED:
    "The existing Meetup Event is in a state this workflow can’t update safely.",
  APPROVED_CONNECTOR_MODE_UNAVAILABLE:
    "Live publishing for this destination isn’t available right now.",
};

function readinessMessage(reason: string | null | undefined) {
  return (
    (reason && readinessText[reason]) ??
    "Needs setup before this destination can be approved."
  );
}

function isVerifiedLiveDestinationLink(execution: DestinationExecutionView) {
  const url = execution.external_url ?? "";
  const safeHost =
    (execution.destination_key === "EVENTBRITE" &&
      /^https:\/\/(www\.)?eventbrite\.[a-z.]+\//i.test(url)) ||
    (execution.destination_key === "MEETUP" &&
      /^https:\/\/(www\.)?meetup\.com\/[A-Za-z0-9_-]+\/events\/[A-Za-z0-9_-]+\/?$/i.test(
        url,
      )) ||
    (execution.destination_key === "INSTAGRAM" &&
      /^https:\/\/(www\.)?instagram\.com\/p\/[A-Za-z0-9_-]+\/?$/i.test(url));
  return Boolean(
    safeHost &&
    execution.runtime_mode === "LIVE" &&
    execution.status === "SUCCEEDED" &&
    execution.verification_status === "VERIFIED" &&
    execution.external_url,
  );
}

function destinationPlanSummary(plan: DestinationPlan): string {
  return Array.from(
    new Set(
      plan.targets.map((target) =>
        target.execution_kind === "MANUAL"
          ? "Manual"
          : target.runtime_mode === "LIVE"
            ? "Live publishing"
            : "Practice mode",
      ),
    ),
  ).join(" · ");
}

function statusTone(status: string) {
  if (status === "SUCCEEDED" || status === "MANUAL_DONE") return "success";
  if (status === "FAILED" || status === "BLOCKED") return "danger";
  if (status === "UNKNOWN") return "warning";
  if (status === "RUNNING") return "progress";
  if (status === "MANUAL_PENDING") return "info";
  return "neutral";
}

function statusExplanation(execution: DestinationExecutionView) {
  const name =
    destinationLabels[execution.destination_key] ?? execution.destination_key;
  if (
    execution.status === "BLOCKED" &&
    execution.runtime_mode === "LIVE" &&
    execution.error_category === "APPROVED_CONNECTOR_MODE_UNAVAILABLE"
  )
    return `${name} was approved to publish live, but live publishing isn’t available right now. Nothing was sent, and it wasn’t switched to practice mode.`;
  if (execution.status === "UNKNOWN")
    return `We couldn’t verify whether ${name} completed. Verify it before trying again so nothing is posted twice.`;
  if (execution.status === "FAILED")
    return `${name} didn’t finish. The other destinations were not affected.`;
  return null;
}

function summaryParts(summary: Distribution["summary"]) {
  return [
    [summary.succeeded_count, "published"],
    [summary.manual_pending_count, "waiting on you"],
    [summary.manual_done_count, "done by hand"],
    [summary.failed_count, "didn’t finish"],
    [summary.accounted_count, "accounted for"],
    [summary.unknown_count, "uncertain"],
  ]
    .filter(([count]) => Number(count) > 0)
    .map(([count, label]) => `${count} ${label}`)
    .join(" · ");
}

function outcomeLabel(outcome: string) {
  const labels: Record<string, string> = {
    NEEDS_APPROVAL: "Awaiting approval",
    APPROVED_NOT_STARTED: "Approved · execution can be resumed",
    ACTION_REQUIRED: "Action required",
    UNRESOLVED_UNKNOWN: "Unresolved · a destination result is uncertain",
    COMPLETED_SUCCESSFULLY: "Completed successfully",
    COMPLETED_WITH_MANUAL_ACTIONS: "Completed with manual actions",
    COMPLETED_WITH_ACKNOWLEDGED_FAILURES:
      "Completed with acknowledged failures",
    COMPLETED_WITH_ACKNOWLEDGED_UNCERTAINTY:
      "Completed with acknowledged uncertainty",
  };
  return labels[outcome] ?? outcome.replaceAll("_", " ").toLowerCase();
}

function readableTime(value: string) {
  return new Intl.DateTimeFormat("en-US", {
    dateStyle: "medium",
    timeStyle: "short",
  }).format(new Date(value));
}

function ExecutionPanel({
  campaign,
  selectedVersionId,
  csrf,
  onUnauthorized,
  onCampaignUpdate,
  onDistributionChange,
}: {
  campaign: Campaign;
  selectedVersionId: string;
  csrf: string;
  onUnauthorized: () => void;
  onCampaignUpdate: (campaign: Campaign) => void;
  onDistributionChange: (distribution: Distribution) => void;
}) {
  const [preview, setPreview] = useState<ApprovalPreview>();
  const [distribution, setDistribution] = useState<Distribution>();
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [busy, setBusy] = useState(false);
  const [copied, setCopied] = useState("");
  const [replacementAcks, setReplacementAcks] = useState<string[]>([]);
  const [uncertainAcks, setUncertainAcks] = useState<string[]>([]);
  const isCurrentVersion = selectedVersionId === campaign.current_version_id;

  const load = useCallback(async () => {
    try {
      const currentDistribution = await api<Distribution>(
        `/api/campaigns/${campaign.id}/distribution`,
        csrf,
      );
      setDistribution(currentDistribution);
      onCampaignUpdate(currentDistribution.campaign);
      onDistributionChange(currentDistribution);
      try {
        const currentPreview = await api<ApprovalPreview>(
          `/api/campaigns/${campaign.id}/approval-preview`,
          csrf,
        );
        setPreview(currentPreview);
      } catch (reason) {
        setPreview(undefined);
        if (reason instanceof CampaignError && reason.code === "AUTH_REQUIRED")
          throw reason;
      }
      setError("");
    } catch (reason) {
      if (reason instanceof CampaignError && reason.code === "AUTH_REQUIRED")
        onUnauthorized();
      setError(
        messageFor(
          reason instanceof CampaignError ? reason.code : "REQUEST_FAILED",
        ),
      );
    }
  }, [
    campaign.id,
    csrf,
    onDistributionChange,
    onCampaignUpdate,
    onUnauthorized,
  ]);

  useEffect(() => {
    const timer = window.setTimeout(() => void load(), 0);
    return () => window.clearTimeout(timer);
  }, [load, campaign.revision]);

  const perform = async <T,>(action: () => Promise<T>, success?: string) => {
    setBusy(true);
    setError("");
    setNotice("");
    try {
      await action();
      await load();
      if (success) setNotice(success);
    } catch (reason) {
      if (reason instanceof CampaignError && reason.code === "AUTH_REQUIRED")
        onUnauthorized();
      if (
        reason instanceof CampaignError &&
        reason.code === "APPROVAL_PLAN_CHANGED"
      ) {
        await load();
        setNotice(
          "The distribution plan changed. Review the updated destinations, then approve again.",
        );
        return;
      }
      setError(
        messageFor(
          reason instanceof CampaignError ? reason.code : "REQUEST_FAILED",
        ),
      );
      await load();
    } finally {
      setBusy(false);
    }
  };

  const post = <T,>(path: string, body?: unknown) =>
    api<T>(path, csrf, "POST", body);
  const activeApproval = distribution?.active_approval;
  const activeExecutions =
    distribution?.executions.filter(
      (execution) => execution.approval_id === activeApproval?.id,
    ) ?? [];
  const activeIsCurrent =
    activeApproval?.campaign_version_id === campaign.current_version_id;
  const activePlan = distribution?.approvals.find(
    (approval) => approval.id === activeApproval?.id,
  );
  const approvedGraphic = activePlan?.destination_plan.targets.find(
    (target) => target.approved_graphic,
  )?.approved_graphic;
  const proposedPlanChanged = Boolean(
    preview &&
    activePlan &&
    activePlan.destination_plan_hash !== preview.destination_plan_hash,
  );
  const canApprove = Boolean(
    isCurrentVersion &&
    !campaign.is_stale &&
    preview?.version_id === campaign.current_version_id &&
    preview.can_approve &&
    preview.replacement_requirements.every((item) =>
      replacementAcks.includes(item.execution_id),
    ) &&
    preview.uncertain_risk_requirements.every((item) =>
      uncertainAcks.includes(item.execution_id),
    ),
  );
  const acknowledgementsComplete = Boolean(
    preview &&
    preview.replacement_requirements.every((item) =>
      replacementAcks.includes(item.execution_id),
    ) &&
    preview.uncertain_risk_requirements.every((item) =>
      uncertainAcks.includes(item.execution_id),
    ),
  );

  const approve = () => {
    if (!preview) return;
    void perform(
      () =>
        post(`/api/campaigns/${campaign.id}/approve`, {
          expected_revision: campaign.revision,
          campaign_version_id: preview.version_id,
          version_hash: preview.version_hash,
          expected_destination_plan_hash: preview.destination_plan_hash,
          replacement_acknowledgements: replacementAcks,
          uncertain_risk_acknowledgements: uncertainAcks,
        }),
      "This exact version is approved. Nothing is sent until you start distribution.",
    );
  };

  const invoke = (execution: DestinationExecutionView, action: string) => {
    void perform(() =>
      post(`/api/destination-executions/${execution.id}/${action}`, {}),
    );
  };

  if (!distribution)
    return (
      <section
        className={styles.executionCard}
        aria-labelledby="execution-heading"
      >
        <p className={styles.eyebrow}>Approval & distribution</p>
        <h2 id="execution-heading">Prepare this exact version.</h2>
        {error && (
          <p className={styles.inlineError} role="alert">
            {error}
          </p>
        )}
        <p className={styles.helperText} role="status">
          Loading destination readiness…
        </p>
      </section>
    );

  const heading =
    !isCurrentVersion || campaign.is_stale
      ? "Approval & distribution"
      : proposedPlanChanged
        ? "The distribution plan changed. Review it before approval."
        : activeApproval && activeIsCurrent
          ? activeExecutions.length > 0
            ? "Here’s what happened."
            : `Version ${campaign.current_version_number} is approved.`
          : `Ready to send Version ${campaign.current_version_number} out?`;
  return (
    <section
      className={styles.executionCard}
      aria-labelledby="execution-heading"
    >
      <p className={styles.eyebrow}>Approval & distribution</p>
      <div className={styles.executionHeading}>
        <div>
          <h2 id="execution-heading">{heading}</h2>
          <p className={styles.helperText}>
            Each destination’s treatment is set by the application. Approving
            locks in this exact plan; starting distribution is a separate step.
          </p>
        </div>
        {distribution.summary.complete && activeIsCurrent && (
          <span className={styles.executionBadge}>Complete</span>
        )}
      </div>
      {error && (
        <p className={styles.inlineError} role="alert">
          {error}
        </p>
      )}
      {notice && (
        <p className={styles.executionNotice} role="status">
          {notice}
        </p>
      )}

      {!isCurrentVersion && (
        <p className={styles.executionWarning} role="status">
          You are viewing an earlier version. It is read-only; approval applies
          only to the current version.
        </p>
      )}
      {campaign.is_stale && distribution.approvals.length > 0 && (
        <p className={styles.executionWarning} role="status">
          The earlier approval is retained in history but cannot execute after
          this material Event change. Create an updated campaign version before
          seeking approval again.
        </p>
      )}

      {proposedPlanChanged && (
        <p className={styles.executionWarning} role="status">
          The server’s proposed destination plan differs from the one previously
          approved. The earlier approval remains in history and will not change.
        </p>
      )}

      {(!activeApproval || proposedPlanChanged) &&
        isCurrentVersion &&
        !campaign.is_stale &&
        preview && (
          <>
            <div className={styles.executionSummary}>
              <strong>
                Version {preview.version_number} will go to{" "}
                {preview.destination_plan.targets.length}{" "}
                {preview.destination_plan.targets.length === 1
                  ? "destination"
                  : "destinations"}
              </strong>
              <GraphicIncluded
                graphic={
                  preview.destination_plan.targets.find(
                    (target) => target.approved_graphic,
                  )?.approved_graphic
                }
                versionNumber={preview.version_number}
                approved={false}
              />
              <ul>
                {preview.destination_plan.targets.map((target) => {
                  const readiness = preview.destination_readiness.find(
                    (item) =>
                      item.destination_key === target.destination_key &&
                      item.target_key === target.target_key,
                  );
                  return (
                    <li key={`${target.destination_key}:${target.target_key}`}>
                      <span>
                        {destinationLabels[target.destination_key] ??
                          target.destination_key}
                      </span>
                      {target.ui_label && (
                        <span className={styles.planGroups}>
                          {target.ui_label}
                        </span>
                      )}
                      <span>
                        {readiness?.status === "BLOCKED"
                          ? "Cannot publish automatically"
                          : target.execution_kind === "MANUAL"
                            ? target.selected_groups?.length
                              ? `You post it · ${target.selected_groups.length} ${
                                  target.selected_groups.length === 1
                                    ? "group"
                                    : "groups"
                                }`
                              : "You post it · groups chosen in Facebook"
                            : target.runtime_mode === "LIVE"
                              ? "Will publish live"
                              : "Practice mode"}
                      </span>
                      {readiness?.status === "BLOCKED" && (
                        <span className={styles.executionWarning}>
                          {readinessMessage(readiness.reason)}
                        </span>
                      )}
                      {target.selected_groups?.length ? (
                        <span className={styles.planGroups}>
                          {target.selected_groups
                            .map((group) => group.display_name)
                            .join(" · ")}
                        </span>
                      ) : null}
                    </li>
                  );
                })}
              </ul>
              {preview.destination_plan.content_only.map((item) => (
                <p key={item.destination_key} className={styles.helperText}>
                  {destinationLabels[item.destination_key]} copy remains
                  available for review but won’t be sent, because Business
                  Connect access there isn’t verified yet.
                </p>
              ))}
              {preview.destination_plan.exclusions
                .filter(
                  (item) =>
                    !preview.destination_plan.content_only.some(
                      (contentOnly) =>
                        contentOnly.destination_key === item.destination_key,
                    ),
                )
                .map((item) => (
                  <p key={item.destination_key} className={styles.helperText}>
                    {destinationLabels[item.destination_key] ??
                      item.destination_key}
                    : not included this time.
                  </p>
                ))}
            </div>

            {preview.group_selection_pending && (
              <p className={styles.executionWarning} role="status">
                Your Facebook Group choices aren’t saved yet. Save the group
                selection above, then approve that version.
              </p>
            )}
            {preview.blocking_unknowns.length > 0 && (
              <p className={styles.executionWarning} role="alert">
                An earlier destination result is still uncertain. Verify it
                below before approving this version.
              </p>
            )}
            {preview.replacement_requirements.length > 0 && (
              <div className={styles.executionAcks}>
                <p>
                  Earlier versions already affected these destinations. Confirm
                  that this new version is an intentional replacement.
                </p>
                {preview.replacement_requirements.map((item) => (
                  <label key={item.execution_id}>
                    <input
                      type="checkbox"
                      checked={replacementAcks.includes(item.execution_id)}
                      onChange={() =>
                        setReplacementAcks((items) =>
                          items.includes(item.execution_id)
                            ? items.filter((id) => id !== item.execution_id)
                            : [...items, item.execution_id],
                        )
                      }
                    />
                    {destinationLabels[item.destination_key] ??
                      item.destination_key}{" "}
                    · Version {item.version_number} (
                    {destinationStatus(
                      item.status,
                      undefined,
                      false,
                      item.runtime_mode,
                    )}
                    )
                  </label>
                ))}
              </div>
            )}
            {preview.uncertain_risk_requirements.length > 0 && (
              <div className={styles.executionAcks}>
                <p>
                  These earlier outcomes remain uncertain. Confirm the
                  possibility of duplicate public content before proceeding.
                </p>
                {preview.uncertain_risk_requirements.map((item) => (
                  <label key={item.execution_id}>
                    <input
                      type="checkbox"
                      checked={uncertainAcks.includes(item.execution_id)}
                      onChange={() =>
                        setUncertainAcks((items) =>
                          items.includes(item.execution_id)
                            ? items.filter((id) => id !== item.execution_id)
                            : [...items, item.execution_id],
                        )
                      }
                    />
                    {destinationLabels[item.destination_key] ??
                      item.destination_key}{" "}
                    · possible prior publication
                  </label>
                ))}
              </div>
            )}
            <button
              className={styles.eventPrimary}
              type="button"
              disabled={busy || !canApprove || !acknowledgementsComplete}
              onClick={approve}
            >
              {busy
                ? "Saving approval…"
                : proposedPlanChanged
                  ? "Approve this distribution plan"
                  : "Approve this version"}
            </button>
          </>
        )}

      {activeApproval && activeIsCurrent && activeExecutions.length === 0 && (
        <div className={styles.executionSummary}>
          <p>
            Approved {readableTime(activeApproval.approved_at)}. When you’re
            ready, send it to the destinations listed above.
          </p>
          <button
            className={styles.eventPrimary}
            type="button"
            disabled={busy || !isCurrentVersion}
            onClick={() =>
              void perform(() =>
                post(`/api/approvals/${activeApproval.id}/start`),
              )
            }
          >
            {busy
              ? "Starting distribution…"
              : activePlan?.destination_plan.targets.some(
                    (target) =>
                      target.execution_kind === "AUTO" &&
                      target.runtime_mode === "LIVE",
                  )
                ? "Start distribution"
                : "Start simulated distribution"}
          </button>
        </div>
      )}

      {activeApproval && activeIsCurrent && activeExecutions.length > 0 && (
        <>
          <div
            className={styles.executionOutcome}
            data-outcome={distribution.summary.outcome}
            role="status"
          >
            <strong>{outcomeLabel(distribution.summary.outcome)}</strong>
            <span>{summaryParts(distribution.summary)}</span>
            {distribution.summary.complete &&
              distribution.summary.completed_at && (
                <span>
                  Completion recorded{" "}
                  {readableTime(distribution.summary.completed_at)}
                </span>
              )}
          </div>
          {activePlan && (
            <GraphicIncluded
              graphic={approvedGraphic}
              versionNumber={activePlan.version_number ?? 0}
              approved
            />
          )}
          <div className={styles.destinationList}>
            {activeExecutions.map((execution) => {
              const status = destinationStatus(
                execution.status,
                execution.disposition.type,
                execution.retryable,
                execution.runtime_mode,
              );
              const explanation = statusExplanation(execution);
              const blockerMessage =
                execution.status === "BLOCKED" &&
                execution.error_category &&
                readinessText[execution.error_category]
                  ? readinessMessage(execution.error_category)
                  : execution.human_error;
              return (
                <article
                  className={styles.destinationCard}
                  data-testid="destination-card"
                  data-status={execution.status}
                  data-kind={execution.execution_kind}
                  key={execution.id}
                >
                  <div className={styles.destinationHeading}>
                    <div>
                      <h3>
                        {destinationLabels[execution.destination_key] ??
                          execution.destination_key}
                      </h3>
                      <StatusLabel
                        className={styles.destinationStatus}
                        tone={statusTone(execution.status)}
                      >
                        {status}
                      </StatusLabel>
                    </div>
                    {execution.execution_kind === "MANUAL" && (
                      <span className={styles.manualTag}>Manual</span>
                    )}
                  </div>
                  {execution.execution_kind === "AUTO" && (
                    <p className={styles.helperText}>
                      {execution.verification_status === "TEST_MOCKED"
                        ? "Mocked LIVE path · no network call"
                        : execution.runtime_mode === "LIVE"
                          ? execution.status === "SUCCEEDED"
                            ? `Live on ${destinationLabels[execution.destination_key] ?? execution.destination_key} · verified`
                            : execution.status === "BLOCKED"
                              ? "Live plan blocked"
                              : execution.status === "UNKNOWN"
                                ? "Live result needs verification"
                                : execution.status === "FAILED"
                                  ? "Live attempt did not complete"
                                  : "Live publishing"
                          : "Practice mode"}
                    </p>
                  )}
                  {isVerifiedLiveDestinationLink(execution) && (
                    <a
                      className={styles.secondaryButton}
                      href={execution.external_url!}
                      target="_blank"
                      rel="noopener noreferrer"
                      aria-label={`View on ${destinationLabels[execution.destination_key] ?? execution.destination_key} (opens in a new tab)`}
                    >
                      View on{" "}
                      {destinationLabels[execution.destination_key] ??
                        execution.destination_key}
                    </a>
                  )}
                  {execution.result_summary && (
                    <p className={styles.helperText}>
                      {execution.result_summary}
                    </p>
                  )}
                  {explanation && (
                    <p className={styles.helperText}>{explanation}</p>
                  )}
                  {blockerMessage && (
                    <p className={styles.executionWarning}>{blockerMessage}</p>
                  )}

                  {execution.manual_package && (
                    <div className={styles.manualPackage}>
                      <div className={styles.manualStep}>
                        <h4>Where to share</h4>
                        <p>{execution.manual_package.group_selection}</p>
                        {execution.manual_package.selected_groups?.length ? (
                          <FrozenGroupList
                            groups={execution.manual_package.selected_groups.map(
                              (group) => ({
                                ...group,
                                chosen_url: group.url,
                              }),
                            )}
                          />
                        ) : null}
                      </div>
                      <div className={styles.manualStep}>
                        <h4>Graphic</h4>
                        {execution.manual_package.graphic_preview_url ? (
                          <>
                            <img
                              className={styles.manualGraphicPreview}
                              src={execution.manual_package.graphic_preview_url}
                              alt={`Approved graphic for ${execution.manual_package.event_title}`}
                            />
                            <a
                              className={styles.secondaryButton}
                              href={
                                execution.manual_package.graphic_preview_url
                              }
                              download={`${execution.manual_package.event_title}-graphic.png`}
                            >
                              Download graphic
                            </a>
                          </>
                        ) : (
                          <p>
                            {execution.manual_package.creative_status}. Post the
                            copy as text.
                          </p>
                        )}
                      </div>
                      <div className={styles.manualStep}>
                        <h4>
                          <label htmlFor={`manual-copy-${execution.id}`}>
                            Approved Facebook Groups copy
                          </label>
                        </h4>
                        <textarea
                          id={`manual-copy-${execution.id}`}
                          readOnly
                          value={execution.manual_package.approved_copy}
                        />
                        <button
                          className={styles.secondaryButton}
                          type="button"
                          onClick={async () => {
                            try {
                              await navigator.clipboard.writeText(
                                execution.manual_package!.approved_copy,
                              );
                              setCopied(execution.id);
                            } catch {
                              setCopied("");
                            }
                          }}
                        >
                          {copied === execution.id
                            ? "Copied"
                            : "Copy approved post"}
                        </button>
                      </div>
                      <p className={styles.helperText}>
                        {execution.manual_package.instructions}
                      </p>
                      {execution.status === "MANUAL_PENDING" && (
                        <button
                          className={styles.eventPrimary}
                          type="button"
                          disabled={busy || !isCurrentVersion}
                          onClick={() => invoke(execution, "manual-complete")}
                        >
                          Mark Facebook Groups complete
                        </button>
                      )}
                    </div>
                  )}

                  {execution.status === "FAILED" && execution.retryable && (
                    <button
                      className={styles.secondaryButton}
                      type="button"
                      disabled={busy || !isCurrentVersion}
                      onClick={() => invoke(execution, "retry")}
                    >
                      Retry {destinationLabels[execution.destination_key]}
                    </button>
                  )}
                  {execution.status === "FAILED" &&
                    !execution.retryable &&
                    execution.disposition.type !== "ACKNOWLEDGED_FAILURE" && (
                      <button
                        className={styles.secondaryButton}
                        type="button"
                        disabled={busy || !isCurrentVersion}
                        onClick={() => invoke(execution, "acknowledge-failure")}
                      >
                        Account for final failure
                      </button>
                    )}
                  {execution.status === "BLOCKED" &&
                    execution.disposition.type !== "ACKNOWLEDGED_FAILURE" && (
                      <button
                        className={styles.secondaryButton}
                        type="button"
                        disabled={busy || !isCurrentVersion}
                        onClick={() => invoke(execution, "acknowledge-failure")}
                      >
                        Account for this
                      </button>
                    )}
                  {execution.status === "UNKNOWN" &&
                    execution.disposition.type !== "ABANDONED_UNCERTAIN" && (
                      <div className={styles.actionRow}>
                        <button
                          className={styles.eventPrimary}
                          type="button"
                          disabled={busy || !isCurrentVersion}
                          onClick={() => invoke(execution, "reconcile")}
                        >
                          Verify destination result
                        </button>
                        <button
                          className={styles.secondaryButton}
                          type="button"
                          disabled={busy || !isCurrentVersion}
                          onClick={() =>
                            invoke(execution, "acknowledge-uncertain")
                          }
                        >
                          Acknowledge uncertainty
                        </button>
                      </div>
                    )}

                  <details className={styles.attemptHistory}>
                    <summary>
                      History · {execution.attempts.length}{" "}
                      {execution.attempts.length === 1 ? "action" : "actions"}
                    </summary>
                    <ol>
                      {execution.attempts.map((attempt) => (
                        <li key={`${execution.id}:${attempt.attempt_number}`}>
                          {attempt.operation === "PUBLISH"
                            ? "Publish"
                            : attempt.operation === "VERIFY"
                              ? "Verification"
                              : attempt.operation === "ACK_MANUAL"
                                ? "Manual update"
                                : attempt.operation === "PREPARE_MANUAL"
                                  ? "Package prepared"
                                  : "Status update"}{" "}
                          ·{" "}
                          {attempt.outcome
                            ?.replaceAll("_", " ")
                            .toLowerCase() ?? "in progress"}
                          {attempt.finished_at
                            ? ` · ${readableTime(attempt.finished_at)}`
                            : ""}
                          {attempt.human_error
                            ? ` · ${attempt.human_error}`
                            : ""}
                        </li>
                      ))}
                    </ol>
                  </details>
                </article>
              );
            })}
          </div>
        </>
      )}

      {distribution.approvals.length > 0 && (
        <details className={styles.approvalHistory}>
          <summary>
            Approval history · {distribution.approvals.length}{" "}
            {distribution.approvals.length === 1 ? "version" : "versions"}
          </summary>
          <ol>
            {distribution.approvals
              .slice()
              .reverse()
              .map((approval) => (
                <li key={approval.id}>
                  <strong>
                    Version {approval.version_number} ·{" "}
                    {approval.active
                      ? "Current approval"
                      : "Historical approval"}
                  </strong>
                  <span>
                    Approved {readableTime(approval.approved_at)} ·{" "}
                    {approval.destination_plan.targets.length} authorized
                    destinations ·{" "}
                    {destinationPlanSummary(approval.destination_plan)}
                  </span>
                </li>
              ))}
          </ol>
        </details>
      )}
    </section>
  );
}
