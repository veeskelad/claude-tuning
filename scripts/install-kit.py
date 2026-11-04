#!/usr/bin/env python3
"""Ставит базовый набор claude-tuning (kit/) в домашний каталог: скиллы, хуки, настройки, шаблоны.

По умолчанию — сухой прогон: печатает, что сделает. Ничего чужого не перезаписывает: скилл или хук
с тем же именем и другим содержимым остаётся как есть и попадает в отчёт «своя версия».

    python3 install-kit.py              # сухой прогон
    python3 install-kit.py --apply      # поставить
    python3 install-kit.py --check      # проверить установленное (хуки на месте, синтаксис, страж срабатывает)
    python3 install-kit.py --uninstall [--apply]   # убрать поставленное (только файлы, совпадающие с kit, и хуки kit)
    python3 install-kit.py --home DIR   # другой домашний каталог (проба в чистом окружении)
    python3 install-kit.py --extra DIR  # плюс свой модуль той же формы (skills/, hooks/, bin/, rules/, output-styles/, settings.snippet.json)
    python3 install-kit.py --with code,servers   # плюс бонусы по сферам из kit/bonus/ (code, servers, content)

Свой модуль — место для личного (свои серверы, пути, хуки): публичный kit остаётся общим, личное
подключается поверх и в kit не попадает. --extra можно повторять.
"""
import argparse, filecmp, json, os, shutil, subprocess, sys, time

KIT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "kit")
VENDOR = {  # скиллы из чужих репозиториев: ставятся их установщиком, не копией
    "find-skills": "npx skills add vercel-labs/skills --skill find-skills -g",
    "skill-creator": "npx skills add anthropics/skills --skill skill-creator -g",
}

p = argparse.ArgumentParser()
p.add_argument("--apply", action="store_true")
p.add_argument("--check", action="store_true")
p.add_argument("--uninstall", action="store_true")
p.add_argument("--home", default=os.path.expanduser("~"))
p.add_argument("--extra", action="append", default=[], metavar="DIR")
p.add_argument("--with", dest="bonus", default="", metavar="СФЕРЫ", help="бонусы через запятую: " + ", ".join(sorted(os.listdir(os.path.join(KIT, "bonus")))) if os.path.isdir(os.path.join(KIT, "bonus")) else "")
a = p.parse_args()
H = os.path.abspath(a.home)
DRY = not a.apply
BONUS = [b.strip() for b in a.bonus.split(",") if b.strip()]
for b in BONUS:
    if not os.path.isdir(f"{KIT}/bonus/{b}"):
        sys.exit(f"--with {b}: такого бонуса нет (есть: {', '.join(sorted(os.listdir(f'{KIT}/bonus')))})")
KITS = [KIT] + [f"{KIT}/bonus/{b}" for b in BONUS] + [os.path.abspath(os.path.expanduser(d)) for d in a.extra]
for d in KITS[1:]:
    if not os.path.isdir(d):
        sys.exit(f"--extra {d}: нет такой папки")
# Файлы модуля, которые кладутся как есть: подпапка модуля → куда в ~/.claude, исполняемый ли
FILES = [("hooks", ".claude/hooks", True), ("bin", ".claude/bin", True), ("rules", ".claude/rules", False), ("output-styles", ".claude/output-styles", False)]
log = []


def say(kind, text):
    log.append((kind, text))
    print(f"{kind:12} {text}")


def same_tree(x, y):
    c = filecmp.dircmp(x, y, ignore=[".DS_Store", "__pycache__"])
    if c.left_only or c.right_only or c.diff_files or c.funny_files:
        return False
    return all(same_tree(os.path.join(x, d), os.path.join(y, d)) for d in c.common_dirs)


def place(src, dst, is_dir, is_exec=True):
    rel = dst.replace(H, "~")
    if os.path.lexists(dst):
        real = os.path.realpath(dst)
        same = same_tree(src, real) if is_dir and os.path.isdir(real) else (not is_dir and os.path.isfile(real) and filecmp.cmp(src, real, shallow=False))
        say("есть" if same else "своя версия", rel + ("" if same else f"  (сравнить: diff -r {src} {rel})"))
        return
    say("поставлю" if DRY else "поставлено", rel)
    if DRY:
        return
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    if is_dir:
        shutil.copytree(src, dst, ignore=shutil.ignore_patterns(".DS_Store", "__pycache__"))
    else:
        shutil.copy2(src, dst)
        if is_exec:
            os.chmod(dst, 0o755)


def link(target, path):
    if os.path.lexists(path):
        return
    say("ссылка" if not DRY else "свяжу", f"{path.replace(H, '~')} → {target}")
    if not DRY:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        os.symlink(target, path)


def ls(kit, sub):
    d = f"{kit}/{sub}"
    # Кэш Python и мусор Finder — не часть набора: после запуска скрипта из kit/bin появляется __pycache__.
    return sorted(f for f in os.listdir(d) if f not in (".DS_Store", "__pycache__") and not f.endswith(".pyc")) if os.path.isdir(d) else []


def install():
    store = f"{H}/.agents/skills"
    cs = f"{H}/.claude/skills"
    for kit in KITS:
        if kit != KIT:
            print(f"\n— {'бонус ' + os.path.basename(kit) if kit.startswith(KIT + '/bonus/') else 'модуль ' + kit.replace(H, '~')}")
        for name in ls(kit, "skills"):
            place(f"{kit}/skills/{name}", f"{store}/{name}", True)
        # Точки доступа: ~/.claude/skills целиком ссылкой на склад, иначе — по скиллу.
        if not os.path.lexists(cs):
            link("../.agents/skills", cs)
        elif not os.path.islink(cs):
            for name in ls(kit, "skills"):
                link(f"../../.agents/skills/{name}", f"{cs}/{name}")
        if os.path.isdir(f"{H}/.codex"):
            for name in ls(kit, "skills"):
                link(f"../../.agents/skills/{name}", f"{H}/.codex/skills/{name}")
        for sub, to, is_exec in FILES:
            for name in ls(kit, sub):
                place(f"{kit}/{sub}/{name}", f"{H}/{to}/{name}", False, is_exec)
        if os.path.exists(f"{kit}/settings.snippet.json"):
            merge_settings(kit)
        for name in ls(kit, "launchd"):
            install_agent(f"{kit}/launchd/{name}")
    if os.path.isdir(f"{KIT}/statusline"):
        install_statusline()
    # белый список dippy: заготовка, только если своего файла нет
    if os.path.exists(f"{KIT}/dippy/config.example"):
        place(f"{KIT}/dippy/config.example", f"{H}/.dippy/config", False, False)
    if os.path.isdir(f"{KIT}/mods"):
        install_mods()
    print()
    vendor = dict(VENDOR)
    # сторонние скиллы модуля и бонуса — vendor.json рядом с его skills/
    for kit in KITS:
        if os.path.exists(f"{kit}/vendor.json"):
            vendor.update({k: v for k, v in json.load(open(f"{kit}/vendor.json")).items() if not k.startswith("_")})
    for name, cmd in vendor.items():
        if not os.path.exists(f"{store}/{name}") and not os.path.exists(f"{cs}/{name}"):
            say("вручную", f"{name}: {cmd}")
    # Хук dippy в settings.snippet.json без самой программы молча ничего не делает.
    if not shutil.which("dippy"):
        say("вручную", "dippy (по желанию): pipx install dippy — сам одобряет команды только на чтение, подтверждений меньше")
    if os.path.exists(f"{H}/.claude/CLAUDE.md"):
        say("есть", "~/.claude/CLAUDE.md — переписать в карту по kit/claude-md.md (шаг 2 скилла), не копией")
    else:
        place(f"{KIT}/claude-md.md", f"{H}/.claude/CLAUDE.md", False)
    gh = f"{H}/.claude/global-handoff.md"
    if not os.path.exists(gh):
        say("поставлю" if DRY else "поставлено", "~/.claude/global-handoff.md (пустой каркас)")
        if not DRY:
            open(gh, "w").write("# Handoff\n\n## По проектам\n\n## Человеческий контекст\n")
    # Глобальный gitignore: core.excludesFile, если задан, иначе XDG-путь, который git читает по умолчанию.
    ignore = f"{H}/.config/git/ignore"
    if H == os.path.expanduser("~"):
        r = subprocess.run(["git", "config", "--global", "core.excludesFile"], capture_output=True, text=True)
        if r.stdout.strip():
            ignore = os.path.expanduser(r.stdout.strip())
    have = os.path.exists(ignore) and any(l.strip().rstrip("/").endswith(".tasks") for l in open(ignore))
    if not have:
        say("поставлю" if DRY else "поставлено", "~/.config/git/ignore: .tasks/ (чек-листы не попадают в коммиты)")
        if not DRY:
            os.makedirs(os.path.dirname(ignore), exist_ok=True)
            with open(ignore, "a") as f:
                f.write("\n.tasks/\n")


MODS_NAME = "claude-tuning-mods"
LABEL_PREFIX = "claude-tuning"
AGENTS_DIR = "Library/LaunchAgents"
STATUS_CMD = 'bash "$HOME/.claude/statusline.sh"'


def agent_path(template):
    name = os.path.basename(template).removesuffix(".plist.template")
    return f"{H}/{AGENTS_DIR}/{LABEL_PREFIX}.{name}.plist"


def install_agent(template):
    """Задача по расписанию на macOS: шаблон launchd с подставленным домом; на других системах — подсказка."""
    if not template.endswith(".plist.template"):
        return
    dst = agent_path(template)
    if sys.platform != "darwin":
        say("вручную", f"{os.path.basename(template)}: на этой системе нет launchd — расписание через cron, команда в шаблоне")
        return
    body = open(template).read().replace("__HOME__", H).replace("__LABEL_PREFIX__", LABEL_PREFIX)
    if os.path.exists(dst):
        say("есть" if open(dst).read() == body else "своя версия", dst.replace(H, "~"))
        return
    say("поставлю" if DRY else "поставлено", dst.replace(H, "~"))
    if DRY:
        return
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    open(dst, "w").write(body)
    # загружать только в настоящем доме: в пробном каталоге задача запустилась бы от имени человека
    if H == os.path.expanduser("~"):
        subprocess.run(["launchctl", "load", dst], capture_output=True)


def install_statusline():
    src = f"{KIT}/statusline/statusline.sh"
    if not os.path.exists(src):
        return
    place(src, f"{H}/.claude/statusline.sh", False)
    path = f"{H}/.claude/settings.json"
    cur = json.load(open(path)) if os.path.exists(path) else {}
    if "statusLine" in cur:
        say("есть", "statusLine в settings.json — своя, не трогаю")
        return
    say("поправлю" if DRY else "поправлено", "~/.claude/settings.json: statusLine (контекст и лимиты под полем ввода)")
    if DRY:
        return
    cur["statusLine"] = {"type": "command", "command": STATUS_CMD}
    json.dump(cur, open(path, "w"), ensure_ascii=False, indent=2)


def mod_names():
    return [x["name"] for x in json.load(open(f"{KIT}/mods/.claude-plugin/marketplace.json"))["plugins"]]


def install_mods():
    """Моды — локальный marketplace в ~/.claude/mods: папка плюс два ключа в settings.json, без вызова claude."""
    dst = f"{H}/.claude/mods"
    place(f"{KIT}/mods", dst, True)
    path = f"{H}/.claude/settings.json"
    cur = json.load(open(path)) if os.path.exists(path) else {}
    new = json.loads(json.dumps(cur))
    # путь абсолютный: так требует источник directory (документация Claude Code, extraKnownMarketplaces)
    new.setdefault("extraKnownMarketplaces", {}).setdefault(MODS_NAME, {"source": {"source": "directory", "path": dst}})
    for name in mod_names():
        new.setdefault("enabledPlugins", {}).setdefault(f"{name}@{MODS_NAME}", True)
    if new == cur:
        say("есть", f"моды {', '.join(mod_names())} включены")
        return
    say("поправлю" if DRY else "поправлено", f"~/.claude/settings.json: моды {', '.join(mod_names())} (заработают в новом чате)")
    if DRY:
        return
    if os.path.exists(path):
        backup_settings(path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    json.dump(new, open(path, "w"), ensure_ascii=False, indent=2)


def hook_key(h):
    """Команда хука; у хуков без команды (prompt, agent) — весь объект."""
    return h.get("command") or json.dumps(h, sort_keys=True, ensure_ascii=False)


def merge_settings(kit):
    path = f"{H}/.claude/settings.json"
    cur = json.load(open(path)) if os.path.exists(path) else {}
    snip = json.load(open(f"{kit}/settings.snippet.json"))
    new = json.loads(json.dumps(cur))
    for k, v in snip.get("env", {}).items():
        new.setdefault("env", {}).setdefault(k, v)
    for k in ("worktree", "attribution"):
        if k in snip:
            new.setdefault(k, snip[k])
    added = 0
    for ev, entries in snip.get("hooks", {}).items():
        have = {hook_key(h) for e in new.setdefault("hooks", {}).get(ev, []) for h in e.get("hooks", [])}
        have_files = " ".join(have)
        for e in entries:
            cmd = hook_key(e["hooks"][0])
            # обёртка (bypass-ask-to-deny.sh 'bash …/guard.sh') — своя по последнему скрипту, а не по обёртке:
            # иначе вторая обёрнутая запись считалась бы уже стоящей
            script = cmd.split('"$HOME/')[-1].split('"')[0] if '"$HOME/' in cmd else cmd[:60]
            if cmd in have or os.path.basename(script) in have_files:
                continue
            new["hooks"].setdefault(ev, []).append(e)
            added += 1
            say("хук", f"{ev} {e.get('matcher', '')}: {os.path.basename(script)}")
    if new == cur:
        say("есть", "~/.claude/settings.json — всё уже на месте")
        return
    if DRY:
        say("поправлю", f"~/.claude/settings.json (+{added} хуков, недостающие env/worktree/attribution)")
        return
    if os.path.exists(path):
        backup_settings(path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    json.dump(new, open(path, "w"), ensure_ascii=False, indent=2)
    say("поправлено", "~/.claude/settings.json")


def check():
    bad = 0
    for tool in ("jq", "git", "python3"):
        if not shutil.which(tool):
            say("НЕТ", f"{tool} — хуки без него молча не работают"); bad += 1
    path = f"{H}/.claude/settings.json"
    cur = json.load(open(path)) if os.path.exists(path) else {}
    for ev, entries in cur.get("hooks", {}).items():
        for e in entries:
            for h in e.get("hooks", []):
                cmd = h.get("command", "")
                if '"$HOME/' not in cmd:
                    continue
                f = H + "/" + cmd.split('"$HOME/')[1].split('"')[0]
                if not os.path.exists(f):
                    say("НЕТ ФАЙЛА", f"{ev}: {f.replace(H, '~')}"); bad += 1
                elif f.endswith(".sh") and subprocess.run(["bash", "-n", f]).returncode:
                    say("СИНТАКСИС", f); bad += 1
                elif f.endswith(".py") and subprocess.run([sys.executable, "-m", "py_compile", f]).returncode:
                    say("СИНТАКСИС", f); bad += 1
    for kit in KITS:
        for name in ls(kit, "skills"):
            if not os.path.exists(f"{H}/.agents/skills/{name}/SKILL.md") and not os.path.exists(f"{H}/.claude/skills/{name}/SKILL.md"):
                say("НЕТ СКИЛЛА", name); bad += 1
    guard = f"{H}/.claude/hooks/safety-guard.sh"
    if os.path.exists(guard):
        r = subprocess.run(["bash", guard], input=json.dumps({"tool_input": {"command": "rm -rf tests/ patches/ plan/ ~/"}}), capture_output=True, text=True)
        ok = '"deny"' in r.stdout
        say("страж" if ok else "СТРАЖ МОЛЧИТ", "rm -rf tests/ patches/ plan/ ~/ → " + ("deny" if ok else r.stdout.strip() or "пропущено")); bad += not ok
    print(f"\nПроверка: {'всё на месте' if not bad else f'проблем: {bad}'}")
    return bad


def backup_settings(path):
    recent = [f for f in os.listdir(os.path.dirname(path)) if f.startswith("settings.json.bak-")
              and time.time() - os.path.getmtime(os.path.join(os.path.dirname(path), f)) < 7 * 86400]
    if not recent:
        bak = f"{path}.bak-{time.strftime('%Y%m%d')}"
        shutil.copy2(path, bak)
        say("бэкап", bak.replace(H, "~"))


def uninstall():
    """Снимает только то, что совпадает с kit: свои правки и чужие файлы остаются."""
    def drop(src, dst, is_dir):
        if not os.path.lexists(dst):
            return
        real = os.path.realpath(dst)
        same = same_tree(src, real) if is_dir else filecmp.cmp(src, real, shallow=False)
        if not same:
            say("оставлю", f"{dst.replace(H, '~')} — отличается от kit (своя версия)")
            return
        say("уберу" if DRY else "убрано", dst.replace(H, "~"))
        if not DRY:
            shutil.rmtree(dst) if is_dir else os.remove(dst)
    kit_cmds = set()
    for kit in KITS:
        for name in ls(kit, "skills"):
            drop(f"{kit}/skills/{name}", f"{H}/.agents/skills/{name}", True)
            for d in (f"{H}/.claude/skills/{name}", f"{H}/.codex/skills/{name}"):
                if os.path.islink(d) and not os.path.exists(d):
                    say("уберу" if DRY else "убрано", f"{d.replace(H, '~')} (ссылка)")
                    if not DRY:
                        os.remove(d)
        for sub, to, _ in FILES:
            for name in ls(kit, sub):
                drop(f"{kit}/{sub}/{name}", f"{H}/{to}/{name}", False)
        if os.path.exists(f"{kit}/settings.snippet.json"):
            kit_cmds |= {hook_key(h) for es in json.load(open(f"{kit}/settings.snippet.json")).get("hooks", {}).values() for e in es for h in e["hooks"]}
    if os.path.isdir(f"{KIT}/mods"):
        drop(f"{KIT}/mods", f"{H}/.claude/mods", True)
    if os.path.exists(f"{KIT}/statusline/statusline.sh"):
        drop(f"{KIT}/statusline/statusline.sh", f"{H}/.claude/statusline.sh", False)
    if os.path.exists(f"{KIT}/dippy/config.example"):
        drop(f"{KIT}/dippy/config.example", f"{H}/.dippy/config", False)
    for kit in KITS:
        for name in ls(kit, "launchd"):
            dst = agent_path(f"{kit}/launchd/{name}")
            if not os.path.exists(dst):
                continue
            say("уберу" if DRY else "убрано", dst.replace(H, "~"))
            if not DRY:
                if H == os.path.expanduser("~"):
                    subprocess.run(["launchctl", "unload", dst], capture_output=True)
                os.remove(dst)
    path = f"{H}/.claude/settings.json"
    if not os.path.exists(path):
        return
    cur = json.load(open(path))
    removed = 0
    # моды: ключи, которые поставил install_mods
    if cur.get("statusLine", {}).get("command") == STATUS_CMD:
        del cur["statusLine"]
        removed += 1
    if cur.get("extraKnownMarketplaces", {}).pop(MODS_NAME, None) is not None:
        removed += 1
        for name in mod_names():
            cur.get("enabledPlugins", {}).pop(f"{name}@{MODS_NAME}", None)
    for ev, entries in list(cur.get("hooks", {}).items()):
        keep = [e for e in entries if not any(hook_key(h) in kit_cmds for h in e.get("hooks", []))]
        removed += len(entries) - len(keep)
        if keep:
            cur["hooks"][ev] = keep
        else:
            del cur["hooks"][ev]
    if not removed:
        say("есть", "~/.claude/settings.json — хуков kit нет")
        return
    say("уберу" if DRY else "убрано", f"~/.claude/settings.json: {removed} хуков и моды kit (env, CLAUDE.md, handoff и чек-листы не трогаются)")
    if not DRY:
        backup_settings(path)
        json.dump(cur, open(path, "w"), ensure_ascii=False, indent=2)
    # Ссылка ~/.claude/skills → ../.agents/skills остаётся: в складе могут быть и другие скиллы.


if a.check:
    sys.exit(1 if check() else 0)
if a.uninstall:
    print(f"{'Сухой прогон удаления' if DRY else 'Удаление'} kit → {H}\n")
    uninstall()
    if DRY:
        print("\nНичего не изменено. Убрать: --uninstall --apply")
    sys.exit(0)
print(f"{'Сухой прогон' if DRY else 'Установка'} kit → {H}\n")
install()
if DRY:
    print("\nНичего не изменено. Поставить: --apply")
