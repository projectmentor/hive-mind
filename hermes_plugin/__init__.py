"""Hive-mind memory provider plugin for Hermes.

Mirrors every built-in memory() tool write to the hive-mind CLI
(~/projects/hive-mind/hv) so all facts flow into the shared corpus
and sync to peer nodes automatically.

Activate in config.yaml:
    memory:
      provider: hive-mind

No external dependencies — uses only subprocess + stdlib.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional

from agent.memory_provider import MemoryProvider

logger = logging.getLogger(__name__)

HV_PATH = Path.home() / "projects" / "hive-mind" / "hv"


def _hv(*args: str) -> tuple[bool, str]:
    """Run the hv CLI and return (success, output)."""
    try:
        result = subprocess.run(
            [str(HV_PATH), *args],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0:
            return True, result.stdout.strip()
        else:
            return False, result.stderr.strip()
    except Exception as e:
        return False, str(e)


class HiveMindMemoryProvider(MemoryProvider):
    """Memory provider that mirrors Hermes memory writes to hive-mind."""

    @property
    def name(self) -> str:
        return "hive-mind"

    def is_available(self) -> bool:
        return HV_PATH.exists() and HV_PATH.is_file()

    def initialize(self, session_id: str, **kwargs) -> None:
        self._session_id = session_id
        self._agent_context = kwargs.get("agent_context", "primary")
        if not self.is_available():
            logger.warning("hive-mind: hv CLI not found at %s", HV_PATH)
        else:
            logger.info("hive-mind memory provider ready (hv at %s)", HV_PATH)

    def system_prompt_block(self) -> str:
        if not self.is_available():
            return ""
        ok, out = _hv("stats")
        if not ok:
            return ""
        return f"\n\n## Hive Mind Memory\nYour memory() writes are mirrored to the shared hive-mind corpus.\n{out}\nTo search: use `hv search <query>` via terminal. To sync: `./hv sync now`.\n"

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        """No-op — hive search is explicit via terminal, not auto-injected."""
        return ""

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        """No additional tools — the memory() tool is sufficient."""
        return []

    def on_memory_write(
        self,
        action: str,
        target: str,
        content: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Mirror every built-in memory write to hive-mind."""
        # Skip cron/subagent contexts to avoid noise
        if self._agent_context not in ("primary", ""):
            return
        # Skip removes — hive is append-only
        if action == "remove":
            return
        if not self.is_available():
            return

        tags = f"hermes,{target}"
        ok, out = _hv("remember", content, "--tags", tags, "--source", "hermes")
        if ok:
            logger.debug("hive-mind: mirrored memory write → %s", out)
        else:
            logger.warning("hive-mind: failed to mirror memory write: %s", out)

    def shutdown(self) -> None:
        pass
