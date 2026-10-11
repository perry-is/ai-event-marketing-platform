import copy
import hashlib
import io
import uuid

import pytest
from fastapi import HTTPException
from PIL import Image
from sqlalchemy import select
from sqlalchemy.exc import DBAPIError

from app.campaign_service import SUPPORTED_SURFACES, campaign_hash_projection, snapshot_hash
from app.graphics import (
    FakeImageProvider,
    compose_campaign_graphic,
    image_prompt,
    layout_revision,
    validate_generated_image,
)
from app.models import (
    Approval,
    CampaignVersion,
    CampaignVersionAsset,
    GeneratedAsset,
)
from tests.conftest import sign_in
from tests.test_campaigns import ready_event


def _headers(client, settings):
    return {
        "origin": settings.app_origin,
        "x-csrf-token": client.get("/api/me").json()["csrf_token"],
    }


def _campaign(client, settings, event):
    result = client.post(
        f"/api/events/{event['id']}/campaign",
        json={"selected_surfaces": list(SUPPORTED_SURFACES)},
        headers=_headers(client, settings),
    )
    assert result.status_code == 200, result.text
    return result.json()


def _approve(client, settings, campaign):
    preview = client.get(f"/api/campaigns/{campaign['id']}/approval-preview").json()
    response = client.post(
        f"/api/campaigns/{campaign['id']}/approve",
        json={
            "expected_revision": preview["campaign"]["revision"],
            "campaign_version_id": preview["version_id"],
            "version_hash": preview["version_hash"],
            "expected_destination_plan_hash": preview["destination_plan_hash"],
            "replacement_acknowledgements": [],
            "uncertain_risk_acknowledgements": [],
        },
        headers=_headers(client, settings),
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_graphic_routes_require_application_session(client):
    identifier = str(uuid.uuid4())
    assert (
        client.post(
            f"/api/campaigns/{identifier}/graphics", json={"expected_revision": 1}
        ).status_code
        == 401
    )
    assert client.get(f"/api/campaign-versions/{identifier}/graphic").status_code == 401


def test_graphic_successor_approval_history_and_manual_execution_bind_exact_asset(
    client, settings, provider, app
):
    sign_in(client, provider)
    event = ready_event(client, settings, provider)
    first = _campaign(client, settings, event)
    v1 = copy.deepcopy(first["current_version"])
    response = client.post(
        f"/api/campaigns/{first['id']}/graphics",
        json={"expected_revision": first["revision"]},
        headers=_headers(client, settings),
    )
    assert response.status_code == 200, response.text
    with_graphic = response.json()
    v2 = with_graphic["current_version"]
    assert v2["version_number"] == 2
    assert v2["primary_graphic"]["role"] == "PRIMARY_GRAPHIC"
    assert v2["primary_graphic"]["width"] == 1080
    assert v2["primary_graphic"]["height"] == 1080
    assert v2["content_hash"] != v1["content_hash"]
    assert with_graphic["state"] == "REVIEW"
    assert with_graphic["revision"] == 2
    assert with_graphic["versions"][0]["revision_targets"] == ["primary_graphic"]
    assert with_graphic["versions"][0]["revision_request"] == "Generated campaign graphic."

    first_history = client.get(f"/api/campaigns/{first['id']}/versions/1").json()["viewed_version"]
    assert first_history["primary_graphic"] is None
    assert first_history["content_hash"] == v1["content_hash"]

    preview_path = v2["primary_graphic"]["preview_url"]
    original_preview = client.get(preview_path)
    assert original_preview.status_code == 200
    assert original_preview.headers["content-type"].startswith("image/png")
    assert original_preview.headers["cache-control"] == "no-store"
    mime, width, height = validate_generated_image(original_preview.content)
    assert (mime, width, height) == ("image/png", 1080, 1080)
    old_graphic_hash = hashlib.sha256(original_preview.content).hexdigest()
    assert old_graphic_hash == v2["primary_graphic"]["sha256"]

    approval = _approve(client, settings, with_graphic)
    assert approval["campaign_version_id"] == v2["id"]
    started = client.post(
        f"/api/approvals/{approval['id']}/start", headers=_headers(client, settings)
    )
    assert started.status_code == 200, started.text
    distribution = started.json()
    manual = next(
        item for item in distribution["executions"] if item["destination_key"] == "FACEBOOK_GROUPS"
    )
    assert manual["campaign_version_id"] == v2["id"]
    assert manual["manual_package"]["graphic_asset_id"] == v2["primary_graphic"]["asset_id"]
    assert manual["manual_package"]["graphic_sha256"] == v2["primary_graphic"]["sha256"]
    assert manual["manual_package"]["graphic_preview_url"] == preview_path
    assert all(
        target.get("approved_graphic", {}).get("asset_id") == v2["primary_graphic"]["asset_id"]
        for target in approval["destination_plan"]["targets"]
    )
    eventbrite = next(
        item for item in distribution["executions"] if item["destination_key"] == "EVENTBRITE"
    )
    assert (
        eventbrite["plan_entry"]["approved_graphic"]["asset_id"]
        == v2["primary_graphic"]["asset_id"]
    )
    assert (
        eventbrite["attempts"][0]["evidence"]["approved_graphic"]["sha256"]
        == v2["primary_graphic"]["sha256"]
    )

    # A copy-only revision creates a complete successor snapshot, including the
    # same immutable graphic asset, without duplicating asset bytes or approval.
    copy_revision = client.post(
        f"/api/campaigns/{first['id']}/revisions",
        json={
            "expected_revision": client.get(f"/api/campaigns/{first['id']}").json()["revision"],
            "revision_request": "Make LinkedIn warmer.",
        },
        headers=_headers(client, settings),
    )
    assert copy_revision.status_code == 200, copy_revision.text
    after_copy = copy_revision.json()
    v3 = after_copy["current_version"]
    assert v3["version_number"] == 3
    assert (
        v3["content"]["linkedin_organization_post"] != v2["content"]["linkedin_organization_post"]
    )
    assert v3["primary_graphic"]["asset_id"] == v2["primary_graphic"]["asset_id"]
    assert v3["primary_graphic"]["sha256"] == v2["primary_graphic"]["sha256"]
    assert v3["content_hash"] != v2["content_hash"]
    assert after_copy["state"] == "REVIEW"
    history_v2_after_copy = client.get(f"/api/campaigns/{first['id']}/versions/2").json()[
        "viewed_version"
    ]
    assert history_v2_after_copy["content_hash"] == v2["content_hash"]
    assert history_v2_after_copy["primary_graphic"]["asset_id"] == v2["primary_graphic"]["asset_id"]
    assert hashlib.sha256(client.get(preview_path).content).hexdigest() == old_graphic_hash
    distribution_after_copy = client.get(f"/api/campaigns/{first['id']}/distribution").json()
    assert distribution_after_copy["active_approval"] is None
    assert distribution_after_copy["approvals"][0]["campaign_version_id"] == v2["id"]
    historical_manual_after_copy = next(
        item
        for item in distribution_after_copy["executions"]
        if item["destination_key"] == "FACEBOOK_GROUPS"
    )
    assert historical_manual_after_copy["campaign_version_id"] == v2["id"]
    assert (
        historical_manual_after_copy["manual_package"]["graphic_asset_id"]
        == v2["primary_graphic"]["asset_id"]
    )
    with app.state.database.sessions() as db:
        final_assets = db.scalars(
            select(GeneratedAsset).where(
                GeneratedAsset.campaign_id == uuid.UUID(first["id"]),
                GeneratedAsset.asset_type == "CAMPAIGN_GRAPHIC",
            )
        ).all()
        assert len(final_assets) == 1
        v2_link = db.scalar(
            select(CampaignVersionAsset).where(
                CampaignVersionAsset.version_id == uuid.UUID(v2["id"])
            )
        )
        v3_link = db.scalar(
            select(CampaignVersionAsset).where(
                CampaignVersionAsset.version_id == uuid.UUID(v3["id"])
            )
        )
        assert v2_link.asset_id == v3_link.asset_id == uuid.UUID(v2["primary_graphic"]["asset_id"])

    revision = client.post(
        f"/api/campaigns/{first['id']}/graphics",
        json={
            "expected_revision": after_copy["revision"],
            "instruction": "Use less orange and make the background feel more professional.",
        },
        headers=_headers(client, settings),
    )
    assert revision.status_code == 200, revision.text
    v4_campaign = revision.json()
    v4 = v4_campaign["current_version"]
    assert v4["version_number"] == 4
    assert v4["id"] != v3["id"]
    assert v4["primary_graphic"]["asset_id"] != v3["primary_graphic"]["asset_id"]
    assert v4["primary_graphic"]["sha256"] != v3["primary_graphic"]["sha256"]
    assert v4["content"] == v3["content"]
    assert v4_campaign["state"] == "REVIEW"
    assert v4_campaign["current_version"]["is_current"] is True
    history_v2 = client.get(f"/api/campaigns/{first['id']}/versions/2").json()["viewed_version"]
    assert history_v2["primary_graphic"]["asset_id"] == v2["primary_graphic"]["asset_id"]
    assert history_v2["content_hash"] == v2["content_hash"]
    assert hashlib.sha256(client.get(preview_path).content).hexdigest() == old_graphic_hash
    assert (
        client.get(f"/api/campaigns/{first['id']}/distribution").json()["approvals"][0][
            "campaign_version_id"
        ]
        == v2["id"]
    )

    distribution_after = client.get(f"/api/campaigns/{first['id']}/distribution").json()
    assert distribution_after["active_approval"] is None
    historical_manual = next(
        item
        for item in distribution_after["executions"]
        if item["destination_key"] == "FACEBOOK_GROUPS"
    )
    assert (
        historical_manual["manual_package"]["graphic_asset_id"] == v2["primary_graphic"]["asset_id"]
    )

    invalid = client.post(
        f"/api/campaigns/{first['id']}/graphics",
        json={
            "expected_revision": v4_campaign["revision"],
            "instruction": "Change the date on the graphic to October 24.",
        },
        headers=_headers(client, settings),
    )
    assert invalid.status_code == 409
    assert invalid.json()["error"]["code"] == "EVENT_CORRECTION_REQUIRED"
    assert client.get(f"/api/campaigns/{first['id']}").json()["current_version_number"] == 4

    with app.state.database.sessions() as db:
        versions = db.scalars(
            select(CampaignVersion)
            .where(CampaignVersion.campaign_id == uuid.UUID(first["id"]))
            .order_by(CampaignVersion.version_number)
        ).all()
        assets = db.scalars(
            select(GeneratedAsset).where(GeneratedAsset.campaign_id == uuid.UUID(first["id"]))
        ).all()
        assert [value.version_number for value in versions] == [1, 2, 3, 4]
        assert len([value for value in assets if value.asset_type == "CAMPAIGN_GRAPHIC"]) == 2
        v2_row = versions[1]
        assert (
            versions[2].snapshot["primary_graphic"]["asset_id"] == v2["primary_graphic"]["asset_id"]
        )
        with pytest.raises(DBAPIError, match="campaign versions are immutable"):
            with db.begin_nested():
                v2_row.snapshot["primary_graphic"]["sha256"] = "0" * 64
                from sqlalchemy.orm.attributes import flag_modified

                flag_modified(v2_row, "snapshot")
                db.flush()
        primary_link = db.scalar(
            select(CampaignVersionAsset).where(
                CampaignVersionAsset.version_id == uuid.UUID(v2["id"])
            )
        )
        with pytest.raises(DBAPIError, match="generated assets are immutable"):
            with db.begin_nested():
                db.delete(primary_link)
                db.flush()
        approval_rows = db.scalars(select(Approval)).all()
        assert len(approval_rows) == 1
        assert approval_rows[0].campaign_version_id == uuid.UUID(v2["id"])


def test_graphic_failure_and_text_only_hash_compatibility(client, settings, provider, app):
    sign_in(client, provider)
    event = ready_event(client, settings, provider)
    campaign = _campaign(client, settings, event)
    old_version = copy.deepcopy(campaign["current_version"])

    class FailedImageProvider:
        model_id = "fake-failure"

        def generate(self, prompt):
            raise HTTPException(503, "GRAPHIC_GENERATION_FAILED")

        def edit(self, source, prompt):
            raise AssertionError("no edit call expected")

    app.state.image_provider = FailedImageProvider()
    failed = client.post(
        f"/api/campaigns/{campaign['id']}/graphics",
        json={"expected_revision": campaign["revision"]},
        headers=_headers(client, settings),
    )
    assert failed.status_code == 503
    unchanged = client.get(f"/api/campaigns/{campaign['id']}").json()
    assert unchanged["current_version"]["id"] == old_version["id"]
    assert unchanged["current_version_number"] == 1
    with app.state.database.sessions() as db:
        assert db.scalars(select(GeneratedAsset)).all() == []

    legacy_snapshot = {
        "schema_version": 1,
        "event": {"id": "event-1", "revision": 3, "title": "Lunch & Learn"},
        "selected_surfaces": ["INSTAGRAM", "LINKEDIN_PERSONAL"],
        "content": {"marketing_headline": "Lunch & Learn", "instagram_caption": "Join us"},
        "generation": {"generator_id": "template-a"},
    }
    assert set(campaign_hash_projection(legacy_snapshot)) == {
        "event_facts",
        "selected_surfaces",
        "content",
    }
    changed = copy.deepcopy(legacy_snapshot)
    changed["storage_key"] = "temporary/path"
    assert snapshot_hash(legacy_snapshot) == snapshot_hash(changed)


def test_graphic_hash_tracks_asset_identity_but_not_storage_metadata():
    snapshot = {
        "event": {"id": "event", "revision": 1, "title": "Networking"},
        "selected_surfaces": ["INSTAGRAM"],
        "content": {"instagram_caption": "Join us"},
        "primary_graphic": {
            "asset_id": "asset-a",
            "role": "PRIMARY_GRAPHIC",
            "sha256": "a" * 64,
            "composition_version": "campaign-square-1080-v1",
            "layout_variant": "STANDARD",
        },
    }
    equivalent = copy.deepcopy(snapshot)
    equivalent["primary_graphic"]["temporary_storage_key"] = "discarded/path"
    assert snapshot_hash(snapshot) == snapshot_hash(equivalent)
    assert snapshot_hash(snapshot) == snapshot_hash(equivalent)
    for field, value in (("asset_id", "asset-b"), ("sha256", "b" * 64)):
        changed = copy.deepcopy(snapshot)
        changed["primary_graphic"][field] = value
        assert snapshot_hash(snapshot) != snapshot_hash(changed)


def test_prompt_is_visual_only_and_deterministic_composition_requires_canonical_facts():
    event = {
        "title": "Event Marketingions Breakfast",
        "starts_at": "2026-10-22T17:30:00+00:00",
        "timezone": "America/Denver",
        "delivery": "IN_PERSON",
        "facts": {"topic": "Referrals", "location": {"venue": "Example Venue"}},
    }
    prompt = image_prompt(event, "Professional, warm and clear", None)
    assert "logos, watermarks" in prompt.lower()
    assert "do not invent, imitate, trace or redraw" in prompt.lower()
    assert "do not render any letters" in prompt.lower()
    assert "October 22" not in prompt
    visual = FakeImageProvider().generate(prompt).image_bytes
    composed, width, height = compose_campaign_graphic(visual, event)
    assert (width, height) == (1080, 1080)
    assert validate_generated_image(composed) == ("image/png", 1080, 1080)
    with Image.open(io.BytesIO(composed)) as result:
        assert result.mode == "RGB"
    with pytest.raises(HTTPException) as missing:
        compose_campaign_graphic(visual, {**event, "title": ""})
    assert missing.value.detail == "GRAPHIC_EVENT_DETAILS_REQUIRED"


def test_layout_revision_accepts_only_complete_supported_requests():
    assert layout_revision("Make the title larger.") == "TITLE_LARGER"
    assert layout_revision("Move the date higher") == "DETAILS_HIGHER"
    with pytest.raises(HTTPException) as mixed:
        layout_revision("Make the title larger and use less orange.")
    assert mixed.value.detail == "GRAPHIC_LAYOUT_REVISION_NOT_SUPPORTED"
