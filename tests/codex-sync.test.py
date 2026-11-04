#!/usr/bin/env python3
"""Направление codex: перенос MCP Claude → Codex (sync_from_claude.py) и каталог скиллов (skill_inventory.py).

    python3 tests/codex-sync.test.py

HOME — временная папка с выдуманными ~/.claude.json и ~/.codex/config.toml: настоящие конфиги не читаются.
Нужен Python 3.11+ (tomllib).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    tomllib = None  # type: ignore[assignment]

ROOT = Path(__file__).resolve().parent.parent
HOOKS = ROOT / "kit/tracks/codex/codex-hooks"
SYNC = HOOKS / "sync_from_claude.py"
INVENTORY = HOOKS / "skill_inventory.py"
FAKE_VALUE = "fake-value-000"


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(os.path.realpath(self.tmp.name))
        self.env = {k: v for k, v in os.environ.items() if not k.startswith(("CLAUDE_", "CODEX_", "GIT_"))}
        self.env.update(HOME=str(self.home), PYTHONDONTWRITEBYTECODE="1")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def run_py(self, script: Path, *args: str, stdin: str = "") -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, str(script), *args], input=stdin, capture_output=True, text=True,
                              env=self.env, timeout=60)


@unittest.skipIf(tomllib is None, "нужен Python 3.11+")
class SyncTests(Base):
    def setUp(self) -> None:
        super().setUp()
        self.proj = self.home / "proj"
        (self.home / ".claude").mkdir()
        (self.home / ".codex").mkdir()
        (self.proj / ".claude").mkdir(parents=True)
        (self.proj / ".codex").mkdir()
        (self.home / ".claude.json").write_text(json.dumps({
            "mcpServers": {"tracker": {"command": "npx", "args": ["-y", "t"], "env": {"TOKEN": FAKE_VALUE}}},
            "projects": {str(self.proj): {"hasTrustDialogAccepted": True, "mcpServers": {
                "notes": {"type": "http", "url": "https://notes.example/mcp"},
                "repo": {"type": "http", "url": "https://repo.example/mcp"}}}}}))
        (self.proj / ".claude/settings.json").write_text(json.dumps({"permissions": {
            "allow": ["mcp__notes__get_page", "mcp__repo__get_issue", "Bash(ls)"],
            "ask": ["mcp__notes__save_page"], "deny": ["mcp__notes__delete_page"]}}))
        (self.home / ".codex/config.toml").write_text('[mcp_servers.Repo]\ncommand = "npx"\n')
        (self.proj / ".codex/config.toml").write_text(
            'model_verbosity = "medium"\n[mcp_servers.repo]\nurl = "https://repo.example/mcp"\nenabled = false\n')

    def toml(self, path: Path) -> dict:
        return tomllib.loads(path.read_text())

    def test_writes_blocks_and_is_idempotent(self) -> None:
        self.assertEqual(self.run_py(SYNC, "--check").returncode, 1)
        out = self.run_py(SYNC)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertNotIn(FAKE_VALUE, out.stdout + out.stderr)  # значение не печатается
        g = self.toml(self.home / ".codex/config.toml")["mcp_servers"]
        self.assertEqual(g["tracker"]["env"], {"TOKEN": FAKE_VALUE})
        self.assertEqual(g["Repo"]["command"], "npx")
        self.assertEqual(g["Repo"]["tools"]["get_issue"]["approval_mode"], "approve")
        p = self.toml(self.proj / ".codex/config.toml")
        self.assertEqual(p["model_verbosity"], "medium")
        s = p["mcp_servers"]
        self.assertEqual(s["notes"]["disabled_tools"], ["delete_page"])
        self.assertEqual(s["notes"]["tools"]["save_page"]["approval_mode"], "prompt")
        self.assertNotIn("Repo", s)  # проектный файл не зависит от глобальных серверов машины
        self.assertFalse(s["repo"]["enabled"])  # выключенный вне блока сервер остаётся выключенным
        self.assertEqual(self.run_py(SYNC, "--check").returncode, 0)

    def test_manual_edit_outside_block_survives(self) -> None:
        self.run_py(SYNC)
        cfg = self.proj / ".codex/config.toml"
        cfg.write_text(cfg.read_text() + '\n[tui]\ntheme = "x"\n')
        self.run_py(SYNC)
        self.assertEqual(self.toml(cfg)["tui"]["theme"], "x")

    def test_project_file_valid_without_global_server(self) -> None:
        (self.home / ".codex/config.toml").write_text("")
        self.run_py(SYNC)
        p = self.toml(self.proj / ".codex/config.toml")
        self.assertEqual(sorted(p["mcp_servers"]), ["notes", "repo"])
        for name, spec in p["mcp_servers"].items():
            self.assertTrue(spec.get("url") or spec.get("command"), name)

    def test_project_file_under_git_is_not_written(self) -> None:
        subprocess.run(["git", "init", "-q", str(self.proj)], check=True, env=self.env)
        before = (self.proj / ".codex/config.toml").read_text()
        out = self.run_py(SYNC)
        self.assertIn("не в .gitignore", out.stdout)
        self.assertEqual((self.proj / ".codex/config.toml").read_text(), before)
        (self.proj / ".gitignore").write_text(".codex/config.toml\n")
        self.run_py(SYNC)
        self.assertIn("claude-sync", (self.proj / ".codex/config.toml").read_text())

    def test_no_codex_config_is_noop(self) -> None:
        (self.home / ".codex/config.toml").unlink()
        out = self.run_py(SYNC)
        self.assertEqual((out.returncode, out.stdout), (0, ""))
        self.assertFalse((self.home / ".codex/config.toml").exists())


class InventoryTests(Base):
    def skill(self, base: Path, name: str, desc: str) -> None:
        d = base / ".claude/skills" / name
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text(f"---\nname: {name}\ndescription: |\n  {desc}\n  вторая строка\n---\nтело\n")

    def test_lists_project_and_unlinked_skills(self) -> None:
        proj = self.home / "work/app"
        sub = proj / "src"
        sub.mkdir(parents=True)
        self.skill(proj, "deploy-app", "Выкатка приложения.")
        self.skill(self.home, "my-own", "Личный скилл.")
        self.skill(self.home, "linked", "Уже виден Codex.")
        (self.home / ".codex/skills/linked").mkdir(parents=True)
        (self.home / ".codex/skills/linked/SKILL.md").write_text("---\nname: linked\n---\n")
        out = self.run_py(INVENTORY, stdin=json.dumps({"cwd": str(sub), "source": "startup"}))
        ctx = json.loads(out.stdout)["hookSpecificOutput"]["additionalContext"]
        self.assertIn("`deploy-app`", ctx)
        self.assertIn("Выкатка приложения. вторая строка", ctx)
        self.assertIn("`my-own`", ctx)
        self.assertNotIn("`linked`", ctx)
        self.assertNotIn("тело", ctx)  # тела скиллов не грузятся

    def test_nothing_to_list_is_silent(self) -> None:
        out = self.run_py(INVENTORY, stdin=json.dumps({"cwd": str(self.home)}))
        self.assertEqual((out.returncode, out.stdout), (0, ""))


if __name__ == "__main__":
    unittest.main(verbosity=1)
