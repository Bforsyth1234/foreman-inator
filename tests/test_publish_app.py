"""todo #39: a public option on the manifest; private stays the default; the key is never
part of what gets shared."""

from __future__ import annotations

import html
import json
import re
from pathlib import Path

import httpx
import pytest

from foreman_inator.setup import manifest

CB = "http://127.0.0.1:1/callback"


def _posted_manifest(page: str) -> dict:
    value = re.search(r"name='manifest' value='([^']*)'", page).group(1)
    return json.loads(html.unescape(value))


def test_default_manifest_is_private():
    assert manifest.build_manifest(CB, "x")["public"] is False


def test_public_true_sets_only_the_public_field():
    private = manifest.build_manifest(CB, "x")
    public = manifest.build_manifest(CB, "x", public=True)
    assert public["public"] is True
    assert {k: v for k, v in public.items() if k != "public"} == {
        k: v for k, v in private.items() if k != "public"
    }
    assert public["default_permissions"] == manifest.PERMISSIONS


def test_public_with_relay_keeps_both():
    built = manifest.build_manifest(CB, "x", relay_url="https://smee.io/c", public=True)
    assert built["public"] is True
    assert built["hook_attributes"]["url"] == "https://smee.io/c"


@pytest.mark.parametrize("value", ["true", 1, "yes", None])
def test_only_an_explicit_bool_is_accepted(value):
    with pytest.raises(TypeError):
        manifest.build_manifest(CB, "x", public=value)


def test_callback_server_posts_public_manifest():
    with manifest.CallbackServer(app_name="x", public=True) as server:
        assert server.manifest["public"] is True
        page = httpx.get(server.start_url).text
    assert _posted_manifest(page)["public"] is True
    assert "your public foreman-inator App" in page


def test_callback_server_default_posts_private_manifest():
    with manifest.CallbackServer(app_name="x") as server:
        page = httpx.get(server.start_url).text
    assert _posted_manifest(page)["public"] is False
    assert "your private foreman-inator App" in page


def test_public_app_text_has_install_link_and_no_key():
    text = manifest.public_app_text("foreman-inator-abc123")
    assert "https://github.com/apps/foreman-inator-abc123/installations/new" in text
    assert "Never send the key" in text
    assert "BEGIN" not in text  # no PEM material


def test_publishing_doc_records_rule_12_sources():
    doc = (Path(__file__).parents[1] / "docs" / "publishing.md").read_text()
    assert "registering-a-github-app-from-a-manifest" in doc
    assert "read 2026-11-25" in doc
    assert "fman setup --public" in doc
