# -*- coding: utf-8 -*-
"""cnki_search.py —— 知网检索与结果解析

用途：可见 Edge + 复用 profile 打开知网，检索关键词，抓列表与详情页字段，写 records_cnki.json。
参数：
    --query "关键词"      必填（简单检索框输入，空格分隔）
    --limit N             条数上限，默认读配置
    --run-dir DIR         输出目录
    --selectors FILE      选择器文件（首次需校准）
    --no-detail           只抓列表，不进详情页（快速验证选择器）
    --config PATH         配置文件
    --json                结果打到 stdout
退出码：0 成功 / 1 解析失败或部分失败 / 2 需用户处理（滑块、首页打不开） / 3 配置错误
依赖：playwright（msedge）
说明：SU= 字段表达式只做记录与手工检索用；脚本统一走简单检索框，避免高级检索页改版风险。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path
from urllib.parse import urljoin

sys.path.insert(0, str(Path(__file__).resolve().parent))
import browser_util as bu  # noqa: E402
import litcommon as lc  # noqa: E402

DOI_RE = re.compile(r"10\.\d{4,9}/[^\s\"'<>,;]+")
AFFIL_HINTS = ("大学", "学院", "实验室", "研究所", "研究中心", "医院")


def _clean_authors(elements) -> str:
    """作者区常混入机构名（实测：#authorpart 里带“天津师范大学…”），按关键词剔除。"""
    names, seen = [], set()
    for el in elements:
        name = re.sub(r"\d+$", "", (el.inner_text() or "").strip()).strip()
        if not name or name in seen or any(hint in name for hint in AFFIL_HINTS):
            continue
        seen.add(name)
        names.append(name)
    return "; ".join(names)


def _submit(page, sel: dict, query: str, log) -> bool:
    box = bu.find_first(page, sel.get("home", {}).get("search_input", []))
    if box is None:
        return False
    box.click()
    box.fill(query)
    page.keyboard.press("Enter")
    log(f"[知网] 已提交检索「{query}」")
    try:
        page.wait_for_load_state("domcontentloaded", timeout=30000)
    except Exception:
        pass
    return True


def _collect(page, sel: dict, limit: int, log) -> list[dict]:
    """抓结果列表；不足则尝试翻页（最多 3 页）。"""
    items: list[dict] = []
    seen: set[str] = set()
    for page_no in range(3):
        links = bu.find_all(page, sel.get("result", {}).get("item_link", []))
        for link in links:
            try:
                href = link.get_attribute("href") or ""
                title = (link.inner_text() or "").strip()
            except Exception:
                continue
            if not href or not title:
                continue
            url = urljoin("https://kns.cnki.net/", href)
            if url in seen:
                continue
            seen.add(url)
            items.append({"标题": title, "url": url})
            if len(items) >= limit:
                return items
        if page_no == 2:
            break
        nxt = bu.find_first(page, sel.get("result", {}).get("next_page", []))
        if nxt is None:
            break
        try:
            nxt.click()
            time.sleep(3)
        except Exception:
            break
    return items


def _parse_detail(page, sel: dict, rec: dict, log) -> dict:
    """详情页取字段；取不到留空，绝不编造。"""
    detail = sel.get("detail", {})
    title_el = bu.find_first(page, detail.get("title", []))
    if title_el:
        rec["标题"] = (title_el.inner_text() or rec["标题"]).strip()

    author_els = bu.find_all(page, detail.get("authors", []))
    if author_els:
        rec["作者"] = _clean_authors(author_els)

    source_el = bu.find_first(page, detail.get("source", []))
    if source_el:
        source = (source_el.inner_text() or "").strip()
        rec["期刊/来源"] = re.sub(r"\s*[.。]\s*$", "", source)

    tip_el = bu.find_first(page, detail.get("top_tip", []))
    tip = (tip_el.inner_text() or "").strip() if tip_el else ""
    if tip:
        year = re.search(r"(19|20)\d{2}", tip)
        if year:
            rec["年份"] = year.group(0)
        vol = re.search(r"(\d+)\s*卷", tip) or re.search(r",\s*(\d+)\s*\(", tip)
        if vol:
            rec["卷"] = vol.group(1)
        issue = re.search(r"\((\d+)\)", tip) or re.search(r"(\d+)\s*期", tip)
        if issue:
            rec["期"] = issue.group(1)
        pages = re.search(r":\s*([0-9A-Za-z\-–]+)\s*$", tip)
        if pages:
            rec["页码"] = pages.group(1)

    abstract_el = bu.find_first(page, detail.get("abstract", []))
    if abstract_el:
        # 摘要有“更多”折叠，用 text_content 取完整 DOM 文本，inner_text 可能被截断
        rec["摘要"] = (abstract_el.text_content() or "").strip()

    keyword_els = bu.find_all(page, detail.get("keywords", []))
    if keyword_els:
        raw = " ".join((el.text_content() or "") for el in keyword_els)
        words = [w.strip(" ;；,，.。") for w in re.split(r"[;；]", raw)]
        rec["关键词"] = "; ".join(dict.fromkeys(w for w in words if w))

    try:
        html = page.content()
        doi = DOI_RE.search(html)
        if doi:
            rec["DOI"] = doi.group(0).rstrip(".")
    except Exception:
        pass

    rec["pdf_available"] = bu.find_first(page, detail.get("pdf_button", [])) is not None
    rec["caj_available"] = bu.find_first(page, detail.get("caj_button", [])) is not None
    return rec


def search_cnki(cfg: dict, query: str, limit: int, run_dir: Path, log,
                selectors: dict | None = None, detail: bool = True,
                start_url: str = "https://kns.cnki.net/") -> tuple[int, list[dict]]:
    sel = selectors or bu.load_selectors(None, log)
    shot_dir = run_dir / "日志"
    pw, context, page = bu.open_context(cfg, log)
    try:
        if not bu.goto(page, start_url, cfg, log):
            log("[知网] 首页打不开")
            bu.screenshot(page, shot_dir, "cnki_home_fail")
            return lc.EXIT_NEED_USER, []
        if not bu.wait_for_verify(page, cfg, shot_dir, log):
            return lc.EXIT_NEED_USER, []
        if not _submit(page, sel, query, log):
            log("[知网] 未找到检索框，页面可能改版")
            bu.screenshot(page, shot_dir, "cnki_no_searchbox")
            return lc.EXIT_PARTIAL, []

        time.sleep(3)
        if "/verify/" in page.url and not bu.wait_for_verify(page, cfg, shot_dir, log):
            return lc.EXIT_NEED_USER, []

        items = _collect(page, sel, limit, log)
        log(f"[知网] 列表抓到 {len(items)} 条")
        if not items:
            path = bu.screenshot(page, shot_dir, "cnki_no_items")
            log(f"[知网] 解析 0 条，已截图 {path}；请校准 selectors.json 的 result.item_link")
            return lc.EXIT_PARTIAL, []

        records = []
        for index, item in enumerate(items, 1):
            rec = {
                "标题": item["标题"], "作者": "", "期刊/来源": "", "年份": "", "卷": "", "期": "",
                "页码": "", "DOI": "", "PMID": "", "来源库": "知网", "文献类型": "",
                "摘要": "", "关键词": "", "全文状态": "未下载", "本地文件": "",
                "原文链接": item["url"], "备注": "", "source_db": "cnki", "url": item["url"],
                "pdf_available": False, "caj_available": False,
            }
            if detail:
                time.sleep(3)  # 频率克制
                if bu.goto(page, item["url"], cfg, log):
                    if "/verify/" in page.url:
                        bu.wait_for_verify(page, cfg, shot_dir, log)
                    rec = _parse_detail(page, sel, rec, log)
                else:
                    rec["备注"] = "详情页打开失败"
            records.append(rec)
            log(f"[知网] {index}/{len(items)} {rec['标题'][:30]} | PDF={rec['pdf_available']}")
        return lc.EXIT_OK, records
    finally:
        bu.close_context(pw, context, log)


def main() -> int:
    lc.setup_console()
    parser = argparse.ArgumentParser(description="知网检索与解析")
    parser.add_argument("--query", required=True, help="检索关键词（空格分隔）")
    parser.add_argument("--limit", type=int, help="条数上限")
    parser.add_argument("--run-dir", help="输出目录")
    parser.add_argument("--selectors", help="selectors.json")
    parser.add_argument("--no-detail", action="store_true", help="只抓列表")
    parser.add_argument("--config", help="配置文件")
    parser.add_argument("--json", action="store_true", help="结果打到 stdout")
    args = parser.parse_args()
    lc.print_env()
    cfg = lc.load_config(args.config)
    limit = max(1, min(args.limit or cfg["databases"]["cnki"].get("retmax", 20), 50))
    run_dir = Path(args.run_dir) if args.run_dir else lc.make_run_dir(cfg, args.query)
    log = lc.RunLog(run_dir / "日志" / "run.log")
    try:
        code, records = search_cnki(
            cfg, args.query, limit, run_dir, log,
            bu.load_selectors(args.selectors, log), detail=not args.no_detail,
        )
        lc.write_json(run_dir / "records_cnki.json", records)
        log(f"[知网] 写入 {run_dir / 'records_cnki.json'}（{len(records)} 条）")
        if args.json:
            print(json.dumps(records, ensure_ascii=False, indent=2))
        return code
    finally:
        log.close()


if __name__ == "__main__":
    sys.exit(main())
