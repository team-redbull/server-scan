# ADR-0010: Every push to main publishes both images to GHCR, versioned by Conventional Commits

## Status

Accepted

## Context

CI (`.github/workflows/ci.yml`) only ever lint/tested — nothing built or
published a container image, and nothing decided what version a given
build even was. The platform has two images (the API/collector image
from the root `Containerfile`, and the frontend from
`frontend/Containerfile`) that needed a real publishing story before
either could be deployed anywhere.

Two decisions were needed: where to publish, and how to decide a
version number automatically on every push rather than requiring a
human to hand-pick one each time.

## Decision

### Registry: GitHub Container Registry (ghcr.io)

Already the natural choice given the repo lives on GitHub: no separate
registry account/credentials to provision, and `GITHUB_TOKEN` (the
token every workflow run already gets, scoped to just this repo) is
sufficient to push — no new secret to create or rotate. `docker/
login-action@v4` with `username: ${{ github.actor }}` /
`password: ${{ secrets.GITHUB_TOKEN }}` is the standard pattern for
this; the job just needs `permissions: packages: write` declared.

### Versioning: Conventional Commits, automatic, patch-by-default

`PaulHatch/semantic-version` scans every commit since the last `v*` tag
and picks the bump: a `BREAKING CHANGE:` footer or `!` after the type
(`feat!:`) → major, `feat:` → minor, anything else (`fix:`, unprefixed,
...) → patch. Its default `major_pattern`/`minor_pattern` are already
those Conventional Commits shapes, so nothing is configured beyond
`tag_prefix: "v"`. It outputs `version_tag` (e.g. `v1.4.2`) for the
image-tagging steps to consume.

It calculates only — pushing the tag is a separate `git tag && git push`
step, which is why the job still needs `permissions: contents: write`.
A guard step sits between the two: it refuses to continue if the
computed tag is empty, already exists, or does not sort strictly after
the current latest tag. A wrong version is worse than a failed build,
because publishing images under an existing or backwards tag is not
recoverable by re-running.

**Superseded:** this was originally `mathieudutour/github-tag-action@v6`,
which did the same job in one step. It declares `using: node20`; Node 20
reached end of life in April 2026, GitHub's runners began force-running
such actions on Node 24 in June 2026, and that fallback is removed in
autumn 2026, at which point the action stops executing and this job
publishes nothing. There was no upgrade available — its last release was
March 2024 and its default branch still declares node20. Two other
candidates were rejected: `anothrNick/github-tag-action` is
Docker-based (immune to the deprecation) but bumps on `#major`/`#minor`
tokens rather than Conventional Commits, which would have silently
changed the versioning rule, and `cycjimmy/semantic-release-action`
declares an even older node16.

Chosen over hand-picking a version per push (defeats the point of
automating this) and over "always bump patch, tag major/minor by hand"
(considered — genuinely simpler, zero commit-message discipline
required — but Conventional Commits is standard enough, and expressive
enough to capture an intentional breaking change automatically, that it
was worth the one real cost below).

**The real cost, stated plainly**: this repository's commit history
before this ADR does not follow Conventional Commits, and adopting this
means future commit messages should — starting a message with `feat:`,
`fix:`, `feat!:`/a `BREAKING CHANGE:` footer for anything that actually
warrants a minor or major bump. Until that habit is established, every
push still bumps patch safely (the `default_bump: patch` fallback) —
this doesn't produce wrong version numbers, it just means every release
looks like a patch release until commit messages start signaling
otherwise on purpose.

### Tag scheme

Both images get the same set, generated from one version string by
`docker/metadata-action@v6` (the tool built for exactly this — avoids
hand-writing the semver-component-splitting logic):

- The full version (`v1.4.2`)
- Rolling `{major}.{minor}` (`v1.4`) and `{major}` (`v1`) lines
- `latest`
- `sha-<short-sha>` — exact traceability back to source between
  releases, independent of the semver line

### One job, both images, gated on the rest of CI

`publish` `needs: [lint, test, frontend, e2e]` and only runs
`if: github.event_name == 'push' && github.ref == 'refs/heads/main'` —
it never runs for a pull request, and never publishes anything that
hasn't passed the full suite first. Both images build for
`linux/amd64` only, matching the existing `Containerfile`'s own
x86_64-specific Python download (python-build-standalone) — multi-
platform (`docker/setup-qemu-action` + a platform matrix) is a real
option later if arm64 is ever actually needed, not before.

## Consequences

- Commit messages from here on should follow Conventional Commits when
  a change is more than a patch — this is now a real, functional signal
  (it changes the published version), not just a style nicety. Past
  commits don't need to be rewritten; the tag action only looks forward
  from the last tag.
- With no prior tag, `PaulHatch/semantic-version` starts from `0.0.x`.
  That case is behind us — the repository already has `v2.0.4` — but the
  guard step would catch anything unexpected before it published.
- The two actions count patches differently: the previous one bumped
  once per *run*, this one derives the patch number from the commits
  since the last version-changing commit. A push containing three
  `fix:` commits therefore advances the patch by three rather than one.
  Both are monotonically increasing and valid semver, so this is a
  numbering difference, not a correctness one.
- Both `ghcr.io/team-redbull/server-scan-api` and
  `ghcr.io/team-redbull/server-scan-frontend` inherit the repository's
  visibility (private repo → private package) by default; no separate
  visibility configuration was added here.
- Build provenance attestation (`actions/attest-build-provenance`) and
  multi-arch builds are reasonable future hardening steps, deliberately
  not included here — scope was "publish, versioned" per the request
  that prompted this ADR, not a broader supply-chain-security pass.

## Update (2026-09-13): repository renamed, versioning restarted at v1.0.0

The GitHub repository was renamed `server_scan` → `server-scan`, which
changes the image names CI publishes (`ghcr.io/${{ github.repository }}-*`)
to `ghcr.io/team-redbull/server-scan-api` and `-frontend`. At the
operator's direction, every `v*` tag and GitHub Release up to v17.4.3 was
deleted at the same time so versioning starts over under the new name —
the releases carried no assets, only notes regenerated from commit
subjects, so nothing unique was lost. The old `server_scan-*` packages
were deleted from GHCR too; an existing deployment must move its image
repository to the new name before its next pod reschedule.

With no tag to scan from, `semantic-version` reads the whole history from
0.0.0, finds the `feat!:` commits in it, and the first push after the
rename publishes **v1.0.0**, whose release notes list every commit once.
Git history is unchanged; the old URL redirects. Both charts' `appVersion`
were reset to `1.0.0` to match.
