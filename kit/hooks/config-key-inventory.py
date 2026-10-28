#!/usr/bin/env python3
"""Инвентаризация ИМЁН ключей типизированного конфига. Значения не печатаются никогда.

Единственная команда, которой guard-secret-file-read.sh разрешает касаться env/secret-файлов:
отвечает на «есть ли N8N_API_KEY?» и «какие параметры предусмотрены?», не пропуская значение
в транскрипт. Контракт (на нём завязан allowlist в hook — менять только вместе с ним):

    config-key-inventory.py <path>     имена в stdout, по одному в строке
    config-key-inventory.py --selftest самопроверка на синтетике (значения не должны утечь)

Заменяет прежнее исключение для `grep -o '^KEY='` в hook: у shell слишком много форм, в
которых та же команда печатает значение (правка 2026-07-21).

Правила вывода, ради которых это отдельный файл, а не однострочник:
  * в stdout попадают ТОЛЬКО имена — ни строк файла, ни значений, ни фрагментов;
  * в stderr — ТОЛЬКО текст без содержимого файла. Никогда str(exc): сообщения json/yaml/toml
    цитируют проблемную строку целиком, то есть значение (главная дыра такого инструмента);
  * имя, само похожее на секрет (длинное или с посторонними символами), заменяется на <omitted>.
"""
import json
import os
import re
import stat
import sys

MAX_BYTES = 256 * 1024   # больше — это уже не конфиг, а дамп
MAX_KEYS = 500           # лимит на число возвращаемых имён
MAX_NAME = 64            # длиннее — вероятно, само значение оказалось ключом
MAX_DEPTH = 12

NAME_OK = re.compile(r'^[A-Za-z0-9_.\-]+$')
ENV_KEY = re.compile(r'^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_-]*)\s*=')

# классы, которые не инвентаризуются в принципе: ключи, kubeconfig, state, хранилища паролей
DENY = re.compile(
    r'\.(pem|key|p12|pfx|jks|keystore|tfstate|ppk|asc|gpg)$'
    r'|\.tfstate(\.backup)?$'
    r'|(^|/)id_(rsa|ed25519|ecdsa|dsa)(\.|$)'
    r'|kubeconfig|(^|/)\.kube/|(^|/)\.ssh/|(^|/)\.gnupg/'
    r'|(^|/)\.htpasswd$|(^|/)\.pgpass$',
    re.I,
)


def fail(msg, code=2):
    print(f'config-key-inventory: {msg}', file=sys.stderr)
    raise SystemExit(code)


def kind(name):
    """Тип определяем по ИМЕНИ, не по содержимому: sniffing на секрет-файле — лишнее чтение."""
    if re.match(r'^\.env(\..+)?$', name) or name.endswith('.env'):
        return 'env'
    if name.endswith('.json'):          # включая .tfvars.json
        return 'json'
    if name.endswith(('.yaml', '.yml')):
        return 'yaml'
    if name.endswith('.toml'):
        return 'toml'
    if name.endswith('.tfvars'):        # HCL: присваивания верхнего уровня той же формы
        return 'env'
    return None


def safe(part):
    part = str(part)
    return part if len(part) <= MAX_NAME and NAME_OK.match(part) else '<omitted>'


def walk(node, prefix='', depth=0):
    """Пути вложенных полей: database.host, items[].name. Значения не трогаем."""
    if depth > MAX_DEPTH:
        return
    if isinstance(node, dict):
        for key, value in node.items():
            path = f'{prefix}.{safe(key)}' if prefix else safe(key)
            yield path
            yield from walk(value, path, depth + 1)
    elif isinstance(node, list):
        for item in node:
            yield from walk(item, prefix + '[]', depth + 1)


def parse_env(path):
    with open(path, encoding='utf-8', errors='replace') as fh:
        return [safe(m.group(1)) for m in map(ENV_KEY.match, fh) if m]


def parse_json(path):
    with open(path, encoding='utf-8', errors='replace') as fh:
        return list(walk(json.load(fh)))


def parse_yaml(path):
    try:
        import yaml
    except ImportError:
        fail('PyYAML недоступен — YAML разобрать нечем')
    with open(path, encoding='utf-8', errors='replace') as fh:
        names = []
        for doc in yaml.safe_load_all(fh):
            names.extend(walk(doc))
        return names


def parse_toml(path):
    try:
        import tomllib
    except ImportError:
        fail('tomllib недоступен (нужен python 3.11+)')
    with open(path, 'rb') as fh:
        return list(walk(tomllib.load(fh)))


PARSERS = {'env': parse_env, 'json': parse_json, 'yaml': parse_yaml, 'toml': parse_toml}


def inventory(raw):
    """Все отказы — до чтения содержимого."""
    path = os.path.abspath(raw)
    try:
        st = os.lstat(path)
    except OSError:
        fail('файл недоступен')
    # Симлинк ИМЕННО в последнем компоненте: только он способен подменить то, что мы прочитаем,
    # оставив «безопасное» имя (foo.env -> ~/.ssh/id_rsa). Симлинки в родительских каталогах не
    # проверяем — на macOS через них идут /tmp и /var, отказ по ним отверг бы легитимные пути.
    if stat.S_ISLNK(st.st_mode):
        fail('файл — симлинк, отказ (имя не гарантирует, куда он ведёт)')
    if not stat.S_ISREG(st.st_mode):
        fail('не обычный файл — отказ')
    if st.st_size > MAX_BYTES:
        fail(f'файл больше лимита {MAX_BYTES} Б — отказ')
    # DENY по обоим написаниям: запрещённый класс не должен прятаться за симлинком каталога
    if DENY.search(path) or DENY.search(os.path.realpath(path)):
        fail('запрещённый класс файла (приватный ключ / kubeconfig / state) — отказ')
    fmt = kind(os.path.basename(path))
    if fmt is None:
        fail('неподдерживаемый тип — нужен .env*, JSON, YAML, TOML или tfvars')
    try:
        return PARSERS[fmt](path)
    except SystemExit:
        raise
    except Exception as exc:
        # только класс исключения: текст сообщения цитирует содержимое файла
        fail(f'разбор не удался ({type(exc).__name__}) — вывод подавлен')


def main(argv):
    if len(argv) != 2:
        fail('нужен ровно один аргумент: путь к конфигу')
    if argv[1] == '--selftest':
        return selftest()
    names = sorted(set(inventory(argv[1])))
    if not names:
        print('config-key-inventory: имён не найдено', file=sys.stderr)
        return 0
    for name in names[:MAX_KEYS]:
        print(name)
    if len(names) > MAX_KEYS:
        print(f'config-key-inventory: обрезано — показано {MAX_KEYS} из {len(names)}',
              file=sys.stderr)
    return 0


def selftest():
    import tempfile
    secret = 'sk-liveVALUE0123456789'
    files = {
        '.env': f'# note\nexport N8N_API_KEY={secret}\nDB_PASSWORD={secret}\nBROKEN\n',
        'a.json': json.dumps({'database': {'host': 'h', 'password': secret}, 'items': [{'name': 'n'}]}),
        'a.toml': f'[database]\nhost = "h"\npassword = "{secret}"\n',
        'a.tfvars': f'region = "eu"\napi_token = "{secret}"\n',
    }
    with tempfile.TemporaryDirectory() as tmp:
        for name, body in files.items():
            with open(os.path.join(tmp, name), 'w') as fh:
                fh.write(body)
        got = {}
        for name in files:
            got[name] = sorted(set(inventory(os.path.join(tmp, name))))
            assert not any(secret in n for n in got[name]), f'значение утекло: {name}'
        assert got['.env'] == ['DB_PASSWORD', 'N8N_API_KEY'], got['.env']
        assert 'database.password' in got['a.json'] and 'items[].name' in got['a.json'], got['a.json']
        assert got['a.toml'] == ['database', 'database.host', 'database.password'], got['a.toml']
        assert got['a.tfvars'] == ['api_token', 'region'], got['a.tfvars']

        os.symlink(os.path.join(tmp, '.env'), os.path.join(tmp, 'link.env'))
        for bad in ['link.env', 'nope.env', 'a.pem', 'a.txt']:
            with open(os.path.join(tmp, 'a.pem'), 'w') as fh:
                fh.write('x')
            with open(os.path.join(tmp, 'a.txt'), 'w') as fh:
                fh.write('x')
            try:
                inventory(os.path.join(tmp, bad))
            except SystemExit:
                continue
            raise AssertionError(f'должен был отказать: {bad}')
    print('selftest: ok')
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
