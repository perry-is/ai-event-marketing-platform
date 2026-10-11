import { FormEvent, ReactNode } from "react";
import { Link } from "react-router-dom";
import styles from "./App.module.css";
import { StatusLabel } from "./ui";
import { graphicChangeText, type GraphicAction } from "./graphicText";

type Graphic = {
  preview_url: string;
  width: number;
  height: number;
};

export function CampaignGraphicSection({
  eventId,
  title,
  versionNumber,
  graphic,
  change,
  approval,
  historical,
  stale,
  pending,
  busy,
  error,
  showEventLink,
  request,
  onRequestChange,
  onSubmit,
  brief,
}: {
  eventId: string;
  title: string;
  versionNumber: number;
  graphic: Graphic | null | undefined;
  change: "added" | "updated" | null;
  approval: "active" | "previous" | null;
  historical: boolean;
  stale: boolean;
  pending: GraphicAction | null;
  busy: boolean;
  error: string;
  showEventLink: boolean;
  request: string;
  onRequestChange: (value: string) => void;
  onSubmit: (formEvent: FormEvent) => void;
  brief: ReactNode;
}) {
  const editable = !historical && !stale;
  return (
    <section
      className={`${styles.reviewCard} ${styles.graphicCard}`}
      aria-labelledby="creative-direction-heading"
    >
      <p className={styles.eyebrow}>Part of Version {versionNumber}</p>
      <h2 id="creative-direction-heading">Campaign graphic</h2>
      <div className={styles.graphicLayout}>
        <div className={styles.graphicFigureColumn}>
          {graphic ? (
            <figure
              className={styles.generatedGraphic}
              role="group"
              aria-label={`Campaign graphic in Version ${versionNumber}`}
            >
              <img
                src={graphic.preview_url}
                width={graphic.width}
                height={graphic.height}
                alt={`Campaign graphic for ${title}`}
              />
              <figcaption>
                {approval === "active" ? (
                  <StatusLabel className={styles.graphicState} tone="success">
                    {`Included in approved Version ${versionNumber}`}
                  </StatusLabel>
                ) : historical ? (
                  <StatusLabel className={styles.graphicState} tone="neutral">
                    {`Saved with Version ${versionNumber} · read-only`}
                  </StatusLabel>
                ) : null}
                {change && !historical && (
                  <span className={styles.changedTag}>
                    {graphicChangeText(change, versionNumber)}
                  </span>
                )}
                {!change && !historical && approval !== "active" && (
                  <span className={styles.graphicMeta}>
                    Graphic for Version {versionNumber}
                  </span>
                )}
                {approval === "previous" && (
                  <span className={styles.graphicMeta}>
                    Previously approved
                  </span>
                )}
                <a
                  className={styles.graphicLink}
                  href={graphic.preview_url}
                  target="_blank"
                  rel="noopener"
                >
                  View full size
                  <span className={styles.visuallyHidden}>
                    {" "}
                    graphic for Version {versionNumber} (opens in a new tab)
                  </span>
                </a>
              </figcaption>
            </figure>
          ) : (
            <div
              className={styles.graphicEmpty}
              role="group"
              aria-label="No artwork is included in this campaign version"
            >
              <span aria-hidden="true" className={styles.graphicEmptyIcon}>
                ◇
              </span>
              <strong>
                {historical
                  ? `Version ${versionNumber} doesn’t have a graphic.`
                  : "This campaign doesn’t have a graphic yet."}
              </strong>
              {!historical && (
                <p>
                  I’ll create a background image and place the exact Event name,
                  date, and place on top.
                </p>
              )}
            </div>
          )}
        </div>

        <div className={styles.graphicSide}>
          {editable && (
            <form className={styles.graphicActions} onSubmit={onSubmit}>
              {graphic ? (
                <>
                  <p className={styles.controlKicker}>Change the picture</p>
                  <label htmlFor="graphic-revision">
                    What would you like to change about the graphic?
                  </label>
                  <textarea
                    id="graphic-revision"
                    value={request}
                    onChange={(eventChange) =>
                      onRequestChange(eventChange.target.value)
                    }
                    maxLength={500}
                    aria-describedby="graphic-revision-help"
                    placeholder="For example: Use less orange, or give the title more breathing room."
                  />
                  <p id="graphic-revision-help" className={styles.helperText}>
                    This changes the picture only. The Event name, date, and
                    place on it always come from Event details. You’ll get a new
                    version; Version {versionNumber} stays as it is.
                  </p>
                  <button
                    className={styles.eventPrimary}
                    type="submit"
                    disabled={busy || request.trim().length < 3}
                  >
                    {pending === "revise"
                      ? "Updating the graphic…"
                      : "Revise graphic"}
                  </button>
                </>
              ) : (
                <>
                  <button
                    className={styles.eventPrimary}
                    type="submit"
                    disabled={busy}
                    aria-describedby="graphic-generate-help"
                  >
                    {pending === "generate"
                      ? "Creating your graphic…"
                      : "Generate graphic"}
                  </button>
                  <p id="graphic-generate-help" className={styles.helperText}>
                    Adding a graphic saves a new version of this campaign, so
                    Version {versionNumber} stays unchanged.
                  </p>
                </>
              )}
              <p role="status" className={styles.graphicProgress}>
                {pending === "generate"
                  ? "Creating your graphic… The campaign stays as it is until it’s ready."
                  : pending === "revise"
                    ? "Updating the graphic… The current version stays as it is until the new one is ready."
                    : ""}
              </p>
              {error && (
                <div role="alert" className={styles.inlineError}>
                  <p>{error}</p>
                  {showEventLink && (
                    <Link to={`/events/${eventId}`}>Update Event details</Link>
                  )}
                </div>
              )}
            </form>
          )}
          {historical && (
            <p className={styles.helperText}>
              This is the graphic saved with Version {versionNumber}. Return to
              the current version to change the graphic.
            </p>
          )}
          {!historical && stale && (
            <p className={styles.helperText}>
              The Event changed. Create an updated version before changing the
              graphic.
            </p>
          )}
          {brief}
        </div>
      </div>
    </section>
  );
}

/** Compact "this exact graphic goes with the approval" row for approval and status views. */
export function GraphicIncluded({
  graphic,
  versionNumber,
  approved,
}: {
  graphic: Graphic | null | undefined;
  versionNumber: number;
  approved: boolean;
}) {
  if (!graphic)
    return (
      <p className={styles.graphicIncluded}>
        <span>
          No graphic in Version {versionNumber}. Destinations receive text only.
        </span>
      </p>
    );
  return (
    <div className={styles.graphicIncluded}>
      <img
        src={graphic.preview_url}
        width={graphic.width}
        height={graphic.height}
        alt={`Graphic included in Version ${versionNumber}`}
      />
      <span>
        <strong>Graphic included</strong>
        {approved
          ? ` · the approved graphic from Version ${versionNumber} goes with each destination.`
          : ` · approving Version ${versionNumber} approves this exact graphic with the copy.`}
      </span>
    </div>
  );
}
