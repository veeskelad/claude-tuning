#!/usr/bin/env python3
"""Вставляет INSTALL-VIA-AGENT.txt в docs/index.html между <!--prompt--> и <!--/prompt-->.

Промпт на странице не правится руками: страница и поставка должны совпадать дословно.
    python3 scripts/build-page.py           # обновить страницу
    python3 scripts/build-page.py --check   # код 1, если страница расходится с промптом
"""
import html, os, re, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
page = f"{ROOT}/docs/index.html"
prompt = open(f"{ROOT}/INSTALL-VIA-AGENT.txt", encoding="utf-8").read()
src = open(page, encoding="utf-8").read()
block = f'<!--prompt--><div class="cmd">{html.escape(prompt, quote=False)}</div><!--/prompt-->'
new, n = re.subn(r"<!--prompt-->.*?<!--/prompt-->", lambda _: block, src, flags=re.S)
if n != 1:
    sys.exit("в docs/index.html нет ровно одной пары <!--prompt-->…<!--/prompt-->")
if "--check" in sys.argv:
    print("страница совпадает с промптом" if new == src else "страница расходится с INSTALL-VIA-AGENT.txt")
    sys.exit(0 if new == src else 1)
if new != src:
    open(page, "w", encoding="utf-8").write(new)
print("docs/index.html: промпт обновлён" if new != src else "docs/index.html: без изменений")
