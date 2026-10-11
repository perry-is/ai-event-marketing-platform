import { useCallback, useEffect, useState } from "react";
import {
  Navigate,
  NavLink,
  Route,
  Routes,
  useLocation,
} from "react-router-dom";
import type { components } from "./api.generated";
import styles from "./App.module.css";
import { EventDetail, EventsList, NewEvent, Placeholder } from "./events";
import { CampaignReview } from "./campaigns";
import { SettingsPage } from "./settings";
import { BrandLockup } from "./ui";

type Me = components["schemas"]["Me"];
type State =
  | { kind: "loading" }
  | { kind: "anonymous" }
  | { kind: "unavailable" }
  | { kind: "authenticated"; me: Me };
const sections = [
  { path: "/new", title: "New Event", glyph: "+" },
  { path: "/events", title: "Events", glyph: "▤" },
  { path: "/groups", title: "Groups", glyph: "◎" },
  { path: "/settings", title: "Settings", glyph: "⚙" },
];

function Login() {
  const location = useLocation();
  const [available, setAvailable] = useState<boolean | null>(null);
  useEffect(() => {
    let active = true;
    fetch("/auth/status", { credentials: "same-origin", cache: "no-store" })
      .then(async (response) =>
        response.ok ? (await response.json()).login_available === true : false,
      )
      .then((value) => {
        if (active) setAvailable(value);
      })
      .catch(() => {
        if (active) setAvailable(false);
      });
    return () => {
      active = false;
    };
  }, []);
  return (
    <main className={styles.login}>
      <section className={styles.loginBrandPanel}>
        <BrandLockup tone="dark" size="large" />
        <div className={styles.loginBrandCopy}>
          <p>Events in. Campaigns out.</p>
          <ul>
            <li>Capture event details from anything you have</li>
            <li>Review every campaign version before it goes out</li>
            <li>Approve once, then track each destination</li>
          </ul>
        </div>
        <span className={styles.loginNetwork} aria-hidden="true" />
      </section>
      <div className={styles.loginCard}>
        <div className={styles.loginCardBrand}>
          <BrandLockup />
        </div>
        <p className={styles.eyebrow}>Your private workspace</p>
        <h1>
          Good connections
          <br />
          start here.
        </h1>
        <p className={styles.intro}>
          A quieter place to bring your events together.
        </p>
        {location.search.includes("error=") && (
          <p role="alert" className={styles.alert}>
            We couldn’t complete sign-in. Please try again with your authorized
            Google account.
          </p>
        )}
        {available === true ? (
          <a className={styles.primary} href="/auth/login">
            Continue with Google
          </a>
        ) : (
          <p role="status" className={styles.alert}>
            {available === null
              ? "Checking sign-in availability…"
              : "Sign-in is not available yet. Please contact the person setting up your workspace."}
          </p>
        )}
        <p className={styles.note}>
          Access is limited to authorized Event Marketing accounts.
        </p>
      </div>
    </main>
  );
}

export default function App() {
  const [state, setState] = useState<State>({ kind: "loading" });
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const refresh = useCallback(async () => {
    try {
      const response = await fetch("/api/me", {
        credentials: "same-origin",
        cache: "no-store",
      });
      if (response.status === 401) {
        setState({ kind: "anonymous" });
        return;
      }
      if (!response.ok) {
        setState({ kind: "unavailable" });
        return;
      }
      const me: Me = await response.json();
      setState({ kind: "authenticated", me });
    } catch {
      setState({ kind: "unavailable" });
    }
  }, []);
  useEffect(() => {
    const initialCheck = window.setTimeout(() => {
      void refresh();
    }, 0);
    const onFocus = () => {
      void refresh();
    };
    window.addEventListener("focus", onFocus);
    const interval = window.setInterval(onFocus, 30_000);
    return () => {
      window.clearTimeout(initialCheck);
      window.removeEventListener("focus", onFocus);
      window.clearInterval(interval);
    };
  }, [refresh]);
  const logout = async () => {
    if (state.kind !== "authenticated") return;
    setBusy(true);
    setError("");
    try {
      const response = await fetch("/api/logout", {
        method: "POST",
        credentials: "same-origin",
        headers: { "X-CSRF-Token": state.me.csrf_token },
      });
      if (response.ok || response.status === 401)
        setState({ kind: "anonymous" });
      else setError("We couldn’t sign you out. Please try again.");
    } catch {
      setError("Connection lost. Please reconnect to sign out.");
    } finally {
      setBusy(false);
    }
  };
  if (state.kind === "loading")
    return (
      <p role="status" className={styles.status}>
        Opening your workspace…
      </p>
    );
  if (state.kind === "unavailable")
    return (
      <main className={styles.login}>
        <div className={styles.loginCard}>
          <div className={styles.loginCardBrand}>
            <BrandLockup />
          </div>
          <h1>We can’t connect right now.</h1>
          <p className={styles.intro}>
            Your workspace is temporarily unavailable.
          </p>
          <button
            className={styles.primary}
            onClick={() => {
              void refresh();
            }}
          >
            Try again
          </button>
        </div>
      </main>
    );
  if (state.kind === "anonymous")
    return (
      <Routes>
        <Route path="/login" element={<Login />} />
        <Route path="*" element={<Navigate to="/login" replace />} />
      </Routes>
    );
  return (
    <div className={styles.layout}>
      <a className={styles.skip} href="#content">
        Skip to content
      </a>
      <aside className={styles.sidebar}>
        <BrandLockup tone="dark" caption="Private workspace" />
        <nav aria-label="Main navigation" className={styles.nav}>
          {sections.map((section) => (
            <NavLink key={section.path} to={section.path}>
              <span className={styles.navGlyph} aria-hidden="true">
                {section.glyph}
              </span>
              {section.title}
            </NavLink>
          ))}
        </nav>
        <p className={styles.sidebarFooter}>
          Event campaigns, reviewed before they go out.
        </p>
      </aside>
      <div className={styles.main}>
        <header className={styles.topbar}>
          <span className={styles.userChip}>
            <span className={styles.userInitial} aria-hidden="true">
              {state.me.display_name.trim().charAt(0).toUpperCase() || "•"}
            </span>
            <span>{state.me.display_name}</span>
          </span>
          <button
            className={styles.logout}
            disabled={busy}
            onClick={() => {
              void logout();
            }}
          >
            {busy ? "Signing out…" : "Sign out"}
          </button>
        </header>
        <main id="content" className={styles.content}>
          {error && (
            <p role="alert" className={styles.alert}>
              {error}
            </p>
          )}
          <Routes>
            <Route
              path="/new"
              element={
                <NewEvent csrf={state.me.csrf_token} onUnauthorized={refresh} />
              }
            />
            <Route
              path="/events"
              element={
                <EventsList
                  csrf={state.me.csrf_token}
                  onUnauthorized={refresh}
                />
              }
            />
            <Route
              path="/events/:eventId"
              element={
                <EventDetail
                  csrf={state.me.csrf_token}
                  onUnauthorized={refresh}
                />
              }
            />
            <Route
              path="/events/:eventId/campaign"
              element={
                <CampaignReview
                  csrf={state.me.csrf_token}
                  onUnauthorized={refresh}
                />
              }
            />
            <Route
              path="/groups"
              element={
                <Placeholder
                  title="Groups"
                  message="Manual group selection will arrive in a later implementation package."
                />
              }
            />
            <Route
              path="/settings"
              element={
                <SettingsPage
                  csrf={state.me.csrf_token}
                  onUnauthorized={refresh}
                />
              }
            />
            <Route path="*" element={<Navigate to="/new" replace />} />
          </Routes>
        </main>
        <footer className={styles.appFooter}>
          <span>
            Business <span className={styles.brandAccent}>Connect</span>
          </span>
          <span>Private workspace · authorized accounts only</span>
        </footer>
      </div>
    </div>
  );
}
