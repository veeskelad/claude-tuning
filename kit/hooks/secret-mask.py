#!/usr/bin/env python3
"""Фильтр вывода: значения секретов → метка с именем и отпечатком. Остальной текст как есть.

Зачем: guard-secret-in-output.sh ловит утечку ПОСЛЕ того, как она попала в транскрипт.
Этот фильтр стоит в самой команде (его подставляет secret-guard-bash.py), поэтому
значение до транскрипта не доходит. Дамп config.yaml однажды вывел токены из вложенного
mcp_servers.*.env: фильтр, смотревший только на верхний уровень, их пропустил.

    <команда> | secret-mask.py            обычный режим: ключи с секретным именем, токены
                                          с префиксом, пароли в URL, PEM, флаги CLI
    <команда> | secret-mask.py --strict   дамп окружения/процессов/конфига: плюс ЛЮБОЙ ключ,
                                          если значение похоже на секрет, и одиночные
                                          высокоэнтропийные строки; NUL → перевод строки
    <команда> | secret-mask.py --whole    команда печатает ОДНО значение (printenv TOKEN,
                                          base64 -d, gh auth token): строка-значение → метка
    secret-mask.py --scan                 stdin → только метки найденного (тип, имя, sha, len)
    secret-mask.py --selftest

Поток обрабатывается построчно (tail -f / docker logs -f не ждут EOF); PEM-блок
копится от BEGIN до END и уходит одной меткой.

Метка: ‹secret NAME sha256:0123456789ab len:50›. По ней видно, КАКОЙ ключ и совпадает
ли он с другой стороной, но не значение. Подставить значение, не читая его:
  * файл на сервере — ssh host 'set -a; . ~/.app/.env; set +a; <команда с "$KEY">';
  * в конфиге приложения — ссылка ${KEY} вместо значения (docker compose, n8n и т.п.).
"""
import hashlib
import re
import sys

# Имя ключа, у которого значение — секрет почти всегда (маскируем любое значение от 6 символов).
NAME = (r"pass(?:word|wd|phrase)?|pwd|secret|token|api[_-]?key|apikey|access[_-]?key|"
        r"private[_-]?key|privkey|api[_-]?hash|session[_-]?(?:string|key|token)|cookie|credential|"
        r"client[_-]?secret|bearer|dsn|signing[_-]?key|encryption[_-]?key|webhook[_-]?secret|"
        r"auth(?:orization)?|jwt|requirepass|masterauth|license[_-]?key")
# Слабое имя: `*_KEY`, `*Key`, `*_HASH`, `*_SIG` — секрет, только если значение на него похоже.
WEAK = r"[A-Za-z0-9_.\-]{0,60}?(?:_key|-key|Key|KEY|_hash|_sig|_signature|_salt|_seed|_mnemonic)"

VAL_UNQ = r"[^\s\"'`,}\]#;]"

# KEY=VALUE, KEY: VALUE, "KEY": "VALUE" — ключ содержит секретное слово. Значение в кавычках
# берём целиком до закрывающей кавычки (пароль с пробелами не должен утечь хвостом).
KV = re.compile(
    r'(?P<pre>(?P<key>(?<![A-Za-z0-9_.\-])["\']?[A-Za-z0-9_.\-]{0,60}?(?:' + NAME + r')[A-Za-z0-9_.\-]{0,60}["\']?)'
    r'[ \t]*(?::|=)[ \t]*(?:Bearer[ \t]+|Basic[ \t]+|Token[ \t]+|token[ \t]+)?)'
    r'(?:(?P<q>["\'`])(?P<qval>[^"\'`\n]{6,}?)(?P=q)|(?P<val>' + VAL_UNQ + r'{6,}))', re.I)
KV_WEAK = re.compile(
    r'(?P<pre>(?P<key>(?<![A-Za-z0-9_.\-])["\']?' + WEAK + r'["\']?)[ \t]*(?::|=)[ \t]*)'
    r'(?:(?P<q>["\'`])(?P<qval>[^"\'`\n]{8,}?)(?P=q)|(?P<val>' + VAL_UNQ + r'{8,}))')
# strict: любой ключ.
KV_ANY = re.compile(
    r'(?P<pre>(?P<key>(?<![A-Za-z0-9_.\-])["\']?[A-Za-z_][A-Za-z0-9_.\-]{0,80}["\']?)[ \t]*(?::|=)[ \t]*)'
    r'(?:(?P<q>["\'`])(?P<qval>[^"\'`\n]{10,}?)(?P=q)|(?P<val>' + VAL_UNQ + r'{10,}))')

# Флаги CLI со значением через пробел: --password X, --api-key X, --token X (ps aux, history).
FLAG = re.compile(
    r'(?P<pre>(?<![A-Za-z0-9])--?(?:password|passwd|pass|pwd|token|secret|api-?key|apikey|auth-token|'
    r'access-token|access-key|secret-key|client-secret|requirepass|masterauth|private-key|bearer)'
    r'[ \t]+)(?P<q>["\']?)(?P<val>[^\s"\']{4,})(?P=q)', re.I)
# Пароль отдельным флагом конкретных утилит.
TOOLFLAG = [
    re.compile(r'(?P<pre>\bredis-cli\b[^\n|;]*?\s-a\s+)(?P<q>["\']?)(?P<val>[^\s"\']+)(?P=q)'),
    re.compile(r'(?P<pre>\bmysql(?:dump|admin)?\b[^\n|;]*?\s-p)(?P<q>["\']?)(?P<val>[^\s"\'-][^\s"\']{3,})(?P=q)'),
    re.compile(r'(?P<pre>\bsshpass\s+-p\s*)(?P<q>["\']?)(?P<val>[^\s"\']+)(?P=q)'),
    re.compile(r'(?P<pre>\b(?:curl|wget|http)\b[^\n|;]*?(?:\s-u|--user)\s+["\']?[^\s:"\'@/]+:)(?P<q>)(?P<val>(?!\d+\b)[^\s"\'@]{3,})'),
    re.compile(r'(?P<pre>\bmachine\s+\S+(?:\s+login\s+\S+)?\s+password\s+)(?P<q>)(?P<val>\S+)'),
    # .pgpass: host:port:db:user:password
    re.compile(r'(?m)(?P<pre>^[^:\s#]+:(?:\d+|\*):[^:\s]+:[^:\s]+:)(?P<q>)(?P<val>\S+)$'),
]
# Параметры запроса: ?key=, &sig=, X-Amz-Signature= — у них имени-«секрета» нет.
URL_Q = re.compile(
    r'(?P<pre>[?&](?:key|apikey|api_key|access_token|token|sig|signature|x-amz-signature|'
    r'x-amz-security-token|auth|secret|password|client_secret|code)=)(?P<val>[^&\s"\'#<>]{6,})', re.I)
BEARER = re.compile(r'(?P<pre>\b(?:Bearer|Basic)\s+)(?P<val>[A-Za-z0-9._~+/=\-]{16,})')

# Токены с опознаваемым префиксом — где бы ни стояли.
PREFIXED = re.compile(
    r'(?<![A-Za-z0-9_\-])(?P<val>(?:sk-(?:proj-|ant-|or-)?[A-Za-z0-9_\-]{20,}|'
    r'(?:sk|rk)_(?:live|test)_[A-Za-z0-9]{16,}|whsec_[A-Za-z0-9]{20,}|'
    r'ntn_[A-Za-z0-9]{30,}|secret_[A-Za-z0-9]{30,}|gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,}|'
    r'gl(?:pat|ptt|dt|rt|cbt)-[A-Za-z0-9_\-]{20,}|xox[abeprs]-[A-Za-z0-9\-]{10,}|xapp-[A-Za-z0-9\-]{20,}|'
    r'hv[sbr]\.[A-Za-z0-9_\-]{20,}|y0_[A-Za-z0-9_\-]{20,}|t1\.[A-Za-z0-9_\-]{40,}\.[A-Za-z0-9_\-]{20,}|'
    r'(?:AQVN|YCM|YCP)[A-Za-z0-9_\-]{30,}|(?:AKIA|ASIA)[0-9A-Z]{16}|AIza[0-9A-Za-z_\-]{35}|'
    r'ya29\.[A-Za-z0-9_\-]{30,}|GOCSPX-[A-Za-z0-9_\-]{20,}|npm_[A-Za-z0-9]{36}|pypi-[A-Za-z0-9_\-]{50,}|'
    r'dop_v1_[a-f0-9]{64}|hf_[A-Za-z0-9]{30,}|gsk_[A-Za-z0-9]{40,}|r8_[A-Za-z0-9]{30,}|'
    r'apify_api_[A-Za-z0-9]{30,}|lin_api_[A-Za-z0-9]{30,}|shp(?:at|ss|ca)_[a-fA-F0-9]{32}|'
    r'cf(?:ut|at|k)_[A-Za-z0-9_\-]{30,}|'  # Cloudflare API-токены нового формата (28.09.2026)
    r'gl(?:sa|c)_[A-Za-z0-9_+/=\-]{30,}|'  # Grafana: токены SA (glsa_) и Cloud (glc_)
    r'SG\.[A-Za-z0-9_\-]{16,}\.[A-Za-z0-9_\-]{16,}|AGE-SECRET-KEY-1[0-9A-Z]{50,}|'
    r'eyJ[A-Za-z0-9_\-]{10,}\.eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}|'
    r'\d{8,10}:AA[A-Za-z0-9_\-]{33}))(?![A-Za-z0-9_\-])')

# Пароль в URL: scheme://user:PASSWORD@host и scheme://:PASSWORD@host (redis без имени — 25.09
# так утёк пароль Redis из Environment= unit-файла).
URL_CRED = re.compile(r'(?P<pre>[a-z][a-z0-9+.\-]*://[^\s:/@]*:)(?P<val>[^\s@/]{3,})(?=@)', re.I)

# Пароль в XML-теге (2026-07-14: <password><![CDATA[…]]></password> в override-default.xml).
XML_PW = re.compile(r'(?P<pre><(?P<tag>password|secret|token|access_key|secret_key)[^>]*>(?:<!\[CDATA\[)?)'
                    r'(?P<val>[^<\]\s][^<\]]*?)(?=(?:\]\]>)?</(?P=tag)>)', re.I)

PEM_BEGIN = re.compile(r'-----BEGIN [A-Z0-9 ]*PRIVATE KEY( BLOCK)?-----')
PEM = re.compile(r'-----BEGIN [A-Z0-9 ]*PRIVATE KEY( BLOCK)?-----.*?(?:-----END [A-Z0-9 ]*PRIVATE KEY( BLOCK)?-----|\Z)', re.S)

# Не значения, а ссылки, код или типы — их не трогаем.
NOT_A_VALUE = re.compile(
    r'^(?:\$\{.*|\$\(.*|\$[A-Za-z_]\w*|<.*|\{\{.*|%\(.*|‹secret.*|'
    r'(?:true|false|null|none|nil|yes|no|required|optional|string|str|bytes|int|bool|any|'
    r'secretstr|secretbytes|redacted|changeme|placeholder|example|xxx+|\*+|\.\.\.)|'
    r'[a-z_][a-z0-9_]*(?:\.[a-z_][a-z0-9_]*)+|'       # settings.bot.token — путь атрибутов
    r'_?[a-z]+(?:_[a-z]+)+|[a-z]+(?:[A-Z][a-z]+)+|'  # snake/camel идентификатор без цифр
    r'[a-z]+(?:[A-Z][a-z]+)+(?:\.[A-Za-z_]\w*)+|'    # apiKey.key — camel с атрибутом
    r'[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+|'                # ИМЯ_ПЕРЕМЕННОЙ, а не её значение
    r'(?:Optional|Union|Dict|List|Tuple|Callable|Mapping|Sequence)(?:\[.*)?|'
    r'(?:os\.environ|process\.env|getenv).*|from_env|env:.*|'
    r'(?:~|\.{1,2})?/[^\s]*|'                        # путь к файлу, а не значение
    r'sha256:.*|[A-Za-z0-9_\-]+(?:\.[A-Za-z_]\w*)*\(.*)$', re.I)
# Регистрозависимые части NOT_A_VALUE (при re.I «ИМЯ_ПЕРЕМЕННОЙ» съел бы и hex-секрет).
_CASE_ONLY = re.compile(r'^(?:[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+|[a-z]+(?:[A-Z][a-z]+)+(?:\.[A-Za-z_]\w*)*)$')
_NOT_CI = re.compile(NOT_A_VALUE.pattern.replace(
    r"[a-z]+(?:[A-Z][a-z]+)+(?:\.[A-Za-z_]\w*)+|", "").replace(r"[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+|", ""), re.I)


def not_a_value(v):
    return bool(_NOT_CI.match(v) or _CASE_ONLY.match(v))


DATE = re.compile(r'^\d{4}-\d{2}-\d{2}([T ][\d:.]+Z?([+-]\d{2}:?\d{2})?)?$')
UUID = re.compile(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$', re.I)


def looks_secret(v, min_len=12):
    """Значение похоже на секрет по форме (для слабых имён и strict-режима)."""
    if len(v) < min_len or not_a_value(v) or DATE.match(v):
        return False
    if re.fullmatch(r'\d+', v) or re.fullmatch(r'v?\d+(\.\d+)+([-+][\w.]+)?', v):
        return False
    if re.fullmatch(r'[0-9a-fA-F]{16,}', v) or UUID.match(v):
        return True
    classes = sum(bool(re.search(p, v)) for p in (r'[a-z]', r'[A-Z]', r'\d'))
    if re.search(r'[/:@]', v):
        # путь, образ, host:port — не секрет; сплошной base64 со слэшами — секрет
        return bool(re.fullmatch(r'[A-Za-z0-9+/]{20,}={0,2}', v)) and classes == 3
    if '.' in v and classes < 3 and re.fullmatch(r'[\w.\-]+', v):
        return False  # hostname, версия, имя файла
    return classes == 3 or (classes == 2 and bool(re.search(r'\d', v)) and len(v) >= 16)


STANDALONE = re.compile(r'(?<![A-Za-z0-9_+/=.\-])(?P<val>[A-Za-z0-9_+/.\-]{20,}={0,2})(?![A-Za-z0-9_+/=.\-])')

_found = None  # коллектор для --scan


def label(name, val):
    if _found is not None:
        _found.append((name, val))
    h = hashlib.sha256(val.encode()).hexdigest()[:12]
    return f"‹secret {name} sha256:{h} len:{len(val)}›"


def clean_name(raw):
    return raw.strip("\"'").lstrip('-') or "value"


# Ключи со «секретным» словом, которые секретом не являются: счётчики токенов LLM, автор
# коммита, параметр Google authuser.
NOT_SECRET_KEY = re.compile(r'^["\']?(?:[\w.\-]*?(?:max|input|output|total|prompt|completion|cache[\w]*|reasoning)_?tokens?'
                            r'|tokens?_(?:count|used|limit|type)|token_?type|author\w*|authuser|auth_?(?:type|method|mode|url)'
                            r'|[\w.\-]*?(?:cache|object|idempotency|dedup|sort|partition|primary|foreign|public|lookup|storage|s3|i18n|translation)_?key'
                            r'|[A-Z]+-\d+-[\w.\-]*|text\.[\w.]*'
                            r'|secret_?(?:name|ref|key_?ref)|(?:token|auth|authorization)_?(?:url|uri|endpoint)|password_?(?:file|policy))["\']?$', re.I)
FILE_KEY = re.compile(r'\.(?:py|pyi|ts|tsx|js|jsx|mjs|go|rb|sh|md|json|ya?ml|toml|txt|java|kt|swift|rs|c|h|cpp|php|sql|html?|css|lock|cfg|ini|conf|j2)$', re.I)


def _kv_sub(rx, text, check):
    def kv(m):
        val = m.group("qval") if m.group("qval") is not None else m.group("val")
        key = m.group("key")
        if FILE_KEY.search(key.strip("\"'")):
            # `grep -n` по файлу oauth.py: «oauth.py:12:…» — имя файла, а не ключ; значение
            # проверяем заново, чтобы секрет внутри строки не проскочил
            q = m.group("q") or ""
            return m.group("pre") + q + rx.sub(kv, val) + q
        if not check(val) or NOT_SECRET_KEY.match(key) or DATE.match(val):
            return m.group(0)
        if re.match(r'^[=<>~!]=?\s*v?\d', val):
            return m.group(0)  # pyjwt==2.8.0 — версия пакета
        if len(val.split()) >= 3 and not re.search(r'pass|pwd|phrase|mnemonic|seed', key, re.I):
            return m.group(0)  # фраза из нескольких слов (i18n, описание) у не-парольного ключа
        if val.isdigit() and not re.search(r'pass|pwd|pin|secret', key, re.I):
            return m.group(0)  # число у token/key — счётчик или id, а не значение
        q = m.group("q") or ""
        return m.group("pre") + q + label(clean_name(m.group("key")), val) + q
    return rx.sub(kv, text)


def _simple_sub(rx, text, name, check=lambda v: not not_a_value(v)):
    def f(m):
        val = m.group("val")
        if not check(val):
            return m.group(0)
        q = m.groupdict().get("q") or ""
        return m.group("pre") + q + label(name, val) + q
    return rx.sub(f, text)


# Дешёвый предфильтр: без хотя бы одного признака тяжёлые шаблоны не запускаем (мегабайтный
# вывод иначе стоит десятков секунд). Каждый шаблон выше содержит хотя бы один из признаков.
# Подстроки через `in`, а не регулярка с re.I: в 20 раз быстрее на больших выводах.
_Q_LOWER = ("pass", "pwd", "secret", "token", "key", "auth", "bearer", "basic", "cookie", "dsn", "jwt",
            "cred", "hash", "sig", "salt", "seed", "mnemonic", "session", "://", "begin", "sk-", "_live_",
            "_test_", "whsec", "ntn_", "ghp_", "gho_", "ghu_", "ghs_", "ghr_", "github_pat", "glpat", "glptt",
            "gldt", "glrt", "glcbt", "glsa_", "glc_", "xox", "xapp", "hvs.", "hvb.", "hvr.", "y0_", "t1.", "aqvn", "ycm", "ycp",
            "akia", "asia", "aiza", "ya29", "gocspx", "npm_", "pypi-", "dop_v1", "hf_", "gsk_", "r8_", "apify",
            "lin_api", "shp", "sg.", "cfut_", "cfat_", "cfk_", "age-secret", "eyj", ":aa", "machine", "redis-cli", "mysql", "sshpass",
            "-u ", "--user")
_PGPASS = re.compile(r'(?m)^[^:\s#]+:(?:\d+|\*):[^:\s]+:[^:\s]+:\S')


def quick(text):
    low = text.lower()
    return any(k in low for k in _Q_LOWER) or bool(_PGPASS.search(text))


class _Quick:  # совместимость: сканер вызывает sm._QUICK.search(s)
    search = staticmethod(quick)


_QUICK = _Quick()


def mask(text, strict=False):
    if not strict and not quick(text):
        return text
    if strict:
        text = text.replace("\0", "\n")
    text = PEM.sub(lambda m: label("PRIVATE_KEY", m.group(0)), text)
    text = _kv_sub(KV, text, lambda v: not not_a_value(v))
    text = _kv_sub(KV_WEAK, text, lambda v: looks_secret(v, 8))
    text = _simple_sub(FLAG, text, "FLAG_SECRET")
    for rx in TOOLFLAG:
        text = _simple_sub(rx, text, "CLI_PASSWORD", lambda v: not v.startswith(('$', '‹')))
    text = _simple_sub(XML_PW, text, "XML_PASSWORD", lambda v: not not_a_value(v))
    text = URL_CRED.sub(lambda m: m.group("pre") + label("URL_PASSWORD", m.group("val"))
                        if not m.group("val").startswith(('$', '‹')) else m.group(0), text)
    text = _simple_sub(URL_Q, text, "URL_PARAM", lambda v: not v.startswith(('$', '‹', '{')) and looks_secret(v, 8))
    text = _simple_sub(BEARER, text, "BEARER", lambda v: not v.startswith('‹'))
    text = PREFIXED.sub(lambda m: label("TOKEN", m.group("val")), text)
    if strict:
        text = _kv_sub(KV_ANY, text, looks_secret)
        text = STANDALONE.sub(lambda m: label("HIGH_ENTROPY", m.group("val"))
                              if looks_secret(m.group("val"), 20) else m.group(0), text)
    return text


def mask_whole_line(line):
    """--whole: строка без пробелов — это и есть значение."""
    body = line.rstrip("\r\n")
    tail = line[len(body):]
    s = body.strip()
    if s and " " not in s and len(s) >= 4 and not s.startswith("‹secret") and not re.fullmatch(r'[=\-#*_ ]+', s):
        return body[:len(body) - len(body.lstrip())] + label("VALUE", s) + tail
    return mask(line, strict=True)


def stream(mode):
    buf = []
    out = sys.stdout
    fn = mask_whole_line if mode == "whole" else (lambda s: mask(s, strict=(mode == "strict")))
    for line in sys.stdin:
        if buf:
            buf.append(line)
            if "-----END" in line and "PRIVATE KEY" in line:
                out.write(fn("".join(buf)))
                out.flush()
                buf = []
            continue
        if PEM_BEGIN.search(line) and not re.search(r'-----END [A-Z0-9 ]*PRIVATE KEY', line):
            buf.append(line)
            continue
        out.write(fn(line))
        out.flush()
    if buf:
        out.write(fn("".join(buf)))


def scan(text):
    """Только метки найденного — для сканера транскриптов."""
    global _found
    _found = []
    mask(text)
    res, _found = _found, None
    return res


def selftest():
    import secrets as rnd
    import string
    alnum = string.ascii_letters + string.digits

    def r(n, alpha=alnum):
        s = "".join(rnd.choice(alpha) for _ in range(n))
        # гарантируем все три класса символов, иначе «похоже на секрет» может не сработать
        return "aZ9" + s[3:] if n > 3 else s
    hexs = lambda n: rnd.token_hex(n // 2)
    fake = "ntn_" + r(45)
    pem = "-----BEGIN OPENSSH PRIVATE" + " KEY-----\n" + r(64) + "\n" + r(64) + "\n-----END OPENSSH PRIVATE" + " KEY-----"
    cases = [  # (строка, режим, должна ли остаться как есть)
        (f"NOTION_TOKEN={fake}", "", False),
        (f"    NOTION_TOKEN: {fake}", "", False),
        (f'  "api_key": "{r(24)}"', "", False),
        ("Authorization: Bearer " + r(30), "", False),
        ("postgres://app:" + r(12) + "@db:5432/x", "", False),
        ("Environment=REDIS_URL=redis://:" + hexs(24) + "@127.0.0.1:6379", "", False),
        ("https://mcp.example.ai/mcp?exaApiKey=" + str(__import__("uuid").uuid4()) + "&x=1", "", False),
        ("https://maps.example.com/api?key=" + r(39) + "&q=1", "", False),
        ("TELEGRAM_API_HASH: " + hexs(32), "", False),
        ('password: "' + r(8) + ' horse ' + r(8) + '"', "", False),
        ("root  123  python3 app.py --api-key " + r(32) + " --port 80", "", False),
        ("redis-cli -h 10.0.0.1 -a " + r(20) + " ping", "", False),
        ("mysql -uroot -p" + r(14) + " -e 'select 1'", "", False),
        ("curl -u admin:" + r(14) + " https://x/api", "", False),
        ("machine api.example.com login bot password " + r(20), "", False),
        ("<password><![CDATA[" + r(16) + "]]></password>", "", False),
        ("db.example.test:5432:app:app:" + r(16), "", False),
        ("root:x:0:0:root:/root:/bin/bash", "", True),
        ("OPENROUTER=sk-or-v1-" + hexs(64), "", False),
        ("PAYMENT_API_KEY=" + r(40), "", False),
        ("STRIPE=sk_live_" + r(24), "", False),
        ("apify_api_" + r(36), "", False),
        ("CLOUDFLARE: cfut_" + r(48), "", False),
        ("export GRAFANA_API=glsa_" + r(32) + "_" + hexs(8), "", False),
        ("bash -c source snap\012export GRAFANA_API=glsa_" + r(32) + "_" + hexs(8) + "\012: &&", "", False),
        ("AGE-SECRET-KEY-1" + r(58, string.ascii_uppercase + string.digits).upper(), "", False),
        ("export YC_TOKEN=t1." + r(60) + "." + r(40), "", False),
        (pem, "", False),
        # strict: имя ключа ничего не говорит, значение — похоже на секрет
        ("CACHE_STATUS_CH=" + r(20), "strict", False),
        ("  DB_CONN: " + r(24), "strict", False),
        ("PATH=/usr/local/bin:/usr/bin" + "\0" + "HOME_X=" + r(22), "strict", False),
        ("node  1234  /usr/bin/node server.js " + r(40), "strict", False),
        # whole: одно значение на строке
        (r(18), "whole", False),
        # не трогать
        ("NOTION_TOKEN: ${NOTION_TOKEN}", "", True),
        ("def f(access_token: Optional[str]) -> None:", "", True),
        ("token = settings.bot.admin_token", "", True),
        ("password: required", "", True),
        ("model: gpt-6-luna", "", True),
        ("db_password_file = ~/.db_pass", "", True),
        ('apiKey: apiKey.key', "", True),
        ("api_key = _get_provider_key()", "", True),
        ("token: APP_BOT_TOKEN", "", True),
        ("CACHE_KEY=user_profile", "", True),
        ("ig_cache_key: " + hexs(32), "", True),
        ("pyjwt==2.8.0", "", True),
        ('"text.apiKeyCreate": "Создать новый API-ключ для бота"', "", True),
        ("docker run -u 1000:1000 alpine id", "", True),
        ("  TICKET-123-preprod-secret-visibility: merged", "", True),
        ('password: "' + r(6) + ' ' + r(6) + ' ' + r(6) + '"', "", False),
        ('"input_tokens": 1234567, "max_tokens": 32000', "", True),
        ("Author: Jane Doe <jane@example.com>", "", True),
        ("src/oauth.py:12:def refresh(client):", "", True),
        ("DB_PASSWORD=\nCLOUDFLARE_API_TOKEN=\nPG_PASSWORD=", "", True),
        ("api_key:\n  - PAYMENT_API_KEY_PRIMARY", "", True),
        ("src/oauth.py:14:API_TOKEN=" + r(30), "", False),
        ("      secretName: app-secrets", "", True),
        ('"authored_date": "2026-09-28T10:00:00.000+03:00"', "", True),
        ("password=" + "".join(rnd.choice(string.digits) for _ in range(8)), "", False),
        ("password = self.redis_password.get_secret_value() if x else None", "", True),
        ("PATH=/usr/local/bin:/usr/bin:/bin", "strict", True),
        ("IMAGE=ghcr.io/acme/app:1.2.3", "strict", True),
        ("STARTED_AT=2026-09-28T10:00:00Z", "strict", True),
        ("VERSION=0.9.3", "strict", True),
        ("HOST=db.internal.example.com", "strict", True),
        ("ok 12 passed in 3.2s", "strict", True),
        ("ssh -i ~/.ssh/key --private-key /etc/ssl/k.pem host", "", True),
        ("=== sshd config ===", "whole", True),
    ]
    bad = 0
    for line, mode, keep in cases:
        out = mask_whole_line(line) if mode == "whole" else mask(line, strict=(mode == "strict"))
        ok = (out == line.replace("\0", "\n") if mode == "strict" else out == line) if keep else ("‹secret" in out)
        # значение не должно пережить маску ни в каком виде
        leaked = False
        if not keep:
            for tok in re.findall(r"[A-Za-z0-9_+/=\-]{12,}", line):
                if re.search(r"[0-9]", tok) and re.search(r"[A-Za-z]", tok) and tok in out:
                    leaked = True
        if not ok or leaked:
            bad += 1
            print("FAIL:", mode or "normal", re.sub(r"[A-Za-z0-9]", "a", line)[:70].replace("\0", "\\0"),
                  "(leaked)" if leaked else "")
    print("selftest OK (%d cases)" % len(cases) if not bad else f"selftest FAIL: {bad}/{len(cases)}")
    return bad


if __name__ == "__main__":
    args = sys.argv[1:]
    if args == ["--selftest"]:
        sys.exit(selftest())
    if args == ["--scan"]:
        for name, val in scan(sys.stdin.read()):
            print(f"{name}\tsha256:{hashlib.sha256(val.encode()).hexdigest()[:12]}\tlen:{len(val)}")
        sys.exit(0)
    mode = "strict" if "--strict" in args else "whole" if "--whole" in args else "normal"
    try:
        stream(mode)
    except BrokenPipeError:  # `| head` закрыл трубу — это не ошибка
        sys.exit(0)
