import { useCallback, useEffect, useState } from "react";
import styles from "./App.module.css";
import { api, CampaignError } from "./campaignApi";
import { StatusLabel } from "./ui";

export type GroupView = {
  id: string;
  display_name: string;
  url: string | null;
  geography_label: string | null;
  member_count: number | null;
  category: string | null;
  cautions: string[];
  selectable: boolean;
  unavailable_reason: string | null;
};
type Recommendation = {
  recommendation_id: string;
  rank: number;
  tier: "BEST_FIT" | "GOOD_FIT" | "POSSIBLE_FIT";
  reason: string;
  signals: string[];
  caution: string | null;
  explanation_source: "MODEL" | "DETERMINISTIC";
  group: GroupView;
};
export type RecommendationsView = {
  campaign_id: string;
  campaign_revision: number;
  facebook_groups_enabled: boolean;
  run: {
    run_id: string;
    created_at: string;
    ranking_source: "MODEL" | "DETERMINISTIC" | "DETERMINISTIC_FALLBACK";
    candidate_count: number;
    excluded_counts: Record<string, number>;
    run_notes: string[];
    stale: boolean;
  } | null;
  recommendations: Recommendation[];
  selection: {
    selected: GroupView[];
    pending: boolean;
    saved_group_ids: string[];
    version_number: number;
    approval_active: boolean;
  };
};
export type FrozenGroup = {
  group_record_id: string;
  display_name: string;
  chosen_url: string | null;
  geography_label: string | null;
  member_count: number | null;
  cautions: string[];
};
type BrowseResult = {
  eligible_count: number;
  groups: (GroupView & { tier: string | null })[];
};

const tierLabel: Record<Recommendation["tier"], string> = {
  BEST_FIT: "Best fit",
  GOOD_FIT: "Good fit",
  POSSIBLE_FIT: "Possible fit",
};

function groupMessage(code: string) {
  const messages: Record<string, string> = {
    GROUP_CATALOG_EMPTY:
      "No Facebook Group list has been imported yet, so there is nothing to suggest.",
    GROUP_NOT_ELIGIBLE:
      "That group can’t be used for this event. Its link or details need review first.",
    GROUP_SELECTION_INVALID:
      "One of the chosen groups is no longer available. Deselect it and save again.",
    GROUP_SELECTION_UNCHANGED: "Your group selection is already saved.",
    CAMPAIGN_REVISION_CONFLICT:
      "The campaign changed in another view. Refresh and try again.",
    CAMPAIGN_STALE_REGENERATE_FIRST:
      "The Event changed. Create an updated version before saving groups.",
    CAMPAIGN_INPUTS_CHANGED:
      "The Event changed while I was looking. Try suggesting groups again.",
    AUTH_REQUIRED: "Your session ended. Please sign in again.",
  };
  return (
    messages[code] ?? "I couldn’t update the Facebook Groups. Please try again."
  );
}

function members(count: number | null) {
  if (count == null) return null;
  return `about ${new Intl.NumberFormat("en-US").format(count)} members (from the Group Master)`;
}

function GroupMeta({
  group,
}: {
  group: Pick<GroupView, "geography_label" | "member_count">;
}) {
  const parts = [group.geography_label, members(group.member_count)].filter(
    Boolean,
  );
  return parts.length ? (
    <span className={styles.groupMeta}>{parts.join(" · ")}</span>
  ) : null;
}

function GroupLink({ url, name }: { url: string | null; name: string }) {
  if (!url) return null;
  return (
    <a
      className={styles.groupLink}
      href={url}
      target="_blank"
      rel="noopener noreferrer"
      aria-label={`Open group: ${name} on Facebook (opens in a new tab)`}
    >
      Open group
    </a>
  );
}

export function FrozenGroupList({ groups }: { groups: FrozenGroup[] }) {
  return (
    <ul className={styles.groupList}>
      {groups.map((group) => (
        <li key={group.group_record_id} className={styles.groupItem}>
          <div className={styles.groupBody}>
            <strong>{group.display_name}</strong>
            <GroupMeta group={group} />
            {group.cautions.map((caution) => (
              <span key={caution} className={styles.groupCaution}>
                <span aria-hidden="true">!</span> {caution}
              </span>
            ))}
          </div>
          <GroupLink url={group.chosen_url} name={group.display_name} />
        </li>
      ))}
    </ul>
  );
}

export function FacebookGroupsSection({
  campaignId,
  campaignRevision,
  versionNumber,
  frozenGroups,
  historical,
  stale,
  changedLabel,
  csrf,
  onUnauthorized,
  onDecision,
  onSaved,
}: {
  campaignId: string;
  campaignRevision: number;
  versionNumber: number;
  frozenGroups: FrozenGroup[];
  historical: boolean;
  stale: boolean;
  changedLabel?: string;
  csrf: string;
  onUnauthorized: () => void;
  onDecision: () => void;
  onSaved: (campaign: unknown) => void;
}) {
  const [view, setView] = useState<RecommendationsView>();
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState("");
  const [changeConfirmed, setChangeConfirmed] = useState(false);
  const [confirming, setConfirming] = useState(false);
  const [query, setQuery] = useState("");
  const [browse, setBrowse] = useState<BrowseResult>();

  const fail = useCallback(
    (reason: unknown) => {
      const code =
        reason instanceof CampaignError ? reason.code : "REQUEST_FAILED";
      if (code === "AUTH_REQUIRED") onUnauthorized();
      setError(groupMessage(code));
    },
    [onUnauthorized],
  );

  const [unavailable, setUnavailable] = useState(false);
  // Show the operator's choice immediately while it saves; the server view replaces it afterwards.
  const [optimistic, setOptimistic] = useState<Record<string, boolean>>({});
  const load = useCallback(async () => {
    try {
      const loaded = await api<RecommendationsView>(
        `/api/campaigns/${campaignId}/group-recommendations`,
        csrf,
      );
      if (
        !loaded ||
        !Array.isArray(loaded.recommendations) ||
        !loaded.selection
      ) {
        setUnavailable(true);
        return;
      }
      setUnavailable(false);
      setView(loaded);
    } catch (reason) {
      // A background read failure is not an action error; keep the rest of review usable.
      if (reason instanceof CampaignError && reason.code === "AUTH_REQUIRED")
        onUnauthorized();
      setUnavailable(true);
    }
  }, [campaignId, csrf, onUnauthorized]);

  useEffect(() => {
    const timer = window.setTimeout(() => void load(), 0);
    return () => window.clearTimeout(timer);
  }, [load, campaignRevision]);

  const run = async (label: string, action: () => Promise<void>) => {
    setBusy(label);
    setError("");
    try {
      await action();
    } catch (reason) {
      fail(reason);
    } finally {
      setBusy(null);
    }
  };

  const suggest = () =>
    run("suggest", async () => {
      setView(
        await api<RecommendationsView>(
          `/api/campaigns/${campaignId}/group-recommendations`,
          csrf,
          "POST",
        ),
      );
    });

  const search = (value: string) =>
    run("browse", async () => {
      const params = new URLSearchParams({ limit: "20" });
      if (value.trim()) params.set("q", value.trim());
      setBrowse(
        await api<BrowseResult>(
          `/api/campaigns/${campaignId}/eligible-groups?${params}`,
          csrf,
        ),
      );
    });

  const toggle = (
    group: GroupView,
    select: boolean,
    recommendationId?: string,
  ) =>
    run(`decide:${group.id}`, async () => {
      setOptimistic((current) => ({ ...current, [group.id]: select }));
      try {
        setView(
          await api<RecommendationsView>(
            `/api/campaigns/${campaignId}/group-decisions`,
            csrf,
            "POST",
            {
              expected_revision: view?.campaign_revision ?? campaignRevision,
              group_record_id: group.id,
              decision: select ? "SELECT" : "REJECT",
              recommendation_id: recommendationId ?? null,
              confirm_withdraw_approval: changeConfirmed,
            },
          ),
        );
        onDecision();
      } catch (reason) {
        if (
          reason instanceof CampaignError &&
          reason.code === "GROUP_CHANGE_REQUIRES_NEW_APPROVAL"
        ) {
          setConfirming(true);
          return;
        }
        throw reason;
      } finally {
        setOptimistic((current) => {
          const next = { ...current };
          delete next[group.id];
          return next;
        });
      }
    });

  const save = () =>
    run("save", async () => {
      const campaign = await api<unknown>(
        `/api/campaigns/${campaignId}/group-selection`,
        csrf,
        "POST",
        {
          expected_revision: view?.campaign_revision ?? campaignRevision,
        },
      );
      setChangeConfirmed(false);
      onSaved(campaign);
    });

  const heading = (
    <>
      <p className={styles.eyebrow}>Facebook Groups · you post manually</p>
      <h2 id="facebook-groups-heading">Where to share on Facebook</h2>
      {changedLabel && (
        <span className={styles.changedTag}>{changedLabel}</span>
      )}
    </>
  );

  if (historical || stale) {
    return (
      <section
        className={styles.reviewCard}
        aria-labelledby="facebook-groups-heading"
      >
        {heading}
        {frozenGroups.length ? (
          <>
            <p className={styles.helperText}>
              Groups saved with Version {versionNumber}
              {historical ? " · read-only" : ""}.
            </p>
            <FrozenGroupList groups={frozenGroups} />
          </>
        ) : (
          <p className={styles.helperText}>
            No groups were chosen for Version {versionNumber}.
          </p>
        )}
        {stale && !historical && (
          <p className={styles.helperText}>
            Create an updated version from the new Event details before changing
            groups.
          </p>
        )}
      </section>
    );
  }

  const selection = view?.selection;
  const selectedIds = new Set(
    selection?.selected.map((group) => group.id) ?? [],
  );
  const recommendedIds = new Set(
    view?.recommendations.map((item) => item.group.id) ?? [],
  );
  const extraSelected =
    selection?.selected.filter((group) => !recommendedIds.has(group.id)) ?? [];
  const locked = Boolean(
    selection?.approval_active && !selection.pending && !changeConfirmed,
  );
  const count = selectedIds.size;

  const checkbox = (group: GroupView, recommendationId?: string) => (
    <input
      type="checkbox"
      checked={optimistic[group.id] ?? selectedIds.has(group.id)}
      disabled={
        Boolean(busy) ||
        locked ||
        (!group.selectable && !selectedIds.has(group.id))
      }
      onChange={(change) =>
        void toggle(group, change.target.checked, recommendationId)
      }
      aria-describedby={`group-detail-${group.id}`}
    />
  );

  return (
    <section
      className={`${styles.reviewCard} ${styles.groupsCard}`}
      aria-labelledby="facebook-groups-heading"
    >
      {heading}
      <p className={styles.helperText}>
        I suggest groups from your Group Master; you choose where it goes.
        Nothing is posted for you — you’ll share the approved post yourself.
      </p>
      {error && (
        <p role="alert" className={styles.inlineError}>
          {error}
        </p>
      )}
      {unavailable && !view && (
        <p className={styles.helperText} role="status">
          Facebook Group suggestions aren’t available right now. The rest of the
          campaign is unaffected.
        </p>
      )}

      {locked && (
        <div className={styles.groupLockNotice}>
          <StatusLabel className={styles.graphicState} tone="success">
            {`Approved with Version ${selection?.version_number}`}
          </StatusLabel>
          <p>These are the groups in the approved plan.</p>
          <FrozenGroupList groups={frozenGroups} />
          {!confirming ? (
            <button
              className={styles.secondaryButton}
              type="button"
              onClick={() => setConfirming(true)}
            >
              Change groups
            </button>
          ) : null}
        </div>
      )}

      {confirming && !changeConfirmed && (
        <div
          className={styles.executionWarning}
          role="alertdialog"
          aria-labelledby="group-change-title"
        >
          <p id="group-change-title">
            <strong>Changing groups ends the current approval.</strong> Version{" "}
            {versionNumber} stays in history exactly as approved. You’ll save a
            new version with your new groups and approve it before anything goes
            out.
          </p>
          <div className={styles.actionRow}>
            <button
              className={styles.eventPrimary}
              type="button"
              onClick={() => {
                setChangeConfirmed(true);
                setConfirming(false);
              }}
            >
              Change groups
            </button>
            <button
              className={styles.secondaryButton}
              type="button"
              onClick={() => setConfirming(false)}
            >
              Keep approved groups
            </button>
          </div>
        </div>
      )}

      {!locked && view && !view.run && (
        <div className={styles.groupEmpty}>
          <strong>Find groups that fit this event.</strong>
          <p>
            I’ll look through your Group Master for good matches and explain
            each one.
          </p>
          <button
            className={styles.eventPrimary}
            type="button"
            disabled={Boolean(busy)}
            onClick={() => void suggest()}
          >
            {busy === "suggest"
              ? "Finding groups for this event…"
              : "Suggest groups"}
          </button>
        </div>
      )}

      {!locked && view?.run && (
        <>
          <div className={styles.groupRunBar}>
            <h3>Recommended for this event</h3>
            <button
              className={styles.secondaryButton}
              type="button"
              disabled={Boolean(busy)}
              onClick={() => void suggest()}
            >
              {busy === "suggest" ? "Finding groups…" : "Refresh suggestions"}
            </button>
          </div>
          {view.run.ranking_source === "DETERMINISTIC_FALLBACK" && (
            <p className={styles.executionNotice} role="status">
              AI suggestions aren’t available right now, so these are ranked by
              location and audience only. You can also browse every eligible
              group below.
            </p>
          )}
          {view.run.run_notes.includes("EVENT_STATE_UNKNOWN") && (
            <p className={styles.helperText}>
              The Event location doesn’t include a state, so groups weren’t
              filtered by location.
            </p>
          )}
          {view.run.stale && (
            <p className={styles.helperText}>
              Event details changed since these suggestions. Refresh them to
              re-check fit; your chosen groups stay chosen.
            </p>
          )}
          {view.recommendations.length === 0 ? (
            <p className={styles.helperText}>
              No strong matches came up. Browse the eligible groups below to
              choose your own.
            </p>
          ) : (
            <ul
              className={styles.groupList}
              aria-label="Recommended Facebook Groups"
            >
              {view.recommendations.map((item) => (
                <li key={item.recommendation_id} className={styles.groupItem}>
                  <label className={styles.groupChoice}>
                    {checkbox(item.group, item.recommendation_id)}
                    <span
                      className={styles.groupBody}
                      id={`group-detail-${item.group.id}`}
                    >
                      <span className={styles.groupTitleRow}>
                        <strong>{item.group.display_name}</strong>
                        <StatusLabel
                          className={styles.groupTier}
                          tone={
                            item.tier === "BEST_FIT"
                              ? "success"
                              : item.tier === "GOOD_FIT"
                                ? "info"
                                : "neutral"
                          }
                        >
                          {tierLabel[item.tier]}
                        </StatusLabel>
                      </span>
                      <span className={styles.groupReason}>{item.reason}</span>
                      <GroupMeta group={item.group} />
                      {item.caution && (
                        <span className={styles.groupCaution}>
                          <span aria-hidden="true">!</span> {item.caution}
                        </span>
                      )}
                    </span>
                  </label>
                  <GroupLink
                    url={item.group.url}
                    name={item.group.display_name}
                  />
                </li>
              ))}
            </ul>
          )}
        </>
      )}

      {!locked && extraSelected.length > 0 && (
        <>
          <h3 className={styles.groupSubheading}>Also chosen</h3>
          <ul className={styles.groupList}>
            {extraSelected.map((group) => (
              <li key={group.id} className={styles.groupItem}>
                <label className={styles.groupChoice}>
                  {checkbox(group)}
                  <span
                    className={styles.groupBody}
                    id={`group-detail-${group.id}`}
                  >
                    <strong>{group.display_name}</strong>
                    <GroupMeta group={group} />
                  </span>
                </label>
                <GroupLink url={group.url} name={group.display_name} />
              </li>
            ))}
          </ul>
        </>
      )}

      {!locked && view && (
        <details
          className={styles.groupBrowse}
          onToggle={(toggleEvent) => {
            if ((toggleEvent.target as HTMLDetailsElement).open && !browse)
              void search("");
          }}
        >
          <summary>Browse other eligible groups</summary>
          <form
            className={styles.groupSearch}
            role="search"
            onSubmit={(submitEvent) => {
              submitEvent.preventDefault();
              void search(query);
            }}
          >
            <label htmlFor="group-search">Search by name or state</label>
            <div className={styles.answerRow}>
              <input
                id="group-search"
                value={query}
                onChange={(change) => setQuery(change.target.value)}
                placeholder="For example: Denver"
                maxLength={80}
              />
              <button
                className={styles.secondaryButton}
                type="submit"
                disabled={Boolean(busy)}
              >
                Search
              </button>
            </div>
          </form>
          {busy === "browse" && (
            <p role="status">Looking through eligible groups…</p>
          )}
          {browse && (
            <>
              <p className={styles.helperText}>
                {browse.groups.length} of {browse.eligible_count} eligible
                groups shown. Groups with unconfirmed links or conflicting
                details are left out until they’re reviewed.
              </p>
              <ul
                className={styles.groupList}
                aria-label="Eligible Facebook Groups"
              >
                {browse.groups.map((group) => (
                  <li key={group.id} className={styles.groupItem}>
                    <label className={styles.groupChoice}>
                      {checkbox(group)}
                      <span
                        className={styles.groupBody}
                        id={`group-detail-${group.id}`}
                      >
                        <strong>{group.display_name}</strong>
                        <GroupMeta group={group} />
                        {group.cautions.map((caution) => (
                          <span key={caution} className={styles.groupCaution}>
                            <span aria-hidden="true">!</span> {caution}
                          </span>
                        ))}
                      </span>
                    </label>
                    <GroupLink url={group.url} name={group.display_name} />
                  </li>
                ))}
              </ul>
            </>
          )}
        </details>
      )}

      {!locked && selection && (
        <div className={styles.groupSaveBar} role="status">
          <p>
            <strong>
              {count} {count === 1 ? "group" : "groups"} selected
            </strong>
            {selection.pending
              ? " · not saved to the campaign yet"
              : count
                ? ` · saved in Version ${selection.version_number}`
                : ""}
          </p>
          {selection.pending && (
            <button
              className={styles.eventPrimary}
              type="button"
              disabled={Boolean(busy)}
              onClick={() => void save()}
            >
              {busy === "save" ? "Saving your groups…" : "Save group selection"}
            </button>
          )}
        </div>
      )}
      {!locked && selection?.pending && (
        <p className={styles.helperText}>
          Saving creates a new version of this campaign; you’ll approve that
          version before anything goes out.
        </p>
      )}
    </section>
  );
}
