# Session Launcher AGENTS.md

## 项目概述
Session 生态的**执行层**——CCS 创建、生命周期管理、跨 session 协作基础设施。
依赖 hermes-session-roles（角色定义）和 session-pipeline（消息路由）。

## 整体架构
```
hermes-session-roles (定义层) → 读取角色 JSON
        │
        ▼
  session-launcher (执行层) ← 本项目
        │
        ├──→ Sister Bus (SQLite) + Feed Push (Unix Socket)
        └──→ session-pipeline (路由层)
```

**铁律：修改本项目时必须同时考虑上下游影响。**

## Git 工作流
1. 禁止切换分支，始终在 main 分支工作
2. 小步提交，每完成一个逻辑单元立即 commit
3. 出错用新提交修复，不要 revert
4. 本地即生产环境

## 协作红线
1. 协作逻辑在代码层，不在 prompt 文本中
2. 创建方必须守护被创建方
3. 轮次追踪必须内置
4. 死锁超时 > 15 分钟 → 写入 bus 升级给人
5. 实时推送必须可用
6. **stdlib only** — 禁止 `import requests` 等第三方库
7. **禁止 eval()** — 使用安全的 regex 模式匹配替代
8. 路径使用 `Path.home()` 或环境变量，禁止 `/home/administrator` 硬编码

## 测试
```bash
python3 -m pytest tests/test_6dimension_deep_qa.py -v
python3 -m pytest tests/test_e2e_mock.py -v
python3 -m pytest tests/test_system_health.py -v
PYTHONPATH=src python3 -c "import core, tmux_ops, role_manager, codex_ops, sentinel; print('OK')"
```

---

## 自维护指令（Agent 按此执行）

### 1. 每次工作前：运行健康检查基线

```bash
python3 src/ecosystem_health.py --json > /tmp/health_before.json
```

记录修改前的基线，修改后对比。

### 2. 代码变更后：验证六维度

```
P0: 引入 crash 或数据丢失？          → 检查哨兵写入、tmux 进程、SQLite WAL
P1: 安全漏洞（注入/路径遍历/竞态）？  → grep eval()、exec()、os.system()
P2: 可维护性（重复/硬编码/长函数）？ → core.py 函数是否 >200 行？是否复制粘贴？
P3: 性能（N+1/无索引/阻塞）？         → SQLite 查询有无索引、subprocess timeout
P4: 一致性（风格/命名/返回格式）？    → JSON 返回是否统一 camelCase？
P5: 可测试性（可 mock/幂等）？         → 函数是否接受 db_path 参数？
```

### 3. 跨项目变更时：同时检查上下游

```bash
# 改角色定义 → 检查路由表同步
cd /home/administrator/session-pipeline && PYTHONPATH=src python3 -c \
  "from router import get_router; r = get_router(); print(len(r._routing))"

# 改路由逻辑 → 检查 CCS 启动正常
cd /home/administrator/session-launcher && python3 src/ccs.py health
```

### 4. 每周自进化

检查以下退化信号并修复：
- 测试数量是否减少（目标：每周增加而非减少）
- 是否有新的硬编码路径（`grep -r "/home/administrator" src/`）
- 是否有新的第三方依赖（`grep -r "^import \|^from " src/ | grep -v stdlib`）
- README 是否仍然准确（对比实际 CLI 输出）

### 5. 产出标准

每轮变更必须满足：
1. **外部痕迹** — 变更后文件存在，且可被 `git diff` 观察到
2. **需求端价值** — 变更解决了一个真实问题（bug/技术债/功能缺失）
3. **可复用杠杆** — 产出物可被其他模块或项目使用（非一次性）

---

## Browser Harness 关联
Browser Harness（57 人格）注册在 hermes-session-roles/personas/browser-harness/，
通过 `_bh_to_sr_map.json` 映射到 session 角色。

## 环境变量
| 变量 | 默认值 | 说明 |
|------|--------|------|
| SESSION_ROLES_ROOT | ~/hermes-session-roles | 角色定义目录 |
| SESSION_PIPELINE_SRC | ~/session-pipeline/src | Pipeline 源码目录 |
| HERMES_SCRIPTS_DIR | ~/.hermes/scripts | Hermes 脚本目录 |
| CCS_SOCKET_TOKEN | (无) | CCS Socket 认证令牌 |
