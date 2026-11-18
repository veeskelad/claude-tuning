import subprocess, json, os, sys
hook = os.path.expanduser(sys.argv[1])
# таблица проверяет сам страж; набор ставит CLAUDE_GUARD_SKIP_SECRETS=1 (ключи там ловит маска), и с ним строки про .env и env не сошлись бы
env = {k: v for k, v in os.environ.items() if k != "CLAUDE_GUARD_SKIP_SECRETS"}
res = {"dangerous": 0, "ordinary": 0, "ok": 0, "miss": 0}
for line in open(sys.argv[2], encoding="utf-8"):
    exp, cmd = line.rstrip("\n").split("\t", 1)
    res["dangerous" if exp == "deny" else "ordinary"] += 1
    out = subprocess.run(["bash", hook], input=json.dumps({"tool_input": {"command": cmd}}),
                         capture_output=True, text=True, env=env).stdout
    got = "deny" if out.strip() else "allow"
    res["ok" if got == exp else "miss"] += 1
    if got != exp:
        print("MISS", exp, "|", cmd)
print(res)
