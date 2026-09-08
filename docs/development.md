# Development

Use Python 3.11 or newer with uv from the repository root:

```bash
uv sync
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run pyright
uv run --group docs mkdocs build --strict
uv build
```

The source is under `src/ridge`, deterministic tests under `tests`, and public
documentation under `docs`. Read the root contributor instructions and
[architecture](architecture.md) before changing contracts. The public checkout
is sufficient; maintainer-local planning or environment files are not required.

For material changes, also run bounded realistic examples through CLI/MCP and
inspect outputs and side effects. Docker needs a running daemon and a worker
image with Python. SSH needs an existing POSIX account, Python, noninteractive
authentication, and verified host keys. S3 needs an explicitly authorized
disposable bucket prefix and ambient credentials. Mocks do not replace those
checks; report unavailable acceptance separately from passing unit tests.

Never use valuable data for destructive conformance or whole-tree replacement
tests. Create unique disposable roots/prefixes, bound payloads and time, and
clean up only artifacts created by your run. For S3, also inspect unfinished
multipart uploads and account for object versioning when cleaning up.

Before publication, inspect the intended source snapshot, built source/wheel
archives, and generated site for private context, job state, credentials, and
scratch artifacts. `.gitignore` helps normal staging but does not remove already
tracked files or sanitize arbitrary archives. Keep general runtime requirements
and reproducible development commands public.

## Documentation publishing

The `Documentation` GitHub Actions workflow builds the MkDocs site on pull
requests to `main`, pushes to `main`, and manual runs. It uses Python 3.14 and
the documentation dependencies in `uv.lock`; a stale lockfile or a strict-build
warning fails the build. All changes trigger the check because the generated
Python API documentation also depends on source code.

After a successful build on `main`, the workflow publishes the `site/` artifact
to [GitHub Pages](https://vasinov.github.io/ridge-core/). Pull requests and manual
runs on other branches only build. Generated HTML is not committed. Deployment
uses the built-in `GITHUB_TOKEN` and OIDC, with deployment permissions limited
to the deploy job; no personal token or repository secret is required.

For the initial repository setup:

1. Enable GitHub Actions and allow the actions referenced by
   `.github/workflows/docs.yml` under **Settings → Actions → General**.
2. Select **GitHub Actions** under **Settings → Pages → Build and deployment →
   Source**. The repository's visibility and GitHub plan must support Pages.
3. Ensure the `github-pages` environment permits deployments from `main`.
   Any configured required reviewers must approve deployments before they run.
4. Push the workflow to `main`, or select **Actions → Documentation → Run
   workflow** on `main` after it is present there.

The deployment job links to the published site. Build failures appear in
**Build documentation**; check Pages settings, environment rules, and job
permissions if **Deploy documentation** fails. An administrator can also
configure Pages through the GitHub API using an authenticated GitHub CLI;
Git SSH authentication alone does not grant API access.
