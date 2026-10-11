"""Campaign graphic commands: provider work, deterministic composition, atomic successor."""

import hashlib
import logging
import uuid
from datetime import UTC, datetime
from pathlib import Path

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from app.campaign_service import (
    CampaignContent,
    _check_generation_inputs,
    _check_readiness,
    _create_version,
    _find_campaign,
    _version_graphic,
    campaign_view,
    is_stale,
    material_event_snapshot,
    validate_content,
    validate_surfaces,
)
from app.file_storage import resolve_storage_path, storage_key, write_private
from app.graphics import (
    COMPOSITION_VERSION,
    IMAGE_PROMPT_VERSION,
    ImageProvider,
    compose_campaign_graphic,
    image_prompt,
    layout_revision,
    validate_generated_image,
)
from app.models import CampaignVersion, CampaignVersionAsset, GeneratedAsset

LOGGER = logging.getLogger("event_marketing.graphics")


def _read_asset(settings, asset: GeneratedAsset) -> bytes:
    path = resolve_storage_path(settings.private_storage_dir, asset.storage_key)
    try:
        data = path.read_bytes()
    except OSError:
        raise HTTPException(409, "GRAPHIC_SOURCE_NOT_FOUND") from None
    mime, width, height = validate_generated_image(data)
    if (
        hashlib.sha256(data).hexdigest() != asset.sha256
        or len(data) != asset.byte_count
        or mime != asset.mime_type
        or (width, height) != (asset.width, asset.height)
    ):
        raise HTTPException(409, "GRAPHIC_SOURCE_INVALID")
    return data


def _check_layout_request(instruction: str) -> str | None:
    cleaned = instruction.strip()
    if not 3 <= len(cleaned) <= 500:
        raise HTTPException(422, "GRAPHIC_REVISION_REQUEST_INVALID")
    from app.campaign_service import is_event_fact_revision

    if is_event_fact_revision(cleaned):
        raise HTTPException(409, "EVENT_CORRECTION_REQUIRED")
    return layout_revision(cleaned)


def create_graphic_successor(
    sessions: sessionmaker,
    settings,
    campaign_id,
    actor_id,
    expected_revision: int,
    instruction: str | None,
    image_provider: ImageProvider,
) -> dict:
    """Generate/compose one graphic and atomically append its immutable successor version."""
    with sessions() as db:
        campaign, event = _find_campaign(db, campaign_id)
        if campaign.revision != expected_revision:
            raise HTTPException(409, "CAMPAIGN_REVISION_CONFLICT")
        _check_readiness(event)
        if campaign.current_version_id is None:
            raise HTTPException(409, "CAMPAIGN_VERSION_NOT_FOUND")
        current = db.get(CampaignVersion, campaign.current_version_id)
        if is_stale(event, current):
            raise HTTPException(409, "CAMPAIGN_STALE_REGENERATE_FIRST")
        current_graphic = _version_graphic(db, current)
        if current_graphic is None and instruction is not None:
            raise HTTPException(409, "GRAPHIC_REVISION_NOT_AVAILABLE")
        if current_graphic is not None and instruction is None:
            raise HTTPException(422, "GRAPHIC_REVISION_REQUEST_REQUIRED")
        layout_variant = _check_layout_request(instruction) if instruction is not None else None
        current_version_id = current.id
        event_snapshot = material_event_snapshot(event)
        content = CampaignContent.model_validate(current.snapshot["content"], strict=True)
        selected_surfaces = validate_surfaces(current.snapshot["selected_surfaces"])
        if current_graphic is not None:
            graphic_manifest = db.execute(
                select(CampaignVersionAsset, GeneratedAsset)
                .join(GeneratedAsset, GeneratedAsset.id == CampaignVersionAsset.asset_id)
                .where(
                    CampaignVersionAsset.version_id == current.id,
                    CampaignVersionAsset.role == "PRIMARY_GRAPHIC",
                )
            ).one_or_none()
            if graphic_manifest is None:
                raise HTTPException(409, "CAMPAIGN_VERSION_HASH_INVALID")
            previous_final = graphic_manifest[1]
            source_visual_id = previous_final.source_asset_id
            source_visual = db.get(GeneratedAsset, source_visual_id) if source_visual_id else None
            if source_visual is None or source_visual.asset_type != "VISUAL_LAYER":
                raise HTTPException(409, "GRAPHIC_SOURCE_NOT_FOUND")
            source_bytes = _read_asset(settings, source_visual)
        else:
            previous_final = None
            source_visual = None
            source_bytes = None
        captured_campaign_revision = campaign.revision

    if current_graphic is None:
        prompt = image_prompt(event_snapshot, content.creative_direction, None)
        provider_result = image_provider.generate(prompt)
        layout_variant = "STANDARD"
        visual_bytes = provider_result.image_bytes
    elif layout_variant:
        # Explicit supported text-layer changes reuse the exact visual; no model edits text.
        prompt = ""
        provider_result = None
        visual_bytes = source_bytes
    else:
        prompt = image_prompt(event_snapshot, content.creative_direction, instruction)
        provider_result = image_provider.edit(source_bytes, prompt)
        visual_bytes = provider_result.image_bytes
        layout_variant = "STANDARD"

    validate_generated_image(visual_bytes)
    final_bytes, width, height = compose_campaign_graphic(
        visual_bytes, event_snapshot, layout_variant=layout_variant or "STANDARD"
    )
    if provider_result is not None:
        source_id = uuid.uuid4()
        source_key = storage_key()
        source_mime, source_width, source_height = validate_generated_image(visual_bytes)
    else:
        source_id = source_visual.id
        source_key = None
        source_mime = source_visual.mime_type
        source_width, source_height = source_visual.width, source_visual.height
    final_id = uuid.uuid4()
    final_key = storage_key()
    final_hash = hashlib.sha256(final_bytes).hexdigest()
    source_hash = hashlib.sha256(visual_bytes).hexdigest()
    at = datetime.now(UTC)
    saved_paths: list[Path] = []

    try:
        with sessions.begin() as db:
            campaign, event = _find_campaign(db, campaign_id, lock=True)
            if campaign.revision != captured_campaign_revision:
                raise HTTPException(409, "CAMPAIGN_REVISION_CONFLICT")
            if campaign.current_version_id != current_version_id:
                raise HTTPException(409, "CAMPAIGN_REVISION_CONFLICT")
            _check_readiness(event)
            _check_generation_inputs(event, event_snapshot)
            current = db.get(CampaignVersion, current_version_id)
            if is_stale(event, current):
                raise HTTPException(409, "CAMPAIGN_STALE_REGENERATE_FIRST")

            if provider_result is not None:
                source_path = write_private(settings.private_storage_dir, source_key, visual_bytes)
                saved_paths.append(source_path)
                new_source_asset = GeneratedAsset(
                    id=source_id,
                    campaign_id=campaign.id,
                    source_campaign_version_id=current.id,
                    source_asset_id=source_visual.id if source_visual else None,
                    asset_type="VISUAL_LAYER",
                    generation_mode=provider_result.mode,
                    model_id=provider_result.model_id,
                    prompt_version=IMAGE_PROMPT_VERSION,
                    provider_request_id=provider_result.request_id,
                    storage_key=source_key,
                    mime_type=source_mime,
                    width=source_width,
                    height=source_height,
                    byte_count=len(visual_bytes),
                    sha256=source_hash,
                    provenance={
                        "source_campaign_version_id": str(current.id),
                        "source_asset_id": str(source_visual.id) if source_visual else None,
                    },
                    generation_metadata={
                        "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
                        "output_size": "1024x1024",
                        "image_count": 1,
                    },
                    created_by=actor_id,
                    created_at=at,
                )
                db.add(new_source_asset)
                db.flush()
                source_model_id = new_source_asset.id
                model_id = provider_result.model_id
                source_mode = provider_result.mode
                provider_request_id = provider_result.request_id
                prompt_hash = hashlib.sha256(prompt.encode()).hexdigest()
                prompt_version = IMAGE_PROMPT_VERSION
            else:
                source_model_id = source_visual.id
                model_id = previous_final.model_id
                source_mode = "COMPOSE"
                provider_request_id = None
                prompt_hash = previous_final.generation_metadata.get("prompt_sha256")
                prompt_version = previous_final.prompt_version

            final_path = write_private(settings.private_storage_dir, final_key, final_bytes)
            saved_paths.append(final_path)
            final_asset = GeneratedAsset(
                id=final_id,
                campaign_id=campaign.id,
                source_campaign_version_id=current.id,
                source_asset_id=source_model_id,
                asset_type="CAMPAIGN_GRAPHIC",
                generation_mode="COMPOSE",
                model_id=model_id,
                prompt_version=prompt_version,
                provider_request_id=provider_request_id,
                storage_key=final_key,
                mime_type="image/png",
                width=width,
                height=height,
                byte_count=len(final_bytes),
                sha256=final_hash,
                provenance={
                    "source_campaign_version_id": str(current.id),
                    "visual_layer_asset_id": str(source_model_id),
                    "approved_brand_logo_used": False,
                },
                generation_metadata={
                    "composition_version": COMPOSITION_VERSION,
                    "layout_variant": layout_variant or "STANDARD",
                    "source_visual_sha256": source_hash,
                    "prompt_sha256": prompt_hash,
                    "provider_mode": source_mode,
                },
                created_by=actor_id,
                created_at=at,
            )
            db.add(final_asset)
            db.flush()
            primary_graphic = {
                "asset_id": str(final_asset.id),
                "role": "PRIMARY_GRAPHIC",
                "sha256": final_asset.sha256,
                "composition_version": COMPOSITION_VERSION,
                "layout_variant": layout_variant or "STANDARD",
            }
            if provider_result is None:
                revision_request = instruction.strip()
            else:
                revision_request = (
                    instruction.strip() if instruction else "Generated campaign graphic."
                )
            from app.execution_service import prepare_for_successor

            prepare_for_successor(db, campaign, event, actor_id)
            _create_version(
                db,
                campaign,
                event_snapshot,
                validate_content(content, selected_surfaces),
                selected_surfaces,
                actor_id,
                revision_request=revision_request,
                revision_targets=["primary_graphic"],
                primary_graphic=primary_graphic,
            )
            db.flush()
            return campaign_view(db, campaign, event)
    except Exception:
        for path in saved_paths:
            path.unlink(missing_ok=True)
        raise


def campaign_graphic_bytes(sessions: sessionmaker, settings, version_id) -> tuple[bytes, str]:
    try:
        parsed = uuid.UUID(str(version_id))
    except ValueError:
        raise HTTPException(404, "CAMPAIGN_VERSION_NOT_FOUND") from None
    with sessions() as db:
        version = db.get(CampaignVersion, parsed)
        if version is None:
            raise HTTPException(404, "CAMPAIGN_VERSION_NOT_FOUND")
        graphic = _version_graphic(db, version)
        if graphic is None:
            raise HTTPException(404, "CAMPAIGN_GRAPHIC_NOT_FOUND")
        asset = db.get(GeneratedAsset, uuid.UUID(graphic["asset_id"]))
        if asset is None:
            raise HTTPException(404, "CAMPAIGN_GRAPHIC_NOT_FOUND")
        data = _read_asset(settings, asset)
        if asset.asset_type != "CAMPAIGN_GRAPHIC":
            raise HTTPException(404, "CAMPAIGN_GRAPHIC_NOT_FOUND")
        return data, asset.mime_type
