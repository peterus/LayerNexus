"""Tests for ``scripts/next_version.py`` (release version calculation).

The script lives outside the ``core`` package (it runs in the release
workflow without Django), so it is loaded from its file path.
"""

import contextlib
import importlib.util
import io
import subprocess
import sys
import tempfile
from pathlib import Path
from types import ModuleType

from django.conf import settings
from django.test import SimpleTestCase


def _load_script() -> ModuleType:
    """Import ``scripts/next_version.py`` as a module."""
    path = Path(settings.BASE_DIR) / "scripts" / "next_version.py"
    spec = importlib.util.spec_from_file_location("next_version", path)
    module = importlib.util.module_from_spec(spec)
    # Registered first so dataclasses can resolve the module while it executes.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


nv = _load_script()


def _commit(subject: str, body: str = "", sha: str = "a" * 40) -> object:
    """Build a commit record for the pure functions."""
    return nv.Commit(sha=sha, subject=subject, body=body)


class ParseTagTests(SimpleTestCase):
    """``parse_tag`` accepts only plain ``vX.Y.Z`` tags."""

    def test_plain_semver_tag(self) -> None:
        self.assertEqual(nv.parse_tag("v1.2.3"), nv.Version(1, 2, 3))

    def test_multi_digit_parts(self) -> None:
        self.assertEqual(nv.parse_tag("v10.20.300"), nv.Version(10, 20, 300))

    def test_rejects_non_release_tags(self) -> None:
        for tag in ("1.2.3", "v1.2", "v1.2.3-rc1", "v1.2.3+build", "release-1", "vx.y.z", "v01.2.3"):
            with self.subTest(tag=tag):
                self.assertIsNone(nv.parse_tag(tag))

    def test_latest_version_picks_highest_semver_not_lexical(self) -> None:
        tags = ["v0.9.0", "v0.10.0", "v0.2.5", "junk", "v1.0.0-rc1"]
        self.assertEqual(nv.latest_version(tags), nv.Version(0, 10, 0))

    def test_latest_version_none_without_release_tags(self) -> None:
        self.assertIsNone(nv.latest_version(["junk", "v1.0.0-rc1"]))
        self.assertIsNone(nv.latest_version([]))


class BumpLevelTests(SimpleTestCase):
    """Conventional-commit subject/body -> semver bump level."""

    def test_feat_is_minor(self) -> None:
        self.assertEqual(nv.bump_level(_commit("feat: add queue (#1)")), "minor")
        self.assertEqual(nv.bump_level(_commit("feat(jobs): add queue (#1)")), "minor")

    def test_fix_is_patch(self) -> None:
        self.assertEqual(nv.bump_level(_commit("fix: stale gcode (#2)")), "patch")
        self.assertEqual(nv.bump_level(_commit("fix(media): stale gcode (#2)")), "patch")

    def test_bang_is_major(self) -> None:
        self.assertEqual(nv.bump_level(_commit("feat!: drop v1 api")), "major")
        self.assertEqual(nv.bump_level(_commit("fix(api)!: rename field")), "major")
        self.assertEqual(nv.bump_level(_commit("refactor!: new layout")), "major")

    def test_breaking_change_footer_is_major(self) -> None:
        body = "Some text.\n\nBREAKING CHANGE: the API moved to /api/v2/."
        self.assertEqual(nv.bump_level(_commit("feat: new api", body)), "major")
        self.assertEqual(nv.bump_level(_commit("fix: x", "BREAKING-CHANGE: gone")), "major")

    def test_breaking_change_mentioned_mid_line_is_not_major(self) -> None:
        body = "This is not a BREAKING CHANGE: just prose."
        self.assertEqual(nv.bump_level(_commit("fix: x", body)), "patch")

    def test_breaking_footer_on_non_release_type_is_major(self) -> None:
        self.assertEqual(nv.bump_level(_commit("refactor: x", "BREAKING CHANGE: y")), "major")

    def test_type_is_case_insensitive(self) -> None:
        self.assertEqual(nv.bump_level(_commit("Feat: x")), "minor")
        self.assertEqual(nv.bump_level(_commit("FIX: x")), "patch")

    def test_other_types_do_not_release(self) -> None:
        for subject in (
            "chore: bump",
            "docs: spec",
            "ci: tweak",
            "deps: bump django",
            "refactor(parts): move",
            "test: more",
            "build(deps): bump the python-deps group",
            "perf: faster",
            "Merge branch 'main'",
            'Revert "feat: x"',
            "feature: not a type",
            "fixup: not a type",
            "feat no colon",
        ):
            with self.subTest(subject=subject):
                self.assertIsNone(nv.bump_level(_commit(subject)))

    def test_nested_fix_lines_in_squash_body_are_ignored(self) -> None:
        body = "* feat: inner commit\n* fix: another inner commit"
        self.assertIsNone(nv.bump_level(_commit("chore: squash", body)))


class NextVersionTests(SimpleTestCase):
    """The highest bump level across all commits wins."""

    base = nv.Version(0, 3, 1)

    def test_no_commits_no_release(self) -> None:
        self.assertIsNone(nv.next_version(self.base, []))

    def test_only_chores_no_release(self) -> None:
        commits = [_commit("chore: a"), _commit("docs: b")]
        self.assertIsNone(nv.next_version(self.base, commits))

    def test_fix_bumps_patch(self) -> None:
        self.assertEqual(nv.next_version(self.base, [_commit("fix: a")]), nv.Version(0, 3, 2))

    def test_feat_bumps_minor_and_resets_patch(self) -> None:
        commits = [_commit("fix: a"), _commit("feat: b")]
        self.assertEqual(nv.next_version(self.base, commits), nv.Version(0, 4, 0))

    def test_breaking_bumps_major_and_resets_rest(self) -> None:
        commits = [_commit("feat: b"), _commit("fix!: c")]
        self.assertEqual(nv.next_version(self.base, commits), nv.Version(1, 0, 0))

    def test_version_str(self) -> None:
        self.assertEqual(str(nv.Version(1, 2, 3)), "1.2.3")


class ReleaseNotesTests(SimpleTestCase):
    """Release notes group the PR titles by bump level."""

    def test_groups_and_order(self) -> None:
        commits = [
            _commit("fix(media): stale gcode (#102)"),
            _commit("feat(jobs): add all plates (#101)"),
            _commit("chore: bump deps (#100)"),
            _commit("feat!: drop old api (#99)"),
            _commit("feat: plain feature (#98)"),
        ]
        notes = nv.release_notes(commits)
        self.assertEqual(
            notes,
            "## Breaking changes\n\n"
            "- drop old api (#99)\n\n"
            "## New\n\n"
            "- **jobs:** add all plates (#101)\n"
            "- plain feature (#98)\n\n"
            "## Fixes\n\n"
            "- **media:** stale gcode (#102)\n",
        )

    def test_breaking_footer_listed_once_under_breaking(self) -> None:
        notes = nv.release_notes([_commit("feat(api): v2 (#5)", "BREAKING CHANGE: v1 removed")])
        self.assertEqual(notes, "## Breaking changes\n\n- **api:** v2 (#5)\n")

    def test_compare_link(self) -> None:
        notes = nv.release_notes(
            [_commit("fix: a (#1)")],
            repo="peterus/LayerNexus",
            previous_tag="v0.1.0",
            new_tag="v0.1.1",
        )
        self.assertTrue(
            notes.endswith("**Full changelog:** https://github.com/peterus/LayerNexus/compare/v0.1.0...v0.1.1\n")
        )

    def test_no_compare_link_without_previous_tag(self) -> None:
        notes = nv.release_notes([_commit("fix: a (#1)")], repo="peterus/LayerNexus", new_tag="v0.1.1")
        self.assertNotIn("compare", notes)


class ComputeTests(SimpleTestCase):
    """``compute`` combines tag lookup, bump and the non-release version."""

    sha = "abcdef1234567890abcdef1234567890abcdef12"

    def test_release_from_existing_tag(self) -> None:
        result = nv.compute(
            previous=nv.Version(0, 1, 0), head_is_tagged=False, commits=[_commit("feat: x")], sha=self.sha
        )
        self.assertTrue(result.release)
        self.assertEqual(result.version, "0.2.0")
        self.assertEqual(result.tag, "v0.2.0")
        self.assertEqual(result.previous_tag, "v0.1.0")

    def test_no_release_version_has_sha_suffix(self) -> None:
        result = nv.compute(
            previous=nv.Version(0, 3, 1), head_is_tagged=False, commits=[_commit("chore: x")], sha=self.sha
        )
        self.assertFalse(result.release)
        self.assertEqual(result.version, "0.3.1+abcdef1")
        self.assertEqual(result.tag, "")

    def test_no_tag_uses_base_0_1_0(self) -> None:
        result = nv.compute(previous=None, head_is_tagged=False, commits=[_commit("fix: x")], sha=self.sha)
        self.assertTrue(result.release)
        self.assertEqual(result.version, "0.1.1")
        self.assertEqual(result.previous_tag, "")

    def test_no_tag_without_release_commits(self) -> None:
        result = nv.compute(previous=None, head_is_tagged=False, commits=[_commit("docs: x")], sha=self.sha)
        self.assertFalse(result.release)
        self.assertEqual(result.version, "0.1.0+abcdef1")

    def test_head_exactly_tagged_reports_plain_version(self) -> None:
        result = nv.compute(previous=nv.Version(0, 2, 0), head_is_tagged=True, commits=[], sha=self.sha)
        self.assertFalse(result.release)
        self.assertEqual(result.version, "0.2.0")


def _git(cwd: Path, *args: str) -> str:
    """Run git in a scratch repo with a fixed identity."""
    env_args = ["-c", "user.name=Test", "-c", "user.email=test@example.com", "-c", "commit.gpgsign=false"]
    return subprocess.run(  # noqa: S603
        ["git", *env_args, *args],  # noqa: S607
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


class GitIntegrationTests(SimpleTestCase):
    """End to end against a scratch git repository."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self._tmp.name)
        _git(self.repo, "init", "-q", "-b", "main")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _commit(self, message: str) -> str:
        _git(self.repo, "commit", "-q", "--allow-empty", "-m", message)
        return _git(self.repo, "rev-parse", "HEAD")

    def _run(self) -> tuple[dict[str, str], str]:
        out = self.repo / "github_output"
        notes = self.repo / "notes.md"
        with contextlib.redirect_stdout(io.StringIO()):
            nv.main(["--repo-dir", str(self.repo), "--github-output", str(out), "--notes-file", str(notes)])
        outputs = dict(line.split("=", 1) for line in out.read_text().splitlines())
        return outputs, notes.read_text() if notes.exists() else ""

    def test_no_tag_all_history_from_base(self) -> None:
        self._commit("chore: init")
        self._commit("feat: first feature (#1)")
        outputs, notes = self._run()
        self.assertEqual(outputs["release"], "true")
        self.assertEqual(outputs["version"], "0.2.0")
        self.assertEqual(outputs["tag"], "v0.2.0")
        self.assertEqual(outputs["previous_tag"], "")
        self.assertIn("- first feature (#1)", notes)

    def test_only_commits_after_latest_tag_count(self) -> None:
        self._commit("feat: old feature")
        _git(self.repo, "tag", "v0.1.0")
        self._commit("fix: new fix (#2)")
        outputs, notes = self._run()
        self.assertEqual(outputs["version"], "0.1.1")
        self.assertEqual(outputs["previous_tag"], "v0.1.0")
        self.assertNotIn("old feature", notes)

    def test_non_release_commits_after_tag(self) -> None:
        self._commit("feat: old")
        _git(self.repo, "tag", "v0.4.0")
        sha = self._commit("docs: only docs")
        outputs, _ = self._run()
        self.assertEqual(outputs["release"], "false")
        self.assertEqual(outputs["version"], f"0.4.0+{sha[:7]}")
        self.assertEqual(outputs["tag"], "")

    def test_head_is_tagged(self) -> None:
        self._commit("feat: x")
        _git(self.repo, "tag", "v0.1.0")
        outputs, _ = self._run()
        self.assertEqual(outputs["release"], "false")
        self.assertEqual(outputs["version"], "0.1.0")

    def test_tags_not_reachable_from_head_are_ignored(self) -> None:
        self._commit("feat: base")
        _git(self.repo, "tag", "v0.1.0")
        _git(self.repo, "checkout", "-q", "-b", "side")
        self._commit("feat: side")
        _git(self.repo, "tag", "v9.0.0")
        _git(self.repo, "checkout", "-q", "main")
        self._commit("fix: main fix")
        outputs, _ = self._run()
        self.assertEqual(outputs["version"], "0.1.1")
