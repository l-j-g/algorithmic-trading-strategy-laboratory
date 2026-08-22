from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPOSITORY_ROOT / "scripts" / "jesse-workspace.sh"


class JesseWorkspaceStatusTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.upstream = self.root / "jesse-upstream"
        self.research = self.root / "jesse-src"
        self.docker_bin = self.root / "bin"
        self.docker_bin.mkdir()
        self.fake_docker = self.docker_bin / "docker"
        self.fake_docker.write_text(
            "#!/usr/bin/env bash\n"
            "set -euo pipefail\n"
            "if [[ \"${1:-}\" == \"info\" ]]; then exit \"${FAKE_DOCKER_INFO_RC:-0}\"; fi\n"
            "if [[ \"${FAKE_DOCKER_MISSING:-0}\" == \"1\" ]]; then exit 1; fi\n"
            "printf '%s\\n' \"$FAKE_DOCKER_INSPECT\"\n",
            encoding="utf-8",
        )
        self.fake_docker.chmod(0o755)
        for repository in (self.upstream, self.research):
            repository.mkdir()
            self.run_git("init", "--quiet", str(repository))
            self.run_git("-C", str(repository), "config", "user.email", "test@example.invalid")
            self.run_git("-C", str(repository), "config", "user.name", "Workspace Test")
            (repository / "marker").write_text("fixture\n", encoding="utf-8")
            self.run_git("-C", str(repository), "add", "marker")
            self.run_git("-C", str(repository), "commit", "--quiet", "-m", "fixture")
        self.full_revision = self.run_git("-C", str(self.upstream), "rev-parse", "HEAD")
        self.short_revision = self.run_git(
            "-C", str(self.upstream), "rev-parse", "--short=12", "HEAD",
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def run_git(self, *arguments: str) -> str:
        result = subprocess.run(
            ["git", *arguments],
            check=True,
            capture_output=True,
            text=True,
        )
        return result.stdout.strip()

    def run_status(
        self,
        inspect_value: str,
        *,
        missing: bool = False,
        unavailable: bool = False,
    ) -> str:
        environment = os.environ.copy()
        environment.update({
            "PATH": f"{self.docker_bin}{os.pathsep}{environment['PATH']}",
            "JESSE_UPSTREAM_REPOSITORY": str(self.upstream),
            "JESSE_RESEARCH_REPOSITORY": str(self.research),
            "JESSE_IMAGE_REPOSITORY": "ats-lab/fixture",
            "JESSE_CONTAINER_NAME": "jesse-fixture",
            "FAKE_DOCKER_INSPECT": inspect_value,
            "FAKE_DOCKER_MISSING": "1" if missing else "0",
            "FAKE_DOCKER_INFO_RC": "1" if unavailable else "0",
        })
        result = subprocess.run(
            [str(SCRIPT), "status"],
            cwd=REPOSITORY_ROOT,
            check=True,
            capture_output=True,
            text=True,
            env=environment,
        )
        return result.stdout

    def test_running_historical_image_is_explicit_transitional_exception(self) -> None:
        output = self.run_status(
            "running|ats-lab/fixture:historical|" + "7" * 40,
        )

        self.assertIn(f"canonical_image=ats-lab/fixture:{self.short_revision}", output)
        self.assertIn("runtime_state=running", output)
        self.assertIn("runtime_image=ats-lab/fixture:historical", output)
        self.assertIn("provenance_status=transitional_exception", output)
        self.assertIn(
            "provenance_action=after active batch completes: scripts/jesse-workspace.sh stack up",
            output,
        )
        self.assertNotIn("provenance_status=canonical", output)

    def test_exact_image_and_revision_are_required_for_canonical_match(self) -> None:
        output = self.run_status(
            f"running|ats-lab/fixture:{self.short_revision}|{self.full_revision}",
        )

        self.assertIn("provenance_status=canonical", output)
        self.assertIn("provenance_action=none", output)

        mismatched = self.run_status(
            f"running|ats-lab/fixture:{self.short_revision}|{'0' * 40}",
        )
        self.assertIn("provenance_status=transitional_exception", mismatched)
        self.assertNotIn("provenance_status=canonical", mismatched)

    def test_missing_container_is_not_claimed_as_canonical(self) -> None:
        output = self.run_status("", missing=True)

        self.assertIn("provenance_status=not_running", output)
        self.assertIn("provenance_action=scripts/jesse-workspace.sh stack up", output)
        self.assertNotIn("provenance_status=canonical", output)

    def test_unavailable_docker_is_not_claimed_as_stopped_runtime(self) -> None:
        output = self.run_status("", unavailable=True)

        self.assertIn("runtime_state=unknown", output)
        self.assertIn("provenance_status=unavailable", output)
        self.assertIn("canonical target was not compared", output)
        self.assertNotIn("provenance_status=canonical", output)

    def test_status_survives_non_git_sibling_directory(self) -> None:
        import shutil

        shutil.rmtree(self.research)
        self.research.mkdir()
        (self.research / "notes.txt").write_text("plain\n", encoding="utf-8")

        output = self.run_status(
            "running|ats-lab/fixture:historical|" + "7" * 40,
        )

        self.assertIn("not-git", output)
        self.assertIn(f"canonical_image=ats-lab/fixture:{self.short_revision}", output)

    def test_status_lists_worktree_paths_containing_spaces(self) -> None:
        spaced_parent = self.root / "space dir"
        spaced_parent.mkdir()
        worktree_path = spaced_parent / "feature one"
        self.run_git(
            "-C", str(self.research), "worktree", "add",
            "-b", "task/spaced-fixture", "--", str(worktree_path), "HEAD",
        )

        output = self.run_status("")

        self.assertIn(str(worktree_path), output)

    def test_stack_down_does_not_require_upstream_repository(self) -> None:
        import shutil

        log = self.root / "docker-calls.log"
        self.fake_docker.write_text(
            "#!/usr/bin/env bash\n"
            "set -euo pipefail\n"
            'printf "%s\\n" "$*" >> "$FAKE_DOCKER_LOG"\n',
            encoding="utf-8",
        )
        self.fake_docker.chmod(0o755)
        shutil.rmtree(self.upstream)
        compose_dir = self.research / "docker"
        compose_dir.mkdir()
        (compose_dir / "docker-compose.yml").write_text(
            "services:\n  jesse:\n    image: ${JESSE_IMAGE}\n",
            encoding="utf-8",
        )

        environment = os.environ.copy()
        environment.update({
            "PATH": f"{self.docker_bin}{os.pathsep}{environment['PATH']}",
            "JESSE_UPSTREAM_REPOSITORY": str(self.upstream),
            "JESSE_RESEARCH_REPOSITORY": str(self.research),
            "JESSE_IMAGE_REPOSITORY": "ats-lab/fixture",
            "FAKE_DOCKER_LOG": str(log),
        })
        result = subprocess.run(
            [str(SCRIPT), "stack", "down"],
            cwd=REPOSITORY_ROOT,
            check=True,
            capture_output=True,
            text=True,
            env=environment,
        )

        self.assertEqual(result.returncode, 0)
        calls = log.read_text().strip().splitlines()
        self.assertEqual(len(calls), 1)
        self.assertTrue(calls[0].startswith("compose "))
        self.assertTrue(calls[0].endswith(" down"))


if __name__ == "__main__":
    unittest.main()