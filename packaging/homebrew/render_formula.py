"""Fill the Homebrew formula template's url and sha256 from a release sdist (#38).

Usage (from the repo root; standard library only, so plain ``python3`` works)::

    # once foreman-inator is on PyPI (preferred: Homebrew resolves from PyPI anyway)
    python3 packaging/homebrew/render_formula.py --pypi 0.2.0 -o Formula/foreman-inator.rb

    # from a local sdist, e.g. the asset the release workflow attaches to the GitHub
    # release; url defaults to that GitHub release asset
    python3 packaging/homebrew/render_formula.py --sdist dist/foreman_inator-0.2.0.tar.gz

The template is ``packaging/homebrew/foreman-inator.rb``; ``@@URL@@`` and
``@@SHA256@@`` are its only placeholders. The resource blocks are not filled here:
``brew update-python-resources`` generates them (docs/homebrew.md).

PyPI JSON API read 2026-11-25: https://docs.pypi.org/api/json/ (``GET
/pypi/<project>/<version>/json``; ``urls[]`` lists the release's files with
``packagetype`` "sdist"/"bdist_wheel", ``url`` and ``digests.sha256``).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import urllib.request
from collections.abc import Callable
from pathlib import Path

TEMPLATE = Path(__file__).with_name("foreman-inator.rb")
PROJECT = "foreman-inator"
# uv_build (like every PEP 625 backend) names the sdist with the normalized name.
SDIST_RE = re.compile(r"foreman_inator-(?P<version>[0-9][0-9A-Za-z.+!-]*)\.tar\.gz")
SHA256_RE = re.compile(r"[0-9a-f]{64}")
# The whole url, so nothing that means something inside a Ruby string (``"``, ``\\``,
# ``#{``) can reach the formula.
URL_RE = re.compile(r"^https://[A-Za-z0-9._~/%-]+$")
PLACEHOLDERS = ("@@URL@@", "@@SHA256@@")
GITHUB_ASSET = "https://github.com/Bforsyth1234/foreman-inator/releases/download/v{version}/{name}"
PYPI_JSON = "https://pypi.org/pypi/{project}/{version}/json"


class RenderError(ValueError):
    """The inputs would make a broken formula."""


def sdist_version(url_or_name: str) -> str:
    """The version in an sdist file name or URL (``foreman_inator-0.2.0.tar.gz``)."""
    name = url_or_name.rsplit("/", 1)[-1]
    match = SDIST_RE.fullmatch(name)
    if match is None:
        raise RenderError(f"not a foreman-inator sdist: {name!r}")
    return match["version"]


def render(template: str, *, url: str, sha256: str, version: str | None = None) -> str:
    """Return ``template`` with url and sha256 filled in, after checking both."""
    if not url.startswith("https://"):
        raise RenderError(f"url must be https: {url!r}")
    if URL_RE.fullmatch(url) is None:
        raise RenderError(f"url has characters a formula url must not have: {url!r}")
    found = sdist_version(url)
    if version is not None and found != version:
        raise RenderError(f"url is version {found}, expected {version}")
    if SHA256_RE.fullmatch(sha256) is None:
        raise RenderError(f"sha256 must be 64 lowercase hex characters: {sha256!r}")
    for placeholder in PLACEHOLDERS:
        if template.count(placeholder) != 1:
            raise RenderError(f"template must contain {placeholder} exactly once")
    out = template.replace("@@URL@@", url).replace("@@SHA256@@", sha256)
    if "@@" in out:
        raise RenderError("template has an unknown @@placeholder@@")
    return out


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _fetch_json(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=30) as resp:  # noqa: S310 (fixed https URL)
        return json.load(resp)


def pypi_sdist(version: str, fetch: Callable[[str], dict] = _fetch_json) -> tuple[str, str]:
    """(url, sha256) of the release's sdist on PyPI."""
    data = fetch(PYPI_JSON.format(project=PROJECT, version=version))
    sdists = [f for f in data.get("urls", []) if f.get("packagetype") == "sdist"]
    if len(sdists) != 1:
        raise RenderError(f"PyPI {PROJECT} {version}: expected one sdist, found {len(sdists)}")
    return sdists[0]["url"], sdists[0]["digests"]["sha256"]


def main(argv: list[str] | None = None, fetch: Callable[[str], dict] = _fetch_json) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    src = parser.add_mutually_exclusive_group(required=True)
    src.add_argument("--pypi", metavar="VERSION", help="use the sdist published on PyPI")
    src.add_argument("--sdist", type=Path, help="hash this local sdist")
    parser.add_argument("--url", help="with --sdist: where it is hosted (default: GitHub release)")
    parser.add_argument("--template", type=Path, default=TEMPLATE)
    parser.add_argument("-o", "--output", type=Path, help="write here instead of stdout")
    args = parser.parse_args(argv)

    try:
        if args.pypi:
            url, sha256 = pypi_sdist(args.pypi, fetch)
            version = args.pypi
        else:
            version = sdist_version(args.sdist.name)
            url = args.url or GITHUB_ASSET.format(version=version, name=args.sdist.name)
            sha256 = file_sha256(args.sdist)
        text = render(args.template.read_text(), url=url, sha256=sha256, version=version)
    except (RenderError, OSError) as exc:
        print(f"render_formula: {exc}", file=sys.stderr)
        return 1
    if args.output:
        args.output.write_text(text)
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
