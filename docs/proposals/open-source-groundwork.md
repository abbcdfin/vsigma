# Proposal: Open-Source Groundwork — What a Contributor Needs Before the First Pull Request

**Status:** **DRAFT** 2026-10-30. Deferred by the owner until it is needed: there are no
external contributors yet. §C names when to build it. Awaits a Decision Check Point (§G).
**Prompted by:** the first public release, `v0.2.2` (2026-10-29). The repository and the
package are public, but the project is still set up for its maintainers' machine.
**Builds on:** the 2026-06-28 PyPI readiness list (plan.md, deferred work). Its metadata,
classifiers, URLs, `CHANGELOG.md` and `LICENSE` shipped with `v0.2.2`; this proposal holds the
rest.

## A. Problem

1. **The lockfile sends every contributor to one mirror.** `pyproject.toml` makes the Tsinghua
   mirror the default index (`[[tool.uv.index]] name = "tuna"`), and `uv.lock` records it in
   1,215 URLs (§H.1). A fresh clone's `uv sync` downloads everything from that mirror. The
   lock's hashes still verify each file, so this is not an integrity problem: it is speed and
   availability far from the mirror, and a surprising default. The mirror is there because
   pypi.org is not reliably reachable from the maintainers' network (§H.8).
2. **Nothing checks a change except the person who made it.** `make check` (ruff, mypy, pytest)
   runs locally only; there is no `.github/`. The classifiers claim Python 3.11–3.13, but the
   suite runs on whichever Python the maintainer's environment has.
3. **The version lives in three places:** `pyproject.toml`, `__version__` in
   `src/vsigma/__init__.py`, and `uv.lock`. Each release edits all three by hand, and
   `test_smoke.py` checks only that `__version__` is a non-empty string (§H.5), so a mismatch
   would ship.
4. **Releases are uploaded by hand with an API token.** `v0.2.2` is reproducible (§H.7), but
   publishing depends on one machine and a long-lived secret.
5. **There is no entry point for contributors or security reports.** The README gives
   `make install` and `make check`; nothing says which branch to target, that features start as
   proposals, or how to report a vulnerability privately. One design gap is already known and
   public: a session id is a bearer capability ([session-admission](./session-admission.md)).
6. **`mcp` is a hard dependency of one module.** Only `vsigma/mcp.py` imports it, but at module
   level, and `vsigma/__init__.py` re-exports from that module, so `import vsigma` needs it
   (§H.6).

## B. Design

### B.1 Lock against pypi.org; the mirror becomes a per-machine choice

Remove the `[[tool.uv.index]]` entry and re-lock with `uv lock --default-index
https://pypi.org/simple`. Plain `uv lock` is not enough: an index-less configuration still
accepts the mirror's lock as it is (§H.2). The re-lock keeps all 69 package versions.

The maintainers' machine then needs to reach pypi.org and `files.pythonhosted.org`, directly or
through a proxy (`HTTPS_PROXY`). uv cannot point a pypi.org lock at a mirror: with the mirror as
the default index the lock counts as stale (§H.3), and `uv sync` would rewrite it with mirror
URLs. CI's `uv lock --locked` (B.2) catches a rewritten lock before it is merged.

### B.2 CI on every push and pull request

One GitHub Actions workflow on pushes and pull requests to `dev` and `master`:

- set up uv; Python 3.11, 3.12 and 3.13, matching the classifiers;
- `uv lock --locked`, then `uv sync --locked`;
- `uv run ruff check .`, `uv run mypy`, `uv run pytest` (the `make check` gate);
- `uv build`, so packaging breaks show up before a release.

The suite needs no LLM credentials (`FakeLLM`), so the workflow needs no secrets.

### B.3 One version, in `__init__.py`

`pyproject.toml` declares `dynamic = ["version"]` and `[tool.hatch.version] path =
"src/vsigma/__init__.py"`. The build reads `__version__`, and `uv.lock` stops recording the
version (§H.4). A release then edits one line and the CHANGELOG.

### B.4 Publishing from a tag

A second workflow runs when a `v*` tag is pushed to GitHub:

1. build once, and check that the built version equals the tag;
2. publish to TestPyPI;
3. publish the same files to PyPI, in a GitHub environment that waits for the maintainer's
   approval.

Both uploads use PyPI's trusted publishing (OIDC), so no token is stored anywhere. Each index
needs a one-time trusted-publisher entry naming the repository, the workflow and the
environment. Tagging becomes publishing, which suits the release rule: the release notes go in
the release commit before the tag, and a published tag never moves. Only new release tags reach
GitHub, so the workflow never sees the old ones.

### B.5 `CONTRIBUTING.md` and `SECURITY.md`

- **`CONTRIBUTING.md`:**
  - setup and the gate (`make install`, `make check`);
  - the branches: `dev` integrates and takes pull requests, `master` holds releases;
  - features start as proposals in `docs/proposals/` and go through a Decision Check Point;
  - each change adds a line under `## Unreleased` in `CHANGELOG.md`.
- **`SECURITY.md`:**
  - supported versions: the latest release only, below 1.0;
  - reports go through GitHub's private vulnerability reporting, a repository setting;
  - known limitations already documented (session ids as bearer capabilities, architecture §5),
    so they are not filed as new findings.

### B.6 The `mcp` extra (not built here)

`pip install vsigma[mcp]` would install `mcp` only on request. That needs `vsigma/mcp.py` to
import lazily and `__init__.py` to expose its names through a module `__getattr__`, raising an
`ImportError` that names the extra. It breaks anyone relying on the default install, so it
belongs in `v0.3.0` (second number for breaking releases), as a separate candidate.

## C. When to build

Build B.1, B.2 and B.5 when the first of these happens:

- an external pull request or issue arrives;
- the work for `v0.3.0` starts.

B.3 is independent and can ride along with any release. B.4 follows B.2, when manual uploads
become a burden or before `v0.3.0`.

## D. Build order

1. B.3, the single version: smallest and independent.
2. B.1 and B.2 together: CI's `uv lock --locked` is what keeps the pypi.org lock from drifting
   back to mirror URLs.
3. B.5, the two files, pointing at the CI gate.
4. B.4, the publishing workflow, first used on a real release: TestPyPI first, then PyPI after
   approval.

## E. Alternatives considered

- **Keep the mirror lock.** Works everywhere and the hashes verify every file; costs nothing.
  But contributors far from the mirror pay in speed and availability for a choice made for one
  network. Acceptable while there are no contributors, which is why §C defers this.
- **A second lockfile for the maintainers' network.** Two locks drift apart; rejected.
- **`__version__ = importlib.metadata.version("vsigma")`.** Needs installed metadata, so a
  source tree used without installing reports the wrong version or fails. Rejected for B.3.
- **An API token stored as a GitHub secret.** A long-lived secret where OIDC needs none.
  Rejected for B.4.
- **CI on the private GitLab remote.** Contributors work on GitHub, and both remotes carry the
  same commits; one CI is enough.

## F. Non-goals

- A documentation site, coverage thresholds, pre-commit hooks.
- A code of conduct and issue or pull-request templates, until there is a community to serve.
- Declaring Python versions beyond 3.13: a separate decision once CI can test them.
- The `mcp` extra (B.6), which is a `v0.3.0` candidate of its own.

## G. Verdicts to decide

1. **When to build** (§C): on the first external contribution or issue, or at the start of
   `v0.3.0`. Recommended: yes.
2. **Lock against pypi.org**, with the maintainers' machine reaching it directly or by proxy
   (B.1). Recommended: yes.
3. **CI**: Python 3.11–3.13 on pushes and pull requests to `dev` and `master`, gating on the
   lock check, `make check` and the build (B.2). Recommended: yes.
4. **The version in `__init__.py` only**, read by hatch (B.3). Recommended: yes.
5. **Publishing from a tag** with trusted publishing, TestPyPI first, PyPI behind approval (B.4).
   Recommended: yes.
6. **`CONTRIBUTING.md` and `SECURITY.md`**, with GitHub's private vulnerability reporting
   (B.5). Recommended: yes.
7. **The `mcp` extra** moves to its own `v0.3.0` candidate (B.6). Recommended: yes.

## H. Evidence (uv 0.12.1; 2026-10-30, on a scratch copy of `dev`)

1. `pyproject.toml` declares the mirror as the default index; `uv.lock` holds 1,215 mirror URLs.
2. With the index entry removed, `uv lock` leaves the lock as it is ("Resolved 69 packages",
   still 1,215 mirror URLs). `uv lock --default-index https://pypi.org/simple` re-resolves
   (1m32s on the maintainers' network): no mirror URLs, 1,147 `files.pythonhosted.org` URLs,
   and the same 69 package versions.
3. With the pypi.org lock, `uv lock --locked` passes; with `UV_DEFAULT_INDEX` set to the
   mirror it fails ("To update the lockfile, run `uv lock`").
4. With `dynamic = ["version"]` and `[tool.hatch.version]` pointing at `__init__.py`,
   `uv build` produces `vsigma-0.2.2`, and `uv lock` reports `Updated vsigma v0.2.2 ->
   (dynamic)`: the lock's `vsigma` entry no longer carries a version.
5. `tests/test_smoke.py:7-8` asserts only that `vsigma.__version__` is a non-empty `str`.
6. `src/vsigma/mcp.py:21-24` imports `mcp` at module level; `src/vsigma/__init__.py:56`
   re-exports `make_mcp_tools` and three other names from it.
7. `v0.2.2` rebuilt from its tag is byte-identical to the files prepared for upload, and
   `twine check --strict` passes on both.
8. pypi.org was unreachable from the maintainers' network on 2026-10-29 and reachable on
   2026-10-30.
