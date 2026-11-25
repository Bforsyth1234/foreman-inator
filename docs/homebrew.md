# Homebrew tap

For the maintainer. This is how `brew install Bforsyth1234/foreman-inator/foreman-inator`
would be set up (#38, GitHub issue #20). The repo holds the pieces; creating the tap and
publishing the formula are your steps.

| File | What |
| --- | --- |
| `packaging/homebrew/foreman-inator.rb` | The formula **template**. `@@URL@@` and `@@SHA256@@` are placeholders; no resource blocks yet. |
| `packaging/homebrew/render_formula.py` | Fills url and sha256 from a release sdist (on PyPI, or a local file). Standard library only. |
| `tests/test_homebrew_formula.py` | `ruby -c` on the template and a rendered copy; checks it against `pyproject.toml`; the renderer's checks. |

## It cannot work yet

**fde-kit is not on PyPI** (checked 2026-11-23, see [releasing.md](releasing.md#the-fde-kit-problem)).
A Homebrew Python formula installs every dependency from a pinned `resource` block, and
`brew update-python-resources` builds those blocks by resolving the package's
dependencies on PyPI. The built package says `Requires-Dist: fde-kit`, so resolution stops
there. Hand-writing a resource for fde-kit from a GitHub tarball would work around it, but
then the tap would depend on an unpublished name, which is the same dependency-confusion
hole releasing.md describes.

So the order is:

1. Option A in releasing.md: publish fde-kit to PyPI, pin it in `pyproject.toml`
   (`fde-kit>=0.1,<0.2`), relock.
2. Flip `PUBLISH_ENABLED` and release foreman-inator to PyPI. The release workflow's
   publish gate (`uv pip install --dry-run --no-sources` against PyPI alone) is the live
   check from #31: once it passes, a formula can resolve too.
3. Then the tap, below.

## Your steps

Use the version you just released for `0.2.0`.

1. **Create the tap repo** on GitHub: `Bforsyth1234/homebrew-foreman-inator`, public. The
   `homebrew-` prefix is what lets `brew tap Bforsyth1234/foreman-inator` find it. Either
   create it empty and clone it, or let Homebrew start it (adds a README and a CI workflow):

   ```sh
   brew tap-new Bforsyth1234/foreman-inator
   cd "$(brew --repository Bforsyth1234/foreman-inator)"
   ```

2. **Fill version and sha256** from this repo's checkout, writing into the tap:

   ```sh
   python3 packaging/homebrew/render_formula.py --pypi 0.2.0 \
     -o "$(brew --repository Bforsyth1234/foreman-inator)/Formula/foreman-inator.rb"
   ```

   `--pypi` reads `https://pypi.org/pypi/foreman-inator/0.2.0/json` and takes the sdist's
   url and sha256. For the sdist attached to the GitHub release instead:
   `--sdist dist/foreman_inator-0.2.0.tar.gz` (hashes the file; `--url` overrides the
   default GitHub release-asset URL). The PyPI sdist is preferred: it is what
   `brew update-python-resources` and `brew livecheck` expect.

3. **Generate the resource blocks** (every Python dependency, pinned, with checksums;
   certifi, cryptography and pydantic are excluded because the formula uses Homebrew's
   own formulae for them):

   ```sh
   brew update-python-resources Bforsyth1234/foreman-inator/foreman-inator
   ```

   Read the diff: every resource URL should be on `files.pythonhosted.org`, and `fde-kit`
   should be among them.

4. **Build, test, audit:**

   ```sh
   HOMEBREW_NO_INSTALL_FROM_API=1 brew install --build-from-source --verbose \
     Bforsyth1234/foreman-inator/foreman-inator
   brew test Bforsyth1234/foreman-inator/foreman-inator
   brew audit --new --formula Bforsyth1234/foreman-inator/foreman-inator
   brew style Bforsyth1234/foreman-inator/foreman-inator
   fman --version
   ```

   Homebrew asks you to trust a non-official tap before loading formulae from it
   (`brew trust Bforsyth1234/foreman-inator`). That is your call; nothing here does it.

5. **Commit and push** `Formula/foreman-inator.rb` to the tap repo. Users then run:

   ```sh
   brew install Bforsyth1234/foreman-inator/foreman-inator
   fman setup
   ```

6. **Each later release:** repeat steps 2 to 4 with the new version (or
   `brew bump-formula-pr` once the formula exists), commit, push.

Then add the Homebrew line to the README's install section and close #20.

## What the formula does

- `include Language::Python::Virtualenv` and `virtualenv_install_with_resources`: a
  virtualenv under `libexec`, with `fman` and `foreman-inator` linked into `bin`.
- `depends_on "python@3.15"` (homebrew-core's default python on 2026-11-25; the package
  needs 3.12 or later). When homebrew-core moves on, change it to match.
- `depends_on "rust" => :build` for jiter (anthropic, via fde-kit), which builds from
  source. `certifi`, `cryptography` (for `pyjwt[crypto]`) and `pydantic` (fde-kit) come
  from homebrew-core formulae, as homebrew-core's own `llm` and `twine` formulae do, and
  `pypi_packages exclude_packages:` keeps them out of the resources.
- `depends_on :macos`: the worker uses `sandbox-exec`, launchd and the Keychain.
- `test do`: both entry points print the formula's version, and `fman status` (offline,
  read-only) reports "No tickets yet" in the empty `HOME` that `brew test` gives it. If
  Homebrew's test sandbox blocks its Ollama probe on localhost and that ever turns into an
  error, drop that line rather than add network to the test.

Checked locally on 2026-11-25 (Homebrew 7.0.16): a rendered copy passes `ruby -c` and
`brew style` in a scratch local tap (removed afterwards). `brew audit` and the install
were not run: the first needs the tap trusted, the second needs fde-kit on PyPI.

## Docs read (rule 12, 2026-11-25)

- Python for Formula Authors: https://docs.brew.sh/Python-for-Formula-Authors (now
  redirects to https://docs.brew.sh/Language-Specific-Formulae, "Python")
- Formula Cookbook: https://docs.brew.sh/Formula-Cookbook (`brew audit --new`,
  `--build-from-source`, `brew test`, test-block guidance)
- How to Create and Maintain a Tap: https://docs.brew.sh/How-to-Create-and-Maintain-a-Tap
  (`homebrew-` prefix, `Formula/` directory, untrusted taps)
- homebrew-core `Formula/l/llm.rb` and `Formula/t/twine.rb` (dependency formulae,
  `pypi_packages exclude_packages:`)
- PyPI JSON API: https://docs.pypi.org/api/json/ (`GET /pypi/<project>/<version>/json`)
