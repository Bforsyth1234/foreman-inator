"""A tiny local Python package index for install tests (todo #45).

Nothing here touches pypi.org or the user's caches. ``build_index(root)`` writes a PEP 503
"simple" index under ``root``:

* ``fmantoydep`` 1.0.0, a one-module pure-Python wheel built here from source text;
* pytest and the packages it needs on this Python, repacked from the distributions already
  installed in the test environment (their installed files, with a fresh ``RECORD``), so a
  ``uv run pytest`` project can be locked and installed from the index alone.

``PackageIndex(root)`` serves it over HTTP on a loopback port (a thread); ``.url`` is the
``/simple`` URL to give uv. Loopback HTTP, not ``file://``, so the install step's network
policy is what lets the install reach it, and verify's ``none`` is what stops verify.
"""

from __future__ import annotations

import base64
import hashlib
import html
import http.server
import importlib.metadata as md
import io
import re
import threading
import zipfile
from pathlib import Path

from packaging.markers import default_environment
from packaging.requirements import Requirement

TOY = "fmantoydep"
TOY_VERSION = "1.0.0"
TOY_SOURCE = 'GREETING = "hello from fmantoydep"\n\n\ndef greet(name):\n    return f"hi {name}"\n'


def _norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _record_line(arcname: str, data: bytes) -> str:
    digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()
    return f"{arcname},sha256={digest},{len(data)}"


def _write_wheel(path: Path, files: dict[str, bytes], dist_info: str) -> Path:
    record = [_record_line(a, d) for a, d in files.items()]
    record.append(f"{dist_info}/RECORD,,")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for arcname, data in files.items():
            zf.writestr(arcname, data)
        zf.writestr(f"{dist_info}/RECORD", "\n".join(record) + "\n")
    path.write_bytes(buf.getvalue())
    return path


def toy_wheel(out_dir: Path) -> Path:
    di = f"{TOY}-{TOY_VERSION}.dist-info"
    files = {
        f"{TOY}/__init__.py": TOY_SOURCE.encode(),
        f"{di}/METADATA": (
            f"Metadata-Version: 2.1\nName: {TOY}\nVersion: {TOY_VERSION}\n"
            "Summary: foreman-inator test package\n"
        ).encode(),
        f"{di}/WHEEL": b"Wheel-Version: 1.0\nGenerator: fman-tests\nRoot-Is-Purelib: true\n"
        b"Tag: py3-none-any\n",
    }
    return _write_wheel(out_dir / f"{TOY}-{TOY_VERSION}-py3-none-any.whl", files, di)


_SKIP_DIST_INFO = {"RECORD", "INSTALLER", "REQUESTED", "direct_url.json", "uv_cache.json"}


def repack(dist: md.Distribution, out_dir: Path) -> Path:
    """A ``py3-none-any`` wheel of an installed pure-Python distribution."""
    name = dist.metadata["Name"]
    version = dist.version
    files: dict[str, bytes] = {}
    for f in dist.files or ():
        rel = str(f)
        if rel.startswith("..") or "__pycache__" in rel or rel.endswith(".pyc"):
            continue
        parts = rel.split("/")
        if parts[0].endswith(".dist-info") and parts[-1] in _SKIP_DIST_INFO:
            continue
        p = Path(str(f.locate()))
        if p.is_file():
            files[rel] = p.read_bytes()
    di = next(p.split("/")[0] for p in files if p.split("/")[0].endswith(".dist-info"))
    wheel = f"{di}/WHEEL"
    files[wheel] = (
        b"Wheel-Version: 1.0\nGenerator: fman-tests\nRoot-Is-Purelib: true\nTag: py3-none-any\n"
    )
    stem = re.sub(r"[-.]+", "_", name)
    return _write_wheel(out_dir / f"{stem}-{version}-py3-none-any.whl", files, di)


def closure(root: str) -> list[md.Distribution]:
    """``root`` and what it requires on this interpreter, from the installed dists."""
    env = default_environment()
    seen: dict[str, md.Distribution] = {}
    todo = [root]
    while todo:
        name = _norm(todo.pop())
        if name in seen:
            continue
        dist = md.distribution(name)
        seen[name] = dist
        for raw in dist.requires or ():
            req = Requirement(raw)
            if req.marker is not None and not req.marker.evaluate({**env, "extra": ""}):
                continue
            todo.append(req.name)
    return list(seen.values())


def build_index(root: Path) -> Path:
    """Write the simple index under ``root``; returns ``root``."""
    files = root / "files"
    files.mkdir(parents=True, exist_ok=True)
    wheels = [toy_wheel(files)] + [repack(d, files) for d in closure("pytest")]
    simple = root / "simple"
    by_project: dict[str, list[Path]] = {}
    for w in wheels:
        by_project.setdefault(_norm(w.name.split("-")[0]), []).append(w)
    for project, ws in by_project.items():
        d = simple / project
        d.mkdir(parents=True, exist_ok=True)
        links = "".join(
            f'<a href="../../files/{html.escape(w.name)}#sha256='
            f'{hashlib.sha256(w.read_bytes()).hexdigest()}">{html.escape(w.name)}</a>\n'
            for w in ws
        )
        (d / "index.html").write_text(f"<!DOCTYPE html><html><body>\n{links}</body></html>\n")
    index = "".join(f'<a href="{p}/">{p}</a>\n' for p in sorted(by_project))
    (simple / "index.html").write_text(f"<!DOCTYPE html><html><body>\n{index}</body></html>\n")
    return root


class _Handler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args: object) -> None:  # keep test output clean
        pass


class PackageIndex:
    """Serve ``root`` (from ``build_index``) on ``127.0.0.1:<free port>``; ``hits`` counts
    requests, so a test can tell an install that used the network from one served by the
    cache."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.hits = 0
        outer = self

        class Handler(_Handler):
            def __init__(self, *a: object, **kw: object) -> None:
                super().__init__(*a, directory=str(outer.root), **kw)  # type: ignore[misc]

            def do_GET(self) -> None:  # noqa: N802
                outer.hits += 1
                super().do_GET()

        class Server(http.server.ThreadingHTTPServer):
            daemon_threads = True

            def handle_error(self, request: object, client_address: object) -> None:
                pass  # a client that hangs up mid-download (uv retries) is not a failure

        self.server = Server(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}/simple"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self) -> PackageIndex:
        self.thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.server.shutdown()
        self.server.server_close()
