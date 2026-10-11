import base64
import json

import httpx2
import pytest
from fastapi import HTTPException

from app.graphics import FakeImageProvider, OpenAIImageProvider


def test_openai_image_generation_uses_one_images_api_call_and_validates_bytes():
    image = FakeImageProvider().generate("fixture prompt").image_bytes
    requests = []

    def response(request):
        requests.append(request)
        assert request.url.path == "/v1/images/generations"
        body = json.loads(request.content)
        assert body["model"] == "gpt-image-2.5-flare-test"
        assert body["n"] == 1
        assert body["size"] == "1024x1024"
        assert body["output_format"] == "png"
        return httpx2.Response(
            200,
            json={"data": [{"b64_json": base64.b64encode(image).decode()}]},
            headers={"x-request-id": "req_graphic_generate"},
        )

    client = httpx2.Client(transport=httpx2.MockTransport(response))
    provider = OpenAIImageProvider("test-provider-key", "gpt-image-2.5-flare-test", client)
    try:
        result = provider.generate("fixture prompt")
    finally:
        provider.close()
    assert len(requests) == 1
    assert result.mode == "GENERATE"
    assert result.request_id == "req_graphic_generate"
    assert result.image_bytes == image


def test_openai_image_edit_uses_single_multipart_edit_call():
    image = FakeImageProvider().generate("first").image_bytes
    requests = []

    def response(request):
        requests.append(request)
        assert request.url.path == "/v1/images/edits"
        assert b'name="model"' in request.content
        assert b"gpt-image-2.5-sunburst-test" in request.content
        assert b'name="image"' in request.content
        assert b"visual-layer.png" in request.content
        assert b"Use less orange" in request.content
        return httpx2.Response(200, json={"data": [{"b64_json": base64.b64encode(image).decode()}]})

    client = httpx2.Client(transport=httpx2.MockTransport(response))
    provider = OpenAIImageProvider("test-provider-key", "gpt-image-2.5-sunburst-test", client)
    try:
        result = provider.edit(image, "Use less orange")
    finally:
        provider.close()
    assert len(requests) == 1
    assert result.mode == "EDIT"
    assert result.prompt == "Use less orange"


def test_openai_image_provider_rejects_invalid_provider_bytes():
    client = httpx2.Client(
        transport=httpx2.MockTransport(
            lambda _request: httpx2.Response(
                200,
                json={"data": [{"b64_json": base64.b64encode(b"not an image").decode()}]},
            )
        )
    )
    provider = OpenAIImageProvider("test-provider-key", "gpt-image-2.5-flare-test", client)
    try:
        with pytest.raises(HTTPException) as caught:
            provider.generate("fixture prompt")
    finally:
        provider.close()
    assert caught.value.status_code == 503
    assert caught.value.detail == "GRAPHIC_GENERATION_FAILED"


def test_image_provider_does_not_retry_rate_limited_request():
    requests = []

    def response(request):
        requests.append(request)
        return httpx2.Response(
            429,
            json={
                "error": {
                    "message": "rate limited",
                    "type": "rate_limit_error",
                    "param": None,
                    "code": "rate_limit_exceeded",
                }
            },
        )

    client = httpx2.Client(transport=httpx2.MockTransport(response))
    provider = OpenAIImageProvider("test-provider-key", "gpt-image-2.5-flare-test", client)
    try:
        with pytest.raises(HTTPException) as caught:
            provider.generate("fixture prompt")
    finally:
        provider.close()
    assert caught.value.status_code == 503
    assert len(requests) == 1
