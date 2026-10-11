import styles from "./App.module.css";

/**
 * Approved Event Marketing logo master, when one is supplied.
 *
 * No approved logo file exists yet (docs/brand-asset-inventory.md): the supplied JPEGs are
 * flattened reference art and must not be traced or approximated. Until an approved master
 * is added under `public/brand/`, the lockup renders the typographic wordmark only. Set this
 * to the asset's public path (for example "/brand/event-marketing-logo.svg") to show it.
 */
const APPROVED_LOGO_SRC: string | null = null;

/** Event Marketing wordmark with a reserved slot for the approved logo. */
export function BrandLockup({
  tone = "light",
  size = "regular",
  caption,
}: {
  tone?: "light" | "dark";
  size?: "regular" | "large";
  caption?: string;
}) {
  return (
    <div className={styles.brandLockup} data-tone={tone} data-size={size}>
      {APPROVED_LOGO_SRC && (
        <img
          className={styles.brandLogo}
          src={APPROVED_LOGO_SRC}
          alt=""
          aria-hidden="true"
        />
      )}
      <div className={styles.brandText}>
        <span className={styles.brand}>
          Business <span className={styles.brandAccent}>Connect</span>
        </span>
        {caption && <span className={styles.brandCaption}>{caption}</span>}
      </div>
    </div>
  );
}

const journey = ["Event details", "Campaign", "Approval", "Distribution"];

/**
 * Where this Event sits in the overall journey. `current` is the index of the
 * step that needs attention; earlier steps read as done.
 */
export function ProgressSteps({
  current,
  complete = false,
}: {
  current: number;
  complete?: boolean;
}) {
  return (
    <ol className={styles.progressSteps} aria-label="Event progress">
      {journey.map((label, index) => {
        const state =
          index < current || (complete && index === current)
            ? "done"
            : index === current
              ? "current"
              : "upcoming";
        return (
          <li
            key={label}
            data-state={state}
            aria-current={state === "current" ? "step" : undefined}
          >
            <span className={styles.stepMarker} aria-hidden="true">
              {state === "done" ? "✓" : index + 1}
            </span>
            <span>{label}</span>
            <span className={styles.visuallyHidden}>
              {state === "done"
                ? " (done)"
                : state === "current"
                  ? " (current step)"
                  : ""}
            </span>
          </li>
        );
      })}
    </ol>
  );
}

/** Text-first status label with a decorative glyph so status never relies on color. */
export function StatusLabel({
  tone,
  children,
  className,
}: {
  tone: "success" | "warning" | "danger" | "info" | "neutral" | "progress";
  children: string;
  className?: string;
}) {
  const glyph = {
    success: "✓",
    warning: "?",
    danger: "!",
    info: "→",
    neutral: "•",
    progress: "…",
  }[tone];
  return (
    <p className={className} data-tone={tone}>
      <span className={styles.statusGlyph} aria-hidden="true">
        {glyph}
      </span>
      <span>{children}</span>
    </p>
  );
}
