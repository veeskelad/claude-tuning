#!/usr/bin/env python3
"""Направление codex: мост Codex → хуки Claude на синтетических JSON хуков.

    python3 tests/codex-bridge.test.py

Скрипты kit/tracks/codex/codex-hooks/ запускаются так, как их зовёт Codex (JSON на stdin, ответ в stdout),
с HOME во временной папке: настоящие ~/.claude и ~/.codex не читаются и не меняются, codex и claude не
запускаются. Хуки Claude — заглушки (и настоящий safety-guard.sh из kit/hooks, если есть jq).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HOOKS = ROOT / "kit/tracks/codex/codex-hooks"
KIT_HOOKS = ROOT / "kit/hooks"
GUARD = HOOKS / "pre_tool_guard.py"
BRIDGE = HOOKS / "claude_bridge.py"
DANGER = "rm -rf tests/ patches/ plan/ ~/"

# Заглушка в духе safety-guard.sh: rm -rf по домашней папке — deny, git push --force — ask, остальное — молчание.
SAFETY_STUB = r'''#!/bin/bash
CMD=$(python3 -c 'import json,sys; print(json.load(sys.stdin).get("tool_input",{}).get("command",""))')
case "$CMD" in
  *"rm -rf"*"~/"*) printf '%s\n' '{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"deny","permissionDecisionReason":"⛔ stub: rm -rf по домашней папке"}}' ;;
  *"push --force"*) printf '%s\n' '{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"ask","permissionDecisionReason":"stub: force-push"}}' ;;
esac
exit 0
'''


def hook(cmd: str, timeout: int = 10) -> dict:
    return {"type": "command", "command": cmd, "timeout": timeout}


def py_hook(code: str) -> dict:
    """Хук-заглушка на Python: d — JSON события; печатает то, что вернёт выражение (dict → JSON, str → текст)."""
    src = f"import json,sys; d=json.load(sys.stdin); r=({code}); print(json.dumps(r, ensure_ascii=False) if isinstance(r, dict) else (r or ''), end='')"
    return hook("python3 -c " + json.dumps(src))


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(os.path.realpath(self.tmp.name))
        self.project = self.home / "work/proj"
        self.project.mkdir(parents=True)
        self.env = {k: v for k, v in os.environ.items()
                    if not k.startswith(("CLAUDE_", "CODEX_")) and k != "PYTHONPATH"}
        self.env.update(HOME=str(self.home), PYTHONDONTWRITEBYTECODE="1", CLAUDE_PARITY_MCP_SYNC="0")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def settings(self, hooks: dict, where: Path | None = None) -> None:
        path = (where or self.home) / ".claude/settings.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"hooks": hooks}))

    def trust(self, value: bool) -> None:
        (self.home / ".claude.json").write_text(
            json.dumps({"projects": {str(self.project): {"hasTrustDialogAccepted": value}}}))

    def stub(self, name: str, body: str) -> str:
        path = self.home / ".claude/hooks" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)
        path.chmod(0o755)
        return f'bash "$HOME/.claude/hooks/{name}"'

    def run_script(self, script: Path, data: object, *args: str) -> tuple[int, str]:
        raw = data if isinstance(data, str) else json.dumps(data)
        p = subprocess.run([sys.executable, str(script), *args], input=raw, capture_output=True, text=True,
                           env=self.env, timeout=60)
        return p.returncode, p.stdout.strip()

    def pre(self, command: str = "", tool: str = "Bash", tool_input: dict | None = None, cwd: Path | None = None) -> dict:
        data = {"session_id": "s-1", "turn_id": "t-1", "hook_event_name": "PreToolUse", "model": "test-model",
                "cwd": str(cwd or self.project), "tool_name": tool, "tool_use_id": "c-1",
                "tool_input": tool_input if tool_input is not None else {"command": command}}
        code, out = self.run_script(GUARD, data)
        self.assertEqual(code, 0)
        return json.loads(out) if out else {}

    def event(self, name: str, data: dict) -> dict:
        payload = {"session_id": "s-1", "cwd": str(self.project), "hook_event_name": name, **data}
        code, out = self.run_script(BRIDGE, payload, name)
        self.assertEqual(code, 0)
        return json.loads(out) if out else {}

    @staticmethod
    def decision(out: dict) -> str:
        return (out.get("hookSpecificOutput") or {}).get("permissionDecision", "")

    @staticmethod
    def reason(out: dict) -> str:
        return (out.get("hookSpecificOutput") or {}).get("permissionDecisionReason", "")


class PreToolUseTests(Base):
    def setUp(self) -> None:
        super().setUp()
        self.settings({"PreToolUse": [{"matcher": "Bash", "hooks": [hook(self.stub("safety-guard.sh", SAFETY_STUB))]}]})

    def test_deny_is_enforced(self) -> None:
        out = self.pre(DANGER)
        self.assertEqual(self.decision(out), "deny")
        self.assertIn("rm -rf по домашней папке", self.reason(out))

    def test_allow_passes_silently(self) -> None:
        self.assertEqual(self.pre("ls -la"), {})

    def test_other_tools_not_matched(self) -> None:
        self.assertEqual(self.pre(tool="web_search", tool_input={"query": DANGER}), {})

    def test_exit_code_2_is_deny(self) -> None:
        self.settings({"PreToolUse": [{"matcher": "Bash", "hooks": [hook("echo 'стоп по коду 2' >&2; exit 2")]}]})
        out = self.pre("ls")
        self.assertEqual(self.decision(out), "deny")
        self.assertEqual(self.reason(out), "стоп по коду 2")

    def test_bad_input_fails_closed(self) -> None:
        code, out = self.run_script(GUARD, "not json")
        self.assertEqual(code, 0)
        self.assertEqual(self.decision(json.loads(out)), "deny")

    def test_ask_becomes_deny_with_one_shot_approval(self) -> None:
        cmd = "git push --force origin feature"
        out = self.pre(cmd)
        self.assertEqual(self.decision(out), "deny")
        text = self.reason(out)
        self.assertIn("Claude спросил бы подтверждение", text)
        self.assertIn("stub: force-push", text)
        m = re.search(r"--command-file (\S+) --command-sha256 (\w+) --cwd (\S+)", text)
        self.assertIsNotNone(m)
        pending, digest, cwd = m.groups()  # type: ignore[union-attr]
        self.assertEqual(Path(pending).read_text(), cmd)
        self.assertEqual(Path(pending).stat().st_mode & 0o777, 0o600)
        self.assertEqual(digest, hashlib.sha256(cmd.encode()).hexdigest())
        self.assertTrue(pending.startswith(str(self.home / ".codex/state/claude-parity/approvals")))
        # человек сказал «да» — агент выпускает разрешение той самой командой
        p = subprocess.run([sys.executable, str(HOOKS / "lifecycle_approval.py"), "--command-file", pending,
                            "--command-sha256", digest, "--cwd", cwd, "--reason", "тест: да в чате"],
                           capture_output=True, text=True, env=self.env)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertFalse(Path(pending).exists())
        allowed = self.pre(cmd)
        self.assertNotEqual(self.decision(allowed), "deny")
        self.assertIn("Одноразовое разрешение", (allowed.get("hookSpecificOutput") or {}).get("additionalContext", ""))
        self.assertEqual(self.decision(self.pre(cmd)), "deny")  # одно разрешение — один запуск
        audit = self.home / ".codex/state/claude-parity/approval-audit.jsonl"
        events = [json.loads(line)["event"] for line in audit.read_text().splitlines()]
        self.assertEqual(events, ["issued", "consumed"])
        self.assertNotIn(cmd, audit.read_text())  # в журнале нет текста команды

    def test_approval_is_bound_to_cwd(self) -> None:
        cmd = "git push --force origin feature"
        text = self.reason(self.pre(cmd))
        pending, digest, cwd = re.search(r"--command-file (\S+) --command-sha256 (\w+) --cwd (\S+)", text).groups()  # type: ignore[union-attr]
        subprocess.run([sys.executable, str(HOOKS / "lifecycle_approval.py"), "--command-file", pending,
                        "--command-sha256", digest, "--cwd", cwd, "--reason", "да"], check=True,
                       capture_output=True, env=self.env)
        other = self.home / "work/other"
        other.mkdir()
        self.assertEqual(self.decision(self.pre(cmd, cwd=other)), "deny")

    def test_deny_wins_over_approval_and_ask(self) -> None:
        self.settings({"PreToolUse": [{"matcher": "Bash", "hooks": [
            hook(self.stub("safety-guard.sh", SAFETY_STUB)),
            py_hook("{'hookSpecificOutput': {'permissionDecision': 'ask', 'permissionDecisionReason': 'ещё вопрос'}}"),
        ]}]})
        out = self.pre(DANGER)
        self.assertEqual(self.decision(out), "deny")
        self.assertNotIn("спросил бы", self.reason(out))

    def test_secret_guard_rewrite_is_passed_back(self) -> None:
        masked = {"command": "printenv | python3 secret-mask.py"}
        self.settings({"PreToolUse": [{"matcher": "Bash", "hooks": [
            hook("python3 -c " + json.dumps(
                "import json; print(json.dumps({'hookSpecificOutput': {'permissionDecision': 'ask', "
                f"'permissionDecisionReason': 'маска', 'updatedInput': {masked!r}}}}}))") + " # secret-guard-bash.py"),
            py_hook("{'hookSpecificOutput': {'updatedInput': {'command': 'другая переписка'}}}"),
        ]}]})
        out = self.pre("printenv")
        self.assertEqual(self.decision(out), "allow")
        self.assertEqual(out["hookSpecificOutput"]["updatedInput"], masked)

    def test_apply_patch_is_judged_as_write_and_edit(self) -> None:
        self.settings({"PreToolUse": [{"matcher": "Write|Edit", "hooks": [py_hook(
            "{'hookSpecificOutput': {'permissionDecision': 'deny', 'permissionDecisionReason': d['tool_name'] + ':' + d['tool_input']['file_path'].rsplit('/', 1)[-1]}} "
            "if 'KEY' in json.dumps(d['tool_input']) else None")]}]})
        patch = ("*** Begin Patch\n*** Add File: new.txt\n+KEY=1\n*** Update File: old.txt\n@@\n-a\n+KEY=2\n"
                 "*** Delete File: gone.txt\n*** End Patch")
        out = self.pre(tool="apply_patch", tool_input={"command": patch})
        self.assertEqual(self.decision(out), "deny")
        self.assertEqual(self.reason(out), "Write:new.txt\nEdit:old.txt")
        clean = patch.replace("KEY", "x")
        self.assertEqual(self.pre(tool="apply_patch", tool_input={"command": clean}), {})

    def test_mcp_tool_name_passes_through(self) -> None:
        self.settings({"PreToolUse": [{"matcher": "mcp__.*", "hooks": [py_hook(
            "{'hookSpecificOutput': {'permissionDecision': 'deny', 'permissionDecisionReason': d['tool_name']}}")]}]})
        self.assertEqual(self.reason(self.pre(tool="mcp__tracker__delete_issue", tool_input={"id": 1})),
                         "mcp__tracker__delete_issue")

    def test_project_hooks_only_when_trusted_in_claude(self) -> None:
        self.settings({"PreToolUse": [{"matcher": "Bash", "hooks": [py_hook(
            "{'hookSpecificOutput': {'permissionDecision': 'deny', 'permissionDecisionReason': 'проектный'}}")]}]},
            where=self.project)
        self.trust(False)
        self.assertEqual(self.pre("ls"), {})
        self.trust(True)
        self.assertEqual(self.reason(self.pre("ls")), "проектный")

    def test_claude_only_hooks_are_skipped(self) -> None:
        marker = self.home / "ran"
        self.settings({"PreToolUse": [{"matcher": "Bash", "hooks": [
            hook(f"touch {marker} # dippy"),
            hook(f"touch {marker} # session-autosave.sh"),
            {"type": "prompt", "prompt": "проверь"},
        ]}]})
        self.assertEqual(self.pre("ls"), {})
        self.assertFalse(marker.exists())


class NoClaudeTests(Base):
    def test_missing_settings_is_noop(self) -> None:
        self.assertEqual(self.pre(DANGER), {})
        for name, data in (("PostToolUse", {"tool_name": "Bash", "tool_input": {"command": "ls"}}),
                           ("SessionStart", {"source": "startup"}), ("UserPromptSubmit", {"prompt": "x"}),
                           ("Stop", {"stop_hook_active": False}), ("PreCompact", {"trigger": "auto"})):
            self.assertEqual(self.event(name, data), {}, name)
        self.assertFalse((self.home / ".claude").exists())  # мост ничего не создал

    def test_broken_settings_is_noop(self) -> None:
        (self.home / ".claude").mkdir()
        (self.home / ".claude/settings.json").write_text("{не json")
        self.assertEqual(self.pre(DANGER), {})


class LifecycleTests(Base):
    def test_session_start_by_source_and_plain_text(self) -> None:
        self.settings({"SessionStart": [
            {"matcher": "startup", "hooks": [hook("echo 'открытые задачи: 2 --agent claude'")]},
            {"matcher": "compact", "hooks": [hook("echo 'после сжатия'")]},
        ]})
        ctx = self.event("SessionStart", {"source": "startup"})["hookSpecificOutput"]
        self.assertEqual(ctx["hookEventName"], "SessionStart")
        self.assertEqual(ctx["additionalContext"], "открытые задачи: 2 --agent codex")
        self.assertEqual(self.event("SessionStart", {"source": "compact"})["hookSpecificOutput"]["additionalContext"],
                         "после сжатия")

    def test_post_tool_use_block_and_context(self) -> None:
        self.settings({"PostToolUse": [
            {"matcher": "Bash|Read", "hooks": [py_hook("{'decision': 'block', 'reason': 'в выводе ключ'} if 'sk-' in json.dumps(d) else None")]},
            {"matcher": "*", "hooks": [py_hook("{'hookSpecificOutput': {'additionalContext': 'круг из шагов'}}")]},
        ]})
        out = self.event("PostToolUse", {"tool_name": "Bash", "tool_input": {"command": "cat x"},
                                         "tool_response": "sk-test-000"})
        self.assertEqual(out["decision"], "block")
        self.assertEqual(out["reason"], "в выводе ключ")
        self.assertEqual(out["hookSpecificOutput"], {"hookEventName": "PostToolUse", "additionalContext": "круг из шагов"})
        clean = self.event("PostToolUse", {"tool_name": "Bash", "tool_input": {"command": "ls"}, "tool_response": "ok"})
        self.assertNotIn("decision", clean)

    def test_user_prompt_submit(self) -> None:
        self.settings({"UserPromptSubmit": [{"matcher": "ignored", "hooks": [
            py_hook("{'decision': 'block', 'reason': 'в просьбе ключ'} if 'sk-' in d['prompt'] else 'напоминание'")]}]})
        self.assertEqual(self.event("UserPromptSubmit", {"prompt": "привет"})["hookSpecificOutput"],
                         {"hookEventName": "UserPromptSubmit", "additionalContext": "напоминание"})
        self.assertEqual(self.event("UserPromptSubmit", {"prompt": "sk-1"})["decision"], "block")

    def test_stop_block_respects_stop_hook_active(self) -> None:
        self.settings({"Stop": [{"hooks": [py_hook("{'decision': 'block', 'reason': 'чек-лист не закрыт'}")]}]})
        self.assertEqual(self.event("Stop", {"stop_hook_active": False}),
                         {"decision": "block", "reason": "чек-лист не закрыт"})
        self.assertEqual(self.event("Stop", {"stop_hook_active": True}), {})

    def test_pre_compact_runs_for_side_effect(self) -> None:
        marker = self.home / "snapshot"
        self.settings({"PreCompact": [{"matcher": "auto", "hooks": [hook(f"echo текст; touch {marker}")]}]})
        self.assertEqual(self.event("PreCompact", {"trigger": "manual"}), {})
        self.assertFalse(marker.exists())
        self.assertEqual(self.event("PreCompact", {"trigger": "auto"}), {})
        self.assertTrue(marker.exists())

    def test_payload_reaches_hook_with_claude_names(self) -> None:
        dump = self.home / "payload.json"
        self.settings({"PostToolUse": [{"matcher": "Edit", "hooks": [hook(f'cat > {dump}; echo "$CLAUDE_PROJECT_DIR" >> {dump}.env')]}]})
        self.event("PostToolUse", {"tool_name": "apply_patch",
                                   "tool_input": {"command": "*** Begin Patch\n*** Update File: a.py\n-x\n+y\n*** End Patch"}})
        got = json.loads(dump.read_text())
        self.assertEqual((got["hook_event_name"], got["tool_name"]), ("PostToolUse", "Edit"))
        self.assertEqual(got["tool_input"]["file_path"], str(self.project / "a.py"))
        self.assertEqual(Path(f"{dump}.env").read_text().strip(), str(self.project))

    def test_unknown_event_is_noop(self) -> None:
        code, out = self.run_script(BRIDGE, {"cwd": str(self.project)}, "PermissionRequest")
        self.assertEqual((code, out), (0, ""))


class TemplateTests(Base):
    """hooks.json.template: каждая команда ведёт в файл набора и работает из установленного места."""

    def setUp(self) -> None:
        super().setUp()
        self.template = json.loads((HOOKS / "hooks.json.template").read_text())
        dst = self.home / ".codex/hooks/claude-parity"
        dst.parent.mkdir(parents=True)
        shutil.copytree(HOOKS, dst, ignore=shutil.ignore_patterns("__pycache__", "*.template"))

    def commands(self) -> list[tuple[str, str]]:
        return [(ev, h["command"]) for ev, groups in self.template["hooks"].items() for g in groups for h in g["hooks"]]

    def test_paths_are_home_relative_and_exist(self) -> None:
        for ev, cmd in self.commands():
            m = re.fullmatch(r'python3 "\$HOME/\.codex/hooks/claude-parity/(\w+\.py)"(?: (\w+))?', cmd)
            self.assertIsNotNone(m, cmd)
            script, arg = m.groups()  # type: ignore[union-attr]
            self.assertTrue((HOOKS / script).is_file(), script)
            if script == "claude_bridge.py":
                self.assertEqual(arg, ev)
        self.assertEqual({ev for ev, _ in self.commands()},
                         {"PreToolUse", "PostToolUse", "SessionStart", "UserPromptSubmit", "Stop", "PreCompact"})

    def test_installed_commands_run_with_guard(self) -> None:
        self.settings({"PreToolUse": [{"matcher": "Bash", "hooks": [hook(self.stub("safety-guard.sh", SAFETY_STUB))]}]})
        for ev, cmd in self.commands():
            data = {"session_id": "s", "cwd": str(self.project), "hook_event_name": ev, "source": "startup",
                    "tool_name": "Bash", "tool_input": {"command": DANGER}, "prompt": "x", "stop_hook_active": False}
            p = subprocess.run(["bash", "-c", cmd], input=json.dumps(data), capture_output=True, text=True,
                               env=self.env, timeout=60)
            self.assertEqual(p.returncode, 0, (cmd, p.stderr))
            out = json.loads(p.stdout) if p.stdout.strip() else {}
            if ev == "PreToolUse":
                self.assertEqual(self.decision(out), "deny")
            else:
                self.assertEqual(out, {}, cmd)


@unittest.skipUnless(shutil.which("jq") and (KIT_HOOKS / "safety-guard.sh").exists(), "нужен jq и kit/hooks/safety-guard.sh")
class RealGuardTests(Base):
    """Настоящий safety-guard.sh набора в обёртке bypass-ask-to-deny.sh — так его ставит базовый settings.snippet.json."""

    def setUp(self) -> None:
        super().setUp()
        dst = self.home / ".claude/hooks"
        dst.mkdir(parents=True)
        for name in ("safety-guard.sh", "bypass-ask-to-deny.sh"):
            shutil.copy2(KIT_HOOKS / name, dst / name)
        snippet = json.loads((ROOT / "kit/settings.snippet.json").read_text())
        guard = [e for e in snippet["hooks"]["PreToolUse"] if "safety-guard.sh" in e["hooks"][0].get("command", "")]
        self.assertEqual(len(guard), 1)
        self.settings({"PreToolUse": guard})

    def test_kit_guard_denies_in_codex(self) -> None:
        self.assertEqual(self.decision(self.pre(DANGER)), "deny")

    def test_kit_guard_allows_plain_command(self) -> None:
        self.assertNotEqual(self.decision(self.pre("git status")), "deny")


if __name__ == "__main__":
    unittest.main(verbosity=1)
