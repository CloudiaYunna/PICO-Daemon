# PICO Daemon

监控 PICO Connect / ps_server 进程状态并在异常时自动重启的 Windows 桌面工具。

> [!WARNING]
> **AI 声明**：本项目 100% 的代码由 AI 生成，未经人工逐行审查。请自行评估风险后再使用。

## 功能

- 定时按进程名精确检测 `ps_server.exe` 与 PICO Connect 是否运行
- 异常时声音提示（可选），自动结束并重启相关进程（含伴随进程 `pico_setting.exe`）
- 运行时自动捕获进程真实路径并写回 `config.json`，无需手动配置路径
- 系统托盘常驻，关闭窗口仅收纳到托盘；命名互斥量保证单实例
- MIUI/HyperOS 深色风格界面，设置自动保存、下次启动恢复

## 下载

从 [Releases](../../releases/latest) 下载打包好的 exe，开箱即用，无需安装 Python。

程序运行时会在**自身所在目录**自动生成 `config.json` 配置文件，用于保存界面设置与自动捕获的进程路径；删除该文件即恢复默认配置。

## 从源码运行

- Windows 10/11
- Python 3.8+

```bash
pip install customtkinter psutil
pip install pystray pillow   # 可选：启用系统托盘
python src/PICO_Daemon.py
```

启动后自动开始监测。检测间隔、重启冷却、声音开关等设置与进程路径均保存在脚本同目录的 `config.json`，可手动编辑（非法值会自动回退默认）。

## config.json 主要字段

| 字段 | 说明 | 默认值 |
| --- | --- | --- |
| `check_interval` | 检测间隔（秒） | `5` |
| `restart_cooldown` | 重启冷却（秒），防止反复重启 | `15` |
| `sound_enabled` | 异常时声音提示 | `false` |
| `auto_start` | 启动后自动开始监测 | `true` |
| `restart_only_missing` | `true` 只重启未运行的进程 | `false` |
| `ps_server` / `pico_connect` | 监控目标的进程名与回退路径 | 见脚本内默认值 |
| `companions` | 伴随进程列表，重启时一并结束/重启 | `pico_setting.exe` |

## 免责声明

本项目为个人开发的第三方工具，与 PICO、字节跳动（ByteDance）及其关联公司无任何关联，亦非官方产品。"PICO"、"PICO Connect" 等名称与商标归其各自所有者所有。使用本工具产生的一切后果由使用者自行承担。

## 许可证

见 [LICENSE](LICENSE)。
