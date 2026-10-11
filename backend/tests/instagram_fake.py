"""Stateful, no-network Instagram Login Graph API fake for live-adapter tests."""

import hashlib
from io import BytesIO
from urllib.parse import parse_qs

import httpx2
from PIL import Image

from app.instagram import REQUIRED_SCOPES

ACCOUNT_ID = "17841400000000001"
USERNAME = "eventmarketing_test"
ACCESS_TOKEN = "synthetic-instagram-access-token"


class FakeInstagramCredentials:
    configured = True

    def __init__(self, *, account_id=ACCOUNT_ID, scopes=REQUIRED_SCOPES, username=USERNAME):
        self.account_id = account_id
        self.scopes = frozenset(scopes)
        self.username = username

    def access_grant(self):
        from app.instagram import InstagramCredentialGrant

        return InstagramCredentialGrant(
            access_token=ACCESS_TOKEN,
            account_id=self.account_id,
            username=self.username,
            scopes=self.scopes,
        )


class FakeInstagram:
    def __init__(self, media_fetcher=None):
        self.media_fetcher = media_fetcher or self._fetch_media_url
        self.calls = []
        self.containers = {}
        self.media = {}
        self.container_creates = 0
        self.publish_calls = 0
        self._container_id_sequence = 0
        self._media_id_sequence = 0
        self.quota_usage = 0
        self.quota_total = 100
        self.next_container_status = "FINISHED"
        self.error_next = None
        self.graph_error_next: int | None = None
        self.timeout_before_create = False
        self.timeout_after_create = False
        self.timeout_before_publish = False
        self.timeout_after_publish = False
        self.malformed_next = False
        self.profile_override = {}

    @staticmethod
    def _fetch_media_url(url: str) -> bytes:
        response = httpx2.get(url, timeout=10, follow_redirects=False)
        response.raise_for_status()
        return response.content

    def client(self):
        return httpx2.Client(transport=httpx2.MockTransport(self.handle), timeout=10)

    def reset(self):
        self.calls.clear()
        self.containers.clear()
        self.media.clear()
        self.container_creates = 0
        self.publish_calls = 0
        self.quota_usage = 0
        self.quota_total = 100
        self.next_container_status = "FINISHED"
        self.error_next = None
        self.graph_error_next = None
        self.timeout_before_create = False
        self.timeout_after_create = False
        self.timeout_before_publish = False
        self.timeout_after_publish = False
        self.malformed_next = False
        self.profile_override = {}

    def _response(self, request, status=200, *, json_body=None, content=b""):
        return httpx2.Response(
            status,
            json=json_body if json_body is not None else None,
            content=content if json_body is None else None,
            request=request,
        )

    def handle(self, request):
        path = request.url.path
        self.calls.append(
            {
                "method": request.method,
                "path": path,
                "headers": {key.lower(): value for key, value in request.headers.items()},
                "data": parse_qs(request.content.decode("utf-8")) if request.content else {},
            }
        )
        if request.headers.get("Authorization") != f"Bearer {ACCESS_TOKEN}":
            return self._response(request, 401, json_body={"error": {"code": 190}})
        if "/v26.0/" not in path:
            return self._response(request, 400, json_body={"error": {"code": 100}})
        if self.error_next:
            status = self.error_next
            self.error_next = None
            return self._response(request, status, json_body={"error": {"code": status}})
        if self.graph_error_next is not None:
            code = self.graph_error_next
            self.graph_error_next = None
            return self._response(
                request, 200, json_body={"error": {"message": "fake", "code": code}}
            )
        if self.malformed_next:
            self.malformed_next = False
            return self._response(request, 200, content=b"not-json")

        suffix = path.split("/v26.0/", 1)[1]
        method = request.method
        if suffix == "me" and method == "GET":
            return self._response(
                request,
                json_body={
                    "user_id": ACCOUNT_ID,
                    "username": USERNAME,
                    "account_type": "Business",
                    **self.profile_override,
                },
            )
        if suffix == f"{ACCOUNT_ID}/content_publishing_limit" and method == "GET":
            return self._response(
                request,
                json_body={
                    "data": [{"quota_usage": self.quota_usage, "quota_total": self.quota_total}]
                },
            )
        if suffix == f"{ACCOUNT_ID}/media" and method == "POST":
            if self.timeout_before_create:
                self.timeout_before_create = False
                raise httpx2.ConnectTimeout("synthetic timeout before container")
            data = parse_qs(request.content.decode("utf-8"))
            image_url = data.get("image_url", [None])[0]
            caption = data.get("caption", [None])[0]
            if not image_url or not caption:
                return self._response(request, 400, json_body={"error": {"code": 100}})
            image_bytes = self.media_fetcher(image_url)
            with Image.open(BytesIO(image_bytes)) as decoded:
                if decoded.format != "JPEG":
                    return self._response(request, 400, json_body={"error": {"code": 100}})
                decoded.load()
            self.container_creates += 1
            self._container_id_sequence += 1
            container_id = f"fake-container-{self._container_id_sequence:04d}"
            self.containers[container_id] = {
                "id": container_id,
                "status_code": self.next_container_status,
                "caption": caption,
                "image_url": image_url,
                "image_bytes": image_bytes,
                "image_sha256": hashlib.sha256(image_bytes).hexdigest(),
                "request_version": "v26.0",
            }
            if self.timeout_after_create:
                self.timeout_after_create = False
                raise httpx2.ReadTimeout("synthetic timeout after container acceptance")
            return self._response(request, json_body={"id": container_id})

        if suffix.endswith("/media_publish") and method == "POST":
            if self.timeout_before_publish:
                self.timeout_before_publish = False
                raise httpx2.ConnectTimeout("synthetic timeout before publish")
            data = parse_qs(request.content.decode("utf-8"))
            container_id = data.get("creation_id", [None])[0]
            container = self.containers.get(container_id)
            if not container or container["status_code"] != "FINISHED":
                return self._response(request, 400, json_body={"error": {"code": 100}})
            self.publish_calls += 1
            self._media_id_sequence += 1
            media_id = f"fake-media-{self._media_id_sequence:04d}"
            self.media[media_id] = {
                "id": media_id,
                "caption": container["caption"],
                "media_type": "IMAGE",
                "permalink": f"https://www.instagram.com/p/fake{self._media_id_sequence:08d}/",
                "username": USERNAME,
                "timestamp": "2026-10-07T12:00:00+0000",
                "container_id": container_id,
            }
            container["status_code"] = "PUBLISHED"
            self.quota_usage += 1
            if self.timeout_after_publish:
                self.timeout_after_publish = False
                raise httpx2.ReadTimeout("synthetic timeout after publish acceptance")
            return self._response(request, json_body={"id": media_id})

        container_id = suffix.split("?", 1)[0]
        if container_id in self.containers and method == "GET":
            item = self.containers[container_id]
            return self._response(
                request,
                json_body={"id": item["id"], "status_code": item["status_code"]},
            )
        media_id = container_id
        if media_id in self.media and method == "GET":
            return self._response(request, json_body=self.media[media_id])
        return self._response(request, 404, json_body={"error": {"code": 100}})

    @property
    def write_calls(self):
        return [call for call in self.calls if call["method"] not in {"GET", "HEAD"}]
