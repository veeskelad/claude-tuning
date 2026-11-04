#!/usr/bin/env bash
# Периодическая проверка: не лежат ли значения секретов в транскриптах агентов.
# Печатает ТОЛЬКО метки: файл, строка, тип, sha256[:12], длина. Значений не печатает никогда.
#
#   scan-transcripts-for-secrets.sh                 Claude Code, файлы новее 7 дней
#   scan-transcripts-for-secrets.sh -d 30           новее 30 дней
#   scan-transcripts-for-secrets.sh -d 30 --codex   плюс ~/.codex/sessions
#   scan-transcripts-for-secrets.sh --summary       только сводка по типам и уникальным значениям
#
# Детектор — secret-mask.py из ~/.claude/hooks/ (те же шаблоны, что маскируют вывод команд; без него
# скрипт не работает). Плейсхолдеры с малым числом разных символов (sk-xxxx…, <token>) и метки
# ‹secret …› не считаются.
# Нашлось значение → сверь sha256-префикс с действующим ключом (secret-value.py fp <файл> <ключ>):
# совпало — ротируй; не совпало — ключ уже сменён или это фейк из теста.
set -uo pipefail
[ -f "$HOME/.claude/hooks/secret-mask.py" ] || { echo "нет ~/.claude/hooks/secret-mask.py" >&2; exit 1; }
exec python3 - "$@" <<'PY'
import glob, hashlib, importlib.util, json, os, sys, time

args = sys.argv[1:]
days = 7
if "-d" in args:
    days = float(args[args.index("-d") + 1])
summary = "--summary" in args
roots = [os.path.expanduser("~/.claude/projects")]
if "--codex" in args:
    roots.append(os.path.expanduser("~/.codex/sessions"))

spec = importlib.util.spec_from_file_location("sm", os.path.expanduser("~/.claude/hooks/secret-mask.py"))
sm = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sm)

cut = time.time() - days * 86400
files = []
for r in roots:
    files += [f for f in glob.glob(os.path.join(r, "**", "*.jsonl"), recursive=True) if os.path.getmtime(f) >= cut]


def strings(o):
    if isinstance(o, str):
        yield o
    elif isinstance(o, dict):
        for v in o.values():
            yield from strings(v)
    elif isinstance(o, list):
        for v in o:
            yield from strings(v)


seen, hits, by_type, done = set(), [], {}, set()
for f in sorted(files):
    rel = f.replace(os.path.expanduser("~"), "~")
    with open(f, errors="replace") as fh:
        for n, line in enumerate(fh, 1):
            try:
                d = json.loads(line)
            except Exception:
                continue
            for s in strings(d):
                if len(s) < 12 or (len(s) > 20000 and " " not in s[:20000]):
                    continue  # короткое или base64-тело картинки/PDF
                hs = hash(s)
                if hs in done:
                    continue  # тот же текст уже проверен (повторы tool_result в сайдчейнах)
                done.add(hs)
                if not sm._QUICK.search(s):
                    continue
                # построчно: тяжёлые шаблоны — только на строках с признаком; PEM — целиком
                if "PRIVATE KEY" in s:
                    chunk = s
                else:
                    chunk = "\n".join(l[:4000] for l in s.splitlines() if sm._QUICK.search(l))
                for name, val in sm.scan(chunk):
                    core = val
                    if name == "PRIVATE_KEY":
                        body = [l for l in val.splitlines() if l and "-----" not in l]
                        core = "".join(body)
                        if len(core) < 40:  # обрезок из документации, а не ключ
                            continue
                    elif len(set(core)) < 8:
                        continue
                    h = hashlib.sha256(val.encode()).hexdigest()[:12]
                    key = (rel, h)
                    if key in seen:
                        continue
                    seen.add(key)
                    hits.append((rel, n, name, h, len(val)))
                    by_type.setdefault(name, set()).add(h)

if not summary:
    for rel, n, name, h, ln in hits:
        print(f"{rel}:{n}\t{name}\tsha256:{h}\tlen:{ln}")
print(f"\n# файлов просмотрено: {len(files)} (новее {days:g} дн.); находок: {len(hits)}; "
      f"уникальных значений: {len({h for *_, h, _ in hits})}", file=sys.stderr if not summary else sys.stdout)
for name, hs in sorted(by_type.items(), key=lambda x: -len(x[1])):
    print(f"#   {name}: {len(hs)} уник.", file=sys.stderr if not summary else sys.stdout)
PY
