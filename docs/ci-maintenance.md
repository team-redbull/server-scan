# Keeping CI current (manual, roughly quarterly)

Every action in `.github/workflows/ci.yml` is SHA-pinned, so nothing updates
itself; Dependabot was removed deliberately (ADR-0013).

1. **Pins:** compare each `uses:` line's `# vX.Y.Z` to the latest release, and
   verify the new tag resolves first (`github-tag-action` has no `v6`;
   `setup-uv` has no `v8`/`v9`/`v10`).
2. **Vulnerable / unused:** `/gate` runs `pip-audit`, `deptry`, `npm audit --omit=dev`;
   check whether a vulnerable package is actually reached before bumping (the
   `python-multipart` fix was deletion, not an upgrade).
3. **Runtime deprecations:** actions declare a Node version (`using: node20`);
   GitHub removes old ones (that forced the tagging-action replacement, ADR-0010).
4. **Base image:** `Containerfile` pins `ubi9/ubi-minimal` to a minor (9.8);
   check for a newer 9.x.
5. **ty:** pinned exactly (`ty==0.0.78`); on a bump expect diagnostics to move
   and read a new error as ty changing. Trust `ty check` over the docs, and a
   dependency bump can change output with no change to our code (ADR-0019).
6. **Mongo on a new runner kernel:** `mongod` refuses to start on Linux 6.19+ (SERVER-121912, tcmalloc rseq), and
   `ubuntu-latest` runners got that kernel on 2026-10-29. The two jobs with a Mongo service (`test`, `e2e`) run on
   `ubuntu-22.04`; `GLIBC_TUNABLES=glibc.pthread.rseq=0` did not bypass the check. Move back to `ubuntu-latest`
   once the pinned `mongo:8` ships the fix.
