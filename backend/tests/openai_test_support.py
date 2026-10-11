"""Synthetic Responses API payload helpers for HTTP-boundary tests."""

import json

import httpx2

from app.extraction import OpenAIExtractionService


def text_proposal(field_path, candidate_value, evidence, **extra):
    return {
        "field_path": field_path,
        "candidate_value": candidate_value,
        "confidence": "HIGH",
        "evidence": evidence,
        "uncertainty_reason": None,
        **extra,
    }


def response_payload(output, *, status="completed", response_id="resp_fixture"):
    return {
        "id": response_id,
        "object": "response",
        "created_at": 1_791_216_000,
        "status": status,
        "error": None,
        "incomplete_details": None,
        "model": "gpt-6-luna",
        "output": [
            {
                "id": "msg_fixture",
                "type": "message",
                "status": "completed",
                "role": "assistant",
                "content": [
                    {
                        "type": "output_text",
                        "text": json.dumps(output),
                        "annotations": [],
                        "logprobs": [],
                    }
                ],
            }
        ],
        "parallel_tool_calls": True,
        "tool_choice": "auto",
        "tools": [],
    }


def mock_response(output, *, status="completed", response_id="resp_fixture"):
    return httpx2.Response(
        200,
        json=response_payload(output, status=status, response_id=response_id),
    )


def mock_client(handler):
    http_client = httpx2.Client(transport=httpx2.MockTransport(handler))
    service = OpenAIExtractionService(
        api_key="test-provider-key",
        model_id="gpt-6-luna",
        http_client=http_client,
    )
    return service, http_client


def install_openai_mock(app, handler):
    service, http_client = mock_client(handler)
    app.state.extractor = service
    return service, http_client
