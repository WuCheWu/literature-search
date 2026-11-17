# -*- coding: utf-8 -*-
"""download_fulltext.py —— 全文下载与校验（PubMed OA + 知网 PDF/CAJ）

用途：读 records_*.json，逐条下载全文，回写状态与本地路径。
参数：
    --run-dir DIR       必填（含 records_pubmed.json / records_cnki.json）
    --max N             本次最多下载篇数，默认读配置
    --skip-pubmed       跳过 PubMed
    --skip-cnki         跳过知网
    --all               连「待定/排除」也下载（默认只下纳入，待定看配置）
    --config PATH       配置文件
    --json              回写后打印统计
退出码：0 全部按预期处理 / 1 有下载失败 / 2 需用户处理（滑块、浏览器） / 3 配置错误
依赖：requests（trust_env=False）、playwright（知网）、标准库
规则：只下开放获取或学校已订阅内容；不点购买、不绕付费墙；非 PDF 一律删除并记失败。
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
import platform_util as pu  # noqa: E402
import platforms as pf  # noqa: E402

PDF_MAGIC = b"%PDF"


def _valid_pdf(path: Path, min_bytes: int) -> bool:
    try:
        if not path.exists() or path.stat().st_size < min_bytes:
            return False
        with path.open("rb") as fh:
            return fh.read(4) == PDF_MAGIC
    except OSError:
        return False


def _filename(rec: dict) -> str:
    author = (rec.get("作者") or "").split(";")[0].split(",")[0].strip() or "未知作者"
    year = (rec.get("年份") or "").strip() or "无年份"
    title = lc.sanitize_filename(rec.get("标题") or "untitled", 40)
    return f"{lc.sanitize_filename(author, 20)}_{year}_{title}"


def _save_pdf(content: bytes, out_dir: Path, rec: dict, min_bytes: int) -> Path | None:
    target = lc.unique_path(out_dir, _filename(rec), ".pdf")
    target.write_bytes(content)
    if _valid_pdf(target, min_bytes):
        return target
    target.unlink(missing_ok=True)
    return None


def _try_pmc(session, pmcid: str, cfg, out_dir, rec, log) -> Path | None:
    pid = pmcid if pmcid.upper().startswith("PMC") else f"PMC{pmcid}"
    urls = [
        f"https://www.ncbi.nlm.nih.gov/pmc/articles/{pid}/pdf/",
        f"https://europepmc.org/articles/{pid}?pdf=render",
    ]
    for url in urls:
        try:
            resp = session.get(url, timeout=cfg["download"]["timeout_sec"], allow_redirects=True)
            if resp.status_code == 200 and resp.content[:4] == PDF_MAGIC:
                saved = _save_pdf(resp.content, out_dir, rec, cfg["download"]["min_bytes"])
                if saved:
                    log(f"[下载] PMC 成功：{saved.name}")
                    return saved
        except Exception as exc:  # noqa: BLE001
            log(f"[下载] PMC 失败（{url}）：{type(exc).__name__}: {exc}")
    return None


def _try_unpaywall(session, doi: str, cfg, out_dir, rec, log) -> Path | None:
    email = cfg["databases"]["pubmed"].get("email") or ""
    if not email:
        log("[下载] 未配置 pubmed.email，跳过 Unpaywall")
        return None
    try:
        api = f"https://api.unpaywall.org/v2/{doi}"
        data = session.get(api, params={"email": email}, timeout=30).json()
        candidates = []
        best = data.get("best_oa_location") or {}
        if best.get("url_for_pdf"):
            candidates.append(best["url_for_pdf"])
        for loc in data.get("oa_locations") or []:
            if loc.get("url_for_pdf") and loc["url_for_pdf"] not in candidates:
                candidates.append(loc["url_for_pdf"])
        for url in candidates[:2]:
            resp = session.get(url, timeout=cfg["download"]["timeout_sec"], allow_redirects=True)
            if resp.status_code == 200:
                saved = _save_pdf(resp.content, out_dir, rec, cfg["download"]["min_bytes"])
                if saved:
                    log(f"[下载] Unpaywall 成功：{saved.name}")
                    return saved
    except Exception as exc:  # noqa: BLE001
        log(f"[下载] Unpaywall 失败：{type(exc).__name__}: {exc}")
    return None


def _has_playwright() -> bool:
    """浏览器兜底是可选项：没装 playwright 就降级，不能拖垮整批下载与出表。"""
    import importlib.util

    return importlib.util.find_spec("playwright") is not None


def _browser_pmc_batch(cfg: dict, jobs: list[dict], out_dir: Path, log, interval: float) -> int:
    """PMC 直连被 Cloudflare 403 时的浏览器兜底。

    实测（2026-11-16）：requests 与 context.request 直接请求 PMC/Europe PMC 均 403；
    先用可见 Edge 打开 europepmc.org 过盾，再用 context.request 取
    https://europepmc.org/api/getPdf?pmcid=PMCxxxxx 返回 200（%PDF）。
    """
    if not jobs:
        return 0
    if not _has_playwright():
        log("[下载] 未安装 playwright，跳过 Europe PMC 浏览器兜底"
            "（这些文献先记 仅摘要/需付费，装好后可单跑 download_fulltext.py 补下）")
        return 0
    done = 0
    pw, context, page = bu.open_context(cfg, log)
    try:
        if not bu.goto(page, "https://europepmc.org/", cfg, log):
            log("[下载] Europe PMC 打不开，PubMed OA 浏览器兜底跳过")
            return 0
        timeout_ms = int(cfg["download"]["timeout_sec"]) * 1000
        for rec in jobs:
            pmcid = (rec.get("pmcid") or "").strip()
            if not pmcid:
                continue
            body = b""
            for attempt in range(2):
                try:
                    resp = context.request.get(
                        f"https://europepmc.org/api/getPdf?pmcid={pmcid}", timeout=timeout_ms
                    )
                    if resp.status == 200:
                        body = resp.body()
                except Exception as exc:  # noqa: BLE001
                    log(f"[下载] PMC 兜底请求失败（{pmcid}）：{type(exc).__name__}: {exc}")
                if body[:4] == PDF_MAGIC:
                    break
                # 没过盾：先访问文章页建立会话，再重试
                bu.goto(page, f"https://europepmc.org/article/PMC/{pmcid}", cfg, log)
                time.sleep(2)
            if body[:4] == PDF_MAGIC:
                saved = _save_pdf(body, out_dir, rec, cfg["download"]["min_bytes"])
                if saved:
                    rec["全文状态"] = "已下载"
                    rec["本地文件"] = str(saved)
                    rec["备注"] = "Europe PMC（浏览器过盾）"
                    done += 1
                    log(f"[下载] PMC 兜底成功：{saved.name}（{len(body)} 字节）")
            else:
                log(f"[下载] PMC 兜底未取得 PDF：{pmcid}")
            time.sleep(interval)
    finally:
        bu.close_context(pw, context, log)
    return done


def _pubmed_one(session, rec: dict, cfg, out_dir: Path, log) -> None:
    if rec.get("全文状态") == "已下载" and Path(rec.get("本地文件") or "").exists():
        return
    pmcid = (rec.get("pmcid") or "").strip()
    doi = (rec.get("DOI") or "").strip()
    saved = _try_pmc(session, pmcid, cfg, out_dir, rec, log) if pmcid else None
    if not saved and doi:
        saved = _try_unpaywall(session, doi, cfg, out_dir, rec, log)
    if saved:
        rec["全文状态"] = "已下载"
        rec["本地文件"] = str(saved)
        rec["备注"] = rec.get("备注") or "PMC/Unpaywall 开放获取"
    elif doi:
        rec["全文状态"] = "需付费"
        rec["备注"] = rec.get("备注") or "无开放获取版本，需通过学校订阅"
    else:
        rec["全文状态"] = "仅摘要"
        rec["备注"] = rec.get("备注") or "无 PMCID / DOI"


def _paywall_hint(page) -> str:
    try:
        text = page.content()
    except Exception:
        return ""
    for hint in ("无权限", "请登录", "立即购买", "付费下载"):
        if hint in text:
            return hint
    return ""


def _cnki_one(page, rec: dict, cfg, out_dir: Path, shot_dir: Path, log) -> str:
    if not bu.goto(page, rec.get("url") or "", cfg, log):
        return "下载失败"
    if "/verify/" in page.url and not bu.wait_for_verify(page, cfg, shot_dir, log):
        return "未下载"
    detail = cfg.get("_selectors", {}).get("detail", {})
    pdf_btn = bu.find_first(page, detail.get("pdf_button", ["#pdfDown"]))
    caj_btn = bu.find_first(page, detail.get("caj_button", ["#cajDown"]))
    timeout = int(cfg["download"]["timeout_sec"]) * 1000
    if pdf_btn is None and caj_btn is None:
        hint = _paywall_hint(page)
        bu.screenshot(page, shot_dir, "cnki_no_button")
        return "需付费" if hint else "无权限"
    button, suffix = (pdf_btn, ".pdf") if pdf_btn is not None else (caj_btn, ".caj")
    target = lc.unique_path(out_dir, _filename(rec), suffix)
    try:
        with page.expect_download(timeout=timeout) as download_info:
            button.click()
        download = download_info.value
        download.save_as(str(target))
    except Exception as exc:  # noqa: BLE001
        log(f"[下载] 知网失败：{type(exc).__name__}: {exc}")
        bu.screenshot(page, shot_dir, "cnki_download_fail")
        target.unlink(missing_ok=True)
        return "下载失败"
    if suffix == ".pdf":
        if _valid_pdf(target, cfg["download"]["min_bytes"]):
            rec["本地文件"] = str(target)
            return "已下载"
        target.unlink(missing_ok=True)
        return "下载失败"
    if target.stat().st_size < cfg["download"]["min_bytes"]:
        target.unlink(missing_ok=True)
        return "下载失败"
    rec["本地文件"] = str(target)
    return "仅CAJ"


def _platform_one(context, page, rec: dict, platform: dict, cfg, out_dir: Path,
                  shot_dir: Path, log) -> str:
    """通用平台下载：优先直链（带登录 Cookie），其次点 PDF 按钮/新标签页。

    不点购买、不绕付费墙；遇到登录墙直接返回「需登录」，由主流程提示用户。
    """
    url = (rec.get("url") or rec.get("原文链接") or "").strip()
    if not url:
        return "仅摘要"
    if not bu.goto(page, url, cfg, log):
        return "下载失败"
    if pu.detect_login(page)["required"]:
        log("[下载] 平台要求登录，跳过（可先手动登录或保存账号后重跑）")
        return "需登录"

    selectors = pf.merge_learned(platform).get("selectors") or {}
    targets, elements = pu.pdf_targets(page, selectors)
    if not targets:
        hint = pu.paywall_hint(page)
        if hint:
            log(f"[下载] 未找到 PDF 入口，页面提示「{hint}」")
            return "需付费"
        if platform.get("kind") in ("index", "abstract", "chemistry", "unknown", ""):
            return "仅摘要"
        return "无权限"

    timeout_ms = int(cfg["download"]["timeout_sec"]) * 1000
    for target in targets:
        href = (target.get("href") or "").strip()
        if not href:
            continue
        try:
            resp = context.request.get(href, timeout=timeout_ms)
            body = resp.body() if resp.status == 200 else b""
            if body[:4] == PDF_MAGIC:
                saved = _save_pdf(body, out_dir, rec, cfg["download"]["min_bytes"])
                if saved:
                    rec["本地文件"] = str(saved)
                    rec["备注"] = (rec.get("备注") or "") + "；平台直链"
                    log(f"[下载] 平台直链成功：{saved.name}")
                    return "已下载"
        except Exception as exc:  # noqa: BLE001
            log(f"[下载] 平台直链失败（{href[:80]}）：{type(exc).__name__}: {exc}")

    for target, element in zip(targets, elements):
        before_pages = set(context.pages)
        try:
            with page.expect_download(timeout=min(15000, timeout_ms)) as download_info:
                element.click()
            download = download_info.value
            target_path = lc.unique_path(out_dir, _filename(rec), ".pdf")
            download.save_as(str(target_path))
            if _valid_pdf(target_path, cfg["download"]["min_bytes"]):
                rec["本地文件"] = str(target_path)
                rec["备注"] = (rec.get("备注") or "") + "；平台点击下载"
                log(f"[下载] 平台点击下载成功：{target_path.name}")
                return "已下载"
            target_path.unlink(missing_ok=True)
        except Exception as exc:  # noqa: BLE001
            log(f"[下载] 点击 PDF 未触发下载（{target.get('text') or target.get('href')}）："
                f"{type(exc).__name__}")
            for new_page in [p for p in context.pages if p not in before_pages]:
                pdf_url = (new_page.url or "").strip()
                if not pdf_url.startswith("http"):
                    continue
                try:
                    resp = context.request.get(pdf_url, timeout=timeout_ms)
                    body = resp.body() if resp.status == 200 else b""
                    if body[:4] == PDF_MAGIC:
                        saved = _save_pdf(body, out_dir, rec, cfg["download"]["min_bytes"])
                        if saved:
                            rec["本地文件"] = str(saved)
                            rec["备注"] = (rec.get("备注") or "") + "；平台新标签页"
                            log(f"[下载] 平台新标签页取到 PDF：{saved.name}")
                            return "已下载"
                except Exception as inner:  # noqa: BLE001
                    log(f"[下载] 新标签页取 PDF 失败：{type(inner).__name__}")

    hint = pu.paywall_hint(page)
    bu.screenshot(page, shot_dir, "platform_download_fail")
    return "需付费" if hint else "下载失败"


def _wanted(rec: dict, selected_only: bool, include_pending: bool) -> bool:
    """只下筛选通过的文献：排除一律不下；待定看配置。"""
    if not selected_only:
        return True
    decision = rec.get("判定")
    if decision == "排除":
        return False
    if decision == "待定":
        return include_pending
    return True


def run(cfg: dict, run_dir: Path, log, max_count: int | None = None,
        do_pubmed: bool = True, do_cnki: bool = True, do_platforms: bool = True,
        selectors: dict | None = None, selected_only: bool = False,
        include_pending: bool = False,
        platforms_map: dict | None = None) -> tuple[int, list[dict]]:
    cfg["_selectors"] = selectors or {}
    records = lc.read_records(run_dir)
    if not records:
        log("[下载] 没有记录，先跑检索。")
        return lc.EXIT_PARTIAL, records
    out_dir = run_dir / "全文"
    out_dir.mkdir(parents=True, exist_ok=True)
    shot_dir = run_dir / "日志"
    limit = max_count or int(cfg["download"]["max_per_run"])
    interval = float(cfg["download"]["interval_sec"])
    done = 0
    failed = 0

    if do_pubmed:
        session = lc.build_session()
        pending_pmc = []
        for rec in records:
            if rec.get("source_db") != "pubmed":
                continue
            if not _wanted(rec, selected_only, include_pending):
                rec["备注"] = rec.get("备注") or f"{rec.get('判定') or ''}，未下载全文".strip("，")
                continue
            if done >= limit:
                rec["备注"] = rec.get("备注") or "超出本次下载上限"
                continue
            already = rec.get("全文状态") == "已下载" and Path(rec.get("本地文件") or "").exists()
            _pubmed_one(session, rec, cfg, out_dir, log)
            if rec["全文状态"] == "下载失败":
                failed += 1
            if rec["全文状态"] == "已下载" and not already:
                done += 1
            elif (rec.get("pmcid") or "").strip() and rec["全文状态"] != "仅摘要":
                pending_pmc.append(rec)
            time.sleep(interval)

        # 直连被盾时走浏览器兜底（PMC OA）
        remaining = max(0, limit - done)
        if pending_pmc and remaining > 0:
            log(f"[下载] {len(pending_pmc)} 篇走 Europe PMC 浏览器兜底")
            done += _browser_pmc_batch(cfg, pending_pmc[:remaining], out_dir, log, interval)

    cnki_items = []
    if do_cnki:
        cnki_items = [
            r for r in records
            if r.get("source_db") == "cnki" and _wanted(r, selected_only, include_pending)
        ]
    if cnki_items and not _has_playwright():
        log("[下载] 未安装 playwright，知网全文下载跳过（状态保留，装好后可单跑 download_fulltext.py 补下）")
        cnki_items = []
    if cnki_items:
        pw, context, page = bu.open_context(cfg, log)
        try:
            if not bu.goto(page, "https://kns.cnki.net/", cfg, log):
                log("[下载] 知网打不开，CAJ/PDF 未处理")
                return lc.EXIT_NEED_USER, records
            if not bu.wait_for_verify(page, cfg, shot_dir, log):
                return lc.EXIT_NEED_USER, records
            for rec in cnki_items:
                if rec.get("全文状态") == "已下载" and Path(rec.get("本地文件") or "").exists():
                    continue
                if done >= limit:
                    rec["备注"] = rec.get("备注") or "超出本次下载上限"
                    continue
                status = _cnki_one(page, rec, cfg, out_dir, shot_dir, log)
                rec["全文状态"] = status
                if status == "下载失败":
                    failed += 1
                if status in ("已下载", "仅CAJ"):
                    done += 1
                log(f"[下载] {rec['标题'][:30]} → {status}")
                time.sleep(interval)
        finally:
            bu.close_context(pw, context, log)

    platform_items = []
    if do_platforms:
        platform_items = [
            r for r in records
            if r.get("source_db") not in ("pubmed", "cnki")
            and _wanted(r, selected_only, include_pending)
        ]
    if platform_items and not _has_playwright():
        log("[下载] 未安装 playwright，其他平台全文下载跳过（状态保留，装好后可单跑 download_fulltext.py 补下）")
        platform_items = []
    if platform_items:
        by_source: dict[str, list[dict]] = {}
        for rec in platform_items:
            by_source.setdefault(rec.get("source_db") or "custom", []).append(rec)
        pw, context, page = bu.open_context(cfg, log)
        try:
            for pid, items in by_source.items():
                platform = (platforms_map or {}).get(pid) or pf.get(pid) or {
                    "id": pid, "name": pid, "short": pid, "kind": "unknown", "selectors": {},
                }
                platform = pf.merge_learned(platform)
                log(f"[下载] 平台 {platform.get('short') or pid}：{len(items)} 篇待处理")
                for rec in items:
                    if rec.get("全文状态") == "已下载" and Path(rec.get("本地文件") or "").exists():
                        continue
                    if done >= limit:
                        rec["备注"] = rec.get("备注") or "超出本次下载上限"
                        continue
                    status = _platform_one(context, page, rec, platform, cfg, out_dir, shot_dir, log)
                    rec["全文状态"] = status
                    if status == "下载失败":
                        failed += 1
                    if status == "已下载":
                        done += 1
                    log(f"[下载] {(rec.get('标题') or '')[:30]} → {status}")
                    time.sleep(interval)
        finally:
            bu.close_context(pw, context, log)

    _write_back(run_dir, records)
    log(f"[下载] 完成 {done} 篇，失败 {failed} 篇")
    return (lc.EXIT_PARTIAL if failed else lc.EXIT_OK), records


def _write_back(run_dir: Path, records: list[dict]) -> None:
    """按 source_db 分组写回 records_<库>.json（与检索阶段同一命名规则）。"""
    groups: dict[str, list[dict]] = {}
    for rec in records:
        key = lc.sanitize_filename(str(rec.get("source_db") or "unknown"), 40)
        groups.setdefault(key, []).append(rec)
    for key, items in groups.items():
        lc.write_json(run_dir / f"records_{key}.json", items)


def main() -> int:
    lc.setup_console()
    parser = argparse.ArgumentParser(description="下载全文（PubMed OA + 知网）")
    parser.add_argument("--run-dir", required=True, help="含 records_*.json 的目录")
    parser.add_argument("--max", type=int, help="本次最多下载篇数")
    parser.add_argument("--skip-pubmed", action="store_true")
    parser.add_argument("--skip-cnki", action="store_true")
    parser.add_argument("--skip-platforms", action="store_true", help="跳过通用平台下载")
    parser.add_argument("--task", help="检索任务.json（取 custom_platforms 定义）")
    parser.add_argument("--all", action="store_true", help="连待定/排除也下载")
    parser.add_argument("--selectors", help="selectors.json")
    parser.add_argument("--config", help="配置文件")
    parser.add_argument("--json", action="store_true", help="打印统计")
    args = parser.parse_args()
    lc.print_env()
    cfg = lc.load_config(args.config)
    run_dir = Path(args.run_dir)
    log = lc.RunLog(run_dir / "日志" / "run.log")
    try:
        task = (lc.read_json(args.task, default={}) or {}) if args.task else {}
        customs = {
            item["id"]: item
            for item in ((task.get("search") or {}).get("custom_platforms") or [])
            if isinstance(item, dict) and item.get("id")
        }
        code, records = run(
            cfg, run_dir, log, args.max,
            do_pubmed=not args.skip_pubmed, do_cnki=not args.skip_cnki,
            do_platforms=not args.skip_platforms,
            selectors=bu.load_selectors(args.selectors, log),
            selected_only=not args.all, platforms_map=customs,
        )
        if args.json:
            print(json.dumps(lc.summarize(records), ensure_ascii=False, indent=2))
        return code
    finally:
        log.close()


if __name__ == "__main__":
    sys.exit(main())
