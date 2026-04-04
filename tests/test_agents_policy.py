import pathlib
import unittest


REPOSITORY_ROOT = pathlib.Path(__file__).resolve().parent.parent
AGENTS_PATH = REPOSITORY_ROOT / "AGENTS.md"


class AgentsPolicyTests(unittest.TestCase):
    def test_repository_root_contains_agents_md(self) -> None:
        """Prevent the canonical agent instructions file from being removed."""
        self.assertTrue(
            AGENTS_PATH.is_file(),
            f"Expected repository instruction file at {AGENTS_PATH}",
        )
