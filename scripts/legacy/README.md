# Legacy 脚本说明

这个目录保留旧 venv 脚本的历史文件，供审计与迁移记录使用。

## ⚠️ 已封存，不能执行

setup.ps1、setup.bat 与 test.ps1 都会在任何配置、网络、数据根或
Provider 操作前以非零状态退出。它们不得作为开发、测试或安装入口。

这项 fence 避免历史脚本重新创建旧 .data 布局或写入旧的
config/local.yaml 合同。

## 推荐方案

请使用 Conda 安装脚本：
```powershell
.\scripts\setup-conda.ps1
```

详见: [scripts/README.md](../README.md)

---

## 📋 Legacy 脚本列表

### 1. `setup.ps1` - venv 安装脚本 (PowerShell)

**用途**: 使用 Python venv 创建虚拟环境

**状态**: 已封存；执行会 fail-closed。

**限制**:
- 需要 Python 3.11（不支持 3.13）
- 可能遇到编译错误

---

### 2. `setup.bat` - venv 安装脚本 (CMD/Batch)

**用途**: CMD 版本的 venv 安装脚本

**状态**: 已封存；执行会 fail-closed。

**限制**: 同 `setup.ps1`

---

### 3. `test.ps1` - venv 测试脚本

**用途**: 在 venv 环境中运行验证测试

**状态**: 已封存；执行会 fail-closed。测试请使用 scripts/run-test.ps1 与隔离 .data-test 根。

---

## 正确入口

这些历史文件均不可运行。安装请使用当前受支持的安装文档和脚本；默认测试
自动化使用 scripts/run-test.ps1，并指定隔离 .data-test 根。不得以历史文件
为理由恢复旧数据根、local.yaml 或网络安装行为。

---

*作者: 幽浮酱 ฅ'ω'ฅ*
*归档日期: 2026-02-14*
