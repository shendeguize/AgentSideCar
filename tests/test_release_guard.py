import subprocess
import tempfile
import unittest
from pathlib import Path

from scripts import release_guard


TAG_COMMIT = "1" * 40
MAIN_COMMIT = "2" * 40
RELEASE_COMMIT = "3" * 40
COLLISION_COMMIT = "4" * 40
OTHER_COMMIT = "5" * 40


class FakeRepository:
    def __init__(self, refs, ancestors=()):
        self.refs = dict(refs)
        self.ancestors = set(ancestors)
        self.resolve_calls = []
        self.ancestor_calls = []

    def try_resolve_commit(self, ref):
        self.resolve_calls.append(ref)
        return self.refs.get(ref)

    def is_ancestor(self, ancestor, descendant):
        self.ancestor_calls.append((ancestor, descendant))
        return (ancestor, descendant) in self.ancestors


class ReleaseGuardTests(unittest.TestCase):
    def make_project(self, version="0.4.0", changelog=None):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        package = root / "sidecar"
        package.mkdir()
        (package / "__init__.py").write_text(
            '__version__ = "{}"\n'.format(version),
            encoding="utf-8",
        )
        if changelog is None:
            changelog = (
                "# Changelog\n\n"
                "## [Unreleased]\n\n"
                "## [0.4.0] - 2026-08-24\n\n"
                "### Added\n\n"
                "- Release workflow.\n\n"
                "## [0.3.0] - 2026-08-23\n\n"
                "- Earlier release.\n"
            )
        (root / "CHANGELOG.md").write_text(changelog, encoding="utf-8")
        return root

    def stable_repository(self, *, ancestors=None):
        if ancestors is None:
            ancestors = {
                (TAG_COMMIT, MAIN_COMMIT),
                (TAG_COMMIT, RELEASE_COMMIT),
            }
        return FakeRepository(
            {
                "refs/tags/v0.4.0": TAG_COMMIT,
                "HEAD": TAG_COMMIT,
                "refs/remotes/origin/main": MAIN_COMMIT,
                "refs/remotes/origin/release": RELEASE_COMMIT,
            },
            ancestors,
        )

    def test_valid_stable_release_requires_main_and_release_ancestry(self):
        metadata, notes = release_guard.validate_release(
            "v0.4.0",
            root=self.make_project(),
            repository=self.stable_repository(),
        )

        self.assertEqual("0.4.0", metadata.version)
        self.assertFalse(metadata.prerelease)
        self.assertEqual(TAG_COMMIT, metadata.tag_commit)
        self.assertEqual("refs/remotes/origin/main", metadata.main_ref)
        self.assertEqual("refs/remotes/origin/release", metadata.release_ref)
        self.assertIn("- Release workflow.", notes)

    def test_rc_release_can_be_main_only(self):
        root = self.make_project(
            version="0.5.0-rc.1",
            changelog=(
                "# Changelog\n\n"
                "## [0.5.0-rc.1] - 2026-08-24\n\n"
                "### Added\n\n"
                "- Candidate.\n"
            ),
        )
        repository = FakeRepository(
            {
                "refs/tags/v0.5.0-rc.1": TAG_COMMIT,
                "HEAD": TAG_COMMIT,
                "refs/heads/main": MAIN_COMMIT,
            },
            {(TAG_COMMIT, MAIN_COMMIT)},
        )

        metadata, _ = release_guard.validate_release(
            "v0.5.0-rc.1",
            root=root,
            repository=repository,
        )

        self.assertTrue(metadata.prerelease)
        self.assertIsNone(metadata.release_ref)
        self.assertEqual([(TAG_COMMIT, MAIN_COMMIT)], repository.ancestor_calls)

    def test_branch_tag_collision_uses_qualified_tag_ref(self):
        repository = self.stable_repository()
        repository.refs["refs/heads/v0.4.0"] = COLLISION_COMMIT
        repository.refs["v0.4.0"] = COLLISION_COMMIT

        metadata, _ = release_guard.validate_release(
            "v0.4.0",
            root=self.make_project(),
            repository=repository,
        )

        self.assertEqual(TAG_COMMIT, metadata.tag_commit)
        self.assertEqual(
            ["refs/tags/v0.4.0", "HEAD"],
            repository.resolve_calls[:2],
        )
        self.assertNotIn("v0.4.0", repository.resolve_calls)

    def test_checked_out_head_must_match_peeled_tag_commit(self):
        repository = self.stable_repository()
        repository.refs["HEAD"] = OTHER_COMMIT

        with self.assertRaisesRegex(
            release_guard.ReleaseGuardError,
            r"checked-out HEAD .* does not match peeled tag refs/tags/v0\.4\.0",
        ):
            release_guard.validate_release(
                "v0.4.0",
                root=self.make_project(),
                repository=repository,
            )

    def test_mismatched_tag_and_project_version_is_rejected(self):
        with self.assertRaisesRegex(
            release_guard.ReleaseGuardError,
            r"tag version 0\.4\.0 does not match.*0\.4\.1",
        ):
            release_guard.validate_release(
                "v0.4.0",
                root=self.make_project(version="0.4.1"),
                repository=self.stable_repository(),
            )

    def test_missing_exact_changelog_section_is_rejected(self):
        root = self.make_project(
            changelog="# Changelog\n\n## [0.4.0-rc.1]\n\n- Candidate only.\n"
        )

        with self.assertRaisesRegex(
            release_guard.ReleaseGuardError,
            r"exactly one section for \[0\.4\.0\]",
        ):
            release_guard.validate_release(
                "v0.4.0",
                root=root,
                repository=self.stable_repository(),
            )

    def test_non_ancestor_tag_is_rejected(self):
        repository = self.stable_repository(
            ancestors={(TAG_COMMIT, RELEASE_COMMIT)}
        )

        with self.assertRaisesRegex(
            release_guard.ReleaseGuardError,
            r"is not an ancestor of refs/remotes/origin/main",
        ):
            release_guard.validate_release(
                "v0.4.0",
                root=self.make_project(),
                repository=repository,
            )

    def test_notes_extraction_and_machine_outputs_are_deterministic(self):
        changelog = (
            "# Changelog\n\n"
            "## [0.4.0] - 2026-08-24\n\n"
            "### Added\n\n"
            "- Exact notes.\n\n"
            "## [0.3.0] - 2026-08-23\n\n"
            "- Not these notes.\n"
        )
        notes = release_guard.extract_changelog_section(changelog, "0.4.0")
        self.assertEqual(
            "## [0.4.0] - 2026-08-24\n\n### Added\n\n- Exact notes.\n",
            notes,
        )

        metadata = release_guard.ReleaseMetadata(
            tag="v0.4.0",
            version="0.4.0",
            prerelease=False,
            tag_commit=TAG_COMMIT,
            main_ref="origin/main",
            release_ref="origin/release",
        )
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "github-output"
            release_guard.write_github_outputs(output, metadata)
            self.assertEqual(
                (
                    "tag=v0.4.0\n"
                    "version=0.4.0\n"
                    "prerelease=false\n"
                    "tag_commit={}\n"
                    "main_ref=origin/main\n"
                    "release_ref=origin/release\n"
                ).format(TAG_COMMIT),
                output.read_text(encoding="utf-8"),
            )

    def test_invalid_semver_leading_zero_is_rejected(self):
        with self.assertRaises(release_guard.ReleaseGuardError):
            release_guard.parse_tag("v0.04.0")


class GitRepositoryTests(unittest.TestCase):
    def run_git(self, root, *arguments):
        completed = subprocess.run(
            ("git",) + arguments,
            cwd=str(root),
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.assertEqual(0, completed.returncode, completed.stderr)
        return completed.stdout.strip()

    def test_lightweight_and_annotated_tags_peel_to_the_commit(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.run_git(root, "init", "--quiet")
        (root / "tracked.txt").write_text("release\n", encoding="utf-8")
        self.run_git(root, "add", "tracked.txt")
        identity = ("-c", "user.name=Release Test", "-c", "user.email=test@example.com")
        self.run_git(root, *identity, "commit", "--quiet", "-m", "release")
        commit = self.run_git(root, "rev-parse", "HEAD")
        self.run_git(root, "tag", "v0.4.0")
        self.run_git(
            root,
            *identity,
            "tag",
            "-a",
            "v0.4.1",
            "-m",
            "annotated release",
        )

        repository = release_guard.GitRepository(root)

        self.assertEqual(
            commit,
            repository.try_resolve_commit("refs/tags/v0.4.0"),
        )
        self.assertNotEqual(
            commit,
            self.run_git(root, "rev-parse", "refs/tags/v0.4.1"),
        )
        self.assertEqual(
            commit,
            repository.try_resolve_commit("refs/tags/v0.4.1"),
        )


if __name__ == "__main__":
    unittest.main()
