"""signal_parser — 标准化信号解析器。

从 signals.py 提取的统一解析层，职责：
1. 将 {type, spec, filter} 新格式信号分发到具体检查函数
2. 自动转换旧格式（{source, filter}）到新格式
3. 扩展类型支持: bus, shell, http, journalctl, memory, disk, custom

用法:
    from events.parser import parse_signal
    ok = parse_signal({"type": "bus", "spec": {"category": "security"}, "filter": ""})
"""

from __future__ import annotations
import subprocess
import os
import re
import urllib.request
import warnings
from pathlib import Path

from paths import BUS_CLIENT
BUS_CLIENT = str(BUS_CLIENT)


def _check_bus(spec: dict, filter_str: str, timeout: int = 10) -> bool:
    """检查 bus 某分类是否有未读消息。"""
    category = spec.get("category", "")
    if not category:
        return False
    try:
        cmd = ["python3", BUS_CLIENT, "unread"]
        cmd.extend(["--cat", category])
        cmd.extend(["--limit", "1", "--json"])
        r = subprocess.run(cmd,
            capture_output=True, text=True, timeout=timeout,
        )
        import json
        data = json.loads(r.stdout) if r.stdout else {}
        facts = data if isinstance(data, list) else data.get("facts", [])
        if not facts:
            return False
        if filter_str:
            for f in facts:
                if re.search(filter_str, f.get("title", ""), re.IGNORECASE):
                    return True
            return False
        return True
    except Exception:
        return False


def _check_shell(spec: dict, filter_str: str, timeout: int = 10) -> bool:
    """执行 shell 命令检查输出。

    shell=True 用于支持管道/重定向等 bash 结构。
    命令来源为 persona JSON（预定义模板），不涉及用户输入。
    """
    cmd = spec.get("command", "")
    if not cmd:
        return False
    # 安全约束：只允许只读操作
    # shlex.split 提取命令名做白名单检查，但允许 shell 管道/重定向
    # 安全约束：所有检查在规范化后的字符串上执行
    _normalized = cmd.replace("${", "").replace("$((", "").replace("$(", "").replace("`", "")
    # 若原 cmd 含 shell 执行符($(`)但规范化后变了 → 拒绝
    if _normalized != cmd:
        return False
    _danger_words = ["rm ", "mkfs", "| tee", "chmod", "chown",
                     "format", "fdisk", "mke2fs", "shred ", "wipefs",
                     "sed -i", "sed --in-place", "awk -i inplace", "awk --in-place",
                     "curl --output", "curl -o ", "curl -O ",
                     "wget --output-document", "wget -O ",
                     "wget -o ", "wget --output-file ", "-delete ", "-exec "]
    if any(block in _normalized for block in _danger_words):
        return False
    # 单词边界检查 dd
    if re.search(r'(?<!\w)dd(?!\w)', _normalized):
        return False
    # shell 重定向检查：允许 fd 重定向（如 2>&1 只读合并 stderr→stdout），块文件写入
    _reduced = re.sub(r'\d+>&\d+', '', _normalized)
    if re.search(r'(?<![=<>])>(?![=])', _reduced):
        return False
    # 禁 shell 控制运算符（; && ||），允许管道 |
    if re.search(r'(?<![|&\\]);|&&|\|\|', _normalized):
        return False
    import shlex as _shlex
    try:
        parts = _shlex.split(cmd)
    except ValueError:
        return False
    if not parts:
        return False
    _safe_cmds = {"cat", "grep", "ls", "head", "tail", "wc", "sort", "uniq", "cut",
                  "find", "test", "[", "echo", "printf", "date", "which", "whoami",
                  "ps", "stat", "df", "du", "free", "id", "pgrep", "systemctl",
                  "journalctl", "awk", "sed", "diff", "comm", "md5sum",
                  "sha256sum", "curl", "wget"}
    # 检查所有管道段命令均在白名单中（防止 cp | rm 等单段检查绕过）
    pipeline_cmds = cmd.split("|")
    for seg in pipeline_cmds:
        try:
            seg_parts = _shlex.split(seg.strip())
        except ValueError:
            return False
        if not seg_parts:
            continue
        seg_name = os.path.basename(seg_parts[0])
        if seg_name not in _safe_cmds:
            return False
        # 禁止白名单命令中使用 -o/--output/--in-place/inplace 等写参数
        for arg in seg_parts[1:]:
            if arg in ("-o", "--output", "-O", "--output-document",
                       "-i", "--in-place", "inplace"):
                return False
    try:
        r = subprocess.run(parts, capture_output=True, text=True, timeout=timeout)
        if r.returncode != 0:
            return False
        if filter_str:
            return bool(re.search(filter_str, r.stdout, re.IGNORECASE))
        return True
    except Exception:
        return False


def _check_http(spec: dict, filter_str: str, timeout: int = 10) -> bool:
    """检查 HTTP 端点是否可达。"""
    url = spec.get("url", "")
    if not url:
        return False
    try:
        r = urllib.request.urlopen(url, timeout=timeout)
        body = r.read().decode("utf-8", errors="replace")
        if filter_str:
            return re.search(filter_str, body, re.IGNORECASE) is not None
        return r.status == 200
    except Exception:
        return False


def _check_journalctl(spec: dict, filter_str: str, timeout: int = 10) -> bool:
    """检查 journalctl 日志是否有匹配行。"""
    unit = spec.get("unit", "")
    since = spec.get("since", "1h")
    cmd = ["journalctl", "--user", "-u", unit, "--since", since, "--no-pager"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if filter_str:
            return bool(re.search(filter_str, r.stdout, re.IGNORECASE))
        return bool(r.stdout.strip())
    except Exception:
        return False


def _check_memory(spec: dict, filter_str: str = "", timeout: int = 10) -> bool:
    """检查可用内存是否低于阈值（默认 500 MB）。"""
    threshold_mb = int(spec.get("threshold_mb", 500))
    try:
        r = subprocess.run(
            ["free", "-m"], capture_output=True, text=True, timeout=5
        )
        # 解析 free -m 输出：MemAvailable 列（最后一列）
        for line in r.stdout.splitlines():
            if line.startswith("Mem:"):
                parts = line.split()
                available = int(parts[-1]) if len(parts) >= 7 else int(parts[3])
                return available < threshold_mb
        return False
    except Exception:
        return False


def _check_disk(spec: dict, filter_str: str = "", timeout: int = 10) -> bool:
    """检查磁盘使用是否超过阈值。"""
    threshold_pct = int(spec.get("threshold_pct", 90))
    try:
        r = subprocess.run(
            ["df", "--output=pcent", "/"], capture_output=True, text=True, timeout=5
        )
        m = re.search(r"(\d+)%", r.stdout)
        if m:
            pct = int(m.group(1))
            return pct >= threshold_pct
        return False
    except Exception:
        return False


def _check_custom(spec: dict, filter_str: str) -> bool:
    """自定义信号：用户指令触发，默认返回 True。"""
    return True


# ── 旧格式兼容 ──

_LEGACY_CHECKERS = {}


def _legacy(source: str) -> str | None:
    """检测旧格式 source 类型。"""
    if "bus_client.py" in source:
        return "bus"
    if "systemctl" in source:
        return "shell"
    if "curl" in source or "http" in source:
        return "http"
    if "journalctl" in source:
        return "journalctl"
    if "diff --cached" in source or "git diff" in source:
        return "git"
    return None


def _check_signal_legacy(signal_def: dict) -> bool:
    """旧格式 {source, filter} 兼容检查。"""
    source = signal_def.get("source", "")
    filter_str = signal_def.get("filter", "")
    sig_type = _legacy(source)
    if sig_type == "bus":
        # 解析 --cat <name>（空格分隔）或 --cat=<name>（等号分隔）
        if "--cat " in source:
            cat = source.split("--cat ")[-1].split()[0]
        elif "cat=" in source:
            cat = source.split("cat=")[-1].split()[0]
        else:
            cat = ""  # --all 或无分类→检查所有
        return _check_bus({"category": cat}, filter_str)

    # 非 bus 旧格式 → 委托给旧 checker（保持语义兼容）
    from events.signals import check_signal_by_name
    if source.startswith("systemctl") and "is-active" in source:
        return check_signal_by_name("systemctl_active", filter_str)
    if source.startswith("curl "):
        return check_signal_by_name("http_health", filter_str)
    if source.startswith("journalctl"):
        return check_signal_by_name("journalctl_errors", filter_str)
    if "git diff" in source:
        return check_signal_by_name("git_staged", filter_str)
    if "meminfo" in source or source.startswith("df "):
        return check_signal_by_name("mem_disk", filter_str)
    if source.startswith("ls"):
        return check_signal_by_name("session_size", filter_str)
    if source.startswith("ps aux"):
        return check_signal_by_name("running_sessions", filter_str)

    # 兜底：交给 _check_shell
    return _check_shell({"command": source}, filter_str)


def parse_signal(signal_def: dict) -> bool:
    """统一解析信号定义，返回是否有待处理工作。

    支持类型: bus|shell|http|journalctl|memory|disk|custom
    旧格式 ({source, filter}) 自动转换。
    """
    # 旧格式转换
    if "source" in signal_def and "type" not in signal_def:
        warnings.warn(
            "input_signals 使用旧格式 {'source': '...'}，"
            "请迁移到新格式 {'type': 'bus|shell|http|journalctl|custom', 'spec': {...}}。"
            "旧格式将在 6 个月后移除。",
            DeprecationWarning, stacklevel=2
        )
        return _check_signal_legacy(signal_def)

    sig_type = signal_def.get("type", "")
    spec = signal_def.get("spec", {})
    filter_str = signal_def.get("filter", "")
    timeout = signal_def.get("timeout_sec", 10)

    try:
        if sig_type == "bus":
            return _check_bus(spec, filter_str, timeout)
        elif sig_type == "shell":
            return _check_shell(spec, filter_str, timeout)
        elif sig_type == "http":
            return _check_http(spec, filter_str, timeout)
        elif sig_type == "journalctl":
            return _check_journalctl(spec, filter_str, timeout)
        elif sig_type == "memory":
            return _check_memory(spec, filter_str, timeout)
        elif sig_type == "disk":
            return _check_disk(spec, filter_str, timeout)
        elif sig_type == "custom":
            return _check_custom(spec, filter_str)
        return False
    except Exception:
        return False
