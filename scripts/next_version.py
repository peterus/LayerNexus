"""Compute the next LayerNexus release from Conventional Commit messages.

Runs in the release workflow (``.github/workflows/docker-publish.yml``) after
CI is green on ``main``. Stdlib only, no Django.

How the version is derived:

* The previous release is the highest ``vX.Y.Z`` tag reachable from ``HEAD``.
  Without any such tag the base is ``0.1.0`` and the whole history counts.
* Every first-parent commit since that tag is classified by its subject (the
  squash-merge PR title): ``feat`` -> minor, ``fix`` -> patch, ``type!:`` or a
  ``BREAKING CHANGE:`` footer -> major. All other types (chore, docs, ci,
  build, refactor, test, ...) do not release.
* Release: version ``X.Y.Z``, tag ``vX.Y.Z`` and grouped release notes.
* No release: version ``<previous>+<sha7>`` (or exactly ``<previous>`` when
  ``HEAD`` carries the tag), no tag.

Usage::

    python scripts/next_version.py --github-output "$GITHUB_OUTPUT" --notes-file notes.md
"""

import argparse
import os
import re
import subprocess
import sys
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple

_TAG_RE = re.compile(r"^v(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")
_SUBJECT_RE = re.compile(r"^(?P<type>[A-Za-z]+)(?:\((?P<scope>[^()]*)\))?(?P<bang>!)?: (?P<description>.+)$")
_BREAKING_FOOTER_RE = re.compile(r"^BREAKING[ -]CHANGE: ", re.MULTILINE)
_LEVELS = ("patch", "minor", "major")
_TYPE_LEVELS = {"feat": "minor", "fix": "patch"}
_SECTIONS = (("major", "Breaking changes"), ("minor", "New"), ("patch", "Fixes"))
# Separators for ``git log`` output: unit (field) and record separator.
_FS = "\x1f"
_RS = "\x1e"


class Version(NamedTuple):
    """A plain ``MAJOR.MINOR.PATCH`` version."""

    major: int
    minor: int
    patch: int

    def __str__(self) -> str:
        """Return ``X.Y.Z``."""
        return f"{self.major}.{self.minor}.{self.patch}"

    def bump(self, level: str) -> "Version":
        """Return the version bumped by ``level`` (``major``/``minor``/``patch``)."""
        if level == "major":
            return Version(self.major + 1, 0, 0)
        if level == "minor":
            return Version(self.major, self.minor + 1, 0)
        return Version(self.major, self.minor, self.patch + 1)


BASE_VERSION = Version(0, 1, 0)


@dataclass(frozen=True)
class Commit:
    """One commit on ``main`` (subject = squash-merged PR title)."""

    sha: str
    subject: str
    body: str = ""


@dataclass(frozen=True)
class Result:
    """Outcome of the version calculation.

    Attributes:
        release: Whether a new release (tag + GitHub release) is due.
        version: App version to bake into the image.
        tag: New tag (``vX.Y.Z``) when releasing, else empty.
        previous_tag: Latest reachable release tag, empty if there is none.
        notes: Markdown release notes (empty when not releasing).
    """

    release: bool
    version: str
    tag: str
    previous_tag: str
    notes: str


def parse_tag(tag: str) -> Version | None:
    """Parse a ``vX.Y.Z`` tag; anything else (pre-releases, junk) gives ``None``."""
    match = _TAG_RE.match(tag)
    if not match:
        return None
    return Version(*(int(part) for part in match.groups()))


def latest_version(tags: Iterable[str]) -> Version | None:
    """Return the highest release version among ``tags`` (semver order)."""
    versions = [version for version in map(parse_tag, tags) if version is not None]
    return max(versions, default=None)


def bump_level(commit: Commit) -> str | None:
    """Classify a commit as ``major``/``minor``/``patch`` or ``None`` (no release)."""
    match = _SUBJECT_RE.match(commit.subject.strip())
    if not match:
        return None
    if match["bang"] or _BREAKING_FOOTER_RE.search(commit.body):
        return "major"
    return _TYPE_LEVELS.get(match["type"].lower())


def next_version(base: Version, commits: Iterable[Commit]) -> Version | None:
    """Bump ``base`` by the highest level among ``commits``; ``None`` if nothing releases."""
    levels = [level for level in map(bump_level, commits) if level is not None]
    if not levels:
        return None
    return base.bump(max(levels, key=_LEVELS.index))


def _note_line(commit: Commit) -> str:
    """Render one commit subject as a release-notes bullet."""
    match = _SUBJECT_RE.match(commit.subject.strip())
    scope = match["scope"]
    description = match["description"].strip()
    return f"- **{scope}:** {description}" if scope else f"- {description}"


def release_notes(
    commits: Sequence[Commit],
    repo: str = "",
    previous_tag: str = "",
    new_tag: str = "",
) -> str:
    """Render Markdown release notes grouped into breaking changes, new features and fixes.

    Args:
        commits: Commits since the previous release, newest first.
        repo: ``owner/name`` for the compare link (optional).
        previous_tag: Previous release tag for the compare link (optional).
        new_tag: The new release tag for the compare link (optional).

    Returns:
        The notes; commits that do not release are left out.
    """
    blocks = []
    for level, title in _SECTIONS:
        lines = [_note_line(commit) for commit in commits if bump_level(commit) == level]
        if lines:
            blocks.append(f"## {title}\n\n" + "\n".join(lines) + "\n")
    if repo and previous_tag and new_tag:
        blocks.append(f"**Full changelog:** https://github.com/{repo}/compare/{previous_tag}...{new_tag}\n")
    return "\n".join(blocks)


def compute(
    previous: Version | None,
    head_is_tagged: bool,
    commits: Sequence[Commit],
    sha: str,
    repo: str = "",
) -> Result:
    """Decide whether to release and which version the build gets.

    Args:
        previous: Latest release reachable from ``HEAD`` (``None`` if untagged).
        head_is_tagged: ``HEAD`` itself carries ``previous``'s tag.
        commits: First-parent commits since ``previous`` (all history if ``None``).
        sha: Full ``HEAD`` commit SHA.
        repo: ``owner/name`` for the compare link in the notes.

    Returns:
        The calculation result.
    """
    base = previous or BASE_VERSION
    previous_tag = f"v{previous}" if previous else ""
    new = None if head_is_tagged else next_version(base, commits)
    if new is None:
        version = str(base) if head_is_tagged else f"{base}+{sha[:7]}"
        return Result(release=False, version=version, tag="", previous_tag=previous_tag, notes="")
    tag = f"v{new}"
    notes = release_notes(commits, repo=repo, previous_tag=previous_tag, new_tag=tag)
    return Result(release=True, version=str(new), tag=tag, previous_tag=previous_tag, notes=notes)


def _git(repo_dir: Path, *args: str) -> str:
    """Run a git command in ``repo_dir`` and return its stdout."""
    return subprocess.run(  # noqa: S603
        ["git", *args],  # noqa: S607
        cwd=repo_dir,
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def read_commits(repo_dir: Path, since_tag: str) -> list[Commit]:
    """Read first-parent commits after ``since_tag`` (whole history if empty), newest first."""
    revision = f"{since_tag}..HEAD" if since_tag else "HEAD"
    log = _git(repo_dir, "log", "--first-parent", f"--format=%H{_FS}%s{_FS}%b{_RS}", revision)
    commits = []
    for record in log.split(_RS):
        if not record.strip():
            continue
        sha, subject, body = record.strip("\n").split(_FS, 2)
        commits.append(Commit(sha=sha, subject=subject, body=body))
    return commits


def calculate(repo_dir: Path, repo: str = "") -> Result:
    """Run the full calculation against the git repository in ``repo_dir``."""
    previous = latest_version(_git(repo_dir, "tag", "--merged", "HEAD", "--list", "v*").split())
    previous_tag = f"v{previous}" if previous else ""
    head_tags = _git(repo_dir, "tag", "--points-at", "HEAD").split()
    sha = _git(repo_dir, "rev-parse", "HEAD").strip()
    commits = read_commits(repo_dir, previous_tag)
    return compute(previous, previous_tag in head_tags, commits, sha, repo=repo)


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point: compute and write ``key=value`` outputs + the notes file."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--repo-dir", type=Path, default=Path.cwd(), help="git checkout to inspect")
    parser.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY", ""), help="owner/name for links")
    parser.add_argument("--github-output", type=Path, help="file to append key=value outputs to")
    parser.add_argument("--notes-file", type=Path, help="file to write the release notes to")
    args = parser.parse_args(argv)

    result = calculate(args.repo_dir, repo=args.repo)
    outputs = (
        f"release={'true' if result.release else 'false'}\n"
        f"version={result.version}\n"
        f"tag={result.tag}\n"
        f"previous_tag={result.previous_tag}\n"
    )
    if args.github_output:
        with args.github_output.open("a") as handle:
            handle.write(outputs)
    if args.notes_file:
        args.notes_file.write_text(result.notes)
    sys.stdout.write(outputs)
    if result.notes:
        sys.stdout.write(f"\n{result.notes}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
