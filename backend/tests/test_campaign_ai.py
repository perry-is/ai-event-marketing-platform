import copy
import json

import httpx2
import pytest
from fastapi import HTTPException

from app.campaign_ai import OpenAICampaignService, _expected_claims
from app.campaign_service import CampaignContent, DeterministicCampaignService
from tests.openai_test_support import mock_response


def event_snapshot():
    return {
        "id": "synthetic-event-id",
        "revision": 4,
        "title": "Lunch & Learn",
        "starts_at": "2026-10-22T17:30:00+00:00",
        "ends_at": "2026-10-22T19:00:00+00:00",
        "timezone": "America/Denver",
        "delivery": "IN_PERSON",
        "facts": {
            "family_label": "Lunch & Learn",
            "topic": "small-business growth",
            "schedule_local_start": "2026-10-22T11:30:00",
            "schedule_local_end": "2026-10-22T13:00:00",
            "location": {"venue": "Example Venue"},
            "people": {"host": "Alex Rivera"},
            "registration": {"price_kind": "FREE", "strategy": "NO_REGISTRATION"},
            "content_facts": {},
        },
    }


def campaign_content(selected=None):
    selected = set(
        selected
        or [
            "EVENTBRITE",
            "MEETUP",
            "LINKEDIN_ORGANIZATION",
            "LINKEDIN_PERSONAL",
            "INSTAGRAM",
            "FACEBOOK_GROUPS",
        ]
    )
    return CampaignContent(
        marketing_headline="Join us for Lunch & Learn",
        short_summary="A warm place to connect and exchange practical ideas.",
        general_description=(
            "Join us on October 22, 2026 at 11:30 AM at Example Venue for an in-person, free event."
        ),
        attendee_experience=None,
        eventbrite_description="Explore practical ideas with the Event Marketing community.",
        meetup_description="Meet local professionals and exchange useful ideas.",
        linkedin_organization_post="Event Marketing welcomes you to a useful community gathering.",
        linkedin_personal_post="I’m looking forward to connecting with local professionals.",
        instagram_caption="Good conversations and practical ideas. Join us!",
        facebook_groups_post="Hello, community. Join us for a practical conversation.",
        creative_direction="Use a welcoming text-led layout; no graphic has been generated.",
    ).model_copy(
        update={
            "eventbrite_description": "Explore practical ideas with the Event Marketing community."
            if "EVENTBRITE" in selected
            else None,
            "meetup_description": "Meet local professionals and exchange useful ideas."
            if "MEETUP" in selected
            else None,
            "linkedin_organization_post": (
                "Event Marketing welcomes you to a useful community gathering."
            )
            if "LINKEDIN_ORGANIZATION" in selected
            else None,
            "linkedin_personal_post": "I’m looking forward to connecting with local professionals."
            if "LINKEDIN_PERSONAL" in selected
            else None,
            "instagram_caption": "Good conversations and practical ideas. Join us!"
            if "INSTAGRAM" in selected
            else None,
            "facebook_groups_post": "Hello, community. Join us for a practical conversation."
            if "FACEBOOK_GROUPS" in selected
            else None,
        }
    )


def output_for(snapshot, content=None):
    return {
        "content": (content or campaign_content()).model_dump(mode="json"),
        "factual_claims": [
            {"field_path": path, "value_json": value}
            for path, value in _expected_claims(snapshot).items()
        ],
    }


def service(handler):
    client = httpx2.Client(transport=httpx2.MockTransport(handler))
    return OpenAICampaignService(
        api_key="test-provider-key",
        model_id="gpt-6-luna-test",
        http_client=client,
    ), client


def test_openai_generation_uses_one_strict_responses_call_and_keeps_metadata_out_of_content():
    snapshot = event_snapshot()
    captured = []

    def respond(request):
        captured.append(json.loads(request.content))
        return mock_response(output_for(snapshot), response_id="resp_campaign_fixture")

    generator, client = service(respond)
    content = generator.generate(
        snapshot,
        [
            "EVENTBRITE",
            "MEETUP",
            "LINKEDIN_ORGANIZATION",
            "LINKEDIN_PERSONAL",
            "INSTAGRAM",
            "FACEBOOK_GROUPS",
        ],
    )
    generator.close()
    client.close()

    assert len(captured) == 1
    request = captured[0]
    assert request["model"] == "gpt-6-luna-test"
    assert request["store"] is False
    assert request["tools"] == []
    assert request["max_output_tokens"] == 6500
    assert request["text"]["format"]["type"] == "json_schema"
    assert request["text"]["format"]["strict"] is True
    assert request["text"]["format"]["schema"]["additionalProperties"] is False
    payload = json.loads(request["input"][0]["content"])
    assert payload["locked_event_facts"]["title"] == snapshot["title"]
    assert "id" not in payload["locked_event_facts"]
    assert payload["locked_fact_claim_reference"] == [
        {"field_path": path, "value_json": value}
        for path, value in _expected_claims(snapshot).items()
    ]
    assert content.general_description.startswith("Join us on October 22")
    digest = content.generation_metadata["input_sha256"]
    assert len(digest) == 64
    assert set(digest) <= set("0123456789abcdef")
    assert content.generation_metadata == {
        "generator_id": "openai-responses-campaign",
        "generator_version": "campaign-generation-v3",
        "prompt_version": "campaign-generation-v3",
        "output_schema_version": 1,
        "model_id": "gpt-6-luna-test",
        "provider_response_id": "resp_campaign_fixture",
        "input_sha256": digest,
    }
    assert "generation_metadata" not in content.model_dump(mode="json")


@pytest.mark.parametrize(
    ("output", "expected_code"),
    [
        (
            {"content": {"marketing_headline": "Only a partial response"}, "factual_claims": []},
            "CAMPAIGN_AI_RESPONSE_INVALID",
        ),
        (
            {
                "content": campaign_content().model_dump(mode="json")
                | {"new_field": "unsupported"},
                "factual_claims": [],
            },
            "CAMPAIGN_AI_RESPONSE_INVALID",
        ),
        (
            {"content": campaign_content().model_dump(mode="json"), "factual_claims": []},
            "CAMPAIGN_COPY_NEEDS_EVENT_REVIEW",
        ),
    ],
)
def test_malformed_or_unsupported_structured_output_fails_closed(output, expected_code):
    generator, client = service(lambda request: mock_response(output))
    with pytest.raises(HTTPException) as caught:
        generator.generate(
            event_snapshot(),
            [
                "EVENTBRITE",
                "MEETUP",
                "LINKEDIN_ORGANIZATION",
                "LINKEDIN_PERSONAL",
                "INSTAGRAM",
                "FACEBOOK_GROUPS",
            ],
        )
    assert caught.value.status_code == 503
    assert caught.value.detail == expected_code
    generator.close()
    client.close()


def test_refusal_and_incomplete_output_are_safe_generation_failures():
    refusal_body = {
        "id": "resp_refused",
        "object": "response",
        "created_at": 1_791_216_000,
        "status": "completed",
        "model": "gpt-6-luna-test",
        "error": None,
        "incomplete_details": None,
        "output": [
            {
                "id": "msg_refused",
                "type": "message",
                "status": "completed",
                "role": "assistant",
                "content": [{"type": "refusal", "refusal": "synthetic refusal"}],
            }
        ],
        "tools": [],
    }
    refusal, refusal_client = service(lambda request: httpx2.Response(200, json=refusal_body))
    with pytest.raises(HTTPException) as caught:
        refusal.generate(event_snapshot(), ["INSTAGRAM"])
    assert caught.value.detail == "CAMPAIGN_GENERATION_FAILED"
    refusal.close()
    refusal_client.close()

    incomplete, incomplete_client = service(
        lambda request: mock_response(output_for(event_snapshot()), status="incomplete")
    )
    with pytest.raises(HTTPException) as caught:
        incomplete.generate(event_snapshot(), ["INSTAGRAM"])
    assert caught.value.detail == "CAMPAIGN_GENERATION_FAILED"
    incomplete.close()
    incomplete_client.close()


@pytest.mark.parametrize(
    ("handler", "request_count", "expected_code"),
    [
        (
            lambda request: httpx2.Response(429, json={"error": {"message": "synthetic limit"}}),
            1,
            "CAMPAIGN_AI_RATE_LIMITED",
        ),
        (
            lambda request: httpx2.Response(503, json={"error": {"message": "synthetic outage"}}),
            1,
            "CAMPAIGN_GENERATION_FAILED",
        ),
        (
            lambda request: (_ for _ in ()).throw(
                httpx2.ReadTimeout("synthetic timeout", request=request)
            ),
            1,
            "CAMPAIGN_AI_TIMEOUT",
        ),
    ],
)
def test_provider_failures_are_safe_and_do_not_retry_automatically(
    handler, request_count, expected_code
):
    requests = []

    def counted(request):
        requests.append(request)
        return handler(request)

    generator, client = service(counted)
    with pytest.raises(HTTPException) as caught:
        generator.generate(event_snapshot(), ["INSTAGRAM"])
    assert caught.value.status_code == 503
    assert caught.value.detail == expected_code
    assert len(requests) == request_count
    generator.close()
    client.close()


@pytest.mark.parametrize("claim_path", ["event.starts_at", "event.facts.location.venue"])
def test_contradictory_structured_locked_fact_prevents_content_return(claim_path):
    snapshot = event_snapshot()
    output = output_for(snapshot)
    claim = next(item for item in output["factual_claims"] if item["field_path"] == claim_path)
    claim["value_json"] = (
        '"October 24"' if claim_path.endswith("starts_at") else '"Different Venue"'
    )
    generator, client = service(lambda request: mock_response(output))
    with pytest.raises(HTTPException) as caught:
        generator.generate(
            snapshot,
            [
                "EVENTBRITE",
                "MEETUP",
                "LINKEDIN_ORGANIZATION",
                "LINKEDIN_PERSONAL",
                "INSTAGRAM",
                "FACEBOOK_GROUPS",
            ],
        )
    assert caught.value.detail == "CAMPAIGN_COPY_NEEDS_EVENT_REVIEW"
    generator.close()
    client.close()


def test_copy_date_contradiction_is_rejected_even_when_claim_echo_is_correct():
    snapshot = event_snapshot()
    content = campaign_content().model_copy(
        update={"general_description": "Join us on October 24, 2026 at Example Venue."}
    )
    generator, client = service(lambda request: mock_response(output_for(snapshot, content)))
    with pytest.raises(HTTPException) as caught:
        generator.generate(
            snapshot,
            [
                "EVENTBRITE",
                "MEETUP",
                "LINKEDIN_ORGANIZATION",
                "LINKEDIN_PERSONAL",
                "INSTAGRAM",
                "FACEBOOK_GROUPS",
            ],
        )
    assert caught.value.detail == "CAMPAIGN_COPY_NEEDS_EVENT_REVIEW"
    generator.close()
    client.close()


@pytest.mark.parametrize(
    "copy_text",
    [
        "This is a paid event with tickets for $25.",
        "Join us online.",
        "Limited seats are available.",
        "Register at https://example.invalid/event.",
    ],
)
def test_machine_detectable_price_delivery_scarcity_and_url_claims_fail_closed(copy_text):
    snapshot = event_snapshot()
    content = campaign_content().model_copy(update={"general_description": copy_text})
    generator, client = service(lambda request: mock_response(output_for(snapshot, content)))
    with pytest.raises(HTTPException) as caught:
        generator.generate(
            snapshot,
            [
                "EVENTBRITE",
                "MEETUP",
                "LINKEDIN_ORGANIZATION",
                "LINKEDIN_PERSONAL",
                "INSTAGRAM",
                "FACEBOOK_GROUPS",
            ],
        )
    assert caught.value.detail == "CAMPAIGN_COPY_NEEDS_EVENT_REVIEW"
    generator.close()
    client.close()


def test_targeted_and_multi_target_revisions_preserve_every_unaffected_field():
    snapshot = event_snapshot()
    selected = [
        "EVENTBRITE",
        "MEETUP",
        "LINKEDIN_ORGANIZATION",
        "LINKEDIN_PERSONAL",
        "INSTAGRAM",
        "FACEBOOK_GROUPS",
    ]
    current = campaign_content()
    captured = []

    def respond(request):
        payload = json.loads(json.loads(request.content)["input"][0]["content"])
        captured.append(payload)
        revised, _ = DeterministicCampaignService().revise(
            current, snapshot, payload["revision_request"], selected
        )
        return mock_response(output_for(snapshot, revised))

    reviser, client = service(respond)
    revised, targets = reviser.revise(current, snapshot, "Make LinkedIn warmer.", selected)
    assert targets == ["linkedin_organization_post", "linkedin_personal_post"]
    assert revised.linkedin_organization_post != current.linkedin_organization_post
    assert revised.linkedin_personal_post != current.linkedin_personal_post
    assert revised.eventbrite_description == current.eventbrite_description
    assert revised.meetup_description == current.meetup_description
    assert captured[0]["allowed_fields"] == targets
    assert revised.generation_metadata["prompt_version"] == "campaign-revision-v3"
    reviser.close()
    client.close()


def test_multi_target_revision_changes_only_the_named_selected_surfaces():
    snapshot = event_snapshot()
    selected = [
        "EVENTBRITE",
        "MEETUP",
        "LINKEDIN_ORGANIZATION",
        "LINKEDIN_PERSONAL",
        "INSTAGRAM",
        "FACEBOOK_GROUPS",
    ]
    current = campaign_content()
    changed = current.model_copy(
        update={
            "eventbrite_description": "A shorter Eventbrite description.",
            "instagram_caption": "A playful Instagram invitation.",
        }
    )
    captured = []

    def respond(request):
        captured.append(json.loads(request.content))
        return mock_response(output_for(snapshot, changed))

    reviser, client = service(respond)
    revised, targets = reviser.revise(
        current,
        snapshot,
        "Shorten Eventbrite and make Instagram more playful.",
        selected,
    )
    assert targets == ["eventbrite_description", "instagram_caption"]
    assert captured[0]["text"]["format"]["strict"] is True
    payload = json.loads(captured[0]["input"][0]["content"])
    assert payload["allowed_fields"] == targets
    before, after = current.model_dump(mode="json"), revised.model_dump(mode="json")
    assert {key for key in before if before[key] != after[key]} == set(targets)
    reviser.close()
    client.close()


def test_revision_that_changes_an_unrelated_surface_is_rejected():
    snapshot = event_snapshot()
    selected = ["EVENTBRITE", "LINKEDIN_ORGANIZATION", "INSTAGRAM"]
    current = campaign_content(selected)
    changed = current.model_copy(
        update={
            "linkedin_organization_post": "A warmer LinkedIn invitation.",
            "eventbrite_description": "An unrelated Eventbrite mutation.",
        }
    )
    reviser, client = service(lambda request: mock_response(output_for(snapshot, changed)))
    with pytest.raises(HTTPException) as caught:
        reviser.revise(current, snapshot, "Make LinkedIn warmer.", selected)
    assert caught.value.detail == "CAMPAIGN_REVISION_FAILED"
    reviser.close()
    client.close()


def test_event_fact_revision_is_rejected_before_provider_call():
    calls = []
    reviser, client = service(lambda request: calls.append(request) or mock_response({}))
    with pytest.raises(HTTPException) as caught:
        reviser.revise(
            campaign_content(), event_snapshot(), "Change the date to October 24.", ["INSTAGRAM"]
        )
    assert caught.value.status_code == 409
    assert caught.value.detail == "EVENT_CORRECTION_REQUIRED"
    assert calls == []
    reviser.close()
    client.close()


def test_revision_for_unselected_linkedin_surface_is_rejected_before_provider_call():
    calls = []
    reviser, client = service(lambda request: calls.append(request) or mock_response({}))
    with pytest.raises(HTTPException) as caught:
        reviser.revise(
            campaign_content(["EVENTBRITE"]),
            event_snapshot(),
            "Make LinkedIn warmer.",
            ["EVENTBRITE"],
        )
    assert caught.value.status_code == 422
    assert caught.value.detail == "REVISION_NOT_SUPPORTED"
    assert calls == []
    reviser.close()
    client.close()


def test_test_fixture_does_not_mutate_input_event_or_return_claims_in_content():
    snapshot = event_snapshot()
    before = copy.deepcopy(snapshot)
    generator, client = service(lambda request: mock_response(output_for(snapshot)))
    result = generator.generate(
        snapshot,
        [
            "EVENTBRITE",
            "MEETUP",
            "LINKEDIN_ORGANIZATION",
            "LINKEDIN_PERSONAL",
            "INSTAGRAM",
            "FACEBOOK_GROUPS",
        ],
    )
    assert snapshot == before
    assert isinstance(result, CampaignContent)
    assert "factual_claims" not in result.model_dump(mode="json")
    generator.close()
    client.close()
