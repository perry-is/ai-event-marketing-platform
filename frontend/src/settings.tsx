import { useCallback, useEffect, useState } from "react";
import styles from "./App.module.css";
import { api, CampaignError } from "./campaignApi";
import { StatusLabel } from "./ui";

export type EventbriteStatus = {
  mode: "SIMULATED" | "LIVE";
  status: "READY" | "NEEDS_SETUP" | "UNAVAILABLE";
  reasons: string[];
  organization_name: string | null;
  organizer_name: string | null;
  checked_live: boolean;
};

export type MeetupStatus = {
  mode: "SIMULATED" | "LIVE";
  status: "READY" | "NEEDS_SETUP" | "UNAVAILABLE";
  reasons: string[];
  member_name: string | null;
  network_name: string | null;
  group_name: string | null;
  checked_live: boolean;
  photo_capability: "DEFERRED" | "READY";
  photo_note: string;
};

export type LinkedInStatus = {
  mode: "SIMULATED" | "LIVE";
  status: "READY" | "CONNECTED" | "NEEDS_SETUP" | "UNAVAILABLE";
  reasons: string[];
  organization_name: string | null;
  connected_member: string | null;
  community_access_tier: "UNVERIFIED" | "DEVELOPMENT" | "STANDARD";
  checked_live: boolean;
  connector_implemented: boolean;
};

export type InstagramStatus = {
  mode: "SIMULATED" | "LIVE";
  status: "READY" | "CONNECTED" | "NEEDS_SETUP" | "UNAVAILABLE" | "SIMULATED";
  reasons: string[];
  username: string | null;
  checked_live: boolean;
  connector_implemented: boolean;
  account_eligible: boolean;
  media_delivery_ready: boolean;
};

export type OAuthConnection = {
  provider: "linkedin" | "instagram";
  status: "NOT_CONNECTED" | "CONNECTED" | "REAUTH_REQUIRED" | "DISCONNECTED";
  account_name: string | null;
  account_id: string | null;
  account_type?: string | null;
  granted_scopes: string[];
  missing_scopes: string[];
  access_expires_at: string | null;
  vault_ready: boolean;
};

export type OAuthConnections = {
  linkedin: OAuthConnection;
  instagram: OAuthConnection;
};

function headline(value: EventbriteStatus) {
  if (value.status === "READY" && value.checked_live)
    return value.mode === "LIVE"
      ? "Ready for live publishing"
      : "Connected · new campaigns use practice mode";
  if (value.status === "UNAVAILABLE") return "Can’t reach Eventbrite right now";
  return value.mode === "LIVE"
    ? "Needs setup before live publishing"
    : "Practice mode · not connected for live publishing";
}

function ConnectionActions({
  provider,
  value,
  csrf,
  onRefresh,
}: {
  provider: "linkedin" | "instagram";
  value?: OAuthConnection;
  csrf: string;
  onRefresh: () => void;
}) {
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  const connected = value?.status === "CONNECTED";
  const reauth = value?.status === "REAUTH_REQUIRED";
  const label = provider === "linkedin" ? "LinkedIn" : "Instagram";

  const connect = async () => {
    setBusy(true);
    setMessage("");
    try {
      const result = await api<{ authorization_url: string }>(
        `/api/oauth/${provider}/connect`,
        csrf,
        "POST",
      );
      if (!result.authorization_url.startsWith("https://"))
        throw new Error("invalid authorization URL");
      window.location.assign(result.authorization_url);
    } catch (error) {
      if (error instanceof CampaignError && error.code === "AUTH_REQUIRED")
        return;
      setMessage(
        `${label} connection could not be started. Check setup and try again.`,
      );
    } finally {
      setBusy(false);
    }
  };

  const disconnect = async () => {
    if (!window.confirm(`Disconnect ${label}?`)) return;
    setBusy(true);
    setMessage("");
    try {
      const result = await api<{
        status: string;
        remote_revocation:
          | "CONFIRMED"
          | "UNCONFIRMED"
          | "NOT_SUPPORTED"
          | "NOT_ATTEMPTED";
      }>(`/api/oauth/${provider}/disconnect`, csrf, "POST");
      onRefresh();
      setMessage(
        result.remote_revocation === "CONFIRMED"
          ? "Disconnected. Provider-side revocation was confirmed."
          : result.remote_revocation === "NOT_SUPPORTED"
            ? "Disconnected here. This provider does not support revocation through this connection."
            : result.remote_revocation === "UNCONFIRMED"
              ? "Disconnected here, but provider-side revocation could not be confirmed."
              : "Disconnected from this workspace.",
      );
    } catch (error) {
      if (error instanceof CampaignError && error.code === "AUTH_REQUIRED")
        return;
      setMessage(`${label} could not be disconnected. Try again shortly.`);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className={styles.settingsStatus}>
      {connected && value?.account_name && (
        <p className={styles.helperText}>
          Connected account: {value.account_name}
        </p>
      )}
      {value && !value.vault_ready && (
        <p className={styles.inlineError}>
          Secure credential storage needs setup before connecting.
        </p>
      )}
      {connected && value && (
        <p className={styles.helperText}>
          {value.missing_scopes.length === 0
            ? "Required publishing permissions are connected."
            : "Reconnect to grant the required publishing permissions."}
          {value.access_expires_at
            ? ` Access expires ${new Date(value.access_expires_at).toLocaleDateString()}.`
            : ""}
        </p>
      )}
      {reauth && (
        <p className={styles.inlineError}>
          This connection needs authorization again.
        </p>
      )}
      <div className={styles.settingsActions}>
        <button
          type="button"
          className={styles.secondaryButton}
          disabled={busy || !value || value.vault_ready === false}
          onClick={() => void connect()}
        >
          {busy
            ? "Opening…"
            : connected || reauth
              ? `Reconnect ${label}`
              : `Connect ${label}`}
        </button>
        {(connected || reauth) && (
          <button
            type="button"
            className={styles.secondaryButton}
            disabled={busy}
            onClick={() => void disconnect()}
          >
            Disconnect {label}
          </button>
        )}
      </div>
      {message && <p role="status">{message}</p>}
    </div>
  );
}

export function SettingsPage({
  csrf,
  onUnauthorized,
}: {
  csrf: string;
  onUnauthorized: () => void;
}) {
  const [eventbrite, setEventbrite] = useState<EventbriteStatus>();
  const [meetup, setMeetup] = useState<MeetupStatus>();
  const [linkedin, setLinkedin] = useState<LinkedInStatus>();
  const [instagram, setInstagram] = useState<InstagramStatus>();
  const [connections, setConnections] = useState<OAuthConnections>();
  const [error, setError] = useState("");
  const [meetupError, setMeetupError] = useState("");
  const [linkedinError, setLinkedinError] = useState("");
  const [instagramError, setInstagramError] = useState("");
  const [connectionsError, setConnectionsError] = useState("");
  const refreshConnections = useCallback(() => {
    api<OAuthConnections>("/api/settings/oauth-connections", csrf)
      .then(setConnections)
      .catch((reason: unknown) => {
        if (reason instanceof CampaignError && reason.code === "AUTH_REQUIRED")
          onUnauthorized();
        setConnectionsError("OAuth connection status couldn’t be loaded.");
      });
  }, [csrf, onUnauthorized]);

  useEffect(() => {
    let active = true;
    api<EventbriteStatus>("/api/settings/eventbrite", csrf)
      .then((value) => {
        if (active) setEventbrite(value);
      })
      .catch((reason: unknown) => {
        if (reason instanceof CampaignError && reason.code === "AUTH_REQUIRED")
          onUnauthorized();
        if (active)
          setError("Connection status couldn’t be loaded. Try again shortly.");
      });
    api<MeetupStatus>("/api/settings/meetup", csrf)
      .then((value) => {
        if (active) setMeetup(value);
      })
      .catch((reason: unknown) => {
        if (reason instanceof CampaignError && reason.code === "AUTH_REQUIRED")
          onUnauthorized();
        if (active)
          setMeetupError(
            "Connection status couldn’t be loaded. Try again shortly.",
          );
      });
    api<LinkedInStatus>("/api/settings/linkedin-organization", csrf)
      .then((value) => {
        if (active) setLinkedin(value);
      })
      .catch((reason: unknown) => {
        if (reason instanceof CampaignError && reason.code === "AUTH_REQUIRED")
          onUnauthorized();
        if (active)
          setLinkedinError(
            "Connection status couldn’t be loaded. Try again shortly.",
          );
      });
    api<InstagramStatus>("/api/settings/instagram", csrf)
      .then((value) => {
        if (active) setInstagram(value);
      })
      .catch((reason: unknown) => {
        if (reason instanceof CampaignError && reason.code === "AUTH_REQUIRED")
          onUnauthorized();
        if (active)
          setInstagramError(
            "Connection status couldn’t be loaded. Try again shortly.",
          );
      });
    refreshConnections();
    return () => {
      active = false;
    };
  }, [csrf, onUnauthorized, refreshConnections]);

  const callbackResult = new URLSearchParams(window.location.search).get(
    "connection",
  );
  const callbackMessage = callbackResult?.endsWith("_connected")
    ? "Account connected. Live publishing readiness is checked before approval."
    : callbackResult?.endsWith("_denied")
      ? "The provider authorization was declined. Your existing connection is unchanged."
      : callbackResult?.endsWith("_failed") || callbackResult === "failed"
        ? "The connection could not be completed. Any existing connection is unchanged."
        : "";
  const linkedinConnected =
    connections?.linkedin.status === "CONNECTED" ||
    (linkedin?.checked_live === true && linkedin.connected_member !== null);
  const instagramConnected =
    connections?.instagram.status === "CONNECTED" ||
    (instagram?.checked_live === true && instagram.status === "READY");

  return (
    <>
      <p className={styles.eyebrow}>Event Marketing workspace</p>
      <h1>Settings</h1>
      <p className={styles.intro}>
        Where your campaigns are allowed to publish.
      </p>
      {callbackMessage && (
        <p role="status" className={styles.helperText}>
          {callbackMessage}
        </p>
      )}
      {connectionsError && (
        <p role="alert" className={styles.inlineError}>
          {connectionsError}
        </p>
      )}
      <div className={styles.providerGrid}>
        <section
          className={`${styles.reviewCard} ${styles.providerCard}`}
          aria-labelledby="eventbrite-settings-heading"
        >
          <p className={styles.eyebrow}>Connected destinations</p>
          <h2 id="eventbrite-settings-heading">Eventbrite</h2>
          {error && (
            <p role="alert" className={styles.inlineError}>
              {error}
            </p>
          )}
          {!eventbrite && !error && <p role="status">Checking Eventbrite…</p>}
          {eventbrite && (
            <div className={styles.settingsStatus}>
              <StatusLabel
                className={styles.graphicState}
                tone={
                  eventbrite.status === "READY" && eventbrite.checked_live
                    ? "success"
                    : eventbrite.status === "UNAVAILABLE"
                      ? "warning"
                      : "neutral"
                }
              >
                {eventbrite.status === "READY" && eventbrite.checked_live
                  ? "Connected"
                  : eventbrite.status === "UNAVAILABLE"
                    ? "Unavailable"
                    : "Needs setup"}
              </StatusLabel>
              <p>
                <strong>{headline(eventbrite)}</strong>
              </p>
              {eventbrite.organization_name && (
                <p className={styles.helperText}>
                  Organization: {eventbrite.organization_name}
                  {eventbrite.organizer_name
                    ? ` · Organizer: ${eventbrite.organizer_name}`
                    : ""}
                </p>
              )}
              {eventbrite.reasons.length > 0 && (
                <ul className={styles.settingsReasons}>
                  {eventbrite.reasons.map((reason) => (
                    <li key={reason}>{reason}</li>
                  ))}
                </ul>
              )}
              <p className={styles.helperText}>
                Eventbrite access is configured privately on the server.
                Approval always shows whether each destination will publish live
                or in practice mode.
              </p>
            </div>
          )}
        </section>
        <section
          className={`${styles.reviewCard} ${styles.providerCard}`}
          aria-labelledby="meetup-settings-heading"
        >
          <p className={styles.eyebrow}>Connected destinations</p>
          <h2 id="meetup-settings-heading">Meetup</h2>
          {meetupError && (
            <p role="alert" className={styles.inlineError}>
              {meetupError}
            </p>
          )}
          {!meetup && !meetupError && <p role="status">Checking Meetup…</p>}
          {meetup && (
            <div className={styles.settingsStatus}>
              <StatusLabel
                className={styles.graphicState}
                tone={
                  meetup.status === "READY" && meetup.checked_live
                    ? "success"
                    : meetup.status === "UNAVAILABLE"
                      ? "warning"
                      : "neutral"
                }
              >
                {meetup.status === "READY" && meetup.checked_live
                  ? "Connected"
                  : meetup.status === "UNAVAILABLE"
                    ? "Connection needs attention"
                    : "Needs setup"}
              </StatusLabel>
              <p>
                <strong>
                  {meetup.status === "READY" && meetup.checked_live
                    ? meetup.mode === "LIVE"
                      ? "Account and group ready for live publishing"
                      : "Account and group connected · new campaigns use practice mode"
                    : meetup.status === "UNAVAILABLE"
                      ? "Meetup connection needs attention"
                      : meetup.mode === "LIVE"
                        ? "Needs setup before live publishing"
                        : "Practice mode · not connected for live publishing"}
                </strong>
              </p>
              {meetup.group_name && (
                <p className={styles.helperText}>
                  Group: {meetup.group_name}
                  {meetup.network_name
                    ? ` · Network: ${meetup.network_name}`
                    : ""}
                </p>
              )}
              {meetup.member_name && (
                <p className={styles.helperText}>
                  Connected as {meetup.member_name}
                </p>
              )}
              {meetup.reasons.length > 0 && (
                <ul className={styles.settingsReasons}>
                  {meetup.reasons.map((reason) => (
                    <li key={reason}>{reason}</li>
                  ))}
                </ul>
              )}
              <p className={styles.helperText}>{meetup.photo_note}</p>
              <p className={styles.helperText}>
                Meetup access is configured privately on the server. Approval
                shows the exact destination treatment for each campaign.
              </p>
            </div>
          )}
        </section>
        <section
          className={`${styles.reviewCard} ${styles.providerCard}`}
          aria-labelledby="linkedin-settings-heading"
        >
          <p className={styles.eyebrow}>Connected destinations</p>
          <h2 id="linkedin-settings-heading">LinkedIn organization</h2>
          {linkedinError && (
            <p role="alert" className={styles.inlineError}>
              {linkedinError}
            </p>
          )}
          {!linkedin && !linkedinError && (
            <p role="status">Checking LinkedIn…</p>
          )}
          {linkedin && (
            <div className={styles.settingsStatus}>
              <StatusLabel
                className={styles.graphicState}
                tone={
                  linkedinConnected ||
                  (linkedin.status === "READY" && linkedin.checked_live)
                    ? "success"
                    : linkedin.status === "UNAVAILABLE"
                      ? "warning"
                      : "neutral"
                }
              >
                {linkedinConnected
                  ? "Connected"
                  : linkedin.status === "UNAVAILABLE"
                    ? "Connection needs attention"
                    : "Needs setup"}
              </StatusLabel>
              <p>
                <strong>
                  {linkedin.status === "CONNECTED"
                    ? "Account connected · Page access is checked before approval"
                    : linkedinConnected
                      ? linkedin.mode === "LIVE" && linkedin.status === "READY"
                        ? "Page ready for live publishing"
                        : linkedin.mode === "LIVE"
                          ? "Page connected · needs setup before live publishing"
                          : "Page connected · new campaigns use practice mode"
                      : linkedin.status === "UNAVAILABLE"
                        ? "LinkedIn connection needs attention"
                        : linkedin.mode === "LIVE"
                          ? "Needs setup before live publishing"
                          : "Practice mode · live connection not ready"}
                </strong>
              </p>
              {linkedin.organization_name && (
                <p className={styles.helperText}>
                  Page: {linkedin.organization_name}
                </p>
              )}
              {linkedin.connected_member && (
                <p className={styles.helperText}>{linkedin.connected_member}</p>
              )}
              <p className={styles.helperText}>
                {linkedin.community_access_tier === "STANDARD"
                  ? "Standard Community Management access is configured."
                  : linkedin.community_access_tier === "DEVELOPMENT"
                    ? "Development access is configured; production publishing needs Standard access."
                    : "Community Management access tier has not been verified."}
              </p>
              {linkedin.reasons.length > 0 && (
                <ul className={styles.settingsReasons}>
                  {linkedin.reasons.map((reason) => (
                    <li key={reason}>{reason}</li>
                  ))}
                </ul>
              )}
              <p className={styles.helperText}>
                Organization posting connector implemented.
              </p>
              <p className={styles.helperText}>
                App access, member permissions, and the Event Marketing Page are
                checked separately. Credentials stay on the server.
              </p>
              <ConnectionActions
                provider="linkedin"
                value={connections?.linkedin}
                csrf={csrf}
                onRefresh={refreshConnections}
              />
            </div>
          )}
        </section>
        <section
          className={`${styles.reviewCard} ${styles.providerCard}`}
          aria-labelledby="instagram-settings-heading"
        >
          <p className={styles.eyebrow}>Connected destinations</p>
          <h2 id="instagram-settings-heading">Instagram</h2>
          {instagramError && (
            <p role="alert" className={styles.inlineError}>
              {instagramError}
            </p>
          )}
          {!instagram && !instagramError && (
            <p role="status">Checking Instagram…</p>
          )}
          {instagram && (
            <div className={styles.settingsStatus}>
              <StatusLabel
                className={styles.graphicState}
                tone={
                  instagramConnected
                    ? "success"
                    : instagram.status === "UNAVAILABLE"
                      ? "warning"
                      : "neutral"
                }
              >
                {instagramConnected
                  ? "Connected"
                  : instagram.status === "UNAVAILABLE"
                    ? "Connection needs attention"
                    : instagram.status === "SIMULATED"
                      ? "Practice mode"
                      : "Needs setup"}
              </StatusLabel>
              <p>
                <strong>
                  {instagram.status === "CONNECTED"
                    ? "Professional account connected · live readiness is checked before approval"
                    : instagramConnected
                      ? "Professional account ready for live publishing"
                      : instagram.status === "UNAVAILABLE"
                        ? "Instagram connection needs attention"
                        : instagram.mode === "LIVE"
                          ? "Needs setup before live publishing"
                          : "Practice mode · live account not connected"}
                </strong>
              </p>
              {instagram.username && (
                <p className={styles.helperText}>
                  Account: @{instagram.username}
                </p>
              )}
              {instagram.reasons.length > 0 && (
                <ul className={styles.settingsReasons}>
                  {instagram.reasons.map((reason) => (
                    <li key={reason}>{reason}</li>
                  ))}
                </ul>
              )}
              <p className={styles.helperText}>
                Secure media delivery:{" "}
                {instagram.media_delivery_ready ? "Ready" : "Needs setup"}
              </p>
              <p className={styles.helperText}>
                Instagram Login, app permissions, and account access are
                configured separately. Meta app access must be verified before
                production use. Credentials and signed media links stay off the
                screen.
              </p>
              <ConnectionActions
                provider="instagram"
                value={connections?.instagram}
                csrf={csrf}
                onRefresh={refreshConnections}
              />
            </div>
          )}
        </section>
      </div>
    </>
  );
}
