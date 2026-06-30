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
