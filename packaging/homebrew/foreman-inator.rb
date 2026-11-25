# Homebrew formula TEMPLATE for foreman-inator (#38 / GitHub issue #20).
#
# This file lives in the foreman-inator repo as a template. The published copy goes in the
# tap repo `Bforsyth1234/homebrew-foreman-inator` at `Formula/foreman-inator.rb`.
# See docs/homebrew.md for the full steps. In short:
#
#   1. Fill url + sha256:
#        python3 packaging/homebrew/render_formula.py --pypi 0.2.0 -o Formula/foreman-inator.rb
#      (or --sdist dist/foreman_inator-0.2.0.tar.gz for the GitHub release asset).
#   2. Generate the resource blocks (every Python dependency, pinned, with checksums):
#        brew update-python-resources Formula/foreman-inator.rb
#   3. brew install --build-from-source, brew test, brew audit --new.
#
# IT CANNOT WORK YET: the dependency fde-kit is not on PyPI (docs/releasing.md, "The
# fde-kit problem"), so step 2 cannot resolve it. Publish fde-kit (and foreman-inator)
# to PyPI first.
#
# Pattern: Homebrew's Python application formula (Language::Python::Virtualenv +
# virtualenv_install_with_resources). Docs read 2026-11-25:
#   https://docs.brew.sh/Python-for-Formula-Authors (redirects to
#   https://docs.brew.sh/Language-Specific-Formulae, "Python")
#   https://docs.brew.sh/Formula-Cookbook
#   https://docs.brew.sh/How-to-Create-and-Maintain-a-Tap
# Dependency formulae and `pypi_packages exclude_packages:` follow homebrew-core's own
# llm.rb and twine.rb (same date): certifi, cryptography and pydantic come from their
# homebrew-core formulae instead of resource blocks. python@3.15 is homebrew-core's
# default python on that date (requires-python is >=3.12).
class ForemanInator < Formula
  include Language::Python::Virtualenv

  desc "GitHub App and local worker that route issues to the cheapest capable model"
  homepage "https://github.com/Bforsyth1234/foreman-inator"
  url "@@URL@@"
  sha256 "@@SHA256@@"
  license "MIT"

  depends_on "rust" => :build # jiter (anthropic, via fde-kit) builds from source
  depends_on "certifi" => :no_linkage
  depends_on "cryptography" => :no_linkage # pyjwt[crypto]
  depends_on :macos # the worker uses sandbox-exec, launchd and the Keychain
  depends_on "pydantic" => :no_linkage # fde-kit, pydantic-settings
  depends_on "python@3.15"

  pypi_packages exclude_packages: %w[certifi cryptography pydantic]

  # RESOURCES: paste the output of `brew update-python-resources` here (it edits the file
  # in place). Do not write these by hand.

  def install
    virtualenv_install_with_resources
  end

  test do
    assert_match "foreman-inator #{version}", shell_output("#{bin}/fman --version")
    assert_match "foreman-inator #{version}", shell_output("#{bin}/foreman-inator --version")
    # Offline and read-only: with the empty HOME `brew test` provides there is no
    # config or database yet, and `status` says so instead of failing.
    assert_match "No tickets yet", shell_output("#{bin}/fman status")
  end
end
