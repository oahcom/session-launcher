#!/usr/bin/env bash
# install_shims.sh — 在 ~/.local/bin 安装角色会调用但系统未提供的命令 shim。
#
# 背景：角色跑在 tmux non-login shell（PATH=~/.local/bin:/usr/local/bin:/usr/bin:/bin），
# 不读 ~/.bash_aliases。prompt（prompts/base.md §9.3）教了 ccs-status / ccs-ls /
# ccs-stop / ccs-out / ccs-health 等别名，角色照做必然 command not found
# （实测 34 次 ccs-status 全部失败）。同理 bus_client.py 裸名与 bus_unread。
#
# 幂等：重复执行只覆盖同名 shim，不影响其他文件。

set -euo pipefail

BIN="${HOME}/.local/bin"
# 由脚本自身位置推导仓库根，仓库换路径后仍可用
LAUNCHER="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BUS="${HOME}/.hermes/scripts/bus_client.py"

mkdir -p "$BIN"

# ccs-<sub>: 转发到 ccs.py 对应子命令
for spec in "ccs-status:status" "ccs-stop:stop" "ccs-out:output" "ccs-health:health"; do
    name="${spec%%:*}"
    sub="${spec##*:}"
    cat > "${BIN}/${name}" <<EOF
#!/usr/bin/env bash
exec python3 ${LAUNCHER}/src/ccs.py ${sub} "\$@"
EOF
    chmod +x "${BIN}/${name}"
done

# ccs-ls 无对应子命令，就地实现
cat > "${BIN}/ccs-ls" <<'EOF'
#!/usr/bin/env bash
tmux ls 2>/dev/null | grep ccs- || echo 'no ccs sessions'
EOF
chmod +x "${BIN}/ccs-ls"

# bus_client.py 裸名：runpy 转发，保留 __file__ 指向真实目录
# （symlink 不可用 —— bus_client.py 用 dirname(abspath(__file__)) 找 bus_protocol，
#   symlink 会解析到 ~/.local/bin 而找不到依赖）
if [ -f "$BUS" ]; then
    cat > "${BIN}/bus_client.py" <<'EOF'
#!/usr/bin/env python3
"""裸名调用 shim：转发到 ~/.hermes/scripts/bus_client.py。"""
import os
import runpy
import sys

_real = os.path.join(os.path.expanduser("~"), ".hermes", "scripts", "bus_client.py")
sys.argv[0] = _real
runpy.run_path(_real, run_name="__main__")
EOF
    chmod +x "${BIN}/bus_client.py"

    # bus_unread：角色自造的别名，等价于 unread 子命令
    cat > "${BIN}/bus_unread" <<EOF
#!/usr/bin/env bash
exec python3 ${BUS} unread "\$@"
EOF
    chmod +x "${BIN}/bus_unread"

    # bus_search：prompt(writer/scout) 教但 PATH 不存在 → command not found
    # 空查询会让底层 FTS5 抛 syntax error，把 traceback 甩给角色，这里挡住
    cat > "${BIN}/bus_search" <<'EOF'
#!/usr/bin/env python3
"""bus_search shim —— 转发 search 子命令，并挡住空/空白查询。

空查询会让底层 FTS5 抛 sqlite3.OperationalError: fts5: syntax error near ""
→ Python traceback 直接甩给角色（实测 rc=1），角色无从判断是自己用错还是
系统坏了。
"""
import os
import runpy
import sys

_BUS = os.path.join(os.path.expanduser("~"), ".hermes", "scripts", "bus_client.py")

def main():
    argv = sys.argv[1:]
    if not argv or not any(not a.startswith("-") for a in argv):
        sys.stderr.write('用法: bus_search "关键词" [--limit N]\n')
        raise SystemExit(2)
    _q = next(a for a in argv if not a.startswith("-"))
    if not _q.strip():
        sys.stderr.write('错误: 搜索关键词不能为空\n')
        raise SystemExit(2)
    sys.argv = [_BUS, "search"] + argv
    runpy.run_path(_BUS, run_name="__main__")

main()
EOF
    chmod +x "${BIN}/bus_search"

    # bus_write / bus_read：prompt 教的语法（第 3 个位置参数 / 位置式分类）
    # 与 bus_client 真实签名（write cat text、read 无位置参数）不兼容 ——
    # 角色 transcript 实测 201 次 unrecognized arguments。用 python shim 归一化。
    # 此前这两个 shim 在 ~/.local/bin 手工创建、未纳入本脚本，重装即丢。
    cat > "${BIN}/bus_write" <<'EOF'
#!/usr/bin/env python3
"""bus_write shim —— 归一化 prompt 教的写法到 bus_client.py 的实际签名。

角色 prompt 教 `bus_write <分类> "标题" "内容"`，但 write 只收 2 个位置参数，
第 3 个必然 argparse 报错。本 shim 把多余的位置参数映射为 --evidence，并默认
--src $CCS_ROLE（此前裸转发不填 src，角色消息归属丢失为 claude）。
"""
import os
import runpy
import sys

_BUS = os.path.join(os.path.expanduser("~"), ".hermes", "scripts", "bus_client.py")
_VALUE_FLAGS = {"--evidence", "-e", "--src", "-s", "--tags", "-t", "--poignancy", "-p"}

def main():
    argv, positional, flags, i = sys.argv[1:], [], [], 0
    while i < len(argv):
        a = argv[i]
        if a in _VALUE_FLAGS:
            flags.append(a)
            if i + 1 < len(argv):
                flags.append(argv[i + 1]); i += 2; continue
            i += 1; continue
        if a.startswith("-"):
            flags.append(a); i += 1; continue
        positional.append(a); i += 1

    if not positional:
        sys.stderr.write('用法: bus_write <分类> "标题" ["内容"] [--src 角色]\n')
        raise SystemExit(2)
    # 标题必填且非空：bus_client 按标题 UPSERT，空标题会命中已有空标题行并**覆盖**
    # 其 cat/src/evidence。实测 2026-10-19 一条空标题写入清空了 #395562 原内容。
    if len(positional) < 2 or not positional[1].strip():
        sys.stderr.write('错误: 标题不能为空（bus 按标题去重，空标题会覆盖已有同标题消息）\n')
        raise SystemExit(2)

    out = ["write", positional[0], positional[1]] + flags
    content = " ".join(positional[2:])
    if content:
        if "--evidence" not in flags and "-e" not in flags:
            out += ["--evidence", content]
        else:
            out[2] = f"{out[2]} {content}"  # 并入标题，不静默丢弃
    if "--src" not in flags and "-s" not in flags:
        out += ["--src", os.environ.get("CCS_ROLE", "claude")]

    sys.argv = [_BUS] + out
    runpy.run_path(_BUS, run_name="__main__")

main()
EOF
    chmod +x "${BIN}/bus_write"

    cat > "${BIN}/bus_read" <<'EOF'
#!/usr/bin/env python3
"""bus_read shim —— 归一化 prompt 教的写法到 bus_client.py 的实际签名。

角色 prompt 教 `bus_read <分类> --limit 5` 或 `bus_read <分类> <数量>`，
但 read 只接受 --cat（无位置参数），实测角色 421 次失败。
本 shim 把首个裸位置参数映射为 --cat，纯数字映射为 --limit。
"""
import os
import runpy
import sys

_BUS = os.path.join(os.path.expanduser("~"), ".hermes", "scripts", "bus_client.py")
_VALUE_FLAGS = {"--cat", "--limit", "--consumer"}

def main():
    argv, positional, flags, i = sys.argv[1:], [], [], 0
    while i < len(argv):
        a = argv[i]
        if a in _VALUE_FLAGS:
            flags.append(a)
            if i + 1 < len(argv):
                flags.append(argv[i + 1]); i += 2; continue
            i += 1; continue
        if a.startswith("-"):
            flags.append(a); i += 1; continue
        positional.append(a); i += 1

    # 分类名优先分配：纯数字才当 limit。若两个裸参数里有一个是数字、另一个不是，
    # 无论先后都各自归位 —— 曾把 `bus_read 3 code_fix` 解析成 limit=3 无分类，
    # 静默返回全库结果（`--cat` 被数字占位吃掉）。分类名是字母/下划线，绝不与
    # 数字混淆，故按类型分配无歧义。
    extra = []
    if "--cat" not in flags and positional:
        _cat = next((p for p in positional if not p.isdigit()), None)
        if _cat:
            extra += ["--cat", _cat]
    if "--limit" not in flags:
        _lim = next((p for p in positional if p.isdigit()), None)
        if _lim:
            extra += ["--limit", _lim]

    sys.argv = [_BUS, "read"] + flags + extra
    runpy.run_path(_BUS, run_name="__main__")

main()
EOF
    chmod +x "${BIN}/bus_read"
fi

echo "shims installed into ${BIN}:"
ls -1 "${BIN}"/ccs-status "${BIN}"/ccs-ls "${BIN}"/ccs-stop \
      "${BIN}"/ccs-out "${BIN}"/ccs-health "${BIN}"/bus_client.py \
      "${BIN}"/bus_unread 2>/dev/null