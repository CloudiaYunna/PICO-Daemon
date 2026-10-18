#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PICO Daemon -- PICO Connect / ps_server 监控与自愈工具
-------------------------------------------------
功能:
  1. 每隔 N 秒按进程名精确匹配(不区分大小写, 可省略 .exe 后缀)检测
     ps_server.exe 与 PICO Connect 是否正常运行。
  2. 异常时发出声音提示, 结束相关进程(伴随进程 pico_setting.exe 一并结束),
     随后按配置/已捕获路径将其重启(带冷却避免反复重启)。
  3. 界面展示: 两个进程运行状态(整卡背景色变化)、监测异常与重启记录。
     整体采用 MIUI/HyperOS 深色设计语言:
     大圆角分组卡片、柔和着色、右对齐设置行、细分隔线。
     可执行路径无需配置: 监测到进程运行时自动捕获真实路径写回 config.json,
     重启时优先使用该路径 (进程从未运行过时按配置中的回退路径尝试)。

界面设置 (检测间隔/重启冷却/声音开关) 保存于脚本同目录的 config.json,
下次启动自动恢复上次的设置; 进程真实路径运行时自动捕获并写回该文件。

伴随进程: config.json 的 "companions" 列表(默认含 pico_setting.exe),
每次触发重启时都会随之一起结束并重启, 但不单独监控、不显示状态卡片。

依赖:
  pip install customtkinter psutil
"""

import copy
import json
import os
import subprocess
import threading
import time
import tkinter.filedialog as filedialog
import tkinter.messagebox as messagebox

import customtkinter as ctk
import psutil
import winsound

try:  # 系统托盘 (可选依赖: 缺失时关闭窗口即退出, 不影响守护功能)
    import pystray
    from PIL import Image, ImageDraw
    HAS_TRAY = True
    TRAY_MISSING = ""
except Exception as _e:  # 缺包或后端加载失败均安全降级, 不让程序崩
    HAS_TRAY = False
    TRAY_MISSING = str(_e) or _e.__class__.__name__

CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
# 图标按序查找: 优先脚本同目录 (便于整体搬移), 最后回退固定位置
ICON_CANDIDATES = [
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "PICO Daemon.ico"),
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "resources", "PICO Daemon.ico"),
]
DEFAULT_PICO_DIR = r"C:/Program Files/PICO Connect"  # 手动定位 PICO Connect 的默认起始目录

# --------------------------------------------------------------------------- #
# MIUI/HyperOS 深色风格配色
# --------------------------------------------------------------------------- #
C_BG = "#202020"          # 窗口背景
C_SURFACE = "#232327"     # 卡片表面
C_SURFACE2 = "#2c2c31"    # 输入框/次级按钮
C_HOVER2 = "#3a3a41"      # 次级按钮悬停
C_LINE = "#333338"        # 分隔线
C_DIM = "#8e8e93"         # 次要文字
C_ACCENT = "#3c7dff"      # 主色 (MIUI 蓝)
C_ACCENT_HOVER = "#5b93ff"
C_OK_BG = "#173021"       # 运行中: 柔和绿色底
C_OK_TX = "#69db7c"
C_OK_HOVER = "#1d3d2a"    # 绿色底悬停
C_BAD_BG = "#3a1c1e"      # 未运行: 柔和红色底
C_BAD_TX = "#ff7b72"
C_STOP = "#d64550"        # 停止监测按钮
C_STOP_HOVER = "#e05a64"
C_BTN_TX = "#dce4ee"      # 次级按钮文字色 (与 CTk 默认按钮文字色一致)

# --------------------------------------------------------------------------- #
# 内置默认配置 (config.json 缺失或非法值时的回退; 手改 config.json 可覆盖)
# --------------------------------------------------------------------------- #
DEFAULT_CONFIG = {
    "check_interval": 5,       # 检测间隔(秒)
    "restart_cooldown": 15,    # 重启冷却(秒): 避免重启后未启动完成被再次判定异常
    "sound_enabled": False,    # 异常时是否发出声音提示(界面开关控制)
    "auto_start": True,        # 启动后是否自动开始监测(无需手动点击)
    "restart_only_missing": False,  # True=只重启未运行的进程; False=两个都重启
    # path 为回退路径: 进程运行时会自动捕获真实路径并覆盖写回 config.json,
    # 初始值仅在进程从未运行过时用于尝试重启
    "ps_server": {
        "name": "ps_server.exe",
        "path": "C:/Program Files/Streaming Service/ps_server.exe"
    },
    "pico_connect": {
        "name": "PICO Connect",
        "path": "E:/Program Files/PICO Connect/PICO Connect.exe"
    },
    # 伴随进程: 触发重启时一并结束并重启 (不单独监控)
    # path 留空: 默认取 PICO Connect 同目录下的同名文件
    "companions": [
        {"name": "pico_setting.exe", "path": ""}
    ]
}


# --------------------------------------------------------------------------- #
# 配置读写 (config.json: 界面设置 + 自动捕获的进程路径)
# --------------------------------------------------------------------------- #
def _norm_name(name):
    """规范化进程名用于精确比较: 去空白、转小写、忽略 .exe 后缀。"""
    n = (name or "").strip().lower()
    return n[:-4] if n.endswith(".exe") else n


def _sanitize_int(cfg, key):
    """单字段安全转正整数: 非法值单独回退默认, 不连累其他设置。"""
    try:
        cfg[key] = max(1, int(cfg.get(key) or 1))
    except (TypeError, ValueError):
        cfg[key] = DEFAULT_CONFIG[key]


def _sanitize(cfg):
    """校正 config.json 手改产生的非法值(如间隔为 0 导致热循环)。"""
    _sanitize_int(cfg, "check_interval")
    _sanitize_int(cfg, "restart_cooldown")
    cfg["sound_enabled"] = bool(cfg.get("sound_enabled", False))
    cfg.pop("alarm_times", None)  # 旧版蜂鸣次数字段, 音效已固定为两声
    for target in ("ps_server", "pico_connect"):
        section = cfg.get(target)
        if not isinstance(section, dict):
            section = copy.deepcopy(DEFAULT_CONFIG[target])
            cfg[target] = section
        if not str(section.get("name", "")).strip():
            section["name"] = DEFAULT_CONFIG[target]["name"]  # 空进程名会导致永久误报
        section.setdefault("path", DEFAULT_CONFIG[target]["path"])
    companions = cfg.get("companions")
    if not isinstance(companions, list):
        companions = copy.deepcopy(DEFAULT_CONFIG["companions"])
    cleaned = []
    for c in companions:
        if isinstance(c, dict) and str(c.get("name", "")).strip():
            c["name"] = str(c["name"]).strip()
            c.setdefault("path", "")
            cleaned.append(c)
    cfg["companions"] = cleaned
    return cfg


def load_config():
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                cfg.update(loaded)
        except (OSError, ValueError):
            pass  # 配置缺失或损坏时回退默认值
    try:
        return _sanitize(cfg)
    except (TypeError, ValueError):
        return copy.deepcopy(DEFAULT_CONFIG)


_cfg_lock = threading.Lock()  # 监测线程与 UI 线程都会写配置, 加锁防交错损坏


def save_config(cfg):
    """线程安全 + 原子写入: 先写临时文件再 os.replace, 写入中途断电/崩溃
    也不会损坏 config.json (最坏保留上一次的完整配置)。"""
    tmp = CONFIG_PATH + ".tmp"
    with _cfg_lock:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=4)
        os.replace(tmp, CONFIG_PATH)


# --------------------------------------------------------------------------- #
# 系统/进程查询 (纯逻辑, 不依赖 GUI)
# --------------------------------------------------------------------------- #
def acquire_single_instance():
    """命名互斥量保证单实例: 重复启动时提示并退出。
    返回 (是否允许运行, 互斥量句柄); 句柄需保持引用防止释放。"""
    try:
        import ctypes
        handle = ctypes.windll.kernel32.CreateMutexW(
            None, False, "PICO.Daemon.Instance")
        # ERROR_ALREADY_EXISTS = 183
        if ctypes.windll.kernel32.GetLastError() == 183:
            try:
                ctypes.windll.user32.MessageBoxW(
                    None, "PICO Daemon 已在运行中。", "提示", 0x40)
            except Exception:
                pass
            return False, None
        return True, handle
    except Exception:
        return True, None  # 判断失败时不阻止运行


def find_procs(name):
    """按进程名精确匹配(忽略大小写与 .exe 后缀), 返回 (process, exe_path) 列表。"""
    key = _norm_name(name)
    if not key:
        return []
    results = []
    for p in psutil.process_iter(["pid", "name", "exe"]):
        try:
            if _norm_name(p.info["name"]) == key:
                results.append((p, p.info["exe"] or ""))
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue
    return results


# --------------------------------------------------------------------------- #
# 主界面
# --------------------------------------------------------------------------- #
class MonitorApp(ctk.CTk):
    def __init__(self):
        # 独立 AppUserModelID: 让任务栏显示本程序自己的图标, 而非 python.exe 的
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
                "PICO.Daemon")
        except Exception:
            pass
        super().__init__()
        self.cfg = load_config()  # 启动时恢复上次的界面设置与进程路径
        self.running = False
        self.monitor_thread = None
        self.last_restart = 0
        self.captured = {}             # 规范化进程名 -> 运行中捕获到的可执行路径
        self._last_state = None        # 上次 (ps_ok, pico_ok), 仅状态变化时写日志
        self._cooldown_logged = False  # 冷却期提示每次异常只写一次
        self._tray_synced = None       # 上次已同步到托盘菜单的 (running, _last_state)

        self.title("PICO Daemon")
        self.geometry("420x720")
        self.resizable(False, True)  # 仅允许纵向拖动高度, 宽度锁定
        icon = next((p for p in ICON_CANDIDATES if os.path.exists(p)), "")
        if icon:
            try:
                self.iconbitmap(icon)  # 窗口标题栏 + 任务栏图标
            except Exception:
                pass
        self.configure(fg_color=C_BG)
        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("blue")

        # 将全局默认字体改为微软雅黑 (Windows 系统默认中文字体)
        try:
            from customtkinter import ThemeManager
            ThemeManager.theme["CTkFont"]["family"] = "微软雅黑"
        except Exception:
            pass

        # 关闭窗口: 有托盘时收纳到托盘 (监测线程保持运行), 无则直接退出
        self._tray = None
        self._tray_hinted = False
        if HAS_TRAY:
            self._setup_tray()
            self.protocol("WM_DELETE_WINDOW", self._hide_to_tray)
        else:
            self.log(f"ℹ 托盘功能不可用: {TRAY_MISSING or '依赖缺失'}")
            self.log("ℹ 启用托盘请执行: pip install pystray pillow")
            self.protocol("WM_DELETE_WINDOW", self._exit_app)

        self._build_ui()
        # 启动首刷延后到事件循环: 避免全表扫描阻塞在窗口显示之前 (弱机白屏感)
        self.after(80, self._refresh_once)

        # 启动后自动开始监测 (可在 config.json 将 auto_start 设为 false 关闭)
        if self.cfg.get("auto_start", True):
            self.toggle_monitor()

    # ----------------------------- 系统托盘 ----------------------------- #
    def _make_tray_image(self):
        """托盘图标: 优先加载 ico 文件, 缺失时生成蓝色圆底 'P' 占位图。"""
        path = next((p for p in ICON_CANDIDATES if os.path.exists(p)), "")
        if path:
            try:
                return Image.open(path).convert("RGBA").resize((64, 64))
            except Exception:
                pass
        img = ImageDraw.Draw(new := Image.new("RGBA", (64, 64), (0, 0, 0, 0)))
        img.rounded_rectangle([4, 4, 60, 60], radius=16, fill="#3c7dff")
        img.text((32, 30), "P", fill="white", anchor="mm")
        return new

    def _setup_tray(self):
        """创建托盘图标并后台运行; 回调在托盘线程, 一律 _safe_after 调度回 UI 线程。"""
        menu = pystray.Menu(
            pystray.MenuItem("打开 PICO Daemon", self._tray_restore, default=True),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(self._pico_state_text, None, enabled=False),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(self._toggle_text, self._tray_toggle),
            pystray.MenuItem("立即重启 PICO Connect", self._tray_restart),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("退出 PICO Daemon", self._tray_exit),
        )
        self._tray = pystray.Icon("PICO Daemon", self._make_tray_image(),
                                  "PICO Daemon", menu)
        self._tray.run_detached()

    def _pico_state_text(self, item):
        """托盘菜单动态状态项 (pystray 每次弹出菜单时重新取文本)。"""
        st = self._last_state
        return f"PICO Connect: {'运行中' if st and st[1] else '未运行'}"

    def _toggle_text(self, item):
        return "停止监测" if self.running else "开始监测"

    def _tray_restore(self, icon, item):
        self._safe_after(0, self._restore_from_tray)

    def _restore_from_tray(self):
        self.deiconify()
        self.lift()
        self.focus_force()

    def _tray_toggle(self, icon, item):
        self._safe_after(0, self.toggle_monitor)

    def _tray_restart(self, icon, item):
        self._safe_after(0, self._restart_pico_only)

    def _restart_pico_only(self):
        """托盘菜单的立即重启: 仅重启 PICO Connect (伴随进程自动跟随)。"""
        conf = self.cfg["pico_connect"]
        if self._last_state and self._last_state[1]:  # 运行中需确认, 防手滑中断
            if not messagebox.askyesno("确认重启", "PICO Connect 正在正常运行，确定要强制重启吗？"):
                return
        self.log("🖱 托盘触发: 重启 PICO Connect")
        threading.Thread(target=self.do_restart,
                         args=[[("PICO Connect", conf, _norm_name(conf["name"]))]],
                         daemon=True).start()

    def _tray_exit(self, icon, item):
        icon.stop()  # 停托盘消息循环 (托盘线程), 窗口清理调度回 UI 线程
        self._safe_after(0, self._exit_app)

    def _hide_to_tray(self):
        """关闭按钮 -> 收纳到托盘: 保存设置并隐藏窗口, 监测线程继续运行。"""
        self._apply_settings_to_cfg()
        try:
            save_config(self.cfg)
        except OSError:
            pass
        self.withdraw()
        self.log("🔽 已收纳到系统托盘, 点击托盘图标恢复窗口")
        if not self._tray_hinted:  # 首次收纳弹一次气泡提示
            self._tray_hinted = True
            try:
                self._tray.notify("仍在后台运行, 点击托盘图标恢复窗口", "PICO Daemon")
            except Exception:
                pass

    def _exit_app(self):
        """真正退出: 停监测线程(等待至多 2 秒) -> 保存设置 -> 销毁窗口。"""
        self.running = False
        t = self.monitor_thread
        if t and t.is_alive():
            t.join(timeout=2)  # 防止 destroy 后线程再碰窗口/写配置
        self._apply_settings_to_cfg()
        try:
            save_config(self.cfg)
        except OSError:
            pass
        try:  # 清空挂起 after 队列, 防止 destroy 后残留回调触发 Tcl 警告
            for aid in self.tk.call("after", "info"):
                self.tk.call("after", "cancel", aid)
        except Exception:
            pass
        self.destroy()

    # ----------------------------- UI 构建 ----------------------------- #
    def _build_ui(self):
        # 顶部标题 (MIUI 左对齐大标题 + 副标题)
        header = ctk.CTkFrame(self, fg_color="transparent")
        header.pack(fill="x", padx=20, pady=(16, 6))
        ctk.CTkLabel(header, text="PICO Daemon",
                     font=ctk.CTkFont(size=20, weight="bold"), anchor="w").pack(anchor="w")
        ctk.CTkLabel(header, text="进程监控 · 异常自动恢复",
                     font=ctk.CTkFont(size=12), text_color=C_DIM, anchor="w").pack(anchor="w")

        # 状态卡片区 (两列等宽, 自然高度)
        cards_frame = ctk.CTkFrame(self, fg_color="transparent")
        cards_frame.pack(fill="x", padx=10, pady=4)
        cards_frame.columnconfigure((0, 1), weight=1, uniform="cols")  # 两列等宽
        self.ps_card = self._make_card(cards_frame, "ps_server.exe", 0, 0)
        self.pico_card = self._make_card(cards_frame, "PICO Connect", 0, 1)

        # 设置区 (MIUI 分组列表样式)
        self._build_settings()

        # 按钮区 (上: 开始监测占满一行; 下: 立即重启 / 测试声音 各占一半)
        btn_frame = ctk.CTkFrame(self, fg_color="transparent")
        btn_frame.pack(fill="x", padx=16, pady=8)
        btn_frame.columnconfigure((0, 1), weight=1, uniform="btns")
        self.toggle_btn = ctk.CTkButton(btn_frame, text="开始监测",
                                        command=self.toggle_monitor,
                                        height=40, corner_radius=12,
                                        fg_color=C_ACCENT, hover_color=C_ACCENT_HOVER,
                                        font=ctk.CTkFont(size=14, weight="bold"))
        self.toggle_btn.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 8))
        ctk.CTkButton(btn_frame, text="立即重启", command=self.manual_restart,
                      height=38, corner_radius=12, text_color=C_BTN_TX,
                      fg_color=C_SURFACE, hover_color=C_HOVER2).grid(
            row=1, column=0, sticky="ew", padx=(0, 4))
        # 声音提示开关: 整钮变色, 绿=开 / 灰=关 (与状态卡配色一致)
        self.sound_btn = ctk.CTkButton(btn_frame, text="", command=self.toggle_sound,
                                       height=38, corner_radius=12)
        self.sound_btn.grid(row=1, column=1, sticky="ew", padx=(4, 0))
        self._update_sound_btn()

        # 日志区
        ctk.CTkLabel(self, text="操作记录", font=ctk.CTkFont(size=13, weight="bold"),
                     anchor="w").pack(anchor="w", padx=20, pady=(6, 4))
        self.log_box = ctk.CTkTextbox(self, height=210, fg_color=C_SURFACE,
                                      corner_radius=16, border_width=0,
                                      font=ctk.CTkFont(size=12))
        self.log_box.pack(fill="both", expand=True, padx=16, pady=(0, 14))

    def _hairline(self, parent):
        """分组列表的细分隔线。"""
        ctk.CTkFrame(parent, fg_color=C_LINE, height=1).pack(fill="x", padx=16, pady=2)

    def _make_card(self, parent, title, r, c):
        """简洁状态卡: 进程名 + 状态文字, 整卡背景色随状态变化。"""
        card = ctk.CTkFrame(parent, fg_color=C_SURFACE, corner_radius=16)
        card.grid(row=r, column=c, padx=6, pady=6, sticky="nsew")
        ctk.CTkLabel(card, text=title, font=ctk.CTkFont(size=12),
                     text_color=C_DIM).pack(pady=(14, 0))
        value = ctk.CTkLabel(card, text="检测中...",
                             font=ctk.CTkFont(size=18, weight="bold"),
                             text_color=C_DIM)
        value.pack(pady=(2, 14))
        return {"frame": card, "value": value}

    def _settings_row(self, sf, text):
        """设置卡标准行: 统一的行容器/边距/左侧标签 (MIUI 设置列表)。"""
        row = ctk.CTkFrame(sf, fg_color="transparent")
        row.pack(fill="x", padx=16, pady=(4, 2))
        ctk.CTkLabel(row, text=text,
                     font=ctk.CTkFont(size=13)).pack(side="left")
        return row

    def _build_settings(self):
        sf = ctk.CTkFrame(self, fg_color=C_SURFACE, corner_radius=16)
        sf.pack(fill="x", padx=16, pady=4)
        ctk.CTkLabel(sf, text="设置", font=ctk.CTkFont(size=13, weight="bold"),
                     anchor="w").pack(anchor="w", padx=16, pady=(12, 4))

        # 行样式: 左侧标签 + 右侧同规格控件 (统一 width=90 height=28 圆角10)
        row1 = self._settings_row(sf, "检测间隔(秒)")
        self.interval_var = ctk.StringVar(value=str(self.cfg["check_interval"]))
        ctk.CTkEntry(row1, textvariable=self.interval_var, width=90, height=28,
                     justify="right", fg_color=C_SURFACE2, border_width=0,
                     corner_radius=10).pack(side="right")
        self._hairline(sf)
        row2 = self._settings_row(sf, "重启冷却(秒)")
        self.cooldown_var = ctk.StringVar(value=str(self.cfg["restart_cooldown"]))
        ctk.CTkEntry(row2, textvariable=self.cooldown_var, width=90, height=28,
                     justify="right", fg_color=C_SURFACE2, border_width=0,
                     corner_radius=10).pack(side="right")
        self._hairline(sf)

        # 手动定位 PICO Connect 安装位置 (自动捕获不到时的兜底):
        # 标签 + 「选择目录」可点击文本 (CTkButton 高 DPI 圆角有描边缺陷, 弃用),
        # 当前路径只读框紧随其下
        row3 = self._settings_row(sf, "PICO Connect 位置")
        pick = ctk.CTkLabel(row3, text="选择目录", text_color=C_ACCENT,
                            cursor="hand2", font=ctk.CTkFont(size=13))
        pick.pack(side="right")
        pick.bind("<Button-1>", lambda e: self.choose_pico_dir())
        pick.bind("<Enter>", lambda e: pick.configure(text_color=C_ACCENT_HOVER))
        pick.bind("<Leave>", lambda e: pick.configure(text_color=C_ACCENT))
        self.path_var = ctk.StringVar(value=self.cfg["pico_connect"]["path"])
        ctk.CTkEntry(sf, textvariable=self.path_var, height=28, justify="left",
                     state="readonly", fg_color=C_SURFACE2, border_width=0,
                     corner_radius=10).pack(fill="x", padx=16, pady=(2, 12))

    # ----------------------------- 路径自动捕获 ----------------------------- #
    def _remember_path(self, cap_key, exe):
        """捕获运行中进程的真实可执行路径: 更新运行时缓存, 并在与已持久化路径
        不同时写回 config.json (进程名可省略 .exe, 统一走 _norm_name 比较)。"""
        if not exe:
            return
        self.captured[cap_key] = exe
        norm = os.path.normcase(os.path.normpath(exe))
        for section in ("ps_server", "pico_connect"):
            if _norm_name(self.cfg[section]["name"]) != cap_key:
                continue
            old = self.cfg[section]["path"]
            if os.path.normcase(os.path.normpath(old)) != norm:
                self.cfg[section]["path"] = exe
                try:
                    save_config(self.cfg)
                except OSError:
                    pass  # 写盘失败不影响监测, 下次捕获会再尝试
            break

    def choose_pico_dir(self):
        """手动指定 PICO Connect 安装目录: 从所选目录定位可执行文件并写回配置。
        默认查找位置为 C:\\Program Files\\PICO Connect\\ (不存在时回退上级)。"""
        if os.path.isdir(DEFAULT_PICO_DIR):
            initial = DEFAULT_PICO_DIR
        elif os.path.isdir(r"C:\Program Files"):
            initial = r"C:\Program Files"
        else:
            initial = "/"
        d = filedialog.askdirectory(parent=self, title="选择 PICO Connect 安装目录",
                                    initialdir=initial)
        if not d:
            return  # 用户取消
        # 进程名可能省略 .exe, 定位文件时优先尝试带 .exe 的真实文件名
        name = self.cfg["pico_connect"]["name"]
        cand = next((os.path.join(d, n) for n in
                     (name if name.lower().endswith(".exe") else name + ".exe", name)
                     if os.path.exists(os.path.join(d, n))), "")
        if cand:
            self._remember_path(_norm_name(name), cand)
            self.path_var.set(cand)  # 同步路径显示框
            self.log(f"📁 已设置 PICO Connect 位置: {cand}")
        else:
            self.log(f"⚠ 所选目录中未找到 {name}.exe，配置未更改")

    def log(self, msg):
        ts = time.strftime("%H:%M:%S")
        line = f"[{ts}] {msg}\n"
        self._safe_after(0, lambda: self._append_log(line))

    def _safe_after(self, delay, fn, *args):
        """窗口销毁后静默丢弃调度, 防止后台线程对已销毁窗口抛 TclError。"""
        try:
            self.after(delay, fn, *args)
        except Exception:
            pass

    def _append_log(self, line):
        self.log_box.insert("end", line)
        self.log_box.see("end")
        # 上限 500 行, 超出丢弃最旧的, 防止挂机数日日志无限膨胀
        lines = int(self.log_box.index("end-1c").split(".")[0])
        if lines > 500:
            self.log_box.delete("1.0", f"{lines - 499}.0")

    def _update_sound_btn(self):
        """开关按钮样式: 开=绿色整钮, 关=与「立即重启」同款次级样式。"""
        if self.cfg["sound_enabled"]:
            self.sound_btn.configure(text="声音提示：开",
                                     fg_color=C_OK_BG, hover_color=C_OK_HOVER,
                                     text_color=C_OK_TX)
        else:
            self.sound_btn.configure(text="声音提示：关",
                                     fg_color=C_SURFACE, hover_color=C_HOVER2,
                                     text_color=C_BTN_TX)

    def toggle_sound(self):
        self.cfg["sound_enabled"] = not self.cfg["sound_enabled"]
        try:
            save_config(self.cfg)
        except OSError:
            pass
        self._update_sound_btn()
        if self.cfg["sound_enabled"]:
            self.log("🔔 声音提示已开启")
            self.play_alarm()  # 开启瞬间试听新音效
        else:
            self.log("🔕 声音提示已关闭")

    def play_alarm(self):
        """异常提示音: 快速的"嘟嘟"两声 (工作线程播放, 不阻塞界面)。"""
        if not self.cfg["sound_enabled"]:
            return  # 声音提示关闭: 报警静默

        def _beep():
            try:
                winsound.Beep(880, 120)   # 嘟
                time.sleep(0.08)
                winsound.Beep(880, 120)   # 嘟
            except Exception:
                pass

        threading.Thread(target=_beep, daemon=True).start()

    def _set_card(self, card, ok):
        """简洁样式: 整卡背景色变化, 文字颜色随之柔化。"""
        card["frame"].configure(fg_color=C_OK_BG if ok else C_BAD_BG)
        card["value"].configure(text="运行中" if ok else "未运行",
                                text_color=C_OK_TX if ok else C_BAD_TX)

    def update_status_ui(self, ps_ok, pico_ok):
        self._set_card(self.ps_card, ps_ok)
        self._set_card(self.pico_card, pico_ok)
        self._sync_tray_menu()

    def _sync_tray_menu(self):
        """状态变化后同步托盘菜单。

        pystray 的 win32 后端在右键弹出时复用缓存的 HMENU, 动态菜单文本
        只在 update_menu() 重建句柄时求值, 不会随 GUI 状态自动刷新,
        因此所有状态变化点都必须显式调用本方法。仅在 (running, _last_state)
        真正变化时重建, 避免每检测周期无效重建。
        """
        if self._tray is None:
            return
        state = (self.running, self._last_state)
        if state == self._tray_synced:
            return
        self._tray_synced = state
        try:
            self._tray.update_menu()
        except Exception:
            pass

    def _scan_all(self):
        """单次遍历进程表, 同时精确匹配 ps_server / PICO Connect。
        (性能: 原先每个检测周期全表扫描 2 次, 现压缩为 1 次)"""
        ps_name = _norm_name(self.cfg["ps_server"]["name"])
        pico_name = _norm_name(self.cfg["pico_connect"]["name"])
        groups = {}  # 归一化进程名 -> 目标标签列表(重名时同进程计入多个目标)
        for key, tag in ((ps_name, "ps"), (pico_name, "pico")):
            groups.setdefault(key, []).append(tag)
        result = {"ps": [], "pico": []}
        for p in psutil.process_iter(["pid", "name", "exe"]):
            try:
                tags = groups.get(_norm_name(p.info["name"]))
                if tags:
                    exe = p.info["exe"] or ""
                    for t in tags:
                        result[t].append((p, exe))
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                continue
        return result

    def _refresh_once(self):
        scan = self._scan_all()
        ps_name = _norm_name(self.cfg["ps_server"]["name"])
        pico_name = _norm_name(self.cfg["pico_connect"]["name"])
        if scan["ps"]:
            self._remember_path(ps_name, scan["ps"][0][1])
        if scan["pico"]:
            self._remember_path(pico_name, scan["pico"][0][1])
        self._last_state = (bool(scan["ps"]), bool(scan["pico"]))
        self.update_status_ui(*self._last_state)

    def check_once(self):
        scan = self._scan_all()
        ps_procs, pico_procs = scan["ps"], scan["pico"]
        ps_name = _norm_name(self.cfg["ps_server"]["name"])
        pico_name = _norm_name(self.cfg["pico_connect"]["name"])
        if ps_procs:
            self._remember_path(ps_name, ps_procs[0][1])
        if pico_procs:
            self._remember_path(pico_name, pico_procs[0][1])

        ps_ok, pico_ok = bool(ps_procs), bool(pico_procs)
        self._safe_after(0, self.update_status_ui, ps_ok, pico_ok)

        # 仅在状态发生变化时写日志, 避免冷却期内每轮刷屏
        state = (ps_ok, pico_ok)
        if state != self._last_state:
            self._last_state = state
            if all(state):
                self.log("✅ 两个进程均正常运行")
            else:
                problems = []
                if not ps_ok:
                    problems.append("ps_server.exe 未运行")
                if not pico_ok:
                    problems.append("PICO Connect 未运行")
                self.log("⚠ 检测到异常: " + "，".join(problems))

        if ps_ok and pico_ok:
            self._cooldown_logged = False
            return

        now = time.time()
        if now - self.last_restart > self.cfg["restart_cooldown"]:
            self._cooldown_logged = False
            if self.cfg["sound_enabled"]:
                self.play_alarm()
                self.log("🔊 已发出声音警报，准备结束并重启相关进程...")
            else:
                self.log("⚠ 准备结束并重启相关进程（声音提示已关闭）")
            failed = [
                ("ps_server", self.cfg["ps_server"], _norm_name(self.cfg["ps_server"]["name"])),
                ("PICO Connect", self.cfg["pico_connect"], _norm_name(self.cfg["pico_connect"]["name"])),
            ]
            if self.cfg.get("restart_only_missing"):
                targets = [t for t, ok in zip(failed, (ps_ok, pico_ok)) if not ok]
            else:
                targets = failed  # 保持原行为: 两个都重启
            # 复用本轮扫描结果, 省去 do_restart 内的重复全表扫描
            self.do_restart(targets, {ps_name: ps_procs, pico_name: pico_procs})
            self.last_restart = now
            self.log("🔄 已执行结束并重启操作")
        elif not self._cooldown_logged:
            self._cooldown_logged = True
            self.log("⏳ 处于重启冷却期，等待进程启动...")

    def do_restart(self, targets=None, scan_map=None):
        """结束并重启指定目标; targets 为 None 时重启全部两个进程。
        伴随进程(config.json 的 companions)总是随之一起结束并重启。
        scan_map: cap_key -> [(proc, exe)], 由调用方传入已有扫描结果,
        未提供的键(如伴随进程)在结束阶段单独扫描。"""
        if targets is None:
            targets = [
                ("ps_server", self.cfg["ps_server"], _norm_name(self.cfg["ps_server"]["name"])),
                ("PICO Connect", self.cfg["pico_connect"], _norm_name(self.cfg["pico_connect"]["name"])),
            ]
        scan_map = scan_map or {}
        for comp in self.cfg.get("companions") or []:
            if comp.get("name"):
                targets = targets + [(comp["name"], comp, _norm_name(comp["name"]))]

        # 1) 优雅结束, 并等待进程实际退出 (最多 3 秒)
        alive = []
        for label, conf, cap_key in targets:
            procs = scan_map.get(cap_key)
            if procs is None:
                procs = find_procs(conf["name"])
            for p, exe in procs:
                if exe:
                    self._remember_path(cap_key, exe)  # 结束前记录真实路径, 备用于重启
                try:
                    p.terminate()
                    self.log(f"  已发送结束信号: {label} PID {p.pid}")
                except Exception as e:
                    self.log(f"  结束失败 {label} PID {p.pid}: {e}")
            _, still_alive = psutil.wait_procs([p for p, _ in procs], timeout=3)
            alive.extend(still_alive)

        # 2) 强杀残留
        for p in alive:
            try:
                p.kill()
            except Exception:
                pass
        psutil.wait_procs(alive, timeout=2)

        # 3) 重启 (优先用本次运行捕获到的真实路径, 无效时退回配置中的路径 --
        #    该路径会随运行时捕获自动更新;
        #    伴随进程两者都缺失时, 默认取 PICO Connect 同目录下的同名文件)
        monitored = {_norm_name(self.cfg["ps_server"]["name"]),
                     _norm_name(self.cfg["pico_connect"]["name"])}
        pico_path = (self.captured.get(_norm_name(self.cfg["pico_connect"]["name"]), "")
                     or self.cfg["pico_connect"]["path"])
        pico_dir = os.path.dirname(pico_path) if pico_path else ""
        for label, conf, cap_key in targets:
            path = self.captured.get(cap_key, "")
            if not path or not os.path.exists(path):
                path = conf.get("path") or ""
            if (not path or not os.path.exists(path)) and cap_key not in monitored and pico_dir:
                cand = os.path.join(pico_dir, conf["name"])
                if os.path.exists(cand):
                    path = cand
            if path and os.path.exists(path):
                try:
                    subprocess.Popen(path, cwd=os.path.dirname(path) or ".")
                    self.log(f"  ✅ 已启动 {label}: {path}")
                except Exception as e:
                    self.log(f"  ❌ 启动 {label} 失败: {e}")
            else:
                self.log(f"  ⚠ 未找到 {label} 的可执行路径，无法自动启动（可运行一次后自动记录，或在设置中自定义安装位置）")

    def manual_restart(self):
        # 两个进程都正常运行时需要确认, 避免手滑中断串流
        if self._last_state and all(self._last_state):
            if not messagebox.askyesno(
                    "确认重启", "两个进程当前都在正常运行，确定要强制重启吗？"):
                return
        self.log("🖱 手动触发重启")
        # 记入冷却期, 避免监测线程在进程尚未启动完成时再次触发自动重启
        self.last_restart = time.time()
        # 在后台线程执行, 避免 do_restart 内部的等待阻塞 UI
        threading.Thread(target=self.do_restart, daemon=True).start()

    def toggle_monitor(self):
        if self.running:
            self.running = False
            self._last_state = None        # 重置状态记忆, 下次启动重新首报
            self._cooldown_logged = False
            self.toggle_btn.configure(text="开始监测",
                                      fg_color=C_ACCENT, hover_color=C_ACCENT_HOVER)
            self.log("⏹ 监测已停止")
            self._sync_tray_menu()
        else:
            self._apply_settings_to_cfg()
            self.running = True
            self.toggle_btn.configure(text="停止监测",
                                      fg_color=C_STOP, hover_color=C_STOP_HOVER)
            self.log("▶ 监测已启动")
            self.monitor_thread = threading.Thread(target=self._monitor_loop, daemon=True)
            self.monitor_thread.start()
            self._sync_tray_menu()

    def _monitor_loop(self):
        # 用分段 sleep, 停止时可快速响应
        while self.running:
            try:
                self.check_once()
            except Exception as e:
                self.log(f"监测出错: {e}")
            for _ in range(self.cfg["check_interval"]):
                if not self.running:
                    break
                time.sleep(1)

    def _apply_settings_to_cfg(self):
        try:
            self.cfg["check_interval"] = max(1, int(self.interval_var.get()))
        except Exception:
            pass
        try:
            self.cfg["restart_cooldown"] = max(1, int(self.cooldown_var.get()))
        except Exception:
            pass

if __name__ == "__main__":
    _ok, _mutex = acquire_single_instance()  # 句柄保持引用, 防止互斥量被释放
    if _ok:
        app = MonitorApp()
        app.mainloop()
