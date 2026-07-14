"""CCS重生器 — 从 tmux 检测死掉的 CCS 并自动重启。
与ecosystem_self_heal.py配合使用: 它负责检测, 本脚本负责重启。
无哨兵文件依赖——运行角色列表硬编码，存活检测用 tmux。

用法: python3 scripts/ccs_resurrector.py [--dry-run]
"""
import subprocess, sys, time
from pathlib import Path

LAUNCHER = Path.home() / "session-launcher" / "src" / "ccs.py"
CORE_ROLES = ["maintainer", "scout", "coordinator", "curator", "debate_verifier",
              "knowledge_curator", "consumer", "test3", "engineer", "pg", "pm",
              "qa", "reviewer", "lr"]

def resurrect():
    dry_run = "--dry-run" in sys.argv
    revived = 0

    for role in CORE_ROLES:
        tmux_name = f"ccs-{role}"
        alive = subprocess.run(["tmux", "has-session", "-t", tmux_name],
                               capture_output=True, timeout=3).returncode == 0
        if alive:
            continue

        print(f"❌ {role}: CCS已死, ", end="")
        if dry_run:
            print("[dry-run 跳过重启]")
        else:
            r = subprocess.run([sys.executable, str(LAUNCHER), "start", role, "--no-attach"],
                             capture_output=True, text=True, timeout=15)
            if r.returncode == 0:
                print(f"✅ 已重生")
            else:
                print(f"⚠️ 重启失败: {r.stderr[:100]}")
            revived += 1

    if revived == 0 and not dry_run:
        print("✅ 所有infinite CCS存活, 无需重生")
    return revived

if __name__ == "__main__":
    print(f"[{time.strftime('%H:%M:%S')}] CCS重生器启动")
    n = resurrect()
    print(f"结果: {n} 个CCS已重生")
