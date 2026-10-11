"""Stateful, no-network LinkedIn REST fake used by WP1K tests."""

import json
import urllib.parse

import httpx2

from app.linkedin import REQUIRED_SCOPES, LinkedInCredentialGrant

MEMBER_URN = "urn:li:person:synthetic-member"
ORGANIZATION_URN = "urn:li:organization:81234567"


class FakeLinkedInCredentials:
    configured = True

    def __init__(self, *, member_urn=MEMBER_URN, scopes=None):
        self.member_urn = member_urn
        self.scopes = frozenset(scopes if scopes is not None else REQUIRED_SCOPES)

    def access_grant(self):
        return LinkedInCredentialGrant(
            access_token="synthetic-linkedin-access-token",
            member_urn=self.member_urn,
            scopes=self.scopes,
        )


class FakeLinkedIn:
    def __init__(self):
        self.posts = {}
        self.images = {}
        self.calls = []
        self.uploads = []
        self._post_index = 0
        self._image_index = 0
        self.timeout_after_create = False
        self.timeout_after_update = False
        self.status_override = None
        self.role = "ADMINISTRATOR"
        self.authorized_org = ORGANIZATION_URN

    def client(self):
        return httpx2.Client(transport=httpx2.MockTransport(self.handle))

    def _record(self, request, body=None):
        self.calls.append(
            {
                "method": request.method,
                "url": str(request.url),
                "path": request.url.path,
                "headers": {
                    key.lower(): request.headers.get(key)
                    for key in (
                        "Authorization",
                        "Linkedin-Version",
                        "X-Restli-Protocol-Version",
                        "X-RestLi-Method",
                    )
                    if request.headers.get(key) is not None
                },
                "body": body,
            }
        )

    def _post_payload(self, row):
        return {
            "id": row["id"],
            "author": row["author"],
            "commentary": row["commentary"],
            "visibility": row["visibility"],
            "distribution": row["distribution"],
            "lifecycleState": row["lifecycleState"],
            **({"content": row["content"]} if row.get("content") else {}),
        }

    def handle(self, request):
        path = request.url.path
        body = None
        if request.content:
            try:
                body = json.loads(request.content)
            except (json.JSONDecodeError, UnicodeDecodeError):
                body = request.content
        self._record(request, body)

        if path.startswith("/dms-uploads/") and request.method == "PUT":
            image_id = next(
                (
                    identifier
                    for identifier, value in self.images.items()
                    if urllib.parse.urlsplit(value["upload_url"]).path == path
                ),
                None,
            )
            image = self.images.get(image_id) if image_id else None
            if image is None:
                return httpx2.Response(404, json={"message": "missing image"})
            image["status"] = "AVAILABLE"
            self.uploads.append(
                {
                    "image_id": image_id,
                    "bytes": request.content,
                    "authorization": request.headers.get("Authorization"),
                }
            )
            return httpx2.Response(201)

        if path == "/rest/organizationAcls" and request.method == "GET":
            assignee = request.url.params.get("roleAssignee")
            return httpx2.Response(
                200,
                json={
                    "elements": [
                        {
                            "state": "APPROVED",
                            "role": self.role,
                            "roleAssignee": MEMBER_URN,
                            "organizationTarget": self.authorized_org,
                        }
                    ]
                    if assignee == MEMBER_URN
                    else []
                },
            )

        if path == "/rest/images" and request.method == "POST":
            if self.status_override:
                return httpx2.Response(self.status_override, json={"message": "synthetic error"})
            self._image_index += 1
            image_id = f"urn:li:image:synthetic-{self._image_index}"
            upload_url = f"https://www.linkedin.com/dms-uploads/synthetic/{self._image_index}"
            owner = body["initializeUploadRequest"]["owner"]
            self.images[image_id] = {
                "id": image_id,
                "owner": owner,
                "status": "WAITING_UPLOAD",
                "upload_url": upload_url,
            }
            return httpx2.Response(
                200, json={"value": {"image": image_id, "uploadUrl": upload_url}}
            )

        if path.startswith("/rest/images/") and request.method == "GET":
            image_id = urllib.parse.unquote(path.rsplit("/", 1)[-1])
            image = self.images.get(image_id)
            if image is None:
                return httpx2.Response(404, json={"message": "missing image"})
            return httpx2.Response(200, json={key: image[key] for key in ("id", "owner", "status")})

        if path == "/rest/posts" and request.method == "POST":
            if self.status_override:
                return httpx2.Response(self.status_override, json={"message": "synthetic error"})
            self._post_index += 1
            post_id = f"urn:li:share:synthetic-{self._post_index}"
            self.posts[post_id] = {"id": post_id, **body}
            if self.timeout_after_create:
                self.timeout_after_create = False
                raise httpx2.ReadTimeout("synthetic timeout after acceptance", request=request)
            return httpx2.Response(201, headers={"x-restli-id": post_id})

        if path.startswith("/rest/posts/"):
            post_id = urllib.parse.unquote(path.rsplit("/", 1)[-1])
            row = self.posts.get(post_id)
            if row is None:
                return httpx2.Response(404, json={"message": "missing post"})
            if request.method == "GET":
                return httpx2.Response(200, json=self._post_payload(row))
            if request.method == "POST":
                row["commentary"] = body["patch"]["$set"]["commentary"]
                if self.timeout_after_update:
                    self.timeout_after_update = False
                    raise httpx2.ReadTimeout("synthetic timeout after update", request=request)
                return httpx2.Response(204)

        return httpx2.Response(404, json={"message": "unhandled synthetic request"})

    def reset(self):
        self.posts.clear()
        self.images.clear()
        self.calls.clear()
        self.uploads.clear()
        # Keep ID counters monotonic: E2E resets clear provider state but retain
        # persisted local bindings, so reusing a synthetic URN would collide.
        self.timeout_after_create = False
        self.timeout_after_update = False
        self.status_override = None
        self.role = "ADMINISTRATOR"
        self.authorized_org = ORGANIZATION_URN
