# ADR-0052: the UI's Claude skill page reads the skill from disk on every request

Status: accepted (2026-11-01)

## Context

`.claude/skills/server-scan-api` teaches Claude Code to query this API. Users need a place to
find it, install it and see what it does. A page that embeds a copy of the skill (a build-time
import or hand-written text) goes stale the first time the skill is edited and needs a second
edit in the UI.

## Decision

The API serves the skill: `GET /api/v1/skill` returns the frontmatter fields, every file
(`SKILL.md`, `references/*.md`), a `version` (a short hash of all files) and `updated_at` (newest
mtime), read from disk per request. `GET /api/v1/skill/download?base_url=` returns a zip named
`<skill>/...`, ready for `~/.claude/skills/`; the SPA passes its own origin, which replaces the
`BASE_URL=` line so a user only adds their token (the value is validated: plain http(s) address,
no whitespace or quotes). Both need a resolved caller, any role. The SPA page `/skill` renders
only what the endpoint returns, including the example prompts, which are the quoted phrases in the
skill's `description`, and refetches every minute and on window focus.

The folder is `INVENTORY_SKILL_DIR`, else `/app/skill` (the `Containerfile` copies
`.claude/skills/server-scan-api` there), else the repo's own `.claude/skills/server-scan-api`
(local dev). So editing the skill needs no UI change: locally the page follows the next refetch;
in production the next image build carries it (CI already builds on every push).

## Consequences

The page shows the skill's markdown as source with an outline, not rendered HTML: a markdown
renderer would be a new dependency for the audit surface. A deployment that mounts a ConfigMap at
`INVENTORY_SKILL_DIR` can change the skill with no image build. The token is never written into
the download.
