import httpx2
import pytest

from app.eventbrite import (
    EventbriteClient,
    EventbriteError,
    description_html,
    parse_address,
    render,
)

ACCOUNT = {"organization_id": "1", "organizer_id": "2", "currency": "USD"}


class Token:
    def authorization_header(self):
        return "Bearer secret-token"


def _entry(**listing):
    base = {
        "name": "Lunch & Learn",
        "starts_at": "2026-10-22T11:30:00-06:00",
        "ends_at": "2026-10-22T13:00:00-06:00",
        "timezone": "America/Denver",
        "delivery": "VIRTUAL",
        "price_kind": "FREE",
        "capacity": 40,
    }
    base.update(listing)
    return {
        "destination_key": "EVENTBRITE",
        "approved_content": "Join us <b>today</b>.\nBring a friend.\n\nSee you there & then.",
        "eventbrite": {"account": dict(ACCOUNT), "listing": base},
    }


def test_description_is_exact_escaped_text_without_rewriting():
    text = "Join us <b>today</b>.\nBring a friend.\n\nSee you there & then."
    assert description_html(text) == (
        "<p>Join us &lt;b&gt;today&lt;/b&gt;.<br>Bring a friend.</p>"
        "<p>See you there &amp; then.</p>"
    )


def test_address_parsing_is_strict_and_never_guesses():
    assert parse_address("123 Main St, Aurora, CO 80012") == {
        "address_1": "123 Main St",
        "city": "Aurora",
        "region": "CO",
        "postal_code": "80012",
        "country": "US",
    }
    assert parse_address("Aurora Event Center") is None
    assert parse_address("12 Rue X, Paris, ZZ 75000") is None


def test_render_maps_schedule_to_utc_and_blocks_missing_facts():
    listing = render(_entry(), ACCOUNT)
    assert listing.start == {"timezone": "America/Denver", "utc": "2026-10-22T17:30:00Z"}
    assert listing.online is True and listing.venue is None
    assert listing.ticket == {"name": "General Admission", "free": True, "quantity_total": 40}
    for change, code in (
        ({"ends_at": None}, "EVENTBRITE_SCHEDULE_REQUIRED"),
        ({"capacity": None}, "EVENTBRITE_CAPACITY_REQUIRED"),
        ({"price_kind": "PAID"}, "EVENTBRITE_PAID_TICKETS_UNSUPPORTED"),
        ({"price_kind": None}, "EVENTBRITE_PRICE_UNKNOWN"),
        ({"delivery": "IN_PERSON", "venue_name": "Hall", "address": "Hall"},
         "EVENTBRITE_ADDRESS_REQUIRED"),
    ):  # fmt: skip
        with pytest.raises(Exception) as raised:
            render(_entry(**change), ACCOUNT)
        assert raised.value.code == code
    with pytest.raises(Exception) as raised:
        render(_entry(), {**ACCOUNT, "organization_id": "999"})
    assert raised.value.code == "EVENTBRITE_ACCOUNT_CHANGED"


@pytest.mark.parametrize(
    ("status", "category"),
    [(401, "AUTHENTICATION"), (403, "PERMISSION"), (429, "RATE_LIMIT"), (400, "VALIDATION")],
)
def test_http_errors_are_normalized_without_payloads(status, category):
    def handler(request):
        assert request.headers["authorization"] == "Bearer secret-token"
        return httpx2.Response(status, json={"error": "SOME_CODE", "error_description": "x"})

    client = EventbriteClient(Token(), httpx2.Client(transport=httpx2.MockTransport(handler)))
    with pytest.raises(EventbriteError) as raised:
        client.request("POST", "/organizations/1/events/", json_body={})
    assert raised.value.category == category
    assert raised.value.provider_code == "SOME_CODE"
    assert raised.value.may_have_applied is False
    assert "secret-token" not in str(raised.value)


def test_timeouts_distinguish_unsent_from_possibly_applied():
    def read_timeout(request):
        raise httpx2.ReadTimeout("late", request=request)

    def connect_error(request):
        raise httpx2.ConnectError("down", request=request)

    def server_error(request):
        return httpx2.Response(502)

    for handler, applied in ((read_timeout, True), (connect_error, False), (server_error, True)):
        client = EventbriteClient(Token(), httpx2.Client(transport=httpx2.MockTransport(handler)))
        with pytest.raises(EventbriteError) as raised:
            client.request("POST", "/organizations/1/events/", json_body={})
        assert raised.value.may_have_applied is applied
    client = EventbriteClient(Token(), httpx2.Client(transport=httpx2.MockTransport(read_timeout)))
    with pytest.raises(EventbriteError) as raised:
        client.request("GET", "/users/me/")
    assert raised.value.may_have_applied is False  # reads never create remote effects
