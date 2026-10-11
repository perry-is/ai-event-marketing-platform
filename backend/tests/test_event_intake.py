import hashlib
from datetime import datetime
from io import BytesIO

from PIL import Image
from pypdf import PdfWriter
from starlette.testclient import TestClient

from app.models import Event, SourceMaterial
from tests.conftest import sign_in

NOTES = (
    "Lunch & Learn on October 22, 2026 from 11:30 AM to 1:00 PM "
    "at Example Venue. Timezone America/Denver."
)


def headers(client, settings):
    return {
        "origin": settings.app_origin,
        "x-csrf-token": client.get("/api/me").json()["csrf_token"],
    }


def create_with_notes(client, settings, provider, text=NOTES):
    sign_in(client, provider)
    response = client.post(
        "/api/events",
        json={"text": text, "input_method": "PASTED"},
        headers=headers(client, settings),
    )
    assert response.status_code == 201, response.text
    return response.json()


def accept(client, settings, event):
    return client.post(
        f"/api/events/{event['id']}/accept-proposals",
        json={"expected_revision": event["revision"]},
        headers=headers(client, settings),
    )


def test_event_apis_require_authenticated_session(client):
    assert client.get("/api/events").status_code == 401
    assert client.post("/api/events", json={"text": NOTES}).status_code == 401
    assert client.get("/api/events/not-an-id").status_code == 401
    assert (
        client.post(
            "/api/events/00000000-0000-0000-0000-000000000000/sources",
            files={"file": ("notes.txt", b"private", "text/plain")},
        ).status_code
        == 401
    )
    assert (
        client.post(
            "/api/events/00000000-0000-0000-0000-000000000000/sources/"
            "00000000-0000-0000-0000-000000000000/retry-extraction",
            json={},
        ).status_code
        == 401
    )


def test_text_intake_proposes_but_does_not_write_canonical_facts(client, settings, provider, app):
    event = create_with_notes(client, settings, provider)
    assert event["title"] is None
    assert event["starts_at"] is None
    assert event["proposals"]
    assert {proposal["field_path"] for proposal in event["proposals"]} == {
        "title",
        "schedule",
        "facts.location.venue",
    }
    assert all(proposal["status"] == "PENDING" for proposal in event["proposals"])
    source = event["sources"][0]
    with app.state.database.sessions() as db:
        row = db.get(SourceMaterial, source["id"])
        assert row.original_text == NOTES
        assert row.sha256 == hashlib.sha256(NOTES.encode()).hexdigest()
        assert row.processing_status == "PROPOSALS_AVAILABLE"
        assert row.extraction["extractor_type"] == "deterministic-demo"
        assert row.extraction["attempts"][0]["status"] == "SUCCEEDED"


def test_acceptance_clarification_readiness_and_revision(client, settings, provider, app):
    event = create_with_notes(client, settings, provider)
    accepted = accept(client, settings, event)
    assert accepted.status_code == 200, accepted.text
    value = accepted.json()
    assert value["title"] == "Lunch & Learn"
    assert value["timezone"] == "America/Denver"
    assert value["delivery"] is None
    assert value["facts"]["location"]["venue"] == "Example Venue"
    assert value["revision"] == event["revision"] + 1
    assert value["readiness"]["ready"] is False
    question = next(item for item in value["clarifications"] if item["status"] == "OPEN")
    assert question["kind"] == "MISSING"
    assert question["field_path"] == "delivery"
    assert question["question"] == "How will people attend?"

    invalid = client.post(
        f"/api/events/{event['id']}/clarifications/{question['id']}",
        json={"expected_revision": value["revision"], "answer": "somewhere"},
        headers=headers(client, settings),
    )
    assert invalid.status_code == 422
    assert invalid.json()["error"]["code"] == "DELIVERY_INVALID"
    invalid_state = client.get(f"/api/events/{event['id']}").json()
    invalid_question = next(
        item for item in invalid_state["clarifications"] if item["id"] == question["id"]
    )
    assert invalid_question["kind"] == "INVALID"
    assert invalid_question["validation_message"] == "Choose in person, virtual, or hybrid."

    resolved = client.post(
        f"/api/events/{event['id']}/clarifications/{question['id']}",
        json={"expected_revision": value["revision"], "answer": "It will be in person."},
        headers=headers(client, settings),
    )
    assert resolved.status_code == 200, resolved.text
    ready = resolved.json()
    assert ready["delivery"] == "IN_PERSON"
    assert ready["readiness"]["ready"] is True
    assert ready["revision"] == value["revision"] + 1
    stored = client.get(f"/api/events/{event['id']}").json()
    assert stored["readiness"]["ready"] is True
    assert stored["clarifications"][0]["status"] == "RESOLVED"
    with app.state.database.sessions() as db:
        row = db.get(Event, event["id"])
        assert row.fact_evidence["delivery"]["status"] == "CONFIRMED"
        assert row.fact_evidence["schedule"]["source_ids"] == [event["sources"][0]["id"]]
        assert row.starts_at == datetime.fromisoformat(ready["starts_at"])


def test_default_timezone_is_applied_when_source_has_no_timezone(client, settings, provider):
    without_zone = "Lunch & Learn on October 22, 2026 from 11:30 AM at Example Venue."
    event = create_with_notes(client, settings, provider, without_zone)
    accepted = accept(client, settings, event).json()
    assert accepted["facts"]["schedule_local_start"] == "2026-10-22T11:30:00"
    assert accepted["timezone"] == "America/Denver"
    assert accepted["starts_at"] is not None
    assert not any(
        item["status"] == "OPEN" and item["field_path"] == "timezone"
        for item in accepted["clarifications"]
    )


def test_suggested_default_venue_is_not_canonical_until_user_accepts(client, settings, provider):
    event = create_with_notes(
        client,
        settings,
        provider,
        "Lunch & Learn on October 22, 2026 from 11:30 AM to 1 PM in person.",
    )
    accepted = accept(client, settings, event).json()
    question = next(
        item
        for item in accepted["clarifications"]
        if item["status"] == "OPEN" and item["field_path"] == "facts.location.venue"
    )
    assert question["suggested_venue"] == {
        "name": "Example Conference Center",
        "street": "100 Main St",
        "city": "Springfield",
        "region": "CO",
        "postal_code": "80202",
        "country": "US",
    }
    assert accepted["facts"]["location"].get("venue") is None
    response = client.post(
        f"/api/events/{event['id']}/suggested-venue",
        json={"expected_revision": accepted["revision"]},
        headers=headers(client, settings),
    )
    assert response.status_code == 200, response.text
    location = response.json()["facts"]["location"]
    assert location["venue"] == "Example Conference Center"
    assert location["street"] == "100 Main St"
    assert location["city"] == "Springfield"
    assert location["region"] == "CO"
    assert location["postal_code"] == "80202"
    assert location["country"] == "US"


def test_year_only_clarification_preserves_month_day_and_start_time(client, settings, provider):
    event = create_with_notes(
        client,
        settings,
        provider,
        "Networking Lunch on Oct 22 at 11:30 AM at Example Venue.",
    )
    accepted = accept(client, settings, event).json()
    question = next(
        item
        for item in accepted["clarifications"]
        if item["status"] == "OPEN" and item["field_path"] == "schedule"
    )
    candidate = question["candidates"][0]["value"]
    assert candidate["date_only"] == "2026-10-22"
    assert candidate["start_time"] == "11:30"
    response = client.post(
        f"/api/events/{event['id']}/clarifications/{question['id']}",
        json={"expected_revision": accepted["revision"], "answer": "2027"},
        headers=headers(client, settings),
    )
    assert response.status_code == 200, response.text
    assert response.json()["facts"]["schedule_local_start"] == "2027-10-22T11:30:00"


def test_explicit_source_venue_is_not_replaced_by_default_suggestion(client, settings, provider):
    event = create_with_notes(client, settings, provider)
    accepted = accept(client, settings, event).json()
    assert accepted["facts"]["location"]["venue"] == "Example Venue"
    assert not any(
        question.get("suggested_venue")
        for question in accepted["clarifications"]
        if question["status"] == "OPEN"
    )


def test_event_description_suggestion_and_acceptance(client, settings, provider):
    event = create_with_notes(client, settings, provider)
    accepted = accept(client, settings, event).json()
    suggestion = client.post(
        f"/api/events/{event['id']}/description-suggestion",
        json={},
        headers=headers(client, settings),
    )
    assert suggestion.status_code == 200, suggestion.text
    assert "Lunch & Learn" in suggestion.json()["description"]
    updated = client.patch(
        f"/api/events/{event['id']}/facts",
        json={
            "expected_revision": accepted["revision"],
            "changes": [
                {
                    "field_path": "facts.content_facts.description",
                    "value": suggestion.json()["description"],
                }
            ],
        },
        headers=headers(client, settings),
    )
    assert updated.status_code == 200, updated.text
    assert (
        updated.json()["facts"]["content_facts"]["description"] == suggestion.json()["description"]
    )


def test_conflicting_sources_never_overwrite_accepted_schedule(client, settings, provider, app):
    source_text = NOTES.replace("at Example Venue", "in person at Example Venue")
    original = create_with_notes(client, settings, provider, source_text)
    accepted = accept(client, settings, original).json()
    second = client.post(
        f"/api/events/{original['id']}/sources",
        data={"text": source_text.replace("October 22", "October 23")},
        headers=headers(client, settings),
    )
    assert second.status_code == 201, second.text
    pending = next(
        item
        for item in second.json()["proposals"]
        if item["status"] == "PENDING" and item["field_path"] == "schedule"
    )
    assert pending["candidate_value"]["local_start"].startswith("2026-10-23")
    applied = accept(client, settings, second.json())
    assert applied.status_code == 200, applied.text
    conflicted = applied.json()
    assert conflicted["starts_at"] == accepted["starts_at"]
    question = next(
        item
        for item in conflicted["clarifications"]
        if item["status"] == "OPEN" and item["field_path"] == "schedule"
    )
    assert question["kind"] == "CONFLICT"
    assert len(question["candidates"]) == 2
    assert conflicted["readiness"]["ready"] is False
    answer = client.post(
        f"/api/events/{original['id']}/clarifications/{question['id']}",
        json={
            "expected_revision": conflicted["revision"],
            "answer": question["candidates"][1]["label"],
        },
        headers=headers(client, settings),
    )
    assert answer.status_code == 200, answer.text
    assert answer.json()["starts_at"] != accepted["starts_at"]
    assert answer.json()["readiness"]["ready"] is True
    with app.state.database.sessions() as db:
        row = db.get(Event, original["id"])
        assert row.fact_evidence["schedule"]["status"] == "CONFIRMED"


def test_supported_files_are_private_and_unsafe_names_are_neutralized(
    client, settings, provider, app
):
    sign_in(client, provider)
    empty = client.post("/api/events", json={}, headers=headers(client, settings)).json()
    raw = b"Handout notes without recognized event fields."
    uploaded = client.post(
        f"/api/events/{empty['id']}/sources",
        files={"file": ("../../notes.txt", raw, "text/plain")},
        headers=headers(client, settings),
    )
    assert uploaded.status_code == 201, uploaded.text
    item = uploaded.json()["sources"][0]
    assert item["filename"] == "notes.txt"
    assert item["processing_status"] == "NO_FACTS_FOUND"
    with app.state.database.sessions() as db:
        row = db.get(SourceMaterial, item["id"])
        path = settings.private_storage_dir / row.storage_key
        assert path.is_file()
        assert path.read_bytes() == raw
        assert row.original_text is None
        assert row.sha256 == hashlib.sha256(raw).hexdigest()
    response = client.get(f"/api/events/{empty['id']}/sources/{item['id']}/download")
    assert response.status_code == 200
    assert response.content == raw
    assert "attachment" in response.headers["content-disposition"]
    assert client.get(f"/api/events/{empty['id']}/sources/{item['id']}/download").status_code == 200


def test_uploaded_text_file_is_preserved_and_produces_proposals(client, settings, provider):
    sign_in(client, provider)
    event = client.post("/api/events", json={}, headers=headers(client, settings)).json()
    raw = NOTES.encode("utf-8")
    response = client.post(
        f"/api/events/{event['id']}/sources",
        files={"file": ("event-notes.txt", raw, "text/plain")},
        headers=headers(client, settings),
    )
    assert response.status_code == 201, response.text
    value = response.json()
    assert value["title"] is None
    assert any(
        proposal["field_path"] == "title" and proposal["status"] == "PENDING"
        for proposal in value["proposals"]
    )
    file_source = value["sources"][0]
    assert file_source["processing_status"] == "PROPOSALS_AVAILABLE"
    downloaded = client.get(f"/api/events/{event['id']}/sources/{file_source['id']}/download")
    assert downloaded.content == raw


def test_supported_pdf_and_image_files_are_verified_before_storage(client, settings, provider):
    sign_in(client, provider)
    event = client.post("/api/events", json={}, headers=headers(client, settings)).json()
    image_bytes = BytesIO()
    Image.new("RGB", (64, 48), color="white").save(image_bytes, format="PNG")
    pdf_bytes = BytesIO()
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    writer.write(pdf_bytes)
    for filename, content, mime in (
        ("flyer.png", image_bytes.getvalue(), "image/png"),
        ("agenda.pdf", pdf_bytes.getvalue(), "application/pdf"),
    ):
        response = client.post(
            f"/api/events/{event['id']}/sources",
            files={"file": (filename, content, mime)},
            headers=headers(client, settings),
        )
        assert response.status_code == 201, response.text
        assert response.json()["sources"][-1]["processing_status"] == "EXTRACTION_FAILED"
        assert response.json()["sources"][-1]["can_retry_extraction"] is False


def test_bad_oversized_and_mismatched_files_are_rejected(client, settings, provider):
    sign_in(client, provider)
    event = client.post("/api/events", json={}, headers=headers(client, settings)).json()
    base = f"/api/events/{event['id']}/sources"
    unsupported = client.post(
        base,
        files={"file": ("script.exe", b"MZ", "application/octet-stream")},
        headers=headers(client, settings),
    )
    assert unsupported.status_code == 415
    malformed = client.post(
        base,
        files={"file": ("image.png", b"not an image", "image/png")},
        headers=headers(client, settings),
    )
    assert malformed.status_code == 422
    mismatch = client.post(
        base, files={"file": ("notes.txt", b"ok", "image/png")}, headers=headers(client, settings)
    )
    assert mismatch.status_code == 415
    large = b"a" * (2 * 1024 * 1024 + 1)
    oversized = client.post(
        base, files={"file": ("long.txt", large, "text/plain")}, headers=headers(client, settings)
    )
    assert oversized.status_code == 413
    assert client.get(
        f"/api/events/{event['id']}/sources/../../etc/passwd/download"
    ).status_code in {401, 404}


def test_fact_corrections_use_revision_guard_and_recompute_ready(client, settings, provider):
    event = create_with_notes(client, settings, provider)
    current = accept(client, settings, event).json()
    response = client.patch(
        f"/api/events/{event['id']}/facts",
        json={
            "expected_revision": current["revision"],
            "changes": [
                {"field_path": "delivery", "value": "IN_PERSON"},
                {"field_path": "title", "value": "October Lunch & Learn"},
            ],
        },
        headers=headers(client, settings),
    )
    assert response.status_code == 200, response.text
    assert response.json()["readiness"]["ready"] is True
    assert response.json()["revision"] == current["revision"] + 1
    stale = client.patch(
        f"/api/events/{event['id']}/facts",
        json={
            "expected_revision": current["revision"],
            "changes": [{"field_path": "title", "value": "Stale"}],
        },
        headers=headers(client, settings),
    )
    assert stale.status_code == 409


def test_delivery_change_retires_obsolete_conditional_question(client, settings, provider):
    text = NOTES.replace("at Example Venue.", "in person.")
    event = create_with_notes(client, settings, provider, text)
    accepted = accept(client, settings, event).json()
    venue_question = next(
        item
        for item in accepted["clarifications"]
        if item["status"] == "OPEN" and item["field_path"] == "facts.location.venue"
    )
    changed = client.patch(
        f"/api/events/{event['id']}/facts",
        json={
            "expected_revision": accepted["revision"],
            "changes": [{"field_path": "delivery", "value": "VIRTUAL"}],
        },
        headers=headers(client, settings),
    )
    assert changed.status_code == 200, changed.text
    current = changed.json()
    questions = {item["id"]: item for item in current["clarifications"]}
    assert questions[venue_question["id"]]["status"] == "RESOLVED"
    assert (
        questions[venue_question["id"]]["answer"]
        == "No longer required after the Event details changed."
    )
    open_paths = {
        item["field_path"] for item in current["clarifications"] if item["status"] == "OPEN"
    }
    assert open_paths == {"facts.location.join_instructions"}
    assert current["readiness"]["ready"] is False


def test_events_list_and_source_access_are_authenticated(client, settings, provider):
    event = create_with_notes(client, settings, provider)
    with TestClient(client.app, base_url=settings.app_origin, follow_redirects=False) as anonymous:
        assert anonymous.get("/api/events").status_code == 401
        assert (
            anonymous.get(
                f"/api/events/{event['id']}/sources/{event['sources'][0]['id']}/download"
            ).status_code
            == 401
        )
    listed = client.get("/api/events")
    assert listed.status_code == 200
    assert listed.json()[0]["id"] == event["id"]
