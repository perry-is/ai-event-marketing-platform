import {
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { expect, test, vi } from "vitest";
import { EventDetail, EventsList, NewEvent } from "./events";

const csrf = "unit-test-csrf";
type TestProposal = {
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
type TestQuestion = {
  id: string;
  kind: string;
  field_path: string;
  question: string;
  candidates: { label: string; value: unknown; source_id: string | null }[];
  status: string;
  answer: string | null;
  created_at: string;
  resolved_at: string | null;
};
type TestEvent = {
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
    registration: Record<string, string>;
    family_label?: string | null;
    topic?: string | null;
    content_facts?: { description?: string | null };
  };
  proposals: TestProposal[];
  clarifications: TestQuestion[];
  readiness: {
    ready: boolean;
    missing_fields: string[];
    open_question_count: number;
  };
  state: string;
  sources: (typeof source)[];
};
const source = {
  id: "source-1",
  kind: "TEXT",
  filename: null,
  content_type: "text/plain; charset=utf-8",
  byte_count: 12,
  processing_status: "PROPOSALS_AVAILABLE",
  can_retry_extraction: false,
  created_at: "2026-01-01T00:00:00+00:00",
};
function eventView(overrides: Partial<TestEvent> = {}): TestEvent {
  return {
    id: "event-1",
    revision: 1,
    title: null,
    starts_at: null,
    ends_at: null,
    timezone: null,
    delivery: null,
    facts: {
      schedule_local_start: null,
      schedule_local_end: null,
      location: {},
      people: {},
      registration: {},
    },
    proposals: [
      {
        id: "proposal-1",
        field_path: "title",
        candidate_value: "Lunch & Learn",
        confidence: "HIGH",
        source_id: "source-1",
        locator: "line:1",
        short_excerpt: "Lunch & Learn on ...",
        uncertainty_reason: null,
        status: "PENDING",
      },
    ],
    clarifications: [],
    readiness: {
      ready: false,
      missing_fields: ["title"],
      open_question_count: 0,
    },
    state: "NEEDS_REVIEW",
    sources: [source],
    ...overrides,
  };
}
const jsonResponse = (value: unknown, status = 200) =>
  new Response(JSON.stringify(value), { status });

function renderRoutes(path: string) {
  render(
    <MemoryRouter initialEntries={[path]}>
      <Routes>
        <Route
          path="/new"
          element={<NewEvent csrf={csrf} onUnauthorized={vi.fn()} />}
        />
        <Route
          path="/events"
          element={<EventsList csrf={csrf} onUnauthorized={vi.fn()} />}
        />
        <Route
          path="/events/:eventId"
          element={<EventDetail csrf={csrf} onUnauthorized={vi.fn()} />}
        />
      </Routes>
    </MemoryRouter>,
  );
}

test("New Event accepts messy notes and sends them as source material", async () => {
  const fetcher = vi.fn(async (url: string, init?: RequestInit) => {
    expect(url).toBe("/api/events");
    expect(init?.method).toBe("POST");
    return jsonResponse(eventView());
  });
  vi.stubGlobal("fetch", fetcher);
  renderRoutes("/new");
  await userEvent.type(
    screen.getByLabelText("What do you know so far?"),
    "Lunch & Learn on October 22",
  );
  await userEvent.click(screen.getByRole("button", { name: "Create Event" }));
  await waitFor(() => expect(fetcher).toHaveBeenCalled());
  expect(fetcher.mock.calls[0][0]).toBe("/api/events");
  expect(JSON.parse(String(fetcher.mock.calls[0][1]?.body))).toEqual({
    text: "Lunch & Learn on October 22",
    input_method: "TYPED",
  });
  const requestHeaders = new Headers(fetcher.mock.calls[0][1]?.headers);
  expect(requestHeaders.get("X-CSRF-Token")).toBe(csrf);
  expect(requestHeaders.get("Content-Type")).toBe("application/json");
});

test("New Event explains unsupported file selection before upload", async () => {
  vi.stubGlobal("fetch", vi.fn());
  renderRoutes("/new");
  const file = new File(["not supported"], "program.exe", {
    type: "application/octet-stream",
  });
  fireEvent.change(
    screen.getByLabelText("Add a flyer or document (optional)"),
    {
      target: { files: [file] },
    },
  );
  expect(await screen.findByRole("alert")).toHaveTextContent(
    "That file type is not supported.",
  );
  expect(fetch).not.toHaveBeenCalled();
});

test("New Event shows a chosen file and lets it be removed", async () => {
  vi.stubGlobal("fetch", vi.fn());
  renderRoutes("/new");
  const file = new File(["Lunch & Learn"], "flyer.pdf", {
    type: "application/pdf",
  });
  await userEvent.upload(
    screen.getByLabelText("Add a flyer or document (optional)"),
    file,
  );
  expect(screen.getByText(/flyer.pdf · /)).toBeVisible();
  await userEvent.click(
    screen.getByRole("button", { name: "Remove flyer.pdf" }),
  );
  expect(screen.queryByText(/flyer.pdf · /)).not.toBeInTheDocument();
});

test("Event detail accepts proposals, shows clarifications and reaches ready", async () => {
  const proposed = eventView();
  const accepted = eventView({
    revision: 2,
    title: "Lunch & Learn",
    starts_at: "2026-10-22T17:30:00+00:00",
    timezone: "America/Denver",
    delivery: null,
    facts: {
      schedule_local_start: "2026-10-22T11:30:00",
      schedule_local_end: "2026-10-22T13:00:00",
      location: { venue: "Example Venue" },
      people: {},
      registration: {},
    },
    proposals: proposed.proposals.map((proposal) => ({
      ...proposal,
      status: "ACCEPTED",
    })),
    clarifications: [
      {
        id: "question-1",
        kind: "MISSING",
        field_path: "delivery",
        question: "How will people attend?",
        candidates: [],
        status: "OPEN",
        answer: null,
        created_at: "2026-01-01T00:00:00+00:00",
        resolved_at: null,
      },
    ],
    readiness: {
      ready: false,
      missing_fields: ["delivery"],
      open_question_count: 1,
    },
    state: "NEEDS_DETAILS",
  });
  const ready = eventView({
    ...accepted,
    revision: 3,
    delivery: "IN_PERSON",
    clarifications: accepted.clarifications.map((question) => ({
      ...question,
      status: "RESOLVED",
      answer: "in person",
    })),
    readiness: { ready: true, missing_fields: [], open_question_count: 0 },
    state: "READY",
  });
  const fetcher = vi.fn(async (url: string, init?: RequestInit) => {
    if (url === "/api/events/event-1" && !init?.method)
      return jsonResponse(proposed);
    if (url.endsWith("/accept-proposals")) return jsonResponse(accepted);
    if (url.endsWith("/clarifications/question-1")) return jsonResponse(ready);
    return jsonResponse({ error: { code: "REQUEST_FAILED" } }, 500);
  });
  vi.stubGlobal("fetch", fetcher);
  renderRoutes("/events/event-1");
  expect(await screen.findByText("Lunch & Learn")).toBeVisible();
  await userEvent.click(
    screen.getByRole("button", { name: "Use these details" }),
  );
  expect(await screen.findByText(/October 22, 2026/)).toBeVisible();
  expect(await screen.findByText("How will people attend?")).toBeVisible();
  const questions = screen.getByRole("heading", {
    name: "Just a couple things I need.",
  });
  const summary = screen.getByRole("heading", { name: "Here’s what we know." });
  expect(
    questions.compareDocumentPosition(summary) &
      Node.DOCUMENT_POSITION_FOLLOWING,
  ).toBeTruthy();
  await userEvent.click(screen.getByRole("checkbox", { name: "In person" }));
  await userEvent.click(screen.getByRole("button", { name: "Save format" }));
  expect(
    await screen.findByText(/essential event details are ready/),
  ).toBeVisible();
  expect(screen.getByText("Example Venue")).toBeVisible();
  expect(screen.getByText("In person")).toBeVisible();
  const answerCall = fetcher.mock.calls.find(([url]) =>
    url.endsWith("/clarifications/question-1"),
  );
  expect(JSON.parse(String(answerCall?.[1]?.body)).answer).toBe("in person");
  expect(
    within(screen.getByRole("list", { name: "Event progress" }))
      .getByText("Event details")
      .closest("li"),
  ).toHaveAttribute("data-state", "done");
  expect(
    fetcher.mock.calls.some(
      ([, init]) =>
        init?.headers && new Headers(init.headers).get("X-CSRF-Token") === csrf,
    ),
  ).toBe(true);
});

test("Event detail corrections are submitted with the current revision", async () => {
  const current = eventView({
    revision: 4,
    title: "Lunch & Learn",
    starts_at: "2026-10-22T17:30:00+00:00",
    timezone: "America/Denver",
    delivery: "IN_PERSON",
    facts: {
      schedule_local_start: "2026-10-22T11:30:00",
      schedule_local_end: null,
      location: { venue: "Example Venue" },
      people: {},
      registration: {},
    },
    proposals: [],
    readiness: { ready: true, missing_fields: [], open_question_count: 0 },
    state: "READY",
  });
  const changed = { ...current, revision: 5, title: "Community Lunch & Learn" };
  const fetcher = vi.fn(async (_url: string, init?: RequestInit) =>
    init?.method === "PATCH" ? jsonResponse(changed) : jsonResponse(current),
  );
  vi.stubGlobal("fetch", fetcher);
  renderRoutes("/events/event-1");
  await screen.findByRole("heading", { name: "Lunch & Learn" });
  await userEvent.click(screen.getByRole("button", { name: "Edit details" }));
  await userEvent.clear(screen.getByLabelText("Event name"));
  await userEvent.type(
    screen.getByLabelText("Event name"),
    "Community Lunch & Learn",
  );
  await userEvent.click(
    screen.getByRole("button", { name: "Save corrections" }),
  );
  expect(
    await screen.findByRole("heading", { name: "Community Lunch & Learn" }),
  ).toBeVisible();
  const patchCall = fetcher.mock.calls.find(
    ([, init]) => init?.method === "PATCH",
  );
  expect(JSON.parse(String(patchCall?.[1]?.body))).toMatchObject({
    expected_revision: 4,
    changes: expect.arrayContaining([
      { field_path: "title", value: "Community Lunch & Learn" },
    ]),
  });
});

test("date clarification uses structured date and time controls", async () => {
  const scheduleQuestion: TestQuestion = {
    id: "schedule-q",
    kind: "CONFIRMATION_REQUIRED",
    field_path: "schedule",
    question: "Please review the suggested date and time.",
    candidates: [
      {
        label: "October 6, 2026 at 12:00 PM · Mountain Time",
        value: {
          date_only: "2026-10-06",
          start_time: "12:00",
          end_time: "14:00",
          timezone: "America/Denver",
          past_date: true,
        },
        source_id: "source-1",
      },
    ],
    status: "OPEN",
    answer: null,
    created_at: "2026-01-01T00:00:00+00:00",
    resolved_at: null,
  };
  const current = eventView({
    revision: 2,
    title: "Lunch & Learn",
    timezone: "America/Denver",
    proposals: [],
    clarifications: [scheduleQuestion],
  });
  const ready = eventView({ ...current, revision: 3, clarifications: [] });
  const fetcher = vi.fn(
    async (url: string, init?: RequestInit) => (
      void init,
      url.endsWith("/clarifications/schedule-q")
        ? jsonResponse(ready)
        : jsonResponse(current)
    ),
  );
  vi.stubGlobal("fetch", fetcher);
  renderRoutes("/events/event-1");
  await screen.findByLabelText("Event date");
  expect(screen.getByLabelText("Event date")).toHaveValue("2026-10-06");
  expect(screen.getByLabelText("Start time")).toHaveValue("12:00");
  expect(screen.getByText(/This suggested date has passed/)).toBeVisible();
  await userEvent.click(
    screen.getByRole("button", { name: "Save date and time" }),
  );
  const call = fetcher.mock.calls.find(([url]) =>
    url.endsWith("/clarifications/schedule-q"),
  );
  expect(JSON.parse(String(call?.[1]?.body)).answer).toBe(
    "2026-10-06T12:00|2026-10-06T14:00|America/Denver",
  );
});

test("AI event description can be suggested, refined and accepted", async () => {
  const current = eventView({
    title: "Lunch & Learn",
    facts: {
      schedule_local_start: null,
      schedule_local_end: null,
      location: {},
      people: {},
      registration: {},
      content_facts: {},
    },
  });
  const updated = {
    ...current,
    revision: 2,
    facts: {
      ...current.facts,
      content_facts: { description: "Join us for a practical Lunch & Learn." },
    },
  };
  const fetcher = vi.fn(async (url: string, init?: RequestInit) => {
    if (url.endsWith("/description-suggestion"))
      return jsonResponse({
        description: "Join us for a practical Lunch & Learn.",
      });
    if (url.endsWith("/facts") && init?.method === "PATCH")
      return jsonResponse(updated);
    return jsonResponse(current);
  });
  vi.stubGlobal("fetch", fetcher);
  renderRoutes("/events/event-1");
  await userEvent.click(
    await screen.findByRole("button", { name: "Suggest with AI" }),
  );
  expect(
    await screen.findByText(/Join us for a practical Lunch & Learn/),
  ).toBeVisible();
  await userEvent.click(
    screen.getByRole("button", { name: "Edit this suggestion" }),
  );
  await userEvent.selectOptions(
    screen.getByLabelText("Refinement style"),
    "SHORTER",
  );
  await userEvent.click(screen.getByRole("button", { name: "Refine with AI" }));
  expect(
    await screen.findAllByText(/Join us for a practical Lunch & Learn/),
  ).toHaveLength(2);
  await userEvent.click(screen.getByRole("button", { name: "Use suggestion" }));
  expect(
    await screen.findByText("Description · source of truth"),
  ).toBeVisible();
  const suggestionRequest = fetcher.mock.calls.find(([url]) =>
    url.endsWith("/description-suggestion"),
  );
  expect(suggestionRequest?.[1]?.method).toBe("POST");
  const suggestionBodies = fetcher.mock.calls
    .filter(([url]) => url.endsWith("/description-suggestion"))
    .map(([, init]) => JSON.parse(String(init?.body)));
  expect(suggestionBodies).toHaveLength(2);
  expect(suggestionBodies[1]).toMatchObject({
    tone: "SHORTER",
    current_description: "Join us for a practical Lunch & Learn.",
  });
  const patchRequest = fetcher.mock.calls.find(
    ([url, init]) => url.endsWith("/facts") && init?.method === "PATCH",
  );
  expect(
    JSON.parse(String(patchRequest?.[1]?.body)).changes[0].field_path,
  ).toBe("facts.content_facts.description");
});

test("partially populated schedule proposals render as human language", async () => {
  const proposed = eventView({
    proposals: [
      {
        id: "schedule-proposal",
        field_path: "schedule",
        candidate_value: {
          local_start: null,
          local_end: null,
          timezone: "America/Denver",
          date_only: "2026-10-06",
          start_time: null,
        },
        confidence: "MEDIUM",
        source_id: "source-1",
        locator: "line 1",
        short_excerpt: "Oct 6",
        uncertainty_reason: "I suggested 2026 for the year; please confirm it.",
        status: "PENDING",
      },
    ],
  });
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => jsonResponse(proposed)),
  );
  renderRoutes("/events/event-1");
  expect(
    await screen.findByText(/2026-10-06 · start time needed/),
  ).toBeVisible();
  expect(document.body.textContent).not.toContain("[object Object]");
  expect(document.body.textContent).not.toContain("None");
});

test("manual Event description can be saved without an AI request", async () => {
  const current = eventView({ title: "Networking Lunch", proposals: [] });
  const fetcher = vi.fn(async (url: string, init?: RequestInit) => {
    if (url.endsWith("/facts") && init?.method === "PATCH")
      return jsonResponse(current);
    return jsonResponse(current);
  });
  vi.stubGlobal("fetch", fetcher);
  renderRoutes("/events/event-1");
  await userEvent.type(
    await screen.findByLabelText("Your description"),
    "A relaxed lunch to meet local business owners.",
  );
  await userEvent.click(
    screen.getByRole("button", { name: "Write my own · Save" }),
  );
  expect(
    fetcher.mock.calls.some(([url]) => url.endsWith("/description-suggestion")),
  ).toBe(false);
  const request = fetcher.mock.calls.find(
    ([url, init]) => url.endsWith("/facts") && init?.method === "PATCH",
  );
  expect(JSON.parse(String(request?.[1]?.body)).changes[0].value).toBe(
    "A relaxed lunch to meet local business owners.",
  );
});

test("failed extraction stays available and can be retried explicitly", async () => {
  const failed = eventView({
    proposals: [],
    sources: [
      {
        ...source,
        processing_status: "EXTRACTION_FAILED",
        can_retry_extraction: true,
      },
    ],
  });
  const retried = eventView({
    ...failed,
    proposals: [],
    sources: [
      {
        ...source,
        processing_status: "NO_FACTS_FOUND",
        can_retry_extraction: false,
      },
    ],
  });
  const fetcher = vi.fn(async (url: string, init?: RequestInit) => {
    if (url.endsWith("retry-extraction")) {
      expect(init?.method).toBe("POST");
      return jsonResponse(retried);
    }
    return jsonResponse(failed);
  });
  vi.stubGlobal("fetch", fetcher);
  renderRoutes("/events/event-1");
  expect(await screen.findByText(/Couldn’t read this source/)).toBeVisible();
  await userEvent.click(screen.getByRole("button", { name: "Retry reading" }));
  expect(await screen.findByText(/No event details found/)).toBeVisible();
  const retryCall = fetcher.mock.calls.find(([url]) =>
    url.endsWith("retry-extraction"),
  );
  expect(retryCall?.[1]?.method).toBe("POST");
  expect(
    retryCall?.[1]?.headers &&
      new Headers(retryCall[1].headers).get("X-CSRF-Token"),
  ).toBe(csrf);
});

test("Events list links to saved Events and shows readiness", async () => {
  const row = {
    id: "event-1",
    revision: 1,
    title: "Lunch & Learn",
    starts_at: "2026-10-22T17:30:00+00:00",
    timezone: "America/Denver",
    delivery: "IN_PERSON",
    state: "READY",
    readiness: { ready: true, missing_fields: [], open_question_count: 0 },
    updated_at: "2026-10-05T12:00:00+00:00",
  };
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => jsonResponse([row])),
  );
  renderRoutes("/events");
  expect(
    await screen.findByRole("link", { name: /Lunch & Learn/ }),
  ).toHaveAttribute("href", "/events/event-1");
  expect(screen.getByText("Ready for next step")).toBeVisible();
});
