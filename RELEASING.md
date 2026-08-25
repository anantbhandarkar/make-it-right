# Releasing Make It Right

This is the procedure `.github/workflows/release.yml` enforces mechanically. Follow it in
order; the release workflow will refuse the tag if you skip a step it can check.

## 1. Decide the version number

Semantic Versioning (`MAJOR.MINOR.PATCH`), against this repo's own specific catalog of what
counts as breaking — see `docs/v2-plan.md`, "The version question" and "What earns a major
bump" — not just the generic semver definition:

- **MAJOR** if any of: a skill slug was renamed or removed (users type these, and
  third-party `AGENTS.md` files name them — no tooling can repair a rename); `schema.
  MANIFEST_VERSION` changed; `install.sh`'s default `--scope` changed on an unchanged
  command line; a new *required* `SKILL.md` frontmatter key was added (this fails every
  third-party skill written against the previous contract, and `install.sh` then refuses to
  install the whole tree); `mir init`'s output paths moved.
- **MINOR** for a new skill, a new CI job, a new opt-in flag, or a bug fix that tightens
  behavior but does not touch the list above (the security fixes in `1.1.0` are the worked
  example: `BASELINE_DENIED` widened, but `MANIFEST_VERSION` stayed 1 and no manifest that
  validated before stopped validating).
- **PATCH** for a fix with no behavior change a caller could observe (typos, comments,
  CI-only changes).

When in doubt, write the `### Breaking` section first (step 3) and let it decide the number,
not the other way around — that is what makes the semver claim checkable from the file
instead of asserted in a commit message nobody re-reads.

## 2. Bump `VERSION`

One line, no `v` prefix — the `v` belongs to the git tag, not the file:

```
1.2.0
```

`init/_version.py` is the only Python that parses it. Nothing else in the repo should ever
read or duplicate this number — `mir --version`, `.mir/manifest.json`'s
`generated_by_version` (if present), and this file's own tag-matching check all derive from
it.

## 3. Write the `CHANGELOG.md` entry

Add a `## [X.Y.Z] - YYYY-MM-DD` section above the previous release, following [Keep a
Changelog](https://keepachangelog.com/en/1.1.0/): `### Added`, `### Changed`,
`### Deprecated`, `### Removed`, `### Fixed`, `### Security` as needed, plus this project's
two additions:

- **`### Breaking`** — include the heading **only if this is a MAJOR release** (`X.0.0`),
  and only with non-empty content in that case. For a non-major release, omit the heading
  entirely rather than writing "None." — `release.yml` fails the tag either way this is
  gotten wrong: a `*.0.0` tag with an empty/missing section, or a non-major tag with a
  non-empty one.
- **`### Upgrading`** — free-form. What a person on the previous version needs to *do*:
  `install.sh --prune` reminders, an exit-code contract change, a config migration.

## 4. Verify locally before tagging

```bash
./validate.py --quiet
python3 init/test_init.py
python3 init/cli.py --version        # confirms VERSION and CHANGELOG agree with each other
python3 -c "import yaml; [yaml.safe_load(open(f)) for f in ['.github/workflows/ci.yml', '.github/workflows/release.yml']]"
```

All four must be clean. `release.yml` reruns `validate.py` and the full test suite again
after the tag is pushed — this step exists so a broken release is caught before, not after,
a tag most people will treat as durable.

## 5. Commit, tag, push

```bash
git add VERSION CHANGELOG.md
git commit -m "Release vX.Y.Z"          # match this repo's own commit style if you're adding more than the bump
git tag -s vX.Y.Z -m "vX.Y.Z — <one-line summary>"
git push origin main
git push origin vX.Y.Z
```

Use `git tag -s` (signed), not a bare `-a`, if you have a signing key configured. **Known
history, not a hypothetical:** `v1.0.0` is an *annotated* tag with no GPG signature at all
(`git tag -v v1.0.0` reports `error: no signature found`), and its tagger identity
(`ununt.dev@gmail.com`) does not match the author identity on the commit it points at
(`advbisanilegal@gmail.com`) — both checked directly against this repo's git history while
writing this file, not assumed. Neither is currently enforced by `release.yml` (it checks
the tag's *content* — VERSION, CHANGELOG, Breaking section — not its cryptographic
provenance), so it is a gap worth closing before this project has external consumers who
would reasonably want to verify a release came from a maintainer, not just that it looks
like one. Track it as follow-up scope, not a blocker for `1.1.0`.

## 6. What the release workflow does

Pushing a `v*` tag runs `.github/workflows/release.yml`, which:

1. Asserts `VERSION` (the file, at the tagged commit) equals the tag with its `v` stripped.
2. Asserts `CHANGELOG.md` has a `## [X.Y.Z]` entry for that version.
3. Asserts the entry's `### Breaking` section is non-empty if and only if the release is
   `*.0.0`.
4. Re-runs `validate.py --quiet` and `python3 init/test_init.py`.
5. Publishes a GitHub Release titled with the tag, whose body is exactly that version's
   `CHANGELOG.md` section (so the release notes and the changelog can never disagree — the
   workflow reuses the same extracted text for both instead of deriving it twice).

If any of 1–4 fails, no release is published. Fix the problem, force-move or delete and
re-push the tag (or push a new patch tag), and re-run.

## 7. There is no build artifact

`install.sh` symlinks this checkout rather than copying it, so "the installed version" is a
property of a user's working tree at `git pull` time, not of anything this workflow
produces. The GitHub Release exists to give the project a changelog-backed history and a
stable URL to point people at (`mir --version` reports `git describe`, which resolves
against these tags) — not to publish a tarball, wheel, or binary. There is currently nothing
to build for this repo, and nothing here should be read as a step toward one; if that
changes, add a build+attach step to `release.yml` explicitly rather than assuming this
procedure covers it.
