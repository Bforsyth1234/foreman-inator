"""GitHub's attachment redirect to its S3 asset bucket (todo #84, GitHub #83).

``github.com/user-attachments/assets/<id>`` 302-redirects to a pre-signed URL on
``github-production-user-asset-6210df.s3.amazonaws.com``. That host is allowed only as
the redirect target of a user-attachments URL, never as a starting URL, and the App token
never reaches it. Every HTTP exchange goes through ``httpx.MockTransport``; no network.
"""

from __future__ import annotations

import httpx
import pytest

from foreman_inator.tickets import images as I

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
GH_URL = "https://github.com/user-attachments/assets/8a0f8d4e-aaaa-bbbb-cccc-1234567890ab"
BUCKET = "https://github-production-user-asset-6210df.s3.amazonaws.com"
S3_URL = (
    f"{BUCKET}/4528782/719186614-8a0f8d4e.png?X-Amz-Algorithm=AWS4-HMAC-SHA256&X-Amz-Signature=abc"
)
OTHER_BUCKET = "https://evil-bucket.s3.amazonaws.com/x.png?X-Amz-Signature=abc"


class Server:
    def __init__(self, routes: dict[str, tuple[int, dict[str, str], bytes]]) -> None:
        self.routes = routes
        self.requests: list[httpx.Request] = []
        self.transport = httpx.MockTransport(self.handle)

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        status, headers, body = self.routes.get(str(request.url), (404, {}, b"nope"))
        return httpx.Response(status, headers=headers, content=body)

    def urls(self) -> list[str]:
        return [str(r.url) for r in self.requests]


def ok() -> tuple[int, dict[str, str], bytes]:
    return 200, {"content-type": "image/png"}, PNG


def redirect(to: str) -> tuple[int, dict[str, str], bytes]:
    return 302, {"location": to}, b""


def test_attachment_redirect_to_github_asset_bucket_is_followed_without_token(tmp_path):
    srv = Server({GH_URL: redirect(S3_URL), S3_URL: ok()})
    data, mime = I.fetch(GH_URL, token="tok-123", transport=srv.transport)
    assert (data, mime) == (PNG, "image/png")
    assert srv.urls() == [GH_URL, S3_URL]
    assert srv.requests[0].headers["authorization"] == "Bearer tok-123"
    assert "authorization" not in srv.requests[1].headers
    imgs, refused = I.collect(
        [I.Source("issue body", f"![shot]({GH_URL})")], tmp_path, transport=srv.transport
    )
    assert refused == [] and [i.url for i in imgs] == [GH_URL]


def test_bucket_url_in_issue_text_is_refused():
    srv = Server({S3_URL: ok()})
    assert not I.allowed_url(S3_URL)
    assert I.image_urls(f"![x]({S3_URL}) and {S3_URL}") == []
    with pytest.raises(I.ImageRefused, match="not a GitHub attachment URL"):
        I.fetch(S3_URL, transport=srv.transport)
    assert srv.requests == []


@pytest.mark.parametrize(
    "target",
    [
        OTHER_BUCKET,
        "http://github-production-user-asset-6210df.s3.amazonaws.com/x.png",
        "https://github-production-user-asset-6210df.s3.amazonaws.com:8443/x.png",
        "https://u:p@github-production-user-asset-6210df.s3.amazonaws.com/x.png",
        "https://github-production-user-asset-6210df.s3.amazonaws.com.evil.com/x.png",
        "https://s3.amazonaws.com/github-production-user-asset-6210df/x.png",
    ],
)
def test_redirect_to_another_bucket_or_malformed_bucket_url_is_refused(target):
    srv = Server({GH_URL: redirect(target), target: ok()})
    with pytest.raises(I.ImageRefused, match="redirect"):
        I.fetch(GH_URL, transport=srv.transport)
    assert srv.urls() == [GH_URL]


def test_bucket_only_reachable_from_a_user_attachments_hop():
    up = "https://user-images.githubusercontent.com/1/abc.png"
    private = "https://private-user-images.githubusercontent.com/1/x.png?jwt=abc"
    gh_other = "https://github.com/some/other/path"
    for start in (up, private):
        srv = Server({start: redirect(S3_URL), S3_URL: ok()})
        with pytest.raises(I.ImageRefused, match="redirect"):
            I.fetch(start, transport=srv.transport)
        assert S3_URL not in srv.urls()
    # user-attachments -> other GitHub URL -> bucket: the github.com hop is off-list anyway,
    # and the bucket is never allowed after a non-attachments hop.
    assert not I._asset_redirect_ok(gh_other, S3_URL)
    assert not I._asset_redirect_ok(S3_URL, S3_URL)


def test_redirect_cap_still_applies_with_bucket_allowed():
    hops = [f"{GH_URL}{i}" for i in range(I.MAX_REDIRECTS + 1)]
    routes = {h: redirect(n) for h, n in zip(hops, hops[1:], strict=False)}
    routes[hops[-1]] = redirect(S3_URL)
    routes[S3_URL] = ok()
    srv = Server(routes)
    with pytest.raises(I.ImageRefused, match="more than"):
        I.fetch(hops[0], transport=srv.transport)
    assert S3_URL not in srv.urls()


def test_bucket_response_size_and_type_caps_unchanged():
    srv = Server({GH_URL: redirect(S3_URL), S3_URL: (200, {}, PNG + b"\x00" * 200)})
    with pytest.raises(I.ImageRefused, match="larger"):
        I.fetch(GH_URL, transport=srv.transport, max_bytes=100)
    srv = Server({GH_URL: redirect(S3_URL), S3_URL: (200, {}, b"<html>not an image")})
    with pytest.raises(I.ImageRefused, match="magic"):
        I.fetch(GH_URL, transport=srv.transport)
