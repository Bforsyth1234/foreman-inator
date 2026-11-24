Fixture (todo #45): a uv project whose tests import a third-party package (`fmantoydep`,
served by `tests/fakes/pkgindex.py`). The test writes `[[tool.uv.index]]` and `uv.lock` at
test time, because the index runs on a random loopback port.
