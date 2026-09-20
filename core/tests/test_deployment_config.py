"""Regression tests for deployment configuration (Dockerfile / docker-compose).

These guard the server-health fixes:

* the ``worker`` service must define its own healthcheck (the image-wide
  ``curl :8000/health/`` probe only fits the ``web`` service), and
* the container must run with a real, writable ``HOME`` (``adduser --system``
  otherwise leaves ``HOME=/nonexistent``).

The checks are intentionally text-based so no YAML dependency is required.
"""

from pathlib import Path

from django.test import SimpleTestCase

REPO_ROOT = Path(__file__).resolve().parents[2]


class DockerfileHomeTests(SimpleTestCase):
    """The release image must give appuser a real home directory."""

    def setUp(self) -> None:
        self.dockerfile = (REPO_ROOT / "Dockerfile").read_text(encoding="utf-8")

    def test_sets_home_env_to_writable_dir(self) -> None:
        """An ``ENV HOME=`` directive is set and does not point at /nonexistent."""
        env_lines = [line.strip() for line in self.dockerfile.splitlines() if line.strip().startswith("ENV HOME=")]
        self.assertTrue(env_lines, "no `ENV HOME=` directive found in Dockerfile")
        for line in env_lines:
            self.assertNotIn("/nonexistent", line)

    def test_appuser_gets_explicit_home_directory(self) -> None:
        """appuser is created with an explicit home directory."""
        self.assertIn("--home /home/appuser", self.dockerfile)


class WorkerHealthcheckTests(SimpleTestCase):
    """The worker service needs its own, non-HTTP healthcheck."""

    def setUp(self) -> None:
        self.compose = (REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8")

    def _service_block(self, name: str) -> str:
        """Return the YAML text of a top-level service block by name."""
        lines = self.compose.splitlines()
        start = next(i for i, line in enumerate(lines) if line.strip() == f"{name}:")
        block: list[str] = []
        for line in lines[start + 1 :]:
            # A new top-level service starts at 2-space indentation.
            if line and not line.startswith("    ") and line.strip().endswith(":"):
                break
            block.append(line)
        return "\n".join(block)

    def test_worker_defines_own_healthcheck(self) -> None:
        """The worker block declares a healthcheck section."""
        worker_block = self._service_block("worker")
        self.assertIn("healthcheck:", worker_block)

    def test_worker_healthcheck_is_process_based(self) -> None:
        """The worker healthcheck probes the moonraker_worker process, not HTTP."""
        worker_block = self._service_block("worker")
        self.assertIn("moonraker_worker", worker_block)
        self.assertNotIn("localhost:8000", worker_block)
