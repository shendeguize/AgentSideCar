import tempfile
import unittest
from pathlib import Path

from scripts import release_guard


TAG_COMMIT = "1" * 40
MAIN_COMMIT = "2" * 40
RELEASE_COMMIT = "3" * 40


class FakeRepository:
    def __init__(self, refs, ancestors=()):
        self.refs = dict(refs)
        self.ancestors = set(ancestors)
        self.ancestor_calls = []

    def try_resolve_commit(self, ref):
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


if __name__ == "__main__":
    unittest.main()
