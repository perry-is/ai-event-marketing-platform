import {
  ChangeEvent,
  FormEvent,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import styles from "./App.module.css";
import { ProgressSteps } from "./ui";

type Source = {
  id: string;
  kind: string;
  filename: string | null;
  content_type: string;
  byte_count: number;
  processing_status: string;
  can_retry_extraction: boolean;
  created_at: string;
};
type Proposal = {
  id: string;
  field_path: string;
  candidate_value: unknown;
  confidence: "HIGH" | "MEDIUM" | "LOW";
  source_id: string;
  locator: string;
  short_excerpt: string;
  uncertainty_reason: string | null;
  status: string;
};
type Candidate = {
  label: string;
  value: unknown;
  source_id: string | null;
  reason?: string | null;
};
type Clarification = {
  id: string;
  kind: string;
  field_path: string;
  question: string;
  candidates: Candidate[];
  status: string;
  answer: string | null;
  validation_message?: string | null;
  suggested_venue?: {
    name: string;
    street: string;
    city: string;
    region: string;
    postal_code: string;
    country: string;
  } | null;
};
type EventView = {
  id: string;
  revision: number;
  title: string | null;
  starts_at: string | null;
  ends_at: string | null;
  timezone: string | null;
  delivery: string | null;
  facts: {
    schedule_local_start: string | null;
    schedule_local_end: string | null;
    location: Record<string, string>;
    people: Record<string, string>;
    registration: Record<string, string | number | undefined>;
    family_label?: string | null;
    topic?: string | null;
    content_facts?: { description?: string | null };
  };
  proposals: Proposal[];
  clarifications: Clarification[];
  readiness: {
    ready: boolean;
    missing_fields: string[];
    open_question_count: number;
  };
  state: string;
  sources: Source[];
};
type EventRow = {
  id: string;
  revision: number;
  title: string;
  starts_at: string | null;
  timezone: string | null;
  delivery: string | null;
  state: string;
  readiness: {
    ready: boolean;
    missing_fields: string[];
    open_question_count: number;
  };
  updated_at: string;
};

const filePolicy: Record<string, number> = {
  ".txt": 2 * 1024 * 1024,
  ".md": 2 * 1024 * 1024,
  ".pdf": 20 * 1024 * 1024,
  ".jpg": 10 * 1024 * 1024,
  ".jpeg": 10 * 1024 * 1024,
  ".png": 10 * 1024 * 1024,
  ".webp": 10 * 1024 * 1024,
};

function messageFor(code: string) {
  const messages: Record<string, string> = {
    FILE_TYPE_NOT_ALLOWED:
      "That file type is not supported. Choose a PDF, image, TXT, or MD file.",
    FILE_TOO_LARGE: "That file is larger than the limit for its file type.",
    TEXT_TOO_LARGE:
      "That text is too long to save. Please shorten it and try again.",
    INVALID_FILE:
      "We couldn’t safely read that file. Try another copy or paste its key details.",
    EVENT_SOURCE_LIMIT_EXCEEDED:
      "This Event has reached its source-material storage limit.",
    REVISION_CONFLICT:
      "This Event changed in another view. Refresh the details and try again.",
    SCHEDULE_ANSWER_INVALID:
      "Please include a complete date, year, and start time.",
    TIMEZONE_INVALID: "Enter a recognized time zone, such as America/Denver.",
    DELIVERY_INVALID: "Choose in person, virtual, or hybrid.",
    CAPACITY_INVALID: "Enter the capacity as a whole number of attendees.",
  };
  return (
    messages[code] ??
    "We couldn’t save that change. Please review the details and try again."
  );
}

async function api<T>(
  path: string,
  csrf: string,
  onUnauthorized: () => void,
  init: RequestInit = {},
): Promise<T> {
  const headers = new Headers(init.headers);
  if (init.method && !["GET", "HEAD"].includes(init.method.toUpperCase()))
    headers.set("X-CSRF-Token", csrf);
  const response = await fetch(path, {
    ...init,
    headers,
    credentials: "same-origin",
    cache: "no-store",
  });
  if (response.status === 401) {
    onUnauthorized();
    throw new Error("Your session ended. Please sign in again.");
  }
  if (!response.ok) {
    const payload = await response.json().catch(() => null);
    const code = payload?.error?.code ?? "REQUEST_FAILED";
    throw new Error(messageFor(code));
  }
  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

function json(method: string, value: unknown): RequestInit {
  return {
    method,
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(value),
  };
}

function validateFile(file: File | undefined): string | null {
  if (!file) return null;
  const suffix = `.${file.name.split(".").at(-1) ?? ""}`.toLowerCase();
  if (!(suffix in filePolicy)) return messageFor("FILE_TYPE_NOT_ALLOWED");
  if (file.size > filePolicy[suffix]) return messageFor("FILE_TOO_LARGE");
  return null;
}

function DateText({ event }: { event: EventView | EventRow }) {
  if (!event.starts_at) return <span>Date and time not set</span>;
  const date = new Date(event.starts_at);
  const timezone = event.timezone ?? "America/Denver";
  const zoneLabel =
    timezone === "America/Denver"
      ? "Mountain Time"
      : timezone === "America/Los_Angeles"
        ? "Pacific Time"
        : timezone === "America/Chicago"
          ? "Central Time"
          : timezone === "America/New_York"
            ? "Eastern Time"
            : timezone;
  return (
    <span>
      {new Intl.DateTimeFormat("en-US", {
        weekday: "long",
        month: "long",
        day: "numeric",
        year: "numeric",
        hour: "numeric",
        minute: "2-digit",
        timeZone: timezone,
      }).format(date)}{" "}
      · {zoneLabel}
    </span>
  );
}

function proposalValue(proposal: Proposal): string {
  if (
    proposal.field_path === "schedule" &&
    typeof proposal.candidate_value === "object" &&
    proposal.candidate_value !== null &&
    "local_start" in proposal.candidate_value
  ) {
    const schedule = proposal.candidate_value as {
      local_start?: string | null;
      timezone?: string | null;
      local_end?: string | null;
      date_only?: string | null;
      start_time?: string | null;
    };
    if (!schedule.local_start && !schedule.date_only)
      return "Date and time need your input";
    if (!schedule.local_start && schedule.date_only) {
      return `${schedule.date_only} · ${schedule.start_time ?? "start time needed"} · Mountain Time`;
    }
    if (!schedule.local_start) return "Date and time need your input";
    const [date, time] = schedule.local_start.split("T");
    const [year, month, day] = date.split("-").map(Number);
    const [hour, minute] = time.split(":").map(Number);
    const dateLabel = new Intl.DateTimeFormat("en-US", {
      month: "long",
      day: "numeric",
      year: "numeric",
      timeZone: "UTC",
    }).format(new Date(Date.UTC(year, month - 1, day)));
    const timeLabel = new Intl.DateTimeFormat("en-US", {
      hour: "numeric",
      minute: "2-digit",
      timeZone: "UTC",
    }).format(new Date(Date.UTC(2026, 0, 1, hour, minute)));
    const zone =
      schedule.timezone === "America/Denver"
        ? "Mountain Time"
        : (schedule.timezone ?? "Mountain Time");
    return `${dateLabel} at ${timeLabel} · ${zone}`;
  }
  if (typeof proposal.candidate_value === "string")
    return proposal.candidate_value;
  if (typeof proposal.candidate_value === "number")
    return String(proposal.candidate_value);
  return "Review this suggested detail";
}

function scheduleCandidate(
  question: Clarification,
): Record<string, string | null> | null {
  const candidate = question.candidates.find(
    (item) => typeof item.value === "object" && item.value !== null,
  )?.value;
  return candidate && typeof candidate === "object"
    ? (candidate as Record<string, string | null>)
    : null;
}

const fieldLabels: Record<string, string> = {
  title: "Event",
  schedule: "Date and time",
  timezone: "Time zone",
  delivery: "Format",
  "facts.location.venue": "Location",
  "facts.location.address": "Address",
  "facts.location.join_url": "Online link",
  "facts.location.join_instructions": "How to join",
  "facts.people.host": "Host",
  "facts.registration.price_kind": "Price",
  "facts.registration.url": "Registration",
  "facts.topic": "Topic",
};

function fieldLabel(path: string): string {
  return (
    fieldLabels[path] ??
    (path.split(".").at(-1) ?? path)
      .replaceAll("_", " ")
      .replace(/^./, (char) => char.toUpperCase())
  );
}

function sentenceCase(value: string): string {
  const text = value.replaceAll("_", " ").toLowerCase();
  return text.charAt(0).toUpperCase() + text.slice(1);
}

function updatedLabel(value: string): string {
  return new Intl.DateTimeFormat("en-US", {
    month: "short",
    day: "numeric",
    year: "numeric",
  }).format(new Date(value));
}

function StateBadge({ state }: { state: string }) {
  const labels: Record<string, string> = {
    NEEDS_REVIEW: "Review details",
    NEEDS_DETAILS: "Needs a few details",
    READY: "Ready for next step",
  };
  return (
    <span className={styles.eventBadge}>{labels[state] ?? "In progress"}</span>
  );
}

export function NewEvent({
  csrf,
  onUnauthorized,
}: {
  csrf: string;
  onUnauthorized: () => void;
}) {
  const navigate = useNavigate();
  const [text, setText] = useState("");
  const [file, setFile] = useState<File>();
  const [inputMethod, setInputMethod] = useState<"TYPED" | "PASTED">("PASTED");
  const pastePending = useRef(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const fileInput = useRef<HTMLInputElement>(null);
  const [dragging, setDragging] = useState(false);
  const chooseFile = (selected: File | undefined) => {
    const problem = validateFile(selected);
    setError(problem ?? "");
    setFile(problem ? undefined : selected);
  };
  const changeFile = (event: ChangeEvent<HTMLInputElement>) => {
    chooseFile(event.target.files?.[0]);
  };
  const removeFile = () => {
    setFile(undefined);
    if (fileInput.current) fileInput.current.value = "";
  };
  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (!text.trim() && !file) {
      setError(
        "Add a few event details or choose a supported file to get started.",
      );
      return;
    }
    const problem = validateFile(file);
    if (problem) {
      setError(problem);
      return;
    }
    setBusy(true);
    setError("");
    try {
      const created = await api<EventView>(
        "/api/events",
        csrf,
        onUnauthorized,
        json("POST", { text: text.trim() || null, input_method: inputMethod }),
      );
      if (file) {
        const form = new FormData();
        form.set("file", file);
        form.set("input_method", "PASTED");
        await api<EventView>(
          `/api/events/${created.id}/sources`,
          csrf,
          onUnauthorized,
          {
            method: "POST",
            body: form,
          },
        );
      }
      navigate(`/events/${created.id}`);
    } catch (reason) {
      setError(
        reason instanceof Error
          ? reason.message
          : "We couldn’t create the Event.",
      );
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className={styles.eventPage}>
      <p className={styles.eyebrow}>Start with what you have</p>
      <h1>Tell me about the event.</h1>
      <p className={styles.eventLead}>
        A few notes, an email, or a rough idea are enough. You can review every
        detail before it becomes part of the Event.
      </p>
      <form className={styles.intakeCard} onSubmit={submit}>
        <label className={styles.inputLabel} htmlFor="event-notes">
          What do you know so far?
        </label>
        <textarea
          id="event-notes"
          className={styles.eventTextarea}
          value={text}
          onChange={(event) => {
            setText(event.target.value);
            setInputMethod(pastePending.current ? "PASTED" : "TYPED");
            pastePending.current = false;
          }}
          onPaste={() => {
            pastePending.current = true;
          }}
          maxLength={2 * 1024 * 1024}
          placeholder="Paste an email or jot down the date, place, and anything else you know…"
        />
        <div
          className={styles.dropzone}
          data-dragging={dragging || undefined}
          onDragOver={(dragEvent) => {
            dragEvent.preventDefault();
            setDragging(true);
          }}
          onDragLeave={() => setDragging(false)}
          onDrop={(dragEvent) => {
            dragEvent.preventDefault();
            setDragging(false);
            chooseFile(dragEvent.dataTransfer.files[0]);
          }}
        >
          <input
            id="event-file"
            ref={fileInput}
            className={styles.visuallyHidden}
            type="file"
            accept=".txt,.md,.pdf,.jpg,.jpeg,.png,.webp"
            aria-describedby="event-file-help"
            onChange={changeFile}
          />
          <label className={styles.dropzoneLabel} htmlFor="event-file">
            Add a flyer or document <span>(optional)</span>
          </label>
          <p id="event-file-help" className={styles.helperText}>
            Drop a file here or choose one. PDF, JPG, PNG, WebP, TXT, or MD.
            Uploaded originals stay private.
          </p>
          {file && (
            <div className={styles.fileChip}>
              <span>
                {file.name} · {(file.size / 1024).toFixed(0)} KB
              </span>
              <button
                type="button"
                onClick={removeFile}
                aria-label={`Remove ${file.name}`}
              >
                Remove
              </button>
            </div>
          )}
        </div>
        {error && (
          <p role="alert" className={styles.inlineError}>
            {error}
          </p>
        )}
        <button className={styles.eventPrimary} disabled={busy} type="submit">
          {busy ? "Reading event details…" : "Create Event"}
        </button>
        <p className={styles.nextHint}>
          Next, I’ll show you what I found and ask about anything unclear.
          Nothing is published.
        </p>
      </form>
    </div>
  );
}

export function EventsList({
  csrf,
  onUnauthorized,
}: {
  csrf: string;
  onUnauthorized: () => void;
}) {
  const [events, setEvents] = useState<EventRow[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  useEffect(() => {
    let active = true;
    api<EventRow[]>("/api/events", csrf, onUnauthorized)
      .then((value) => {
        if (active) setEvents(value);
      })
      .catch((reason: unknown) => {
        if (active)
          setError(
            reason instanceof Error ? reason.message : "Couldn’t load Events.",
          );
      })
      .finally(() => {
        if (active) setLoading(false);
      });
    return () => {
      active = false;
    };
  }, [csrf, onUnauthorized]);
  return (
    <div className={styles.eventPage}>
      <div className={styles.pageHeadingRow}>
        <div>
          <p className={styles.eyebrow}>Your workspace</p>
          <h1>Events</h1>
        </div>
        <Link className={styles.eventPrimaryLink} to="/new">
          New Event
        </Link>
      </div>
      <p className={styles.eventLead}>
        The details you’re gathering and refining.
      </p>
      {error && (
        <p role="alert" className={styles.inlineError}>
          {error}
        </p>
      )}
      {loading ? (
        <p role="status">Loading Events…</p>
      ) : events.length === 0 ? (
        <section className={styles.emptyState}>
          <h2>Your next event starts here.</h2>
          <p>Bring the notes you have and we’ll help organize the details.</p>
          <Link className={styles.eventPrimaryLink} to="/new">
            Create an Event
          </Link>
        </section>
      ) : (
        <div className={styles.eventList}>
          {events.map((event) => (
            <Link
              className={styles.eventRow}
              to={`/events/${event.id}`}
              key={event.id}
            >
              <div className={styles.eventRowMain}>
                <h2>{event.title}</h2>
                <p>
                  <DateText event={event} />
                </p>
                <p className={styles.updatedLabel}>
                  Updated {updatedLabel(event.updated_at)}
                </p>
              </div>
              <StateBadge state={event.state} />
            </Link>
          ))}
        </div>
      )}
    </div>
  );
}

export function EventDetail({
  csrf,
  onUnauthorized,
}: {
  csrf: string;
  onUnauthorized: () => void;
}) {
  const { eventId = "" } = useParams();
  const [event, setEvent] = useState<EventView>();
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [processingLabel, setProcessingLabel] = useState("");
  const [error, setError] = useState("");
  const [answers, setAnswers] = useState<Record<string, string>>({});
  const [scheduleAnswers, setScheduleAnswers] = useState<
    Record<string, { date: string; start: string; end: string }>
  >({});
  const [descriptionSuggestion, setDescriptionSuggestion] = useState("");
  const [descriptionBusy, setDescriptionBusy] = useState(false);
  const [descriptionTone, setDescriptionTone] = useState("");
  const [descriptionInstruction, setDescriptionInstruction] = useState("");
  const [manualDescription, setManualDescription] = useState("");
  const [editing, setEditing] = useState(false);
  const [editTitle, setEditTitle] = useState("");
  const [editSchedule, setEditSchedule] = useState("");
  const [editTimezone, setEditTimezone] = useState("");
  const [editDelivery, setEditDelivery] = useState("");
  const [editVenue, setEditVenue] = useState("");
  const [editAddress, setEditAddress] = useState("");
  const [editAdmission, setEditAdmission] = useState("");
  const [editCapacity, setEditCapacity] = useState("");
  const pending = useMemo(
    () => event?.proposals.filter((item) => item.status === "PENDING") ?? [],
    [event],
  );
  const openQuestions = useMemo(
    () => event?.clarifications.filter((item) => item.status === "OPEN") ?? [],
    [event],
  );
  useEffect(() => {
    let active = true;
    api<EventView>(`/api/events/${eventId}`, csrf, onUnauthorized)
      .then((value) => {
        if (active) {
          setEvent(value);
          setManualDescription(value.facts.content_facts?.description ?? "");
          setEditTitle(value.title ?? "");
          setEditSchedule(value.facts.schedule_local_start?.slice(0, 16) ?? "");
          setEditTimezone(value.timezone ?? "");
          setEditDelivery(value.delivery ?? "");
          setEditVenue(
            value.facts.location.venue ?? value.facts.location.address ?? "",
          );
          setEditAddress(value.facts.location.address ?? "");
          setEditAdmission(String(value.facts.registration.price_kind ?? ""));
          setEditCapacity(
            value.facts.registration.capacity != null
              ? String(value.facts.registration.capacity)
              : "",
          );
        }
      })
      .catch((reason: unknown) => {
        if (active)
          setError(
            reason instanceof Error
              ? reason.message
              : "Couldn’t load this Event.",
          );
      })
      .finally(() => {
        if (active) setLoading(false);
      });
    return () => {
      active = false;
    };
  }, [eventId, csrf, onUnauthorized]);

  const accept = async () => {
    if (!event) return;
    setBusy(true);
    setError("");
    try {
      const value = await api<EventView>(
        `/api/events/${event.id}/accept-proposals`,
        csrf,
        onUnauthorized,
        json("POST", { expected_revision: event.revision }),
      );
      setEvent(value);
    } catch (reason) {
      setError(
        reason instanceof Error
          ? reason.message
          : "Couldn’t use those details.",
      );
    } finally {
      setBusy(false);
    }
  };
  const answer = async (question: Clarification) => {
    if (!event) return;
    setBusy(true);
    setError("");
    try {
      const value = await api<EventView>(
        `/api/events/${event.id}/clarifications/${question.id}`,
        csrf,
        onUnauthorized,
        json("POST", {
          expected_revision: event.revision,
          answer:
            question.field_path === "delivery"
              ? answers[question.id] === "IN_PERSON,VIRTUAL"
                ? "hybrid"
                : answers[question.id] === "IN_PERSON"
                  ? "in person"
                  : answers[question.id] === "VIRTUAL"
                    ? "online"
                    : ""
              : question.field_path === "schedule"
                ? (() => {
                    const draft = scheduleAnswers[question.id] ?? {
                      date: scheduleCandidate(question)?.date_only ?? "",
                      start:
                        scheduleCandidate(question)?.start_time ??
                        scheduleCandidate(question)?.local_start?.slice(
                          11,
                          16,
                        ) ??
                        "",
                      end:
                        scheduleCandidate(question)?.end_time ??
                        scheduleCandidate(question)?.local_end?.slice(11, 16) ??
                        "",
                    };
                    const zone = event.timezone ?? "America/Denver";
                    return `${draft.date}T${draft.start}|${draft.end ? `${draft.date}T${draft.end}` : ""}|${zone}`;
                  })()
                : (answers[question.id] ?? ""),
        }),
      );
      setEvent(value);
      setAnswers((previous) => ({ ...previous, [question.id]: "" }));
    } catch (reason) {
      setError(
        reason instanceof Error ? reason.message : "Couldn’t save that answer.",
      );
      void api<EventView>(`/api/events/${event.id}`, csrf, onUnauthorized)
        .then(setEvent)
        .catch(() => undefined);
    } finally {
      setBusy(false);
    }
  };
  const requestDescription = async (refine = false) => {
    if (!event) return;
    setDescriptionBusy(true);
    setError("");
    try {
      const result = await api<{ description: string }>(
        `/api/events/${event.id}/description-suggestion`,
        csrf,
        onUnauthorized,
        json("POST", {
          tone: descriptionTone || null,
          instruction: descriptionInstruction.trim() || null,
          current_description: refine ? manualDescription : null,
        }),
      );
      setDescriptionSuggestion(result.description);
    } catch (reason) {
      setError(
        reason instanceof Error
          ? reason.message
          : "Couldn’t prepare a description suggestion.",
      );
    } finally {
      setDescriptionBusy(false);
    }
  };
  const saveDescription = async (value: string) => {
    if (!event || !value.trim()) return;
    setBusy(true);
    setError("");
    try {
      const updated = await api<EventView>(
        `/api/events/${event.id}/facts`,
        csrf,
        onUnauthorized,
        json("PATCH", {
          expected_revision: event.revision,
          changes: [
            {
              field_path: "facts.content_facts.description",
              value: value.trim(),
            },
          ],
        }),
      );
      setEvent(updated);
      setManualDescription(value.trim());
      setDescriptionSuggestion("");
    } catch (reason) {
      setError(
        reason instanceof Error
          ? reason.message
          : "Couldn’t save the description.",
      );
    } finally {
      setBusy(false);
    }
  };
  const acceptSuggestedVenue = async (question: Clarification) => {
    if (!event || !question.suggested_venue) return;
    setBusy(true);
    setError("");
    try {
      const value = await api<EventView>(
        `/api/events/${event.id}/suggested-venue`,
        csrf,
        onUnauthorized,
        json("POST", { expected_revision: event.revision }),
      );
      setEvent(value);
      setEditVenue(value.facts.location.venue ?? "");
      setEditAddress(value.facts.location.address ?? "");
    } catch (reason) {
      setError(
        reason instanceof Error ? reason.message : "Couldn’t use that venue.",
      );
    } finally {
      setBusy(false);
    }
  };
  const saveCorrections = async (formEvent: FormEvent) => {
    formEvent.preventDefault();
    if (!event) return;
    const changes: { field_path: string; value: string }[] = [];
    if (editTitle.trim())
      changes.push({ field_path: "title", value: editTitle.trim() });
    if (editSchedule)
      changes.push({ field_path: "schedule", value: editSchedule });
    if (editTimezone.trim())
      changes.push({ field_path: "timezone", value: editTimezone.trim() });
    if (editDelivery)
      changes.push({ field_path: "delivery", value: editDelivery });
    if (editVenue.trim())
      changes.push({
        field_path: "facts.location.venue",
        value: editVenue.trim(),
      });
    if (editAddress.trim())
      changes.push({
        field_path: "facts.location.address",
        value: editAddress.trim(),
      });
    if (editAdmission)
      changes.push({
        field_path: "facts.registration.price_kind",
        value: editAdmission,
      });
    if (editCapacity.trim())
      changes.push({
        field_path: "facts.registration.capacity",
        value: editCapacity.trim(),
      });
    if (!changes.length) return;
    setBusy(true);
    setError("");
    try {
      const value = await api<EventView>(
        `/api/events/${event.id}/facts`,
        csrf,
        onUnauthorized,
        json("PATCH", { expected_revision: event.revision, changes }),
      );
      setEvent(value);
      setEditing(false);
    } catch (reason) {
      setError(
        reason instanceof Error
          ? reason.message
          : "Couldn’t save those corrections.",
      );
    } finally {
      setBusy(false);
    }
  };
  const addSource = async (selected: File | undefined) => {
    if (!event || !selected) return;
    const problem = validateFile(selected);
    if (problem) {
      setError(problem);
      return;
    }
    const form = new FormData();
    form.set("file", selected);
    setBusy(true);
    setProcessingLabel("Reading your source…");
    setError("");
    try {
      const value = await api<EventView>(
        `/api/events/${event.id}/sources`,
        csrf,
        onUnauthorized,
        { method: "POST", body: form },
      );
      setEvent(value);
    } catch (reason) {
      setError(
        reason instanceof Error ? reason.message : "Couldn’t add that source.",
      );
    } finally {
      setBusy(false);
      setProcessingLabel("");
    }
  };

  const retrySource = async (source: Source) => {
    if (!event || !source.can_retry_extraction) return;
    setBusy(true);
    setProcessingLabel("Trying to read your source…");
    setError("");
    try {
      const value = await api<EventView>(
        `/api/events/${event.id}/sources/${source.id}/retry-extraction`,
        csrf,
        onUnauthorized,
        json("POST", {}),
      );
      setEvent(value);
    } catch (reason) {
      setError(
        reason instanceof Error
          ? reason.message
          : "Couldn’t reread that source.",
      );
    } finally {
      setBusy(false);
      setProcessingLabel("");
    }
  };

  if (loading) return <p role="status">Opening your Event…</p>;
  if (!event)
    return (
      <div className={styles.eventPage}>
        <p role="alert" className={styles.inlineError}>
          {error || "This Event could not be found."}
        </p>
        <Link to="/events">Back to Events</Link>
      </div>
    );
  const location =
    event.facts.location.venue ??
    event.facts.location.address ??
    event.facts.location.join_instructions ??
    event.facts.location.join_url;
  const host = event.facts.people.host;
  const price = event.facts.registration.price_kind as string | undefined;
  return (
    <div className={styles.eventPage}>
      <Link to="/events" className={styles.backLink}>
        ← All Events
      </Link>
      <div className={styles.pageHeadingRow}>
        <div>
          <p className={styles.eyebrow}>Event details</p>
          <h1>{event.title || "Your new Event"}</h1>
        </div>
        <StateBadge state={event.state} />
      </div>
      <ProgressSteps current={event.readiness.ready ? 1 : 0} />
      {error && (
        <p role="alert" className={styles.inlineError}>
          {error}
        </p>
      )}
      {processingLabel && busy && <p role="status">{processingLabel}</p>}
      {pending.length > 0 && (
        <section className={styles.reviewCard} aria-labelledby="found-heading">
          <p className={styles.eyebrow}>From your notes</p>
          <h2 id="found-heading">Here’s what I found.</h2>
          <ul className={styles.proposalList}>
            {pending.map((proposal) => (
              <li
                key={proposal.id}
                data-uncertain={
                  proposal.uncertainty_reason || proposal.confidence !== "HIGH"
                    ? true
                    : undefined
                }
              >
                <div>
                  <strong>{fieldLabel(proposal.field_path)}</strong>
                  <span>{proposalValue(proposal)}</span>
                </div>
                {(proposal.uncertainty_reason ||
                  proposal.confidence !== "HIGH") && (
                  <p className={styles.checkNote}>
                    <span aria-hidden="true">?</span>
                    {proposal.uncertainty_reason ||
                      "Please double-check this detail."}
                  </p>
                )}
              </li>
            ))}
          </ul>
          <p className={styles.helperText}>
            These are suggestions from your source. Nothing becomes part of the
            Event until you choose to use them.
          </p>
          <button
            className={styles.eventPrimary}
            onClick={accept}
            disabled={busy}
          >
            {busy ? "Saving details…" : "Use these details"}
          </button>
        </section>
      )}

      {openQuestions.length > 0 && (
        <section
          className={styles.questionsSection}
          aria-labelledby="questions-heading"
        >
          <p className={styles.eyebrow}>
            Needs your answer · {openQuestions.length}{" "}
            {openQuestions.length === 1 ? "question" : "questions"}
          </p>
          <h2 id="questions-heading">Just a couple things I need.</h2>
          {openQuestions.map((question) => (
            <form
              className={styles.questionCard}
              key={question.id}
              onSubmit={(formEvent) => {
                formEvent.preventDefault();
                void answer(question);
              }}
            >
              <label htmlFor={`answer-${question.id}`}>
                {question.question}
              </label>
              {question.validation_message && (
                <p className={styles.inlineError}>
                  {question.validation_message}
                </p>
              )}
              {question.candidates.length > 0 &&
                question.field_path !== "schedule" && (
                  <div className={styles.candidateList}>
                    {question.candidates.map((candidate, index) => (
                      <button
                        key={`${question.id}-${index}`}
                        type="button"
                        className={styles.candidateButton}
                        onClick={() =>
                          setAnswers((previous) => ({
                            ...previous,
                            [question.id]: candidate.label,
                          }))
                        }
                      >
                        Use “{candidate.label}”
                      </button>
                    ))}
                  </div>
                )}
              {question.candidates.map((candidate, index) =>
                candidate.reason ? (
                  <p className={styles.checkNote} key={`reason-${index}`}>
                    {candidate.reason}
                  </p>
                ) : null,
              )}
              {question.field_path === "schedule" ? (
                (() => {
                  const candidate = scheduleCandidate(question);
                  const current = scheduleAnswers[question.id] ?? {
                    date:
                      candidate?.date_only ??
                      candidate?.local_start?.slice(0, 10) ??
                      "",
                    start:
                      candidate?.start_time ??
                      candidate?.local_start?.slice(11, 16) ??
                      "",
                    end:
                      candidate?.end_time ??
                      candidate?.local_end?.slice(11, 16) ??
                      "",
                  };
                  return (
                    <div className={styles.correctionForm}>
                      <p className={styles.helperText}>
                        Choose the date and time. Times use Mountain Time unless
                        the source says otherwise.
                      </p>
                      <label>
                        Date
                        <input
                          aria-label="Event date"
                          type="date"
                          value={current.date}
                          onChange={(e) =>
                            setScheduleAnswers((state) => ({
                              ...state,
                              [question.id]: {
                                ...current,
                                date: e.target.value,
                              },
                            }))
                          }
                        />
                      </label>
                      <label>
                        Starts
                        <input
                          aria-label="Start time"
                          type="time"
                          value={current.start}
                          onChange={(e) =>
                            setScheduleAnswers((state) => ({
                              ...state,
                              [question.id]: {
                                ...current,
                                start: e.target.value,
                              },
                            }))
                          }
                        />
                      </label>
                      <label>
                        Ends (optional)
                        <input
                          aria-label="End time"
                          type="time"
                          value={current.end}
                          onChange={(e) =>
                            setScheduleAnswers((state) => ({
                              ...state,
                              [question.id]: {
                                ...current,
                                end: e.target.value,
                              },
                            }))
                          }
                        />
                      </label>
                      {candidate?.past_date && (
                        <p className={styles.inlineError}>
                          This suggested date has passed. Check the year before
                          saving.
                        </p>
                      )}
                      <button
                        className={styles.eventPrimary}
                        type="submit"
                        disabled={busy || !current.date || !current.start}
                      >
                        Save date and time
                      </button>
                    </div>
                  );
                })()
              ) : question.field_path === "delivery" ? (
                <fieldset className={styles.deliveryChoices}>
                  <legend>How can people attend?</legend>
                  {[
                    ["IN_PERSON", "In person"],
                    ["VIRTUAL", "Online"],
                  ].map(([value, label]) => {
                    const selected = (answers[question.id] ?? "")
                      .split(",")
                      .filter(Boolean);
                    return (
                      <label key={value}>
                        <input
                          type="checkbox"
                          checked={selected.includes(value)}
                          onChange={(e) => {
                            const next = e.target.checked
                              ? [...selected, value]
                              : selected.filter((item) => item !== value);
                            setAnswers((state) => ({
                              ...state,
                              [question.id]: next.sort().join(","),
                            }));
                          }}
                        />
                        {label}
                      </label>
                    );
                  })}
                  <button
                    className={styles.eventPrimary}
                    type="submit"
                    disabled={busy || !(answers[question.id] ?? "").trim()}
                  >
                    Save format
                  </button>
                </fieldset>
              ) : (
                <div className={styles.answerRow}>
                  <input
                    id={`answer-${question.id}`}
                    value={answers[question.id] ?? ""}
                    onChange={(event) =>
                      setAnswers((previous) => ({
                        ...previous,
                        [question.id]: event.target.value,
                      }))
                    }
                    placeholder={"Type your answer"}
                  />
                  <button
                    className={styles.eventPrimary}
                    disabled={busy || !(answers[question.id] ?? "").trim()}
                    type="submit"
                  >
                    Save answer
                  </button>
                </div>
              )}
              {question.suggested_venue && (
                <div className={styles.suggestedVenue}>
                  <p>
                    <strong>{question.suggested_venue.name}</strong>
                    <br />
                    {question.suggested_venue.street}
                    <br />
                    {question.suggested_venue.city},{" "}
                    {question.suggested_venue.region}{" "}
                    {question.suggested_venue.postal_code}
                  </p>
                  <button
                    type="button"
                    className={styles.candidateButton}
                    disabled={busy}
                    onClick={() => void acceptSuggestedVenue(question)}
                  >
                    Use this venue
                  </button>
                  <p className={styles.helperText}>
                    Suggested location · only added to your Event if you choose
                    it.
                  </p>
                </div>
              )}
            </form>
          ))}
        </section>
      )}

      <section className={styles.summaryCard} aria-labelledby="summary-heading">
        <div className={styles.summaryHeading}>
          <div>
            <p className={styles.eyebrow}>Event facts · source of truth</p>
            <h2 id="summary-heading">Here’s what we know.</h2>
            <p className={styles.helperText}>
              Every campaign uses these facts. Change dates, places, or prices
              here rather than in campaign copy.
            </p>
          </div>
          <button
            className={styles.secondaryButton}
            onClick={() => setEditing((value) => !value)}
          >
            {editing ? "Cancel edit" : "Edit details"}
          </button>
        </div>
        {editing ? (
          <form className={styles.correctionForm} onSubmit={saveCorrections}>
            <label>
              Event name
              <input
                value={editTitle}
                onChange={(event) => setEditTitle(event.target.value)}
                maxLength={240}
              />
            </label>
            <label>
              Date and start time
              <input
                type="datetime-local"
                value={editSchedule}
                onChange={(event) => setEditSchedule(event.target.value)}
              />
            </label>
            <label>
              Time zone
              <select
                value={editTimezone}
                onChange={(event) => setEditTimezone(event.target.value)}
              >
                <option value="America/Denver">Mountain Time</option>
                <option value="America/Los_Angeles">Pacific Time</option>
                <option value="America/Chicago">Central Time</option>
                <option value="America/New_York">Eastern Time</option>
                <option value="UTC">UTC</option>
              </select>
            </label>
            <label>
              Delivery
              <select
                value={editDelivery}
                onChange={(event) => setEditDelivery(event.target.value)}
              >
                <option value="">Choose one</option>
                <option value="IN_PERSON">In person</option>
                <option value="VIRTUAL">Virtual</option>
                <option value="HYBRID">Hybrid</option>
              </select>
            </label>
            <label>
              Venue or location
              <input
                value={editVenue}
                onChange={(event) => setEditVenue(event.target.value)}
              />
            </label>
            <label>
              Street address
              <input
                value={editAddress}
                onChange={(event) => setEditAddress(event.target.value)}
                placeholder="123 Main St, Aurora, CO 80012"
              />
            </label>
            <label>
              Admission
              <select
                value={editAdmission}
                onChange={(event) => setEditAdmission(event.target.value)}
              >
                <option value="">Choose one</option>
                <option value="FREE">Free</option>
                <option value="PAID">Paid</option>
              </select>
            </label>
            <label>
              Capacity (number of attendees)
              <input
                inputMode="numeric"
                pattern="[0-9]*"
                value={editCapacity}
                onChange={(event) => setEditCapacity(event.target.value)}
              />
            </label>
            <button
              className={styles.eventPrimary}
              type="submit"
              disabled={busy}
            >
              Save corrections
            </button>
          </form>
        ) : (
          <dl className={styles.factGrid}>
            <div>
              <dt>Event name</dt>
              <dd>{event.title || "Not set yet"}</dd>
            </div>
            <div>
              <dt>Date and time</dt>
              <dd>
                <DateText event={event} />
              </dd>
            </div>
            <div>
              <dt>Location</dt>
              <dd>{location || "Not set yet"}</dd>
            </div>
            <div>
              <dt>Delivery</dt>
              <dd>
                {event.delivery ? sentenceCase(event.delivery) : "Not set yet"}
              </dd>
            </div>
            {host && (
              <div>
                <dt>Host</dt>
                <dd>{host}</dd>
              </div>
            )}
            {price && (
              <div>
                <dt>Price</dt>
                <dd>{sentenceCase(price)}</dd>
              </div>
            )}
            {event.facts.location.address &&
              event.facts.location.address !== location && (
                <div>
                  <dt>Address</dt>
                  <dd>{event.facts.location.address}</dd>
                </div>
              )}
            {event.facts.registration.capacity != null && (
              <div>
                <dt>Capacity</dt>
                <dd>{String(event.facts.registration.capacity)}</dd>
              </div>
            )}
          </dl>
        )}
        {event.readiness.ready ? (
          <div className={styles.readyNote}>
            <p>The essential event details are ready for the next step.</p>
            <Link
              className={styles.eventPrimaryLink}
              to={`/events/${event.id}/campaign`}
            >
              Open campaign review
            </Link>
          </div>
        ) : (
          <p className={styles.helperText}>
            {openQuestions.length > 0 || pending.length > 0
              ? "Finish the items above to get this Event ready for a campaign."
              : "Add the missing details to get this Event ready for a campaign."}{" "}
            Nothing is published from this screen.
          </p>
        )}
      </section>

      <section
        className={styles.summaryCard}
        aria-labelledby="description-heading"
      >
        <p className={styles.eyebrow}>Event description</p>
        <h2 id="description-heading">Give attendees a clear picture.</h2>
        <p className={styles.helperText}>
          AI can suggest wording from the details you provided. Review every
          fact before using it.
        </p>
        {event.facts.content_facts?.description && (
          <p className={styles.eyebrow}>Description · source of truth</p>
        )}
        <label>
          Your description
          <textarea
            rows={5}
            maxLength={5000}
            value={manualDescription}
            onChange={(inputEvent) =>
              setManualDescription(inputEvent.target.value)
            }
            placeholder="Write a description, or ask AI for a starting point."
          />
        </label>
        <div className={styles.candidateList}>
          <button
            className={styles.secondaryButton}
            type="button"
            disabled={descriptionBusy}
            onClick={() => void requestDescription(false)}
          >
            {descriptionBusy ? "Preparing suggestion…" : "Suggest with AI"}
          </button>
          <label>
            Refinement style
            <select
              value={descriptionTone}
              onChange={(inputEvent) =>
                setDescriptionTone(inputEvent.target.value)
              }
            >
              <option value="">Choose a style</option>
              <option value="PROFESSIONAL">More professional</option>
              <option value="INVITING">More inviting</option>
              <option value="SHORTER">Shorter</option>
              <option value="ENERGETIC">More energetic</option>
              <option value="BEGINNER_CLEAR">Clearer for beginners</option>
            </select>
          </label>
          <input
            aria-label="Optional refinement instruction"
            value={descriptionInstruction}
            onChange={(inputEvent) =>
              setDescriptionInstruction(inputEvent.target.value)
            }
            placeholder="Optional: what should change?"
            maxLength={500}
          />
          <button
            className={styles.secondaryButton}
            type="button"
            disabled={descriptionBusy || !manualDescription.trim()}
            onClick={() => void requestDescription(true)}
          >
            Refine with AI
          </button>
          <button
            className={styles.eventPrimary}
            type="button"
            disabled={busy || !manualDescription.trim()}
            onClick={() => void saveDescription(manualDescription)}
          >
            Write my own · Save
          </button>
        </div>
        {descriptionSuggestion && (
          <div className={styles.suggestedVenue}>
            <p className={styles.eyebrow}>
              AI suggestion · review before accepting
            </p>
            <p>{descriptionSuggestion}</p>
            <div className={styles.candidateList}>
              <button
                className={styles.eventPrimary}
                type="button"
                disabled={busy}
                onClick={() => void saveDescription(descriptionSuggestion)}
              >
                Use suggestion
              </button>
              <button
                className={styles.secondaryButton}
                type="button"
                onClick={() => setManualDescription(descriptionSuggestion)}
              >
                Edit this suggestion
              </button>
            </div>
          </div>
        )}
      </section>

      <section className={styles.sourceCard} aria-labelledby="sources-heading">
        <div className={styles.summaryHeading}>
          <div>
            <p className={styles.eyebrow}>Your source material</p>
            <h2 id="sources-heading">Where these details came from</h2>
          </div>
          <input
            id="add-source"
            className={styles.visuallyHidden}
            type="file"
            accept=".txt,.md,.pdf,.jpg,.jpeg,.png,.webp"
            onChange={(inputEvent) => {
              void addSource(inputEvent.target.files?.[0]);
              inputEvent.target.value = "";
            }}
          />
          <label htmlFor="add-source" className={styles.secondaryButton}>
            Add another source
          </label>
        </div>
        {event.sources.length > 0 ? (
          <ul>
            {event.sources.map((source) => (
              <li key={source.id}>
                <span>
                  {source.filename ||
                    (source.kind === "TEXT" ? "Pasted notes" : "Source file")}
                </span>
                <span className={styles.sourceStatus}>
                  {source.processing_status === "EXTRACTING"
                    ? source.kind === "FILE"
                      ? "Processing your file…"
                      : "Reading your event details…"
                    : source.processing_status === "PROPOSALS_AVAILABLE"
                      ? "Ready to review"
                      : source.processing_status === "NO_FACTS_FOUND"
                        ? "No event details found · you can enter them manually"
                        : source.processing_status === "EXTRACTION_FAILED"
                          ? "Couldn’t read this source · enter details manually"
                          : "Saved privately"}
                </span>
                {source.can_retry_extraction && (
                  <button
                    className={styles.secondaryButton}
                    disabled={busy}
                    onClick={() => void retrySource(source)}
                    type="button"
                  >
                    Retry reading
                  </button>
                )}
              </li>
            ))}
          </ul>
        ) : (
          <p className={styles.helperText}>
            Add a flyer, email, or document and I’ll look for more details.
          </p>
        )}
      </section>
      <p className={styles.revisionNote}>
        Event details · revision {event.revision}
      </p>
    </div>
  );
}

export function Placeholder({
  title,
  message,
}: {
  title: string;
  message: string;
}) {
  return (
    <>
      <p className={styles.eyebrow}>Event Marketing workspace</p>
      <h1>{title}</h1>
      <p className={styles.intro}>A foundation for what comes next.</p>
      <section className={styles.placeholder}>
        <span className={styles.badge}>Coming next</span>
        <h2>Your workspace is ready.</h2>
        <p>{message} Nothing is being created or published here yet.</p>
      </section>
    </>
  );
}
