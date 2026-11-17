# -*- coding: utf-8 -*-
"""browser_util.py —— Playwright 公共封装（可见窗口、复用 profile、绕开代理）

用途：被 inst_check / cnki_search / download_fulltext import。
要点（实测教训，勿改）：
  1. 启动前必须清掉所有 *_PROXY；浏览器子进程会继承，否则 ERR_PROXY_CONNECTION_FAILED。
  2. launch 加 --no-proxy-server；不要给 context 传 proxy（传 direct:// 同样失败）。
  3. 必须可见窗口 + 持久化 user_data_dir；headless 一律被滑块拦，profile 不复用则每次都要人拖。
  4. 滑块只能人工过：打印指引 → 每 2 秒查 URL → 超时截图并退出码 2。
依赖：playwright（channel=msedge）
"""
from __future__ import annotations

import time
from datetime import datetime
from pathlib import Path

import litcommon as lc


def load_selectors(path: str | Path | None, log=None) -> dict:
    """读 selectors.json；不存在时从 example 复制并提示校准。"""
    target = Path(path) if path else lc.SKILL_ROOT / "selectors.json"
    if not target.exists():
        example = lc.SKILL_ROOT / "selectors.example.json"
        if example.exists():
            target.write_text(example.read_text(encoding="utf-8"), encoding="utf-8")
            if log:
                log(f"[选择器] 未找到 {target.name}，已从 example 复制；首次运行需对照页面校准。")
    import json

    try:
        return json.loads(lc.read_text_auto(target))
    except Exception as exc:  # noqa: BLE001
        if log:
            log(f"[选择器] 读取失败（{exc}），改用内置候选。")
        return {}


def open_context(cfg: dict, log=None):
    """返回 (playwright, context, page)。调用方负责 browser_close()。"""
    lc.require_deps("playwright")
    from playwright.sync_api import sync_playwright

    removed = lc.kill_proxy_env()
    if log and removed:
        log(f"[浏览器] 已清除代理变量：{', '.join(removed)}")
    profile = Path(cfg["browser"]["profile_dir"])
    profile.mkdir(parents=True, exist_ok=True)
    pw = sync_playwright().start()
    try:
        context = pw.chromium.launch_persistent_context(
            user_data_dir=str(profile),
            channel=cfg["browser"].get("channel", "msedge"),
            headless=False,  # 知网只认可见窗口
            args=[
                "--no-proxy-server",
                "--disable-blink-features=AutomationControlled",
                "--start-maximized",
            ],
            no_viewport=True,
            accept_downloads=True,
            locale="zh-CN",
        )
    except Exception:
        pw.stop()
        raise
    page = context.pages[0] if context.pages else context.new_page()
    page.set_default_timeout(15000)
    if log:
        log(f"[浏览器] 已启动（profile: {profile}）")
    return pw, context, page


def close_context(pw, context, log=None) -> None:
    for closer in (context.close, pw.stop):
        try:
            closer()
        except Exception:
            pass
    if log:
        log("[浏览器] 已关闭")


def screenshot(page, directory: Path, prefix: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{prefix}_{datetime.now():%Y%m%d_%H%M%S}.png"
    try:
        page.screenshot(path=str(path), full_page=False)
    except Exception:
        pass
    return path


def wait_for_verify(page, cfg: dict, shot_dir: Path, log) -> bool:
    """检测 /verify/ 拼图页；有则阻塞等人拖滑块。超时截图返回 False。"""
    wait_sec = int(cfg["browser"].get("verify_wait_sec", 180))
    if "/verify/" not in page.url:
        return True
    print("\n" + "=" * 60)
    print("知网要求滑块验证。请在打开的浏览器窗口里拖动滑块完成拼图，")
    print(f"完成后回到这里（脚本会自动继续，最多等 {wait_sec} 秒）。")
    print("=" * 60)
    deadline = time.time() + wait_sec
    while time.time() < deadline:
        time.sleep(2)
        try:
            if "/verify/" not in page.url:
                log("[滑块] 已通过，继续。")
                return True
        except Exception:
            continue
    path = screenshot(page, shot_dir, "verify_timeout")
    log(f"[滑块] 等待超时，已截图：{path}")
    return False


def find_first(page, selectors: list[str], timeout: int = 3000):
    """按顺序尝试候选选择器，第一个可见命中即返回。"""
    for selector in selectors or []:
        try:
            element = page.query_selector(selector)
        except Exception:
            continue
        if element:
            try:
                if element.is_visible():
                    return element
            except Exception:
                return element
    return None


def find_all(page, selectors: list[str]) -> list:
    for selector in selectors or []:
        try:
            items = page.query_selector_all(selector)
        except Exception:
            continue
        if items:
            return items
    return []


def goto(page, url: str, cfg: dict, log=None) -> bool:
    timeout = int(cfg["browser"].get("nav_timeout_sec", 45)) * 1000
    try:
        page.goto(url, timeout=timeout, wait_until="domcontentloaded")
        return True
    except Exception as exc:  # noqa: BLE001
        if log:
            log(f"[浏览器] 打开失败 {url}：{type(exc).__name__}: {exc}")
        return False
