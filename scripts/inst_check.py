# -*- coding: utf-8 -*-
"""inst_check.py —— 机构身份确认（唯一可靠判据）

用途：用可见 Edge 打开知网 → 过滑块（人工）→ 检索一次 → 结果页是否出现机构名。
参数：
    --keyword "关键词"   用于触发结果页，默认取 --query 或“文献”
    --run-dir DIR        截图与日志目录
    --selectors FILE     选择器文件
    --config PATH        配置文件
    --json               输出 JSON
退出码：0 已识别机构 / 2 需用户处理（滑块超时、未识别、页面异常） / 3 配置错误
依赖：playwright（msedge）
铁律：首页永远不显示机构名，不能用它判定；必须进检索结果页。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import browser_util as bu  # noqa: E402
import litcommon as lc  # noqa: E402


def _wait_institution(page, name: str, log, timeout: int = 45) -> tuple[bool, str]:
    """轮询结果页 HTML，直到出现机构名或超时。"""
    deadline = time.time() + timeout
    last_url = ""
    while time.time() < deadline:
        try:
            html = page.content()
            last_url = page.url
        except Exception as exc:  # 页面跳转中会短暂失败
            html = ""
            log(f"[机构] 读取页面失败（可能正在跳转）：{type(exc).__name__}")
        if name and name in html:
            return True, last_url
        time.sleep(2)
    return False, last_url


def check(cfg: dict, keyword: str, run_dir: Path, log, selectors: dict | None = None) -> dict:
    name = cfg["institution"]["name"]
    sel = selectors or bu.load_selectors(None, log)
    shot_dir = run_dir / "日志"
    result = {"ok": False, "institution": name, "url": "", "screenshot": "", "reason": ""}

    pw, context, page = bu.open_context(cfg, log)
    try:
        if not bu.goto(page, "https://kns.cnki.net/", cfg, log):
            result["reason"] = "知网首页打不开（VPN 或网络问题）"
            result["screenshot"] = str(bu.screenshot(page, shot_dir, "inst_home_fail"))
            return result

        if not bu.wait_for_verify(page, cfg, shot_dir, log):
            result["reason"] = "滑块未完成（等待超时）"
            result["screenshot"] = str(bu.screenshot(page, shot_dir, "inst_verify_timeout"))
            return result

        box = bu.find_first(page, sel.get("home", {}).get("search_input", []))
        if box is None:
            result["reason"] = "未找到首页检索框（页面可能改版，请校准 selectors.json）"
            result["screenshot"] = str(bu.screenshot(page, shot_dir, "inst_no_searchbox"))
            return result

        box.click()
        box.fill(keyword)
        page.keyboard.press("Enter")
        log(f"[机构] 已提交检索「{keyword}」，等待结果页…")

        found, url = _wait_institution(page, name, log)
        result["url"] = url
        if found:
            result["ok"] = True
            log(f"[机构] 结果页出现「{name}」→ 机构身份有效（{url}）")
            return result

        result["reason"] = "结果页未出现机构名"
        result["screenshot"] = str(bu.screenshot(page, shot_dir, "inst_not_recognized"))
        log(f"[机构] 结果页未出现「{name}」，已截图：{result['screenshot']}")
        return result
    finally:
        bu.close_context(pw, context, log)


def run(cfg: dict, keyword: str, run_dir: Path, log, selectors: dict | None = None) -> tuple[int, dict]:
    info = check(cfg, keyword, run_dir, log, selectors)
    if info["ok"]:
        return lc.EXIT_OK, info
    print("\n" + "=" * 60)
    print(f"已连上知网，但未被识别为「{info['institution']}」机构用户。可能原因：")
    print("  1) VPN 已断（请检查 EasyConnect 是否显示已连接）")
    print("  2) 需要从图书馆入口进入（"
          f"{cfg['institution']['library_url']} → 中文数据库 → CNKI）")
    print(f"  3) 页面改版，选择器需校准")
    print(f"原因：{info['reason']}")
    if info["screenshot"]:
        print(f"已截图：{info['screenshot']}")
    print("=" * 60)
    return lc.EXIT_NEED_USER, info


def main() -> int:
    lc.setup_console()
    parser = argparse.ArgumentParser(description="机构身份确认（进结果页看校名）")
    parser.add_argument("--keyword", default="文献", help="触发结果页用的关键词")
    parser.add_argument("--run-dir", help="输出目录")
    parser.add_argument("--selectors", help="selectors.json 路径")
    parser.add_argument("--config", help="配置文件")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    args = parser.parse_args()
    lc.print_env()
    cfg = lc.load_config(args.config)

    run_dir = Path(args.run_dir) if args.run_dir else lc.make_run_dir(cfg, args.keyword)
    log = lc.RunLog(run_dir / "日志" / "run.log")
    try:
        code, info = run(cfg, args.keyword, run_dir, log, bu.load_selectors(args.selectors, log))
        if args.json:
            print(json.dumps(info, ensure_ascii=False, indent=2))
        return code
    finally:
        log.close()


if __name__ == "__main__":
    sys.exit(main())
