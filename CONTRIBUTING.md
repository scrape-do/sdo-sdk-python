<!-- SPDX-License-Identifier: Apache-2.0 -->
# Contributing to sdo-sdk-python

Thanks for taking the time to help out — contributions are genuinely welcome, whether that's a bug report, a failing test that pins down an edge case, a docs fix, or a new feature. This guide gets you from a fresh clone to a passing test run, and explains the few conventions we hold the line on.

`sdo` is Scrape.do's official open-source tooling. This repository is the Python SDK: package `sdo-client` on PyPI, imported as `import sdo`. It's a thin, well-tested wrapper over the Scrape.do API built on [`httpx`](https://www.python-httpx.org/).

The project is developed Solo+AI: the founder is the sole approver and final reviewer. That means review may be asynchronous, and design discussion happens up front (see [Changing the public surface](#changing-the-public-surface)). Don't let that discourage you — clear, small, well-tested PRs get merged.

---

## Dev setup

You need Python 3.11+ and `git`. From the repository root:

```bash
# 1. Create and activate a virtual environment
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate

# 2. Install the pinned dev tools (every package hash-checked), then the
#    package itself in editable mode without resolving anything else
python -m pip install --require-hashes -r requirements-dev.txt
python -m pip install --no-deps --no-build-isolation -e .

# 3. Run the test suite
pytest

# 4. Lint, format and type-check
ruff check src tests tools examples
ruff format --check src tests tools examples
mypy
```

`requirements-dev.txt` pins `pytest`, `pytest-cov`, `respx`, `parsel`, `ruff`,
`mypy`, the runtime dependencies and the build backend, each with its hashes.
It is compiled from `requirements-dev.in`; to change a dependency, edit the
`.in` file and recompile (the command is in its header), for example:

```bash
uv pip compile requirements-dev.in --universal --python-version 3.11 \
    --generate-hashes -o requirements-dev.txt
```

`requirements-build.txt` (the release build chain) and `requirements-audit.txt`
(`pip-audit`) work the same way. CI and the release build install only these
hash-pinned files, so a newly published release of a tool never runs in CI
until a maintainer recompiles the file and a reviewer merges it. Dependabot
opens pip pull requests for security advisories only: its own recompile drops
uv's `--universal` platform markers (colorama on Windows), so version bumps are
made by hand with the command above (`--upgrade-package <name>` for one tool).

No network access is required to run the tests — the suite mocks `httpx`
transports, so `pytest` is fully offline and deterministic.

To auto-fix lint issues and apply formatting:

```bash
ruff check --fix .
ruff format .
```

---

## How we work: TDD first

This SDK is test-driven. The expected loop for any behavioral change is:

1. **Write a failing test** that describes the behavior you want. Run `pytest` and watch it fail for the right reason.
2. **Implement** the smallest change that makes the test pass.
3. **Refactor** with the test as your safety net, keeping it green.

Concretely, if you're fixing a bug, the PR should contain a test that fails on `main` and passes with your fix. If you're adding behavior, the test that asserts it comes first in the diff's intent (we don't enforce commit ordering, but the PR must contain the test).

Tests live in `tests/`. Mirror the module layout — a change to `src/sdo/client.py` gets tests in `tests/test_client.py`. Mock HTTP with `respx` (or an `httpx.MockTransport`); never hit the live API in tests. `conformance.json` is maintained by Scrape.do and shared by the Go, JavaScript and Python SDKs; its sha256 sidecar is checked by the tests. Do not edit `tests/conformance.json` in a contribution; describe the change in an issue.

### What "good behavior" looks like here

- **Auth resolution** follows strict precedence: explicit `token=` parameter, then the `SDO_TOKEN` environment variable. A test that changes resolution order is a red flag — that order is locked.
- **Every request** sends `Accept-Encoding: gzip, deflate, br` and transparently decompresses the response. If you touch the request path, assert the header is present and that a compressed body round-trips correctly.
- **Errors** map onto the `SdoError` taxonomy (e.g. `SdoAuthError`, `SdoTimeoutError`, `SdoTransportError`). New failure modes should raise the right subclass, not a bare `Exception`. Add a test that asserts the exact type.
- **No telemetry, no phone-home.** The SDK never sends usage data anywhere except the Scrape.do API endpoint the user asked for. Don't add it.
- **Credit cost is not modelled.** We surface the `Scrape.do-Request-Cost` response header on `ScrapeResult` and stop there — don't add cost estimation, budgeting, or pre-flight math.

---

## Coverage floor: 99%

CI enforces **99% combined line and branch coverage across the whole `sdo` package**. A PR that drops it below 99% fails the gate. Cover behaviour, not lines: a test should assert what a caller would observe, and a guard should never be deleted to make a number go up.

Check coverage locally before you push:

```bash
pytest --cov=sdo --cov-branch --cov-report=term-missing --cov-fail-under=99
```

The `term-missing` report lists uncovered lines so you can see exactly what needs a test. If you genuinely cannot cover a line (e.g. a defensive branch that can't be reached), say so in the PR description rather than chasing the number with hollow tests — behavioral correctness is the goal, not a percentage.

---

## Changing the public surface

The request parameters, defaults and observable behaviour are shared by all sdo SDKs and pinned by the conformance corpus (`conformance.json`), which Scrape.do maintains against https://scrape.do/documentation. Propose a change in an issue first; a change to a parameter lands in every SDK together.

Any change to the public API must regenerate the committed golden
(`python tools/api_surface.py > api-surface.txt`); CI fails when it drifts.
Every `ScrapeConfig` field after `url` is keyword-only, so a new field goes in
its place in the shared parameter order without breaking callers (the golden
records no position for it, and the change classifies as additive).

If your change adds, renames, removes, or alters the default of a parameter, or changes how a documented field behaves, **open an issue to discuss it first** before writing code. These changes ripple across the whole `sdo` toolchain (CLI, MCP, the other SDKs), so they're coordinated across the SDKs, not patched ad hoc in one language binding: Scrape.do updates the conformance corpus, and the change lands in every SDK together. Bug fixes, internal refactors, docs, and test improvements don't need this — go straight to a PR.

The one exception baked into the design: `client.plugin(...)` is intentionally generic and passes through arbitrary plugin parameters, so calling a new plugin needs no SDK change — that's the whole point of the generic surface.

---

## Code style

Style and formatting are **`ruff`**, full stop. There is no separate formatter, linter, or import-sorter to learn. Run `ruff check .` and `ruff format .` and match what they produce. The configuration lives in `pyproject.toml`; please don't loosen rules to get a PR through — fix the code instead.

A few habits the linter won't catch but we care about:

- Public functions and classes have type hints and a short docstring.
- Prefer explicit keyword arguments at call sites for anything non-obvious.
- Keep `httpx` usage inside the client layer; don't leak transport details into user-facing models.

---

## Commits and sign-off

We use two conventions on every commit. Both are checked in CI by the `commits` job, which fails a pull request until every commit has a Conventional Commit subject and a `Signed-off-by` trailer. It checks the pull request title the same way, because it becomes the squash commit's subject.

### Conventional Commits

Commit messages follow [Conventional Commits](https://www.conventionalcommits.org/):

```
feat(client): surface Scrape.do-Request-Cost header on ScrapeResult
fix(config): reject an invalid SDO_TIMEOUT
test(errors): cover SdoTimeoutError mapping
docs: clarify auth precedence in README
```

Common types: `feat`, `fix`, `docs`, `test`, `refactor`, `chore`, `ci`. A scope in parentheses (e.g. `client`, `config`, `errors`) is encouraged. Breaking changes get a `!` after the type/scope (`feat(client)!: ...`) and a `BREAKING CHANGE:` footer.

### DCO sign-off

Every commit must be signed off under the [Developer Certificate of Origin](https://developercertificate.org/) — this certifies you wrote the change (or have the right to submit it) under the project's license. Add the trailer with `-s`:

```bash
git commit -s -m "fix(config): reject an invalid SDO_TIMEOUT"
```

This appends a line to the message:

```
Signed-off-by: Your Name <you@example.com>
```

Set your `git config user.name` and `user.email` to match. If you forget on the last commit, amend it:

```bash
git commit --amend -s --no-edit
```

The `commits` job fails a pull request until every commit is signed off, so it's worth wiring this into your muscle memory early. For a multi-commit branch, rebase with `git rebase --signoff <base>`.

---

## Pull request checklist

Before you open a PR, confirm:

- [ ] A test was added or updated and it fails without your change (TDD).
- [ ] `pytest` passes, including the conformance suite, with the 99% coverage floor.
- [ ] `ruff check src tests tools examples` and `ruff format --check src tests tools examples` pass.
- [ ] `mypy` passes.
- [ ] `tests/conformance.json` was **not** edited.
- [ ] If the public surface changed, `api-surface.txt` was regenerated (`python tools/api_surface.py > api-surface.txt`) and the change is noted in CHANGELOG.md.
- [ ] No telemetry, no UI, no new auth scheme was introduced.
- [ ] Commits follow Conventional Commits and are DCO signed off (`-s`).
- [ ] The diff is scoped to the stated change.

Open the PR against `main` with a clear description of what changed and why. Link the issue that motivated it. The maintainer will review; expect questions and keep the conversation in the PR.

---

## License

This project is licensed under **Apache-2.0**. By contributing, and by signing off with the DCO, you agree your contributions are provided under that license. There's no separate CLA — the DCO sign-off is what we rely on.

---

## Secrets and tokens

This repository is public. Practically, for you as a contributor, this means: keep secrets out of code and tests, never commit a real token, and assume everything you write is read by the public. Use fake tokens like `test-token` in tests and examples.

---

## Reporting bugs and security issues

- **Bug reports:** open an issue with a minimal reproduction — the exact SDK call, the expected result, and the actual result, with the SDK version (`python -c "import sdo; print(sdo.__version__)"`) and your Python version. Redact your token.
- **Surface proposals:** open an issue first; see [Changing the public surface](#changing-the-public-surface).
- **Security issues:** do not open a public issue. See [SECURITY.md](SECURITY.md).

---

## CI workflows

| Workflow | Runs on | What it does |
|----------|---------|--------------|
| `ci.yml` | every push to `main` and every pull request | `ruff` lint and format check; `mypy --strict`; the test suite with the 99% line + branch coverage floor on Python 3.11–3.14 (Linux), plus macOS and Windows on 3.13, including the cross-language conformance suite and its integrity check; the public-API snapshot (`api-surface.txt`) must be up to date; CodeQL code scanning. |
| `commits.yml` | every pull request: opened, updated, reopened, or its title or description edited | the `commits` job checks every commit's subject and sign-off and the pull request title, which becomes the squash commit's subject. A workflow of its own, so editing a pull request re-runs only this check. |
| `security.yml` | push to `main`, pull requests, and weekly | secret scanning over the full git history (gitleaks), OSV-Scanner over the pinned requirement files, `pip-audit`, and an SBOM uploaded as a build artifact. OpenSSF Scorecard runs on pushes to `main` and weekly, with results in code scanning. |
| `publish.yml` | a version tag, or a manual run | A tag `vX.Y.Z-rc.N` publishes a release candidate to **TestPyPI**; a tag `vX.Y.Z` publishes to **PyPI**. The tag must be on `main` and match `src/sdo/_version.py` (`X.Y.ZrcN` for a candidate). The package is built once with hash-pinned tooling, checked with `twine check`, published by trusted publishing (no stored token) after approval in the `prerelease` or `release` environment, and then installed from the index as a smoke test; for a final release, the GitHub release is then created with the version's CHANGELOG section as notes. A manual run (Actions → publish → Run workflow) uploads nothing: its `publisher-check` job only confirms that PyPI accepts this workflow's OIDC token, after approval in the `release` environment. |
| `tag.yml` | push to `main` touching `src/sdo/_version.py` or `CHANGELOG.md`, or a manual run | Creates the release tag when it does not exist yet: `vX.Y.Z` once `CHANGELOG.md` has a `## [X.Y.Z] - ` section, `vX.Y.Z-rc.N` for `X.Y.ZrcN`. The tag is created with a token of the `scrape-do-publisher` GitHub App, so it starts `publish.yml`. A manual run creates nothing: it reports the tag a push would create and checks that the App token can be minted. |
| Dependabot | weekly | update pull requests for the GitHub Actions, taken only after a 7-day cooldown, and security-advisory pull requests for the pinned Python requirements, which the cooldown does not delay (version bumps of those are recompiled by hand with uv, see [Dev setup](#dev-setup)); each one runs the checks above. |

Every job runs on GitHub-hosted runners except CodeQL, Scorecard, the `tag` job and the GitHub release job, which run on the organisation's self-hosted runners: their uploads and writes are GitHub API calls that the organisation's IP allow list only accepts from there. CodeQL runs there for same-repository events only, so a pull request from a fork is analysed once merged; Scorecard never runs on a pull request. Every action is pinned to a full commit SHA and every job declares least-privilege `permissions:`. Publishing never happens outside the `scrape-do` organisation, never from a commit that is not on `main`, and never without a maintainer's approval.

---

## Releasing

Development is trunk-based: every change lands on `main` through a pull request
with CI green. A release is a tag on `main`. `.github/workflows/tag.yml`
creates it when the release pull request lands, and
`.github/workflows/publish.yml` checks it, builds once with the hash-pinned
toolchain and publishes after approval:

| Tag | `src/sdo/_version.py` | Channel | Environment |
|-----|-----------------------|---------|-------------|
| `vX.Y.Z-rc.N` | `X.Y.ZrcN` (PEP 440) | TestPyPI | `prerelease` |
| `vX.Y.Z` | `X.Y.Z` | PyPI | `release` |

### Trusted publishers (once per index)

Both environments exist with a required reviewer and a deployment rule limited
to `v*` tags. Each index needs a trusted publisher for this workflow; until the
first upload the project does not exist, so it is a **pending** publisher, and
the first successful exchange creates the project. No API token is stored
anywhere.

On each index, sign in with an account that has 2FA, open **Account settings →
Publishing → Add a new pending publisher → GitHub** and enter:

| Field | test.pypi.org | pypi.org |
|-------|---------------|----------|
| PyPI Project Name | `sdo-client` | `sdo-client` |
| Owner | `scrape-do` | `scrape-do` |
| Repository name | `sdo-sdk-python` | `sdo-sdk-python` |
| Workflow name | `publish.yml` | `publish.yml` |
| Environment name | `prerelease` | `release` |

TestPyPI is a separate site with separate accounts. Once the project exists,
its publishers are under the project's **Manage → Publishing** instead.

This repository has GitHub's immutable OIDC subject claim
(`repo:scrape-do@<owner id>/sdo-sdk-python@<repo id>:...`). PyPI matches the
publisher on the repository, owner ID, workflow and environment, not on `sub`,
so it accepts it (unlike npm, see `sdo-sdk-js`), and no token fallback is needed.

### Checking a publisher without uploading

A manual run of `publish.yml` performs only the PyPI token exchange (the
`publisher-check` job) and prints the claims PyPI matched and its answer. There
is no such check for TestPyPI. The `release` environment accepts only `v*`
tags, so allow `main` for the duration of the run and remove the rule
afterwards:

```sh
env=release
id=$(gh api -X POST "repos/scrape-do/sdo-sdk-python/environments/$env/deployment-branch-policies" \
  -f name=main -f type=branch --jq .id)
gh workflow run publish.yml --repo scrape-do/sdo-sdk-python --ref main
# approve the run in the environment, wait for it to finish, then:
gh api -X DELETE "repos/scrape-do/sdo-sdk-python/environments/$env/deployment-branch-policies/$id"
```

`HTTP 200` means PyPI accepts the workflow. `invalid-publisher` means no
publisher matches the claims: compare the printed `repository`, `environment`
and `job_workflow_ref` with the table above.

### Cutting a release

1. Open a pull request `chore(release): vX.Y.Z` that sets
   `src/sdo/_version.py` (`X.Y.ZrcN` for a candidate) and, in `CHANGELOG.md`,
   moves the `[Unreleased]` entries under `## [X.Y.Z] - YYYY-MM-DD`, leaves an
   empty `## [Unreleased]` above it and updates the links at the bottom:

   ```markdown
   [Unreleased]: https://github.com/scrape-do/sdo-sdk-python/compare/vX.Y.Z...HEAD
   [X.Y.Z]: https://github.com/scrape-do/sdo-sdk-python/releases/tag/vX.Y.Z
   ```

2. Squash-merge it once CI is green. `tag.yml` then creates `vX.Y.Z` on the
   merge commit (it skips a final version without its CHANGELOG section, and a
   version whose tag exists). Nobody pushes a tag by hand; the "release tags"
   ruleset lets only admins and the `scrape-do-publisher` App create `v*`
   tags, and the App needs to stay installed on this repository with the org
   secrets `PUBLISHER_APP_ID` and `PUBLISHER_APP_PRIVATE_KEY` shared with it.
3. The tag starts `publish.yml`, which builds and then waits for approval in
   `release` (`prerelease` for a candidate). Approve it under the run's
   **Review deployments**.
4. The `smoke` job installs the version from the index and imports it, and for
   a final release `github-release` creates the GitHub release with the
   version's CHANGELOG section as notes. To check the install yourself, use
   Python 3.11 or newer (an older interpreter finds no installable version,
   `from versions: none`):

   ```sh
   python3.12 -m venv /tmp/sdo-check && /tmp/sdo-check/bin/pip install sdo-client==X.Y.Z
   /tmp/sdo-check/bin/python -c "import sdo; print(sdo.__version__)"
   ```

If `tag.yml` cannot run, an admin can still tag by hand on `main`
(`git tag -s vX.Y.Z -m vX.Y.Z && git push origin vX.Y.Z`); the rest is the same.

A version on PyPI can never be uploaded again, even after it is deleted, so a
broken release is fixed with a new patch version (and the broken one yanked).

---

## Getting help

Open an issue if you're stuck, unsure whether a change needs an issue first, or just want a second pair of eyes before investing in a PR. Small questions are fine — we'd rather answer one early than review the wrong thing late. Welcome aboard.
