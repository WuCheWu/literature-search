# -*- coding: utf-8 -*-
"""公共支撑模块：配置读取、日志、输出目录、文件名、JSON、requests 会话。

用途：被其余脚本 import，不单独执行。
退出码约定（全技能统一）：
    0 成功 / 1 部分失败 / 2 需用户处理（VPN、机构身份、滑块） / 3 配置或环境错误
依赖：标准库 + requests（仅 build_session 用到）
"""
from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path

EXIT_OK = 0
EXIT_PARTIAL = 1
EXIT_NEED_USER = 2
EXIT_CONFIG = 3

SKILL_ROOT = Path(__file__).resolve().parent.parent
ILLEGAL_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f]')

REQUIRED_KEYS = [
    "vpn.portal",
    "vpn.client_dir",
    "vpn.vnic_desc_match",
    "institution.name",
    "institution.library_url",
    "databases.pubmed.enabled",
    "databases.cnki.enabled",
    "browser.channel",
    "browser.profile_dir",
    "browser.headless",
    "browser.verify_wait_sec",
    "download.max_per_run",
    "download.interval_sec",
    "download.timeout_sec",
    "download.retry",
    "download.min_bytes",
    "output_root",
]


def setup_console() -> None:
    """控制台防乱码：跟随当前终端编码，只放宽错误处理。

    实测：PowerShell 5.1 控制台是 GBK(936)，强行写 UTF-8 会变成「瑙ｉ噴鍣?」；
    Git Bash 是 UTF-8。所以不硬编码 UTF-8，只把 errors 设为 replace，
    避免生僻符号（≥、→）导致 UnicodeEncodeError 直接崩掉。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            encoding = (getattr(stream, "encoding", "") or "").lower().replace("-", "")
            if encoding and encoding not in ("utf8", "utf8mb4"):
                stream.reconfigure(errors="replace")
        except Exception:
            pass


def print_env() -> None:
    """每个脚本启动时打印实际解释器——bash 与 PowerShell 的 python 不是同一个。
    走 stderr：stdout 要留给 `--json`，宿主才能直接 json.loads。"""
    print(f"[env] 解释器: {sys.executable}", file=sys.stderr)
    print(f"[env] 版本: {sys.version.split()[0]}  平台: {sys.platform}", file=sys.stderr)


def die(code: int, message: str) -> "None":
    print(f"\n[退出码 {code}] {message}", file=sys.stderr)
    sys.exit(code)


def require_deps(*modules: str) -> None:
    """缺第三方依赖时给安装命令并以退出码 3 结束，而不是甩一段 Traceback。

    参数用 import 名（如 "openpyxl"）。只检查、不自动安装——装不装由用户决定。
    """
    import importlib.util

    missing = [name for name in modules if importlib.util.find_spec(name) is None]
    if not missing:
        return
    die(
        EXIT_CONFIG,
        "缺少 Python 依赖：" + "、".join(missing) + "\n"
        "先一次性装齐：python -m pip install " + " ".join(missing) + "\n"
        "装完重新运行。",
    )


def _dig(data: dict, dotted: str):
    cur = data
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            raise KeyError(dotted)
        cur = cur[part]
    return cur


def load_config(path: str | None = None, require: bool = True) -> dict:
    """读 config.json 并校验必填字段；缺失即退出码 3（P0 验收点）。"""
    cfg_path = Path(path) if path else SKILL_ROOT / "config.json"
    if not cfg_path.exists():
        if not require:
            return {}
        die(
            EXIT_CONFIG,
            f"找不到配置文件：{cfg_path}\n"
            f"请先执行：Copy-Item config.example.json config.json\n"
            f"（在 {SKILL_ROOT} 目录下）",
        )
    try:
        cfg = json.loads(read_text_auto(cfg_path))
    except json.JSONDecodeError as exc:
        die(EXIT_CONFIG, f"配置文件不是合法 JSON：{cfg_path}\n{exc}")
    missing = []
    for key in REQUIRED_KEYS:
        try:
            _dig(cfg, key)
        except KeyError:
            missing.append(key)
    if missing:
        die(EXIT_CONFIG, "config.json 缺少必填字段：" + "、".join(missing))

    if not isinstance(cfg["browser"]["verify_wait_sec"], (int, float)):
        die(EXIT_CONFIG, "browser.verify_wait_sec 必须是数字（秒）")
    if not isinstance(cfg["download"]["max_per_run"], int):
        die(EXIT_CONFIG, "download.max_per_run 必须是整数")
    cfg["_config_path"] = str(cfg_path)
    return cfg


def kill_proxy_env() -> list[str]:
    """清掉会话注入的 *_PROXY。Playwright 子进程会继承，必须启动前清。"""
    removed = []
    for key in list(os.environ):
        if key.upper().endswith("_PROXY"):
            removed.append(key)
            os.environ.pop(key, None)
    return removed


def build_session():
    """requests 会话：禁用 trust_env，否则会走 127.0.0.1 代理卡死。"""
    require_deps("requests")
    import requests

    session = requests.Session()
    session.trust_env = False
    session.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
            )
        }
    )
    return session


def sanitize_filename(text: str, maxlen: int = 120) -> str:
    cleaned = ILLEGAL_CHARS.sub("_", str(text)).strip().rstrip(". ")
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned[:maxlen] or "untitled"


def unique_path(directory: Path, stem: str, suffix: str) -> Path:
    """重名加 _2、_3，绝不覆盖已有文件。"""
    directory.mkdir(parents=True, exist_ok=True)
    candidate = directory / f"{stem}{suffix}"
    index = 2
    while candidate.exists():
        candidate = directory / f"{stem}_{index}{suffix}"
        index += 1
    return candidate


def make_run_dir(cfg: dict, keyword: str, when: datetime | None = None) -> Path:
    """输出目录：<output_root>/<关键词>_<YYYYMMDD>/，内含 全文/ 与 日志/。"""
    when = when or datetime.now()
    root = Path(cfg["output_root"])
    run_dir = root / f"{sanitize_filename(keyword, 40)}_{when:%Y%m%d}"
    (run_dir / "全文").mkdir(parents=True, exist_ok=True)
    (run_dir / "日志").mkdir(parents=True, exist_ok=True)
    return run_dir


def read_text_auto(path: Path | str) -> str:
    """按 utf-8-sig → gbk → utf-8(替换) 依次解码读文本。

    PowerShell 5.1 的 `Set-Content -Encoding UTF8` 会写 BOM，记事本另存为 ANSI 则是 GBK；
    直接用 utf-8 读会当场抛 Traceback，用户只会看到一段红字。
    """
    data = Path(path).read_bytes()
    for encoding in ("utf-8-sig", "gbk"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", "replace")


def read_json(path: Path | str, default=None):
    p = Path(path)
    if not p.exists():
        return default
    try:
        return json.loads(read_text_auto(p))
    except json.JSONDecodeError:
        return default


def write_json(path: Path | str, data) -> Path:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return p


class RunLog:
    """同时写控制台与 日志/run.log。异常必须原文落盘（R9）。"""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = self.path.open("a", encoding="utf-8")

    def __call__(self, message: str) -> None:
        stamp = datetime.now().strftime("%H:%M:%S")
        line = f"[{stamp}] {message}"
        print(line)
        self._fh.write(line + "\n")
        self._fh.flush()

    def raw(self, text: str) -> None:
        self._fh.write(text.rstrip() + "\n")
        self._fh.flush()

    def close(self) -> None:
        try:
            self._fh.close()
        except Exception:
            pass


def read_records(run_dir: Path) -> list[dict]:
    """合并 records_*.json（PubMed / 知网 / 各平台），供筛选、下载与 Excel 使用。

    PubMed、知网排在最前：跨库重复时以它们的题录为准（字段最全）。
    """
    merged: list[dict] = []
    priority = {"records_pubmed.json": 0, "records_cnki.json": 1}
    paths = sorted(
        Path(run_dir).glob("records_*.json"),
        key=lambda p: (priority.get(p.name, 2), p.name),
    )
    for path in paths:
        data = read_json(path, default=[]) or []
        if isinstance(data, list):
            merged.extend(data)
    return merged


def summarize(records: list[dict]) -> dict:
    counts: dict[str, int] = {}
    for rec in records:
        status = rec.get("全文状态") or "未下载"
        counts[status] = counts.get(status, 0) + 1
    return counts
