# Session Launcher 项目

## 项目概述
session 启动时的自动角色匹配 + prompt 注入 + 生命周期控制。
依赖 hermes-session-roles（项目 A）。

## 架构
```
src/
  launcher.py    → 启动逻辑：读 roles → 匹配角色 → 注入 prompt → 控制生命周期
  signals.py     → input_signals 检查：执行命令判断是否有活干
```

## 关键决策
- 依赖 hermes-session-roles 的 CLI 和数据文件
- 启动时读角色注册表，根据 session_hint 匹配
- 无匹配任务 → 退出（零 token 消耗）
- 有匹配 → 注入 prompt 到 CLAUDE.md 启动块

## Git 工作流（强制）

### 核心规则
1. **禁止切换分支** - 始终在 main 分支工作
2. **禁止创建分支** - 不使用 `git checkout -b` 或 `git branch`
3. **小步提交** - 每完成一个逻辑单元立即 commit
4. **出错用新提交修复** - 不要 revert，用新 commit 修复问题

### 为什么
- 本地即生产环境，切换分支会影响运行中的服务
- 多 AI 同时工作，分支切换会导致状态混乱
- 简化工作流，减少出错可能

### 违规处理
如果意外切换了分支，立即：
```bash
git checkout main
```
