#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""嘉立创 3D 模型下载器 (JLC3D-Downloader) v2.0.0

输入立创 / 嘉立创编号（例如 C8734），从 JLCEDA / EasyEDA 官方库下载 STEP 模型。

接口链路（思路参考 LCSC3D 项目 https://github.com/Travelerrrrrr/LCSC3D）：
  1. GET https://lceda.cn/api/products/<C编号>/components
     读取封装 packageDetail.dataStr 里的 SVGNODE（c_etype=outline3D）uuid；
     若封装只记录了 3D 库编号 uuid_3d，则再查 /api/components/<uuid> 解析出文件 uuid。
  2. GET https://modules.lceda.cn/qAxj6KHrDKw4blvCG8QJPs7Y/<uuid>
     失败时回退 modules.easyeda.com；校验 ISO-10303-21 头后再原子落盘。
  3. 少量器件不在 EasyEDA 接口里，回退到 pro.lceda.cn 的搜索链路。

注意：请求必须带可识别的 User-Agent。实测只写 "Mozilla/5.0" 会被 CloudWAF
判定为爬虫并返回 HTTP 418，所以这里使用带版本号的自定义 UA，并在被拦截时
自动切换浏览器 UA 重试一次。
"""

from __future__ import annotations

import configparser
import ctypes
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import webbrowser
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext, ttk

try:
    import requests
except ImportError:  # 缺少依赖时由 main() 给出友好提示
    requests = None

# =====================================================
# 常量
# =====================================================
APP_NAME = "JLC3D-Downloader"
VERSION = "2.1.1"
REPO_URL = "https://github.com/Jupiter1028/JLC3D_Downloader"
AUTHOR = "Jupiter"

# 带版本号的自定义 UA：既能标识来源，也不会被 CloudWAF 当成裸爬虫
USER_AGENT = f"{APP_NAME}/{VERSION} (+{REPO_URL})"
BROWSER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

CONNECT_TIMEOUT = 6
READ_TIMEOUT = 30
MAX_MODEL_BYTES = 64 * 1024 * 1024
DOWNLOAD_CHUNK = 64 * 1024

LOCAL_APP_DATA = Path(os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local"))
APP_DIR = LOCAL_APP_DATA / APP_NAME
CONFIG_FILE = APP_DIR / "config.ini"
LOG_FILE = APP_DIR / "app.log"
LEGACY_CONFIG = Path.home() / ".jlc3d" / "config.ini"

COMPONENT_ENDPOINTS = (
    "https://lceda.cn/api/products/{code}/components",
    "https://easyeda.com/api/products/{code}/components",
)
LIBRARY_ENDPOINTS = (
    "https://lceda.cn/api/components/{uuid}",
    "https://easyeda.com/api/components/{uuid}",
)
MODEL_ENDPOINTS = (
    "https://modules.lceda.cn/qAxj6KHrDKw4blvCG8QJPs7Y/{uuid}",
    "https://modules.easyeda.com/qAxj6KHrDKw4blvCG8QJPs7Y/{uuid}",
)
PRO_API = "https://pro.lceda.cn/api"

WAF_MESSAGE = "请求被嘉立创风控拦截（HTTP 418）。请稍后重试；若持续出现，请把日志反馈给作者。"
CODE_PATTERN = re.compile(r"^C\d{1,10}$")
MODEL_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,128}$")

# 64x64 应用图标（PNG base64）：assets 目录缺失时也能显示窗口图标
EMBEDDED_ICON_PNG = (
    "iVBORw0KGgoAAAANSUhEUgAAADAAAAAwCAMAAABg3Am1AAAAwFBMVEX7/P3k6/y21PzS5v0EV/QC"
    "SOzI2foEZ/qtyfoFeP0Eh/0EONSOt/oAKs9tlvGTxPwJlv1Qt/1TlvcAOuhKeO9MhvENpv1ryP1u"
    "tfwORNgtdvIxp/2PqvEslvyQ2PwvbO1y0/2x5P1Gqv1zpvlsiukuh/g4tP0Mt/0AHMihufYxVdNZ"
    "xv2Fmu1Ia+c3ZNtcdtolW+UAK+eK5f4nT+7d8P4Awf4AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAAAAAAOUD4YAAAAQHRSTlP/////////////////////////////////////////////////////"
    "//////////////////8AAAAAAAAAAAAAAxeF4wAAAxdJREFUeNrdltea4jAMhWO5BtuppBf69L67"
    "7/9qK4cWYGDu9mL1ES7M+e1j2VLwvP8kyDdjvynGFdmFHoomjyYuuqWhF8SZHuKtduIPIf2NuWWo"
    "zydRU4aAEZYdyn0pWa2uesmjJhx5oAb1SLDg+wXiKKbnc6yYCz49ig/AIorxKyxLI8aEGog0uwAg"
    "gkU88QcPyQnBeZqmDy9nADS4yxAolD46qMeEWQfBeh2cAnR5PKZgms22rpQxLkEKA8gpUFIPEgiT"
    "QWgC40ahZpyxzHniKefBGKDgeRumfIbLgGSS4x4FOgsk43TF2WzGuW7HAH5kDczlQjIq+AyNZDhn"
    "ljJSc+Z5rdbT0yyFfGU43gLDjznKZvxDeSxFqdB2BNASvIyrjFM3p/JUO0zz+JnyD6JSjUu11gZH"
    "oGSZ4UwwZh7VNA2MZe4HQkSgbYBmMFVv1oojICSmQT2mPLVrwbV+xcGAV8JDoJ3aSpC1fXgfZ4ko"
    "RTwqMABMMOS2tbqqrH2ildVaPzw8jc8hPFxPPGe5u8xrba1+JwL1Vj+9nNwl2mFhEREmeASJG1jM"
    "ix4HBNnuhRDv7LbCxJUKY9LZof38+Tm+uyvg2+rfwWqVJCtnZtHPi6J4juOmiaIlXAX2RfE1d/oi"
    "RqDrmk5mtwF6/zzveydfNs0yqSs7/QnI8yYuinIZx8mbtpXqFwt6C8hz15G6OEyeMPv69evPfX53"
    "E4iiyLWkRLtz07P5fR7dHWvrEoh2XWzj5uc8m+f5ZEluAPuWt6lQbvo+xhk6FcItYGiQm4pzhn1t"
    "8CeZuQr4/mRoNA7gNTgaO1+438UFILYXRMoaF0jqBIZGaYS4uoJEoSN8zsMCulIx3IoysG0nFwAR"
    "7dBaHCBDv4RSqV8tGDpU+x4YEYQKMJlbAsvCyESAAiHwCdLWO2hPWi8SyiQzJtnMudkGttbPx6NU"
    "nOzCLQLhKgkMmOkhXl9cnvYHKM6rxL0PKTpRKsRwnVUMeTrkSnz3ZkVEwDaEGM7haIUA/fndTGFs"
    "HWeh5EY4g2f8bnm1e3aB74XhAUH+2d+Mvzs6MS/t5w5qAAAAAElFTkSuQmCC"
)


# =====================================================
# 异常
# =====================================================
class JlcError(Exception):
    """可以直接展示给用户的错误。"""


class Cancelled(Exception):
    """用户取消下载。"""


# =====================================================
# 日志与配置
# =====================================================
def log_event(level, message, exc_info=None):
    """把诊断信息写到 %LOCALAPPDATA%\\JLC3D-Downloader\\app.log。"""
    try:
        APP_DIR.mkdir(parents=True, exist_ok=True)
        if LOG_FILE.exists() and LOG_FILE.stat().st_size > 1024 * 1024:
            backup = LOG_FILE.with_name(LOG_FILE.name + ".1")
            try:
                backup.unlink()
            except OSError:
                pass
            try:
                LOG_FILE.replace(backup)
            except OSError:
                pass
        with open(LOG_FILE, "a", encoding="utf-8") as stream:
            stream.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} {level:<7} {message}\n")
            if exc_info:
                traceback.print_exception(*exc_info, file=stream)
    except OSError:
        pass


def known_desktop():
    """优先使用系统“桌面”已知文件夹（兼容 OneDrive 重定向）。"""
    if sys.platform == "win32":
        try:
            buffer = ctypes.create_unicode_buffer(1024)
            if ctypes.windll.shell32.SHGetFolderPathW(None, 0x10, None, 0, buffer) == 0:
                if buffer.value and os.path.isdir(buffer.value):
                    return Path(buffer.value)
        except (AttributeError, OSError):
            pass
    for candidate in (Path.home() / "Desktop", Path.home()):
        if candidate.is_dir():
            return candidate
    return Path.home()


def _read_config(path):
    parser = configparser.ConfigParser()
    parser.read(path, encoding="utf-8")
    value = parser.get("app", "download_path", fallback=None)
    if not value:
        value = parser.get("PATH", "DownloadPath", fallback=None)
    return value


def load_settings():
    """读取下载目录；兼容旧版 ~/.jlc3d/config.ini 并自动迁移。"""
    download_path = known_desktop()
    try:
        if CONFIG_FILE.exists():
            saved = _read_config(CONFIG_FILE)
            if saved:
                download_path = Path(saved).expanduser()
        elif LEGACY_CONFIG.exists():
            saved = _read_config(LEGACY_CONFIG)
            if saved:
                download_path = Path(saved).expanduser()
                save_settings(download_path)
                log_event("INFO", f"已从旧配置迁移下载目录：{download_path}")
    except (OSError, configparser.Error) as exc:
        log_event("WARNING", f"读取配置失败：{exc}")
    return {"download_path": str(download_path)}


def save_settings(download_path):
    try:
        APP_DIR.mkdir(parents=True, exist_ok=True)
        parser = configparser.ConfigParser()
        parser["app"] = {"download_path": str(download_path)}
        with open(CONFIG_FILE, "w", encoding="utf-8") as stream:
            parser.write(stream)
    except OSError as exc:
        log_event("WARNING", f"保存配置失败：{exc}")


# =====================================================
# 通用工具
# =====================================================
def format_size(size):
    if size >= 1024 * 1024:
        return f"{size / 1024 / 1024:.1f} MB"
    if size >= 1024:
        return f"{size / 1024:.0f} KB"
    return f"{size} B"


def resource_path(*parts):
    """定位随程序分发的资源，兼容 PyInstaller onefile 的解包目录。"""
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return base.joinpath(*parts)


def set_app_user_model_id():
    """让 Windows 任务栏把本程序识别为独立应用，而不是 python.exe。"""
    if sys.platform != "win32":
        return
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("JLC3D.Downloader")
    except (AttributeError, OSError):
        pass


def load_window_icon():
    """优先读 assets/app.png；资源缺失时退回内置 PNG。"""
    try:
        png = resource_path("assets", "app.png")
        if png.exists():
            return tk.PhotoImage(file=str(png))
    except (tk.TclError, OSError):
        pass
    try:
        return tk.PhotoImage(data=EMBEDDED_ICON_PNG)
    except tk.TclError:
        return None


def apply_window_icon(window):
    """设置窗口图标。

    注意：Windows 上 iconbitmap 与 iconphoto 同时调用会互相覆盖，任务栏/标题栏
    反而显示成系统默认图标，因此这里只选一种：Windows 用多尺寸 ico，其它平台用 png。
    """
    ico = resource_path("assets", "app.ico")
    if sys.platform == "win32" and ico.exists():
        try:
            window.iconbitmap(default=str(ico))
            return
        except (tk.TclError, OSError):
            pass
    image = load_window_icon()
    if image is not None:
        try:
            window.iconphoto(True, image)
            window._icon_image = image  # 防止 PhotoImage 被回收
        except tk.TclError:
            pass


def safe_filename(name):
    """去掉 Windows 非法字符，避开保留设备名。"""
    clean = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", str(name)).strip(" .")[:100].rstrip(" .")
    if not clean:
        clean = "model"
    if re.fullmatch(r"(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])", clean.split(".")[0], re.I):
        clean = "_" + clean
    return clean


def atomic_write(path, data):
    """先写同目录临时文件再替换，避免留下半个文件。"""
    path = Path(path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise JlcError(f"无法创建保存目录：{path.parent}（{exc.strerror or exc}）") from exc

    handle, temp_name = tempfile.mkstemp(prefix=".jlc3d-", suffix=".part", dir=str(path.parent))
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        for attempt in range(4):
            try:
                os.replace(temp_name, path)
                break
            except PermissionError:
                # 杀毒软件/资源管理器偶尔短暂占用目标文件
                if attempt == 3:
                    raise JlcError(f"无法写入文件（可能被占用）：{path}") from None
                time.sleep(0.2)
    except OSError as exc:
        raise JlcError(f"写入文件失败：{path}（{exc.strerror or exc}）") from exc
    finally:
        try:
            if os.path.exists(temp_name):
                os.unlink(temp_name)
        except OSError:
            pass


def as_document(value):
    """接口里 dataStr 有时是 JSON 字符串，有时已经是 dict。"""
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except ValueError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def is_model_id(value):
    return isinstance(value, str) and bool(MODEL_ID_PATTERN.fullmatch(value))


def find_package_model(result):
    """从封装的 SVGNODE 节点读取 3D 文件 uuid。"""
    package = result.get("packageDetail") or {}
    document = as_document(package.get("dataStr"))
    head = document.get("head") if isinstance(document.get("head"), dict) else {}
    params = head.get("c_para") if isinstance(head.get("c_para"), dict) else {}
    default_title = params.get("3DModel") or package.get("title")
    for shape in document.get("shape") or []:
        if not isinstance(shape, str) or not shape.startswith("SVGNODE~"):
            continue
        node = as_document(shape.partition("~")[2])
        attrs = node.get("attrs") or {}
        if attrs.get("c_etype") not in (None, "outline3D"):
            continue
        if is_model_id(attrs.get("uuid")):
            return attrs["uuid"], attrs.get("title") or default_title or ""
    return None, None


def find_library_id(result):
    """封装里记录的官方 3D 库编号（可能与文件 uuid 不同）。"""
    for key in ("packageDetail", "dataStr"):
        raw = result.get(key)
        document = as_document(raw.get("dataStr")) if key == "packageDetail" and isinstance(raw, dict) else as_document(raw)
        head = document.get("head") if isinstance(document.get("head"), dict) else {}
        if is_model_id(head.get("uuid_3d")):
            return head["uuid_3d"]
    return None


# =====================================================
# 网络层
# =====================================================
@dataclass
class ModelInfo:
    code: str
    title: str
    model_id: str
    library_id: str | None = None


class JlcClient:
    """官方元件 / 模型接口客户端。"""

    def __init__(self):
        if requests is None:  # pragma: no cover - main() 已提前拦截
            raise JlcError("缺少 requests 依赖")
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": USER_AGENT,
            "Accept": "*/*",
            "Referer": "https://lceda.cn/",
        })
        self.using_browser_agent = False

    # ---------- 底层请求 ----------
    def _send(self, url, method="GET", data=None, stream=False):
        """发请求：连接类错误重试 2 次；遇到 418 自动换浏览器 UA 再试一次。"""
        agents = [BROWSER_AGENT] if self.using_browser_agent else [USER_AGENT, BROWSER_AGENT]
        last_error = None

        for index, agent in enumerate(agents):
            attempt = 0
            while attempt < 3:
                try:
                    response = self.session.request(
                        method, url, data=data, stream=stream, timeout=(CONNECT_TIMEOUT, READ_TIMEOUT),
                        headers={"User-Agent": agent},
                    )
                except requests.RequestException as exc:
                    last_error = exc
                    log_event("WARNING", f"请求失败（{attempt + 1}/3）：{url} {exc}")
                    attempt += 1
                    if attempt < 3:
                        time.sleep(0.6 * attempt)
                    continue

                if response.status_code == 418 and index + 1 < len(agents):
                    response.close()
                    log_event("WARNING", "接口返回 418，切换浏览器标识重试")
                    self.using_browser_agent = True
                    self.session.headers["User-Agent"] = BROWSER_AGENT
                    break  # 跳出重试，改用下一个 UA

                if response.status_code in (429, 500, 502, 503, 504) and attempt < 2:
                    delay = self._retry_delay(response)
                    log_event("WARNING", f"服务器返回 {response.status_code}，{delay:.1f}s 后重试")
                    response.close()
                    time.sleep(delay)
                    attempt += 1
                    continue

                return response

        raise JlcError(f"网络连接失败：{self._describe(last_error)}")

    @staticmethod
    def _describe(error):
        if isinstance(error, requests.exceptions.ConnectTimeout):
            return "连接超时，请检查网络或代理设置"
        if isinstance(error, requests.exceptions.ReadTimeout):
            return "读取超时，服务器响应过慢"
        if isinstance(error, requests.exceptions.SSLError):
            return "HTTPS 证书校验失败，请检查系统时间或代理"
        if isinstance(error, requests.exceptions.ConnectionError):
            return "无法连接到服务器，请检查网络/代理"
        return str(error) if error else "未知网络错误"

    @staticmethod
    def _retry_delay(response):
        """优先遵守 Retry-After（秒），否则用固定退避。"""
        try:
            delay = float(response.headers.get("Retry-After"))
        except (TypeError, ValueError):
            delay = 1.2
        return min(max(delay, 0.5), 8.0)

    def _json(self, url, method="GET", data=None, optional=False):
        response = self._send(url, method=method, data=data)
        try:
            if optional and response.status_code in (404, 410):
                return None
            if response.status_code == 418:
                raise JlcError(WAF_MESSAGE)
            if response.status_code >= 400:
                raise JlcError(f"服务器返回 HTTP {response.status_code}")
            try:
                return response.json()
            except ValueError as exc:
                log_event("WARNING", f"非 JSON 响应：{url}（{response.headers.get('Content-Type')}）")
                raise JlcError("接口返回了无法识别的数据，可能被风控拦截或接口有变更") from exc
        finally:
            response.close()

    # ---------- 模型解析 ----------
    def _component(self, code):
        """读取元件文档；所有镜像都明确未收录时返回 None。"""
        last_error = None
        for template in COMPONENT_ENDPOINTS:
            try:
                document = self._json(template.format(code=code), optional=True)
            except JlcError as exc:
                last_error = exc
                log_event("WARNING", f"元件接口失败：{template.format(code=code)} {exc}")
                continue
            if document is None:
                continue
            result = document.get("result") or {}
            if not document.get("success") or not result:
                return None
            return result
        if last_error is not None:
            raise last_error
        return None

    def _library_model(self, library_id):
        """把官方 3D 库记录（docType=16）解析成真正的模型文件 uuid。"""
        for template in LIBRARY_ENDPOINTS:
            try:
                document = self._json(template.format(uuid=library_id), optional=True)
            except JlcError as exc:
                log_event("WARNING", f"3D 库接口失败：{template.format(uuid=library_id)} {exc}")
                continue
            if not document or not document.get("success"):
                continue
            result = document.get("result") or {}
            if str(result.get("docType")) != "16":
                continue
            head = as_document(result.get("dataStr")).get("head")
            asset = head.get("uuid") if isinstance(head, dict) else None
            if is_model_id(asset):
                return asset
        return None

    def _pro_model(self, code):
        """备用链路：pro.lceda.cn 搜索接口（覆盖 EasyEDA 库里没有的器件）。"""
        try:
            products = (((self._json(
                f"{PRO_API}/eda/product/search", method="POST",
                data={"keyword": code, "needAggs": "true", "currPage": "1", "pageSize": "10"},
            ) or {}).get("result") or {}).get("productList") or [])
            if not products:
                return None
            product = next((p for p in products if str(p.get("code", "")).upper() == code), products[0])
            device = product.get("hasDevice")
            if not device:
                return None
            records = (self._json(f"{PRO_API}/devices/searchByIds", method="POST",
                                  data={"uuids[]": device}) or {}).get("result") or []
            attributes = (records[0] or {}).get("attributes") or {}
            model_uuid = attributes.get("3D Model")
            if not is_model_id(model_uuid):
                return None
            records = (self._json(f"{PRO_API}/components/searchByIds?forceOnline=1", method="POST",
                                  data={"uuids[]": model_uuid, "dataStr": "yes"}) or {}).get("result") or []
            asset = as_document((records[0] or {}).get("dataStr")).get("model")
            return asset if is_model_id(asset) else None
        except (JlcError, IndexError, KeyError, TypeError, AttributeError) as exc:
            log_event("WARNING", f"备用接口解析失败：{exc}")
            return None

    def resolve_model(self, code):
        code = (code or "").strip().upper()
        if not CODE_PATTERN.fullmatch(code):
            raise JlcError("请输入以 C 开头的立创编号，例如 C8734")

        result = self._component(code)
        title = str((result or {}).get("title") or "").strip()
        model_id, model_name = find_package_model(result or {})
        library_id = find_library_id(result or {})

        if model_id:
            log_event("INFO", f"{code} 使用封装内 3D 节点：{model_id}")
            return ModelInfo(code=code, title=model_name or title, model_id=model_id, library_id=library_id)

        if library_id:
            asset = self._library_model(library_id)
            if asset:
                log_event("INFO", f"{code} 通过 3D 库记录解析：{asset}")
                return ModelInfo(code=code, title=model_name or title, model_id=asset, library_id=library_id)

        # 备用链路：覆盖 EasyEDA 元件库未收录、但 JLCEDA 有 3D 数据的器件
        asset = self._pro_model(code)
        if asset:
            log_event("INFO", f"{code} 使用备用接口解析：{asset}")
            return ModelInfo(code=code, title=title, model_id=asset, library_id=library_id)

        if result is None:
            raise JlcError(f"官方库中没有 {code}，请核对编号")
        raise JlcError(f"{code}（{title or '未知器件'}）没有可用的 3D 模型")

    # ---------- 下载 ----------
    @staticmethod
    def _host(url):
        return url.split("/")[2]

    def _download_once(self, url, on_progress, cancelled):
        response = self._send(url, stream=True)
        try:
            if response.status_code in (404, 410):
                raise JlcError(f"{self._host(url)} 上没有该模型文件")
            if response.status_code == 418:
                raise JlcError(WAF_MESSAGE)
            if response.status_code >= 400:
                raise JlcError(f"{self._host(url)} 返回 HTTP {response.status_code}")

            try:
                total = int(response.headers.get("Content-Length") or 0)
            except ValueError:
                total = 0

            buffer = bytearray()
            for chunk in response.iter_content(DOWNLOAD_CHUNK):
                if cancelled.is_set():
                    raise Cancelled()
                if not chunk:
                    continue
                buffer.extend(chunk)
                if len(buffer) > MAX_MODEL_BYTES:
                    raise JlcError("模型文件超过 64 MB，已停止下载")
                if on_progress is not None:
                    on_progress(len(buffer), total)

            data = bytes(buffer)
            encoding = (response.headers.get("Content-Encoding") or "identity").lower()
            if total and encoding == "identity" and len(data) != total:
                raise JlcError(f"{self._host(url)} 下载不完整（{len(data)}/{total} 字节）")
            if b"ISO-10303-21" not in data[:2048]:
                raise JlcError(f"{self._host(url)} 返回的不是 STEP 文件（可能被风控拦截）")
            return data
        finally:
            response.close()

    def download_step(self, info, on_progress=None, cancelled=None):
        """按“封装模型 → 3D 库备用模型”的顺序下载，逐个镜像重试。"""
        cancelled = cancelled or threading.Event()
        candidates = [info.model_id]
        if info.library_id:
            try:
                alternative = self._library_model(info.library_id)
            except JlcError:
                alternative = None
            if alternative and alternative not in candidates:
                candidates.append(alternative)

        failures = []
        for uuid in candidates:
            for template in MODEL_ENDPOINTS:
                url = template.format(uuid=uuid)
                try:
                    return self._download_once(url, on_progress, cancelled)
                except Cancelled:
                    raise
                except JlcError as exc:
                    failures.append(f"{self._host(url)}：{exc}")
                    log_event("WARNING", f"模型下载失败：{url} {exc}")
        raise JlcError("下载失败 —— " + "；".join(failures))


# =====================================================
# 界面
# =====================================================
FONT = "Microsoft YaHei"

COLORS = {
    "bg": "#eef1f5",
    "card": "#ffffff",
    "border": "#dde3ea",
    "text": "#1f2328",
    "muted": "#6b7280",
    "accent": "#1f7a4d",
    "accent_dark": "#16603c",
    "accent_soft": "#e7f4ec",
    "danger": "#c62828",
    "danger_dark": "#a91f1f",
    "ghost": "#f1f3f5",
    "ghost_hover": "#e4e7eb",
    "ok": "#1a7f37",
    "warn": "#9a6700",
}

HELP_SECTIONS = (
    ("快速开始", (
        ("1. 在输入框填写立创编号（C 开头，例如 C8734、C25804）。", "body"),
        ("2. 按回车或点击「立即下载」。", "body"),
        ("3. 下载完成后点击「定位文件」，资源管理器会自动选中该 .step 文件。", "body"),
    )),
    ("界面说明", (
        ("· 编号输入框：只接受 C 开头加数字的立创编号。", "body"),
        ("· 立即下载：开始查询并下载，下载过程中按钮会置灰。", "body"),
        ("· 取消：仅在下载时出现，可随时中断，不会留下半截文件。", "body"),
        ("· 进度条：显示百分比、已下载大小和实时速度。", "body"),
        ("· 运行日志：记录每一步结果，红色表示错误。", "body"),
        ("· 更改目录 / 打开目录 / 定位文件：管理保存位置和已下载文件。", "body"),
    )),
    ("文件保存在哪里？", (
        ("默认保存到系统桌面（兼容 OneDrive 重定向），文件名固定为「编号.step」。", "body"),
        ("重复下载会覆盖同名文件；可通过「文件 → 更改下载目录…」修改，设置会自动记住。", "body"),
    )),
    ("配置与日志", (
        ("%LOCALAPPDATA%\\JLC3D-Downloader\\", "mono"),
        ("  config.ini   保存目录设置", "mono"),
        ("  app.log      运行日志（超过 1 MB 自动轮转）", "mono"),
        ("旧版本的 ~/.jlc3d/config.ini 会在首次启动时自动迁移。", "body"),
    )),
    ("常见问题", (
        ("· 提示 HTTP 418：被嘉立创风控拦截，程序会自动换 UA 重试一次；仍失败请稍后再试。", "body"),
        ("· 提示「官方库中没有该编号」：编号不存在，或该器件没有 3D 模型。", "body"),
        ("· 提示「没有可用的 3D 模型」：器件存在，但封装未关联 3D 文件。", "body"),
        ("· 写入失败 / 文件被占用：关闭正在使用该文件的程序后重试。", "body"),
        ("· 无法连接服务器：检查网络或系统代理设置。", "body"),
    )),
    ("快捷键", (
        ("Enter   开始下载", "body"),
        ("Esc     取消下载", "body"),
        ("F1      打开本说明", "body"),
    )),
    ("数据来源", (
        ("模型与元件数据来自 JLCEDA / EasyEDA 官方库，版权归嘉立创 EDA 所有。", "body"),
        ("本工具仅做本地便捷转存，请遵守官方库的使用条款。", "body"),
    )),
)


class JLC3DApp(tk.Tk):
    def __init__(self):
        set_app_user_model_id()
        super().__init__()
        self.settings = load_settings()
        self.download_path = Path(self.settings["download_path"])
        self.events = queue.Queue()
        self.worker = None
        self.cancel_event = threading.Event()
        self.last_file = None
        self._alive = True
        self._progress_mode = "determinate"

        self.title(f"嘉立创 3D 模型下载器  v{VERSION}")
        self._icon_image = None
        apply_window_icon(self)
        self.configure(bg=COLORS["bg"])
        self._center(740, 540)
        self.minsize(680, 500)
        self._init_styles()

        self._build_menu()
        self._build_ui()
        self._append_log("输入立创编号（例如 C8734），按回车或点击「立即下载」。", "dim")
        self.entry.focus_set()
        self._pump()

        self.bind("<F1>", lambda event: self.show_help())
        self.bind("<Escape>", lambda event: self.cancel_download())
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.report_callback_exception = self._on_tk_error
        log_event("INFO", f"启动 {APP_NAME} {VERSION}，下载目录：{self.download_path}")

    # ---------- 布局 ----------
    def _center(self, width, height):
        screen_w = self.winfo_screenwidth()
        screen_h = self.winfo_screenheight()
        x = max(0, (screen_w - width) // 2)
        y = max(0, (screen_h - height) // 3)
        self.geometry(f"{width}x{height}+{x}+{y}")

    def _build_menu(self):
        menu = tk.Menu(self)
        self.config(menu=menu)

        file_menu = tk.Menu(menu, tearoff=0)
        menu.add_cascade(label="文件", menu=file_menu)
        file_menu.add_command(label="更改下载目录…", command=self.choose_path)
        file_menu.add_command(label="打开下载目录", command=self.open_download_dir)
        file_menu.add_separator()
        file_menu.add_command(label="打开日志文件", command=self.open_log)
        file_menu.add_separator()
        file_menu.add_command(label="退出", command=self._on_close)

        help_menu = tk.Menu(menu, tearoff=0)
        menu.add_cascade(label="帮助", menu=help_menu)
        help_menu.add_command(label="使用说明", accelerator="F1", command=self.show_help)
        help_menu.add_separator()
        help_menu.add_command(label="关于", command=self.show_about)
        help_menu.add_command(label="项目主页", command=lambda: webbrowser.open(REPO_URL))

    # ---------- 主题与通用控件 ----------
    def _init_styles(self):
        style = ttk.Style(self)
        if "clam" in style.theme_names():
            style.theme_use("clam")
        style.configure(
            "Jlc.Horizontal.TProgressbar",
            troughcolor="#e6eaef",
            background=COLORS["accent"],
            bordercolor="#e6eaef",
            lightcolor=COLORS["accent"],
            darkcolor=COLORS["accent"],
            thickness=8,
        )

    def _build_header(self):
        """顶部大标题栏。"""
        header = tk.Frame(self, bg=COLORS["accent"])
        header.pack(fill="x")
        inner = tk.Frame(header, bg=COLORS["accent"], padx=24, pady=15)
        inner.pack(fill="x")

        left = tk.Frame(inner, bg=COLORS["accent"])
        left.pack(side="left", anchor="w")
        tk.Label(left, text="嘉立创 3D 模型下载器", bg=COLORS["accent"], fg="#ffffff",
                 font=(FONT, 19, "bold")).pack(anchor="w")
        tk.Label(left, text="JLC3D-Downloader · 输入立创编号，一键下载官方 STEP 模型",
                 bg=COLORS["accent"], fg="#cfe8da", font=(FONT, 9)).pack(anchor="w", pady=(4, 0))

        tk.Label(inner, text=f"v{VERSION}", bg="#2f8f5f", fg="#ffffff",
                 font=(FONT, 9, "bold"), padx=10, pady=3).pack(side="right", anchor="n")

    def _card(self, parent):
        return tk.Frame(parent, bg=COLORS["card"],
                        highlightbackground=COLORS["border"], highlightthickness=1)

    def _button(self, parent, text, command, kind="ghost", small=False, width=None):
        palette = {
            "primary": (COLORS["accent"], "#ffffff", COLORS["accent_dark"]),
            "danger": (COLORS["danger"], "#ffffff", COLORS["danger_dark"]),
            "ghost": (COLORS["ghost"], COLORS["text"], COLORS["ghost_hover"]),
        }
        background, foreground, hover = palette[kind]
        button = tk.Button(
            parent, text=text, command=command, bg=background, fg=foreground,
            activebackground=hover, activeforeground=foreground, disabledforeground="#9aa3af",
            relief="flat", bd=0, cursor="hand2",
            font=(FONT, 9 if small else 10, "bold" if kind != "ghost" else "normal"),
            padx=10 if small else 16, pady=3 if small else 6,
        )
        if width is not None:
            button.config(width=width)
        self._bind_hover(button, background, hover)
        return button

    @staticmethod
    def _bind_hover(widget, normal, hover):
        def enter(event):
            if str(widget.cget("state")) == "normal":
                widget.config(bg=hover)

        def leave(event):
            if str(widget.cget("state")) == "normal":
                widget.config(bg=normal)

        widget.bind("<Enter>", enter)
        widget.bind("<Leave>", leave)

    def _build_ui(self):
        self._build_header()

        body = tk.Frame(self, bg=COLORS["bg"], padx=22, pady=16)
        body.pack(fill="both", expand=True)
        body.columnconfigure(0, weight=1)
        body.rowconfigure(2, weight=1)

        # 1. 输入卡片
        input_card = self._card(body)
        input_card.grid(row=0, column=0, sticky="ew")
        input_inner = tk.Frame(input_card, bg=COLORS["card"], padx=18, pady=14)
        input_inner.pack(fill="x")

        input_row = tk.Frame(input_inner, bg=COLORS["card"])
        input_row.pack(fill="x")
        tk.Label(input_row, text="元器件编号", bg=COLORS["card"], fg=COLORS["text"],
                 font=(FONT, 11, "bold")).pack(side="left", padx=(0, 14))
        self.entry = tk.Entry(
            input_row, font=("Consolas", 15), width=15, relief="flat", bd=0,
            bg="#f7f9fb", fg=COLORS["text"], insertbackground=COLORS["text"],
            highlightthickness=1, highlightbackground=COLORS["border"],
            highlightcolor=COLORS["accent"], disabledbackground="#f1f3f5",
        )
        self.entry.insert(0, "C8734")
        self.entry.pack(side="left", ipady=6, padx=(0, 12))
        self.entry.bind("<Return>", lambda event: self.start_download())
        self.entry.bind("<FocusIn>", lambda event: self.entry.select_range(0, "end"))

        self.btn_download = self._button(input_row, "立即下载", self.start_download,
                                         kind="primary", width=9)
        self.btn_download.pack(side="left")
        self.btn_cancel = self._button(input_row, "取消", self.cancel_download,
                                       kind="danger", width=6)

        tk.Label(input_inner, text="支持立创编号（C 开头）· 回车开始 · Esc 取消 · F1 使用说明",
                 bg=COLORS["card"], fg=COLORS["muted"], font=(FONT, 9)).pack(anchor="w", pady=(10, 0))

        # 2. 进度
        progress_row = tk.Frame(body, bg=COLORS["bg"])
        progress_row.grid(row=1, column=0, sticky="ew", pady=(14, 10))
        progress_row.columnconfigure(0, weight=1)
        self.progress = ttk.Progressbar(progress_row, style="Jlc.Horizontal.TProgressbar",
                                        mode="determinate", maximum=100)
        self.progress.grid(row=0, column=0, sticky="ew")
        self.status_label = tk.Label(progress_row, text="就绪", bg=COLORS["bg"], fg=COLORS["muted"],
                                     font=(FONT, 9), anchor="e", width=34)
        self.status_label.grid(row=0, column=1, sticky="e", padx=(12, 2))

        # 3. 日志卡片
        log_card = self._card(body)
        log_card.grid(row=2, column=0, sticky="nsew")
        log_card.rowconfigure(1, weight=1)
        log_card.columnconfigure(0, weight=1)

        log_head = tk.Frame(log_card, bg=COLORS["card"])
        log_head.grid(row=0, column=0, sticky="ew", padx=16, pady=(10, 4))
        tk.Label(log_head, text="运行日志", bg=COLORS["card"], fg=COLORS["text"],
                 font=(FONT, 10, "bold")).pack(side="left")
        self.btn_clear = self._button(log_head, "清空", self.clear_log, kind="ghost", small=True)
        self.btn_clear.pack(side="right")

        self.log = scrolledtext.ScrolledText(
            log_card, height=9, font=("Consolas", 10), bg="#fbfcfd", fg=COLORS["text"],
            relief="flat", borderwidth=0, wrap="word", padx=10, pady=6,
            insertbackground=COLORS["text"],
        )
        self.log.grid(row=1, column=0, sticky="nsew", padx=1, pady=(0, 1))
        self.log.tag_config("info", foreground=COLORS["text"])
        self.log.tag_config("ok", foreground=COLORS["ok"])
        self.log.tag_config("warn", foreground=COLORS["warn"])
        self.log.tag_config("err", foreground=COLORS["danger"])
        self.log.tag_config("dim", foreground=COLORS["muted"])
        self.log.configure(state="disabled")

        # 4. 底部
        footer = tk.Frame(body, bg=COLORS["bg"])
        footer.grid(row=3, column=0, sticky="ew", pady=(12, 0))
        footer.columnconfigure(0, weight=1)
        self.path_label = tk.Label(footer, text=self._path_text(), bg=COLORS["bg"],
                                   fg=COLORS["muted"], font=(FONT, 9), anchor="w")
        self.path_label.grid(row=0, column=0, sticky="ew")
        self.btn_choose = self._button(footer, "更改目录", self.choose_path)
        self.btn_choose.grid(row=0, column=1, padx=(8, 0))
        self.btn_open = self._button(footer, "打开目录", self.open_download_dir)
        self.btn_open.grid(row=0, column=2, padx=(6, 0))
        self.btn_locate = self._button(footer, "定位文件", self.locate_file)
        self.btn_locate.config(state="disabled")
        self.btn_locate.grid(row=0, column=3, padx=(6, 0))

    def clear_log(self):
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")

    # ---------- 事件循环 ----------
    def _pump(self):
        if not self._alive:
            return
        try:
            while True:
                kind, payload = self.events.get_nowait()
                try:
                    if kind == "log":
                        self._append_log(payload[0], payload[1])
                    elif kind == "progress":
                        self._show_progress(*payload)
                    elif kind == "state":
                        self._set_busy(payload == "busy")
                    elif kind == "done":
                        self._on_done(payload[0])
                    elif kind == "cancelled":
                        self._on_cancelled()
                    elif kind == "error":
                        self._on_error(payload[0])
                except Exception as exc:  # 单个消息处理失败不影响后续刷新
                    log_event("ERROR", f"处理界面消息失败：{kind} {exc}",
                              exc_info=(type(exc), exc, exc.__traceback__))
                    self._append_log(f"内部错误：{exc}", "err")
        except queue.Empty:
            pass
        finally:
            if self._alive:
                self.after(80, self._pump)

    def _post(self, kind, *payload):
        self.events.put((kind, payload))

    def _append_log(self, message, tag="info"):
        timestamp = datetime.now().strftime("%H:%M:%S")
        self.log.configure(state="normal")
        self.log.insert("end", f"[{timestamp}] {message}\n", tag)
        self.log.see("end")
        self.log.configure(state="disabled")

    # ---------- 状态 ----------
    def _set_busy(self, busy):
        self.btn_download.config(
            state="disabled" if busy else "normal",
            text="下载中…" if busy else "立即下载",
            bg=COLORS["muted"] if busy else COLORS["accent"],
        )
        self.entry.config(state="disabled" if busy else "normal")
        if busy:
            self.btn_cancel.config(state="normal")
            if not self.btn_cancel.winfo_manager():
                self.btn_cancel.pack(side="left", padx=(8, 0))
            self.progress.stop()
            self.progress.config(mode="determinate", maximum=100, value=0)
            self._progress_mode = "determinate"
            self.status_label.config(text="连接中…", fg=COLORS["muted"])
        else:
            self.btn_cancel.pack_forget()

    def _show_progress(self, done, total, elapsed):
        if total and total > 0:
            if self._progress_mode != "determinate":
                self.progress.stop()
                self.progress.config(mode="determinate", maximum=100, value=0)
                self._progress_mode = "determinate"
            percent = min(100.0, done * 100.0 / total)
            self.progress["value"] = percent
            speed = done / elapsed if elapsed > 0.05 else 0
            self.status_label.config(
                text=f"{percent:5.1f}%  {format_size(done)}/{format_size(total)}  {format_size(speed)}/s",
                fg=COLORS["text"])
        else:
            if self._progress_mode != "indeterminate":
                self.progress.config(mode="indeterminate", maximum=100)
                self.progress.start(15)
                self._progress_mode = "indeterminate"
            speed = done / elapsed if elapsed > 0.05 else 0
            self.status_label.config(text=f"已下载 {format_size(done)}  {format_size(speed)}/s",
                                     fg=COLORS["text"])

    def _on_done(self, path):
        self.progress.stop()
        self.progress.config(mode="determinate", maximum=100, value=100)
        self._progress_mode = "determinate"
        size = os.path.getsize(path)
        self.status_label.config(text=f"完成  {format_size(size)}", fg=COLORS["ok"])
        self.last_file = Path(path)
        self.btn_locate.config(state="normal")
        self._append_log(f"已保存：{path}", "ok")

    def _on_cancelled(self):
        self.progress.stop()
        self.progress.config(mode="determinate", maximum=100, value=0)
        self._progress_mode = "determinate"
        self.status_label.config(text="已取消", fg=COLORS["warn"])

    def _on_error(self, message):
        self.progress.stop()
        self.progress.config(mode="determinate", maximum=100, value=0)
        self._progress_mode = "determinate"
        self.status_label.config(text="失败", fg=COLORS["danger"])
        self._append_log(message, "err")

    def _on_tk_error(self, exc, value, tb):
        log_event("ERROR", "界面回调异常", exc_info=(exc, value, tb))
        self._append_log(f"内部错误：{value}", "err")

    # ---------- 下载 ----------
    def start_download(self):
        if self.worker is not None and self.worker.is_alive():
            return
        code = self.entry.get().strip().upper()
        if not code:
            messagebox.showwarning("提示", "请输入元器件编号", parent=self)
            return
        if not CODE_PATTERN.fullmatch(code):
            messagebox.showwarning("编号格式不正确",
                                   "请输入以 C 开头的立创编号，例如 C8734。", parent=self)
            return
        self.entry.delete(0, "end")
        self.entry.insert(0, code)
        self.cancel_event.clear()
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")
        self._append_log(f"开始查询 {code} …", "dim")
        self._set_busy(True)
        self.worker = threading.Thread(target=self._work, args=(code,), daemon=True)
        self.worker.start()

    def cancel_download(self):
        if self.worker is not None and self.worker.is_alive():
            self.cancel_event.set()
            self.btn_cancel.config(state="disabled")
            self._append_log("正在取消…", "warn")

    def _work(self, code):
        started = time.time()
        last_update = [0.0]
        try:
            client = JlcClient()
            info = client.resolve_model(code)
            self._post("log", f"找到模型：{info.title or code}", "info")

            def on_progress(done, total):
                now = time.time()
                if now - last_update[0] >= 0.1:
                    last_update[0] = now
                    self._post("progress", done, total, now - started)

            data = client.download_step(info, on_progress, self.cancel_event)
            if self.cancel_event.is_set():
                raise Cancelled()
            target = self.download_path / f"{safe_filename(code)}.step"
            existed = target.exists()
            atomic_write(target, data)
            elapsed = time.time() - started
            suffix = "（已覆盖同名文件）" if existed else ""
            self._post("log", f"下载完成，用时 {elapsed:.1f}s{suffix}", "ok")
            self._post("done", str(target))
        except Cancelled:
            self._post("log", "已取消下载，未写入文件", "warn")
            self._post("cancelled")
        except JlcError as exc:
            log_event("WARNING", f"{code} 下载失败：{exc}")
            self._post("error", str(exc))
        except Exception as exc:  # 兜底：任何异常都要让用户看到
            log_event("ERROR", f"{code} 未预期异常", exc_info=(type(exc), exc, exc.__traceback__))
            self._post("error", f"发生未预期的错误：{exc}")
        finally:
            self._post("state", "idle")

    # ---------- 路径 ----------
    def _path_text(self):
        text = str(self.download_path)
        return f"保存至：{text}" if len(text) <= 66 else f"保存至：…{text[-64:]}"

    def choose_path(self):
        initial = self.download_path if self.download_path.is_dir() else known_desktop()
        chosen = filedialog.askdirectory(initialdir=str(initial), title="选择 STEP 文件保存目录", parent=self)
        if chosen:
            self.download_path = Path(chosen)
            save_settings(self.download_path)
            self.path_label.config(text=self._path_text())
            self._append_log(f"下载目录已改为：{self.download_path}", "dim")

    def open_download_dir(self):
        target = self.download_path
        if not target.is_dir():
            messagebox.showwarning("目录不存在", f"下载目录不存在：\n{target}\n\n请重新选择。", parent=self)
            return
        if sys.platform.startswith("win"):
            os.startfile(str(target))  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(target)])
        else:
            subprocess.Popen(["xdg-open", str(target)])

    def locate_file(self):
        if not self.last_file or not self.last_file.exists():
            messagebox.showinfo("提示", "还没有可定位的下载文件", parent=self)
            return
        path = str(self.last_file)
        if sys.platform.startswith("win"):
            subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])
        elif sys.platform == "darwin":
            subprocess.Popen(["open", "-R", path])
        else:
            subprocess.Popen(["xdg-open", os.path.dirname(path)])

    def open_log(self):
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        if not LOG_FILE.exists():
            LOG_FILE.touch()
        if sys.platform.startswith("win"):
            os.startfile(str(LOG_FILE))  # type: ignore[attr-defined]
        else:
            webbrowser.open(LOG_FILE.as_uri())

    # ---------- 使用说明 / 关于 ----------
    def show_help(self):
        window = tk.Toplevel(self)
        window.title("使用说明")
        window.configure(bg=COLORS["card"])
        window.transient(self)
        window.minsize(520, 420)

        header = tk.Frame(window, bg=COLORS["accent"])
        header.pack(fill="x")
        tk.Label(header, text="使用说明", bg=COLORS["accent"], fg="#ffffff",
                 font=(FONT, 14, "bold"), padx=20, pady=12).pack(side="left")
        tk.Label(header, text=f"v{VERSION}", bg=COLORS["accent"], fg="#cfe8da",
                 font=(FONT, 9)).pack(side="right", padx=16)

        footer = tk.Frame(window, bg=COLORS["card"], padx=18, pady=10)
        footer.pack(side="bottom", fill="x")
        tk.Label(footer, text="提示：按 Esc 可关闭本窗口", bg=COLORS["card"],
                 fg=COLORS["muted"], font=(FONT, 9)).pack(side="left")
        self._button(footer, "关闭", window.destroy, kind="primary").pack(side="right")

        body = tk.Frame(window, bg=COLORS["card"])
        body.pack(fill="both", expand=True, padx=18, pady=(14, 4))
        body.rowconfigure(0, weight=1)
        body.columnconfigure(0, weight=1)

        viewer = tk.Text(body, height=14, wrap="word", relief="flat", bg="#ffffff",
                         fg=COLORS["text"], font=(FONT, 10), padx=10, pady=6,
                         spacing1=1, spacing2=2, spacing3=6, cursor="arrow",
                         highlightthickness=0)
        scrollbar = ttk.Scrollbar(body, orient="vertical", command=viewer.yview)
        viewer.configure(yscrollcommand=scrollbar.set)
        viewer.grid(row=0, column=0, sticky="nsew")
        scrollbar.grid(row=0, column=1, sticky="ns")

        viewer.tag_config("h1", font=(FONT, 12, "bold"), foreground=COLORS["accent"],
                          spacing1=10, spacing3=4)
        viewer.tag_config("body", font=(FONT, 10), foreground=COLORS["text"], lmargin1=4, lmargin2=4)
        viewer.tag_config("mono", font=("Consolas", 9), foreground="#0b4f8a",
                          background="#f4f7fa", lmargin1=4, lmargin2=4)
        for title, lines in HELP_SECTIONS:
            viewer.insert("end", title + "\n", "h1")
            for line, tag in lines:
                viewer.insert("end", line + "\n", tag)
            viewer.insert("end", "\n")
        viewer.config(state="disabled")

        # 内容构建完成后再固定尺寸，避免被 Text 的默认高度撑大
        self._center_window_on(window, 620, 500, self)
        window.bind("<Escape>", lambda event: window.destroy())
        window.focus_set()

    def show_about(self):
        about = tk.Toplevel(self)
        about.title("关于")
        about.configure(bg=COLORS["card"])
        about.resizable(False, False)

        header = tk.Frame(about, bg=COLORS["accent"])
        header.pack(fill="x")
        tk.Label(header, text="嘉立创 3D 模型下载器", bg=COLORS["accent"], fg="#ffffff",
                 font=(FONT, 14, "bold"), padx=18, pady=12).pack(side="left")
        tk.Label(header, text=f"v{VERSION}", bg=COLORS["accent"], fg="#cfe8da",
                 font=(FONT, 9)).pack(side="right", padx=16)

        footer = tk.Frame(about, bg=COLORS["card"], pady=10)
        footer.pack(side="bottom", fill="x")
        self._button(footer, "关闭", about.destroy, kind="ghost").pack()

        text = tk.Text(about, height=10, wrap="word", padx=18, pady=12, font=(FONT, 10),
                       relief="flat", bg=COLORS["card"], fg=COLORS["text"],
                       cursor="arrow", spacing3=4, highlightthickness=0)
        text.pack(fill="both", expand=True)
        content = (
            f"作者：{AUTHOR}\n\n"
            f"{REPO_URL}\n\n"
            "数据来源：JLCEDA / EasyEDA 官方库，\n"
            "模型版权归嘉立创 EDA 所有。\n"
            "本工具仅用于本地便捷转存。\n\n"
            "思路参考 kulya97 与 LCSC3D 项目"
        )
        text.insert("1.0", content)
        start = text.search(REPO_URL, "1.0", stopindex="end")
        if start:
            end = f"{start}+{len(REPO_URL)}c"
            text.tag_add("link", start, end)
            text.tag_config("link", foreground="#0b5ed7", underline=True)
            text.tag_bind("link", "<Button-1>", lambda event: webbrowser.open(REPO_URL))
            text.tag_bind("link", "<Enter>", lambda event: text.config(cursor="hand2"))
            text.tag_bind("link", "<Leave>", lambda event: text.config(cursor="arrow"))
        text.config(state="disabled")
        self._center_window_on(about, 500, 370, self)

    @staticmethod
    def _center_window_on(window, width, height, parent):
        parent.update_idletasks()
        x = parent.winfo_rootx() + (parent.winfo_width() - width) // 2
        y = parent.winfo_rooty() + (parent.winfo_height() - height) // 3
        window.geometry(f"{width}x{height}+{max(0, x)}+{max(0, y)}")

    # ---------- 关闭 ----------
    def _on_close(self):
        if self.worker is not None and self.worker.is_alive():
            if not messagebox.askyesno("正在下载", "下载还没完成，确定要退出吗？", parent=self):
                return
            self.cancel_event.set()
        self._alive = False
        try:
            self.destroy()
        except tk.TclError:
            pass


# =====================================================
# 命令行自检
# =====================================================
def run_selftest(code="C8734"):
    """无界面自检：解析并下载一个编号，返回进程退出码。"""
    code = (code or "C8734").strip().upper()
    try:
        client = JlcClient()
        info = client.resolve_model(code)
        print(f"[selftest] {code} -> {info.title!r} model={info.model_id}")
        data = client.download_step(info)
        print(f"[selftest] downloaded {len(data)} bytes, head={data[:16]!r}")
        workdir = Path(tempfile.mkdtemp(prefix="jlc3d-selftest-"))
        try:
            target = workdir / f"{safe_filename(code)}.step"
            atomic_write(target, data)
            print(f"[selftest] write check OK ({target.stat().st_size} bytes)")
        finally:
            shutil.rmtree(workdir, ignore_errors=True)
        log_event("INFO", f"自检成功：{code} {len(data)} bytes")
        return 0
    except Exception as exc:
        print(f"[selftest] failed: {exc}")
        log_event("ERROR", f"自检失败：{code}", exc_info=(type(exc), exc, exc.__traceback__))
        return 1


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--selftest":
        code = sys.argv[2] if len(sys.argv) > 2 else "C8734"
        return run_selftest(code)

    if requests is None:
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror(
            "缺少依赖",
            "未安装 requests 库，程序无法运行。\n\n"
            "请在命令行执行：\n"
            "    pip install -r requirements.txt\n\n"
            "或单独安装：pip install requests",
        )
        root.destroy()
        return 1

    sys.excepthook = lambda exc, value, tb: log_event("ERROR", "未捕获异常", exc_info=(exc, value, tb))
    app = JLC3DApp()
    app.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
