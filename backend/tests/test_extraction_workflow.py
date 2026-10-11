import base64
import json
from datetime import UTC, datetime, timedelta
from io import BytesIO
from pathlib import Path

import httpx2
from PIL import Image
from pypdf import PdfWriter

from app.models import SourceMaterial
from tests.conftest import sign_in
from tests.openai_test_support import install_openai_mock, mock_response, text_proposal


def headers(client, settings):
    return {
        "origin": settings.app_origin,
        "x-csrf-token": client.get("/api/me").json()["csrf_token"],
    }


def install_responses(app, outputs):
    requests = []

    def respond(request):
        requests.append(json.loads(request.content))
        output = outputs[min(len(requests) - 1, len(outputs) - 1)]
        if isinstance(output, httpx2.Response):
            return output
        return mock_response(output, response_id=f"resp_test_{len(requests)}")

    service, _ = install_openai_mock(app, respond)
    return service, requests


def test_openai_proposals_reach_existing_conflict_path_without_overwriting_facts(
    client, settings, provider, app
):
    first = {
        "proposals": [
            text_proposal("title", "Event Marketing Mixer", "Event Marketing Mixer"),
            text_proposal(
                "schedule",
                {
                    "local_start": "2026-10-22T18:00:00",
                    "timezone": "America/Denver",
                    "local_end": "2026-10-22T20:00:00",
                },
                "October 22, 2026 at 6:00 PM, America/Denver",
            ),
            text_proposal("delivery", "IN_PERSON", "in person"),
            text_proposal("facts.location.venue", "Example Center", "at Example Center"),
        ]
    }
    conflict = {
        "proposals": [
            text_proposal(
                "schedule",
                {
                    "local_start": "2026-10-23T18:00:00",
                    "timezone": "America/Denver",
                    "local_end": "2026-10-23T20:00:00",
                },
                "October 23, 2026 at 6:00 PM, America/Denver",
            )
        ]
    }
    service, requests = install_responses(app, [first, conflict])
    sign_in(client, provider)
    event_response = client.post(
        "/api/events",
        json={"text": "Messy copied event announcement; verify the date with the organizer."},
        headers=headers(client, settings),
    )
    assert event_response.status_code == 201, event_response.text
    initial = event_response.json()
    assert {item["confidence"] for item in initial["proposals"]} == {"HIGH"}
    assert initial["title"] is None
    source_id = initial["sources"][0]["id"]
    assert initial["proposals"][0]["source_id"] == source_id
    assert initial["sources"][0]["processing_status"] == "PROPOSALS_AVAILABLE"
    assert str(initial["id"]) not in json.dumps(requests[0])
    accepted_response = client.post(
        f"/api/events/{initial['id']}/accept-proposals",
        json={"expected_revision": initial["revision"]},
        headers=headers(client, settings),
    )
    assert accepted_response.status_code == 200, accepted_response.text
    accepted = accepted_response.json()
    assert accepted["readiness"]["ready"] is True
    original_start = accepted["starts_at"]

    added = client.post(
        f"/api/events/{initial['id']}/sources",
        data={"text": "The revised source lists a different event date."},
        headers=headers(client, settings),
    )
    assert added.status_code == 201, added.text
    second_source = added.json()["sources"][-1]["id"]
    assert added.json()["proposals"][-1]["source_id"] == second_source
    applied = client.post(
        f"/api/events/{initial['id']}/accept-proposals",
        json={"expected_revision": added.json()["revision"]},
        headers=headers(client, settings),
    )
    assert applied.status_code == 200, applied.text
    conflicted = applied.json()
    assert conflicted["starts_at"] == original_start
    question = next(
        item
        for item in conflicted["clarifications"]
        if item["status"] == "OPEN" and item["field_path"] == "schedule"
    )
    assert question["kind"] == "CONFLICT"
    assert len(question["candidates"]) == 2
    assert len(requests) == 2  # One bounded provider call per source material.
    with app.state.database.sessions() as db:
        source = db.get(SourceMaterial, initial["sources"][0]["id"])
        assert len(source.extraction["attempts"]) == 1
        assert source.extraction["attempts"][0]["model_id"] == "gpt-6-luna"
        assert source.extraction["attempts"][0]["provider_response_id"] == "resp_test_1"
    service.close()


def test_failure_preserves_source_and_manual_fallback_then_explicit_retry(
    client, settings, provider, app
):
    success = {"proposals": [text_proposal("title", "Community Workshop", "Community Workshop")]}
    rate_limit = httpx2.Response(
        429,
        json={"error": {"message": "temporary provider limit", "type": "rate_limit_error"}},
    )
    service, requests = install_responses(app, [rate_limit, success])
    sign_in(client, provider)
    original = "A community workshop is planned; details are still being confirmed."
    failed = client.post(
        "/api/events",
        json={"text": original},
        headers=headers(client, settings),
    )
    assert failed.status_code == 201, failed.text
    value = failed.json()
    source = value["sources"][0]
    assert source["processing_status"] == "EXTRACTION_FAILED"
    assert source["can_retry_extraction"] is True
    assert value["proposals"] == []
    assert value["readiness"]["ready"] is False
    with app.state.database.sessions() as db:
        row = db.get(SourceMaterial, source["id"])
        assert row.original_text == original
        assert row.extraction["attempts"][0]["error_category"] == "RATE_LIMITED"
        assert row.extraction["attempts"][0]["retryable"] is True

    retried = client.post(
        f"/api/events/{value['id']}/sources/{source['id']}/retry-extraction",
        json={},
        headers=headers(client, settings),
    )
    assert retried.status_code == 200, retried.text
    current = retried.json()
    assert current["sources"][0]["processing_status"] == "PROPOSALS_AVAILABLE"
    assert current["sources"][0]["can_retry_extraction"] is False
    assert len(current["proposals"]) == 1
    assert len(requests) == 2
    with app.state.database.sessions() as db:
        row = db.get(SourceMaterial, source["id"])
        assert [item["status"] for item in row.extraction["attempts"]] == [
            "FAILED",
            "SUCCEEDED",
        ]
        assert row.original_text == original
    assert current["title"] is None  # Retry produces a proposal, never canonical truth.
    service.close()


def test_deterministic_validation_drops_unsupported_enum_before_event_review(
    client, settings, provider, app
):
    service, _ = install_responses(
        app,
        [
            {
                "proposals": [
                    text_proposal("title", "A Workshop", "A Workshop"),
                    text_proposal("delivery", "REMOTE", "remote"),
                    text_proposal(
                        "facts.registration.price_kind",
                        {"local_start": None, "timezone": None, "local_end": None},
                        "structured value with the wrong semantic type",
                    ),
                ]
            }
        ],
    )
    sign_in(client, provider)
    response = client.post(
        "/api/events",
        json={"text": "Synthetic event source."},
        headers=headers(client, settings),
    )
    assert response.status_code == 201, response.text
    event = response.json()
    assert [value["field_path"] for value in event["proposals"]] == ["title"]
    accepted = client.post(
        f"/api/events/{event['id']}/accept-proposals",
        json={"expected_revision": event["revision"]},
        headers=headers(client, settings),
    ).json()
    assert accepted["title"] == "A Workshop"
    assert accepted["delivery"] is None
    assert accepted["facts"]["registration"].get("price_kind") is None
    assert accepted["readiness"]["ready"] is False
    assert any(
        item["status"] == "OPEN" and item["field_path"] == "delivery"
        for item in accepted["clarifications"]
    )
    service.close()


def test_medium_confidence_requires_confirmation_and_does_not_become_canonical(
    client, settings, provider, app
):
    service, _ = install_responses(
        app,
        [
            {
                "proposals": [
                    text_proposal(
                        "title",
                        "Community Breakfast",
                        "Community Breakfast",
                        confidence="MEDIUM",
                        uncertainty_reason="The flyer headline is partially obscured.",
                    )
                ]
            }
        ],
    )
    sign_in(client, provider)
    response = client.post(
        "/api/events",
        json={"text": "Synthetic breakfast event source."},
        headers=headers(client, settings),
    )
    event = response.json()
    assert event["proposals"][0]["confidence"] == "MEDIUM"
    accepted = client.post(
        f"/api/events/{event['id']}/accept-proposals",
        json={"expected_revision": event["revision"]},
        headers=headers(client, settings),
    ).json()
    assert accepted["title"] is None
    assert accepted["facts"]["registration"].get("price_kind") is None
    assert accepted["proposals"][0]["status"] == "NEEDS_CONFIRMATION"
    assert any(
        item["status"] == "OPEN" and item["kind"] == "CONFIRMATION_REQUIRED"
        for item in accepted["clarifications"]
    )
    service.close()


def test_stale_attempt_can_be_retried_and_supersedes_old_pending_proposals(
    client, settings, provider, app
):
    output = {"proposals": [text_proposal("title", "Community Workshop", "Community Workshop")]}
    service, requests = install_responses(app, [output, output])
    sign_in(client, provider)
    event = client.post("/api/events", json={}, headers=headers(client, settings)).json()
    added = client.post(
        f"/api/events/{event['id']}/sources",
        data={"text": "Synthetic workshop description."},
        headers=headers(client, settings),
    ).json()
    source = added["sources"][0]
    with app.state.database.sessions.begin() as db:
        row = db.get(SourceMaterial, source["id"])
        row.processing_status = "EXTRACTING"
        extraction = dict(row.extraction)
        attempts = [dict(value) for value in extraction["attempts"]]
        attempts[-1]["status"] = "RUNNING"
        attempts[-1]["started_at"] = (datetime.now(UTC) - timedelta(minutes=4)).isoformat()
        extraction["attempts"] = attempts
        row.extraction = extraction
    current = client.get(f"/api/events/{event['id']}").json()
    assert current["sources"][0]["can_retry_extraction"] is True
    retried = client.post(
        f"/api/events/{event['id']}/sources/{source['id']}/retry-extraction",
        json={},
        headers=headers(client, settings),
    )
    assert retried.status_code == 200, retried.text
    assert len(requests) == 2
    with app.state.database.sessions() as db:
        row = db.get(SourceMaterial, source["id"])
        assert [item["status"] for item in row.extraction["attempts"]] == [
            "INTERRUPTED",
            "SUCCEEDED",
        ]
    assert [item["status"] for item in retried.json()["proposals"]] == [
        "SUPERSEDED",
        "PENDING",
    ]
    service.close()


def test_image_and_pdf_uploads_are_sent_individually_and_originals_remain_unchanged(
    client, settings, provider, app
):
    service, requests = install_responses(app, [{"proposals": []}, {"proposals": []}])
    sign_in(client, provider)
    event = client.post("/api/events", json={}, headers=headers(client, settings)).json()
    image_buffer = BytesIO()
    Image.new("RGB", (32, 24), color="white").save(image_buffer, format="PNG")
    image_bytes = image_buffer.getvalue()
    pdf_buffer = BytesIO()
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    writer.write(pdf_buffer)
    pdf_bytes = pdf_buffer.getvalue()
    for filename, raw, mime_type in (
        ("synthetic-event.png", image_bytes, "image/png"),
        ("synthetic-event.pdf", pdf_bytes, "application/pdf"),
    ):
        response = client.post(
            f"/api/events/{event['id']}/sources",
            files={"file": (filename, raw, mime_type)},
            headers=headers(client, settings),
        )
        assert response.status_code == 201, response.text
        assert response.json()["sources"][-1]["processing_status"] == "NO_FACTS_FOUND"
    assert len(requests) == 2
    for index, (mime_type, raw) in enumerate(
        (("image/png", image_bytes), ("application/pdf", pdf_bytes))
    ):
        body = requests[index]
        assert body["store"] is False
        content = body["input"][0]["content"]
        part = next(item for item in content if item["type"] in {"input_image", "input_file"})
        if mime_type.startswith("image/"):
            encoded = part["image_url"].split(",", 1)[1]
        else:
            encoded = part["file_data"].split(",", 1)[1]
        assert base64.b64decode(encoded) == raw
        assert "/files" not in str(body)
    source_list = response.json()["sources"]
    assert len(source_list) == 2
    with app.state.database.sessions() as db:
        for source_view, raw in zip(source_list, (image_bytes, pdf_bytes), strict=True):
            row = db.get(SourceMaterial, source_view["id"])
            stored = settings.private_storage_dir / row.storage_key
            assert stored.read_bytes() == raw
    service.close()


def test_extraction_evaluation_fixtures_are_synthetic_and_cover_requested_shapes():
    cases = json.loads(
        (Path(__file__).parent / "fixtures" / "extraction-evaluation.json").read_text()
    )
    ids = {case["id"] for case in cases}
    assert {
        "clean-announcement",
        "messy-email",
        "missing-price",
        "conflicting-dates-source-one",
        "conflicting-dates-source-two",
        "virtual-event",
        "hybrid-event",
        "synthetic-flyer",
        "prompt-injection-data",
        "relative-date",
        "irrelevant-text",
    } <= ids
    assert all("Alex Rivera" not in case["source"] for case in cases)
