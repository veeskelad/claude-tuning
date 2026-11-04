#!/usr/bin/env python3
"""UserPromptSubmit: человек повторяет просьбу или поправляет — напомнить записать правило сразу (скилл self-tune).

Без модели, по словам. Срабатывает на «опять», «я же просил», «сколько можно», «запомни», «в следующий раз»,
«всегда делай», «никогда не»… Подсказка уходит модели, а не человеку. Молчит в claude -p (задачи по расписанию,
автосводка) и на служебных сообщениях. Выключить: CLAUDE_TUNING_NUDGE=0.
"""
import json, os, re, sys

NUDGE = re.compile(
    r"(\bопять\b|я же (просил|говорил|писал|сказал)|сколько (раз|можно)|\bзапомни|в следующий раз|каждый раз"
    r"|мы же договаривались|\bты (снова|забыл|опять)|\bперестань\b|\bхватит\b|не первый раз|который раз"
    r"|\bвсегда (делай|пиши|проверяй|ставь|используй|спрашивай|отвечай)|\bникогда не\b"
    r"|\bagain\b|i told you|\bremember (to|that)\b|from now on|every time|stop doing)", re.I)
SKIP = ("<", "[", "{", "This session is being continued", "Base directory for this skill", "Caveat:")


def main():
    if os.environ.get("CLAUDE_TUNING_NUDGE") == "0" or os.environ.get("CLAUDE_CODE_ENTRYPOINT", "").startswith("sdk"):
        return 0
    try:
        prompt = json.load(sys.stdin).get("prompt", "")
    except (ValueError, AttributeError):
        return 0
    text = prompt.strip()
    if not text or text.startswith(SKIP) or len(text) > 4000:
        return 0
    m = NUDGE.search(text)
    if not m:
        return 0
    ctx = (f"Похоже на повтор или поправку («{m.group(0)}»). Сначала сделай саму просьбу. Если это правило на будущее, "
           "в конце хода запиши одно правило по скиллу self-tune («Человек повторил просьбу» → сразу): туда, где оно "
           "сработает в следующий раз (скилл задачи, CLAUDE.md проекта или ~/.claude/CLAUDE.md), с копией файла и строкой "
           "в ~/.claude/tuning-log.md, и одной строкой скажи человеку, что и где записал. Разовая просьба — ничего не записывай.")
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": ctx}}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        sys.exit(0)  # подсказка не должна мешать сообщению дойти
