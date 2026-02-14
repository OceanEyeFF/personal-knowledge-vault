# Legacy 脚本说明

这个目录包含旧的 venv 虚拟环境安装脚本喵～ ฅ'ω'ฅ

## ⚠️ 不推荐使用

这些脚本已被 **Conda 安装方案**取代，原因：
- Python 3.13 存在兼容性问题（`lxml`、`greenlet` 编译失败）
- Conda 方案环境隔离更彻底
- Conda 可以自动管理 Python 版本

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

**运行方式**:
```powershell
.\scripts\legacy\setup.ps1
```

**限制**:
- 需要 Python 3.11（不支持 3.13）
- 可能遇到编译错误

---

### 2. `setup.bat` - venv 安装脚本 (CMD/Batch)

**用途**: CMD 版本的 venv 安装脚本

**运行方式**:
```cmd
.\scripts\legacy\setup.bat
```

**限制**: 同 `setup.ps1`

---

### 3. `test.ps1` - venv 测试脚本

**用途**: 在 venv 环境中运行验证测试

**运行方式**:
```powershell
.\scripts\legacy\test.ps1
```

---

## 何时使用 Legacy 脚本？

**仅在以下情况使用**:
1. 无法安装 Conda
2. 明确使用 Python 3.11（不是 3.13）
3. 熟悉 venv 且愿意手动解决问题

**其他情况请使用 Conda 方案** ✅

---

*作者: 幽浮酱 ฅ'ω'ฅ*
*归档日期: 2026-02-14*
