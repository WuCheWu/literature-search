# -*- coding: utf-8 -*-
"""probe_platform.py —— 自助探测：这个网址能不能检索、能不能下载全文

用途（规则 5）：
    用户从学校公布的数据库清单里给出一批中/外网网址。检索前先逐个打开，实地判断：
      ① 能不能打开（VPN/域名/证书）；② 有没有登录墙；③ 有没有检索入口；
      ④ 结果页/详情页有没有 PDF 入口；⑤ 是否只有题录、是否显示 Get access / 无权限。
    结论写进 平台探测.json，截图留在 平台探测/截图/，下次可复用缓存（默认 7 天）。

判定：
    fulltext_ok     可检索 + 能直接拿到 PDF 入口（未见付费墙）
    oa_partial      可检索 + 部分 PDF（可能是 OA 或混合）
    metadata_only   可检索，但全文受限（题录/需订阅/无 PDF 入口）
    login_required  需要登录（账号密码或机构登录）——会提示用户
    no_search       页面能开，但没找到检索入口
    unreachable     打不开

参数：
    --platform ID        探测注册表里的平台（可重复）
    --url URL            探测任意网址（可重复；未知网址会临时当自定义平台）
    --all                探测注册表里全部可选平台（较慢）
    --keyword "词"       探测用检索词（默认取配置或“文献”）
    --run-dir DIR        输出目录（默认 <output_root>/_平台探测/<时间>）
    --quick              只看首页+结果页，不进详情页
    --wait-login SEC     遇到登录墙时等人手动登录的秒数（0=不等，提示需要账号）
    --refresh            忽略缓存重新探测
    --save               把学到的选择器写入 platform_learned.json
    --add                把 --url 登记为自定义平台后退出（不探测）
    --config PATH        配置文件
    --platforms-file PATH 覆盖 platforms.json（测试用）
    --json               输出 JSON
退出码：0 探测完成且可用 / 1 有网址打不开或无检索入口 / 2 需要登录/需要用户处理 / 3 配置错误
依赖：playwright（msedge）、requests（读学校清单时）
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import browser_util as bu  # noqa: E402
import creds  # noqa: E402
import litcommon as lc  # noqa: E402
import platform_util as pu  # noqa: E402
import platforms as pf  # noqa: E402

VERDICT_LABELS = {
    "fulltext_ok": "可检索 + 可直接下载 PDF",
    "oa_partial": "可检索 + 部分开放获取",
    "metadata_only": "可检索，全文受限（题录/需订阅）",
    "login_required": "需登录（账号密码/机构登录）",
    "no_search": "页面可打开，但未找到检索入口",
    "unreachable": "打不开（网络/VPN/域名）",
    "error": "探测异常",
}
VERDICT_NEXT = {
    "fulltext_ok": "可直接纳入检索平台。",
    "oa_partial": "纳入检索；能下的下，不能下的标「需付费/仅摘要」。",
    "metadata_only": "纳入题录检索；全文请改用学校已订阅的平台。",
    "login_required": "请二选一：①在打开的窗口手动登录；②保存账号后重试。",
    "no_search": "可能需要登录、或该页是导航页；换入口网址或人工确认。",
    "unreachable": "检查 VPN、网址是否写错、是否需要校内访问。",
    "error": "看日志/截图定位；可换 --quick 重试。",
}


def _selectors_for(platform: dict) -> dict:
    selectors = dict(platform.get("selectors") or {})
    ref = platform.get("selectors_ref")
    if ref and not selectors:
        path = lc.SKILL_ROOT / ref
        selectors = lc.read_json(path, default={}) or {}
    return selectors


def _cache_dir(cfg: dict) -> Path:
    configured = (cfg.get("platforms") or {}).get("cache_dir")
    return Path(configured) if configured else Path(cfg["output_root"]) / "_平台能力"


def _cache_path(cfg: dict, pid: str) -> Path:
    return _cache_dir(cfg) / f"{lc.sanitize_filename(pid, 40)}.json"


def load_cache(cfg: dict, platform: dict, url: str, refresh: bool = False) -> dict | None:
    if refresh:
        return None
    path = _cache_path(cfg, platform["id"])
    data = lc.read_json(path, default=None)
    if not isinstance(data, dict):
        return None
    if data.get("entry") != url:
        return None
    ttl_days = float((cfg.get("platforms") or {}).get("cache_ttl_days", 7) or 0)
    if ttl_days > 0:
        try:
            checked = datetime.strptime(data.get("checked_at", ""), "%Y-%m-%d %H:%M:%S")
            if (datetime.now() - checked).total_seconds() > ttl_days * 86400:
                return None
        except ValueError:
            return None
    data["cached"] = True
    return data


def save_cache(cfg: dict, info: dict) -> Path:
    path = _cache_path(cfg, info["platform"])
    lc.write_json(path, info)
    return path


def _learned(page, selectors: dict, box_selector: str) -> dict:
    learned: dict[str, list[str]] = {}
    if box_selector:
        learned["search_input"] = [box_selector]
    for selector in pu.sel_list(selectors, "item_link"):
        try:
            if page.query_selector_all(selector):
                learned["item_link"] = [selector]
                break
        except Exception:
            continue
    for selector in pu.sel_list(selectors, "pdf_button"):
        try:
            if any(pu.is_visible(e) for e in page.query_selector_all(selector)):
                learned["pdf_button"] = [selector]
                break
        except Exception:
            continue
    return learned


def probe_one(cfg: dict, platform: dict, url: str, keyword: str, run_dir: Path, log,
              quick: bool = False, wait_login: int = 0,
              creds_data: dict | None = None) -> dict:
    pid = platform.get("id") or "custom"
    name = platform.get("short") or platform.get("name") or pid
    selectors = pf.merge_learned(platform).get("selectors") or _selectors_for(platform)
    shot_dir = run_dir / "平台探测" / "截图"
    info = {
        "platform": pid,
        "name": name,
        "entry": url,
        "checked_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "reachable": False,
        "status": 0,
        "final_url": "",
        "title": "",
        "login_required": False,
        "login_signals": [],
        "login_method": "not-needed",
        "institution_seen": False,
        "search": {"ok": False, "query": keyword, "box_selector": "", "result_count": 0,
                   "sample": []},
        "download": {"pdf_links": 0, "pdf_buttons": 0, "paywall": "",
                     "detail_pdf": False, "detail_paywall": ""},
        "verdict": "error",
        "verdict_label": VERDICT_LABELS["error"],
        "screenshots": [],
        "selectors_learned": {},
        "note": "",
    }

    pw, context, page = bu.open_context(cfg, log)
    try:
        timeout = int(cfg["browser"].get("nav_timeout_sec", 45)) * 1000
        try:
            response = page.goto(url, timeout=timeout, wait_until="domcontentloaded")
            info["status"] = int(response.status) if response else 0
            info["reachable"] = bool(page.url) and info["status"] < 500
        except Exception as exc:  # noqa: BLE001
            info["note"] = f"{type(exc).__name__}: {exc}"
            info["verdict"] = "unreachable"
            info["verdict_label"] = VERDICT_LABELS["unreachable"]
            info["screenshots"].append(str(bu.screenshot(page, shot_dir, f"probe_{pid}_fail")))
            log(f"[探测] {name} 打不开：{info['note']}")
            return info

        if "/verify/" in page.url:
            if not bu.wait_for_verify(page, cfg, shot_dir, log):
                info["note"] = "滑块未完成（等待超时）"
                info["verdict"] = "login_required"
                info["verdict_label"] = VERDICT_LABELS["login_required"]
                return info

        time.sleep(2)
        try:
            info["final_url"] = page.url
            info["title"] = pu.clean(page.title())
        except Exception:
            pass
        info["institution_seen"] = pu.institution_seen(page, cfg["institution"]["name"])
        info["screenshots"].append(str(bu.screenshot(page, shot_dir, f"probe_{pid}_home")))
        log(f"[探测] {name} 打开：HTTP {info['status']}｜{info['final_url'][:90]}｜{info['title'][:40]}")

        login = pu.detect_login(page)
        if login["required"]:
            info["login_required"] = True
            info["login_signals"] = list(login.get("signals") or [])
            log(f"[探测] {name} 有登录墙：{info['login_signals']}")
            credentials = creds_data if creds_data is not None else creds.get(pid)
            if credentials:
                ok, method = pu.login_with_credentials(page, credentials, log)
                info["login_method"] = method
                if ok:
                    time.sleep(1)
                    if pu.detect_login(page)["required"]:
                        if not bu.goto(page, url, cfg, log):
                            log("[探测] 登录后回入口失败，继续用当前页")
                        time.sleep(2)
                    info["login_required"] = pu.detect_login(page)["required"]
            elif wait_login:
                ok = pu.wait_manual_login(page, cfg, log, wait_login, name)
                info["login_method"] = "manual" if ok else "manual-timeout"
                if ok:
                    time.sleep(1)
                    if pu.detect_login(page)["required"]:
                        if not bu.goto(page, url, cfg, log):
                            log("[探测] 登录后回入口失败，继续用当前页")
                        time.sleep(2)
                    info["login_required"] = pu.detect_login(page)["required"]

            if info["login_required"]:
                info["verdict"] = "login_required"
                info["verdict_label"] = VERDICT_LABELS["login_required"]
                info["screenshots"].append(str(bu.screenshot(page, shot_dir, f"probe_{pid}_login")))
                log(f"[探测] {name} 需要账号/登录，已记录并暂停该平台")
                return info

        box, box_selector = pu.find_search_box(page, selectors)
        if box is None:
            time.sleep(3)
            box, box_selector = pu.find_search_box(page, selectors)
        if box is None:
            info["verdict"] = "no_search"
            info["verdict_label"] = VERDICT_LABELS["no_search"]
            info["screenshots"].append(str(bu.screenshot(page, shot_dir, f"probe_{pid}_nosearch")))
            log(f"[探测] {name} 未找到检索框")
            return info

        info["search"]["box_selector"] = box_selector
        target = pu.submit_search(context, page, box, keyword, selectors, log)
        if target is not page:
            page = target
        time.sleep(2)
        info["final_url"] = page.url
        info["search"]["ok"] = True
        items = pu.collect_results(page, selectors, 5, log)
        info["search"]["result_count"] = len(items)
        info["search"]["sample"] = items[:3]
        info["screenshots"].append(str(bu.screenshot(page, shot_dir, f"probe_{pid}_results")))
        learned = _learned(page, selectors, box_selector)

        targets, _ = pu.pdf_targets(page, selectors)
        info["download"]["pdf_links"] = sum(1 for t in targets if t["href"])
        info["download"]["pdf_buttons"] = sum(1 for t in targets if not t["href"])
        info["download"]["paywall"] = pu.paywall_hint(page)
        detail_targets = []
        if items and not quick:
            if bu.goto(page, items[0]["url"], cfg, log):
                time.sleep(2)
                info["screenshots"].append(str(bu.screenshot(page, shot_dir, f"probe_{pid}_detail")))
                detail_targets, _ = pu.pdf_targets(page, selectors)
                info["download"]["detail_pdf"] = bool(detail_targets)
                info["download"]["detail_paywall"] = pu.paywall_hint(page)
                for target in detail_targets:
                    selector = target.get("selector") or ""
                    if selector and selector != "generic":
                        learned["pdf_button"] = [selector]
                        break
                detail_meta = pu.extract_detail(page, selectors, platform, log)
                info["detail_sample"] = {
                    key: detail_meta.get(key) for key in ("标题", "作者", "年份", "DOI")
                }
                info["detail_abstract_len"] = len(detail_meta.get("摘要") or "")

        has_pdf = bool(targets) or bool(detail_targets)
        paywall = info["download"]["detail_paywall"] or info["download"]["paywall"]
        if has_pdf and not paywall:
            verdict = "fulltext_ok"
        elif has_pdf and paywall:
            verdict = "oa_partial"
        elif info["search"]["result_count"] == 0 and not targets:
            verdict = "metadata_only"
            info["note"] = info["note"] or "检索有响应但未抓到结果，可能是选择器/页面结构问题"
        else:
            verdict = "metadata_only"
        info["verdict"] = verdict
        info["verdict_label"] = VERDICT_LABELS[verdict]
        info["selectors_learned"] = learned
        log(f"[探测] {name} → {info['verdict_label']}（结果 {info['search']['result_count']} 条，"
            f"PDF 入口 {info['download']['pdf_links'] + info['download']['pdf_buttons']}，"
            f"付费墙={paywall or '未见'}）")
        return info
    except Exception as exc:  # noqa: BLE001
        info["verdict"] = "error"
        info["verdict_label"] = VERDICT_LABELS["error"]
        info["note"] = f"{type(exc).__name__}: {exc}"
        info["screenshots"].append(str(bu.screenshot(page, shot_dir, f"probe_{pid}_error")))
        log(f"[探测] {name} 异常：{info['note']}")
        return info
    finally:
        bu.close_context(pw, context, log)


def _serializable(info: dict) -> dict:
    return {k: v for k, v in info.items() if not k.startswith("_")}


def ensure(cfg: dict, platform_ids: list[str], run_dir: Path, log, keyword: str,
           platforms_map: dict | None = None, refresh: bool = False,
           wait_login: int = 0, quick: bool | None = None) -> dict:
    """批量探测：优先缓存，结果汇总到 <run_dir>/平台探测.json。"""
    platforms_map = platforms_map or {}
    if quick is None:
        quick = not bool((cfg.get("platforms") or {}).get("probe_detail", True))
    results: dict[str, dict] = {}
    for pid in platform_ids:
        platform = platforms_map.get(pid) or pf.get(pid)
        if not platform:
            log(f"[探测] 跳过未知平台：{pid}")
            continue
        url = platform.get("entry") or platform.get("home") or ""
        cached = load_cache(cfg, platform, url, refresh)
        if cached:
            cached.update({"platform": pid, "name": platform.get("short") or pid, "cached": True})
            results[pid] = cached
            log(f"[探测] {cached['name']} 用缓存（{cached.get('checked_at')}）：{cached.get('verdict_label')}")
            continue
        info = probe_one(cfg, platform, url, keyword, run_dir, log, quick=quick, wait_login=wait_login)
        save_cache(cfg, info)
        results[pid] = info
    if results:
        lc.write_json(run_dir / "平台探测.json", [_serializable(v) for v in results.values()])
    return results


def main() -> int:
    lc.setup_console()
    parser = argparse.ArgumentParser(description="探测文献平台：能不能检索、能不能下载全文")
    parser.add_argument("--platform", action="append", help="平台 id（可重复）")
    parser.add_argument("--url", action="append", help="任意网址（可重复）")
    parser.add_argument("--all", action="store_true", help="探测全部可选平台")
    parser.add_argument("--keyword", default="", help="探测用检索词")
    parser.add_argument("--run-dir", help="输出目录")
    parser.add_argument("--quick", action="store_true", help="只看首页+结果页")
    parser.add_argument("--wait-login", type=int, default=0, help="人工登录等待秒数")
    parser.add_argument("--refresh", action="store_true", help="忽略缓存")
    parser.add_argument("--save", action="store_true", help="保存学到的选择器")
    parser.add_argument("--add", action="store_true", help="把 --url 登记为自定义平台后退出")
    parser.add_argument("--config", help="配置文件")
    parser.add_argument("--platforms-file", help="覆盖 platforms.json")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    args = parser.parse_args()
    lc.print_env()

    registry_path = args.platforms_file
    if args.add:
        if not args.url:
            lc.die(lc.EXIT_CONFIG, "--add 需要配合 --url 使用")
        for url in args.url:
            item, created = pf.add_custom(url, "", registry_path)
            print(f"{'已登记' if created else '已存在'}：{item['id']}｜{item['name']}")
        return lc.EXIT_OK

    cfg = lc.load_config(args.config)
    keyword = args.keyword or "文献"
    run_dir = Path(args.run_dir) if args.run_dir else (
        Path(cfg["output_root"]) / "_平台探测" / f"{datetime.now():%Y%m%d_%H%M%S}"
    )
    run_dir.mkdir(parents=True, exist_ok=True)
    log = lc.RunLog(run_dir / "日志" / "probe.log")

    targets: list[tuple[dict, str]] = []
    try:
        if args.all:
            for platform in pf.selectable(registry_path):
                targets.append((pf.merge_learned(platform, registry_path),
                                platform.get("entry") or platform.get("home")))
        for pid in args.platform or []:
            platform = pf.get(pid, registry_path)
            if not platform:
                lc.die(lc.EXIT_CONFIG, f"没有这个平台：{pid}（先用 platforms.py --add 登记）")
            targets.append((pf.merge_learned(platform, registry_path),
                            platform.get("entry") or platform.get("home")))
        for url in args.url or []:
            platform = pf.resolve(url, registry_path)
            if platform:
                targets.append((pf.merge_learned(platform, registry_path), url))
            else:
                targets.append((pf.inline_custom(url), url))
        if not targets:
            lc.die(lc.EXIT_CONFIG, "没有目标：用 --platform / --url / --all 指定")

        results = []
        for platform, url in targets:
            info = probe_one(cfg, platform, url, keyword, run_dir, log,
                             quick=args.quick, wait_login=args.wait_login)
            save_cache(cfg, info)
            results.append(info)

        lc.write_json(run_dir / "平台探测.json", [_serializable(r) for r in results])
        if args.save:
            for info in results:
                if info.get("selectors_learned"):
                    path = pf.learn(info["platform"], info["selectors_learned"],
                                    source=str(run_dir / "平台探测.json"))
                    log(f"[探测] 已保存选择器：{info['platform']} → {path}")

        print("\n" + "=" * 78)
        print("平台探测结果")
        print("-" * 78)
        for info in results:
            print(f"  {info['name'][:18]:<20}{info['verdict_label']:<28}"
                  f"结果 {info['search']['result_count']} 条")
            if info.get("note"):
                print(f"      说明：{info['note']}")
        print(f"\n  报告：{run_dir / '平台探测.json'}")
        print(f"  截图：{run_dir / '平台探测' / '截图'}")
        print("=" * 78)

        if args.json:
            print(json.dumps([_serializable(r) for r in results], ensure_ascii=False, indent=2))

        codes = {r["verdict"] for r in results}
        if "unreachable" in codes or "error" in codes:
            return lc.EXIT_PARTIAL
        if "login_required" in codes:
            return lc.EXIT_NEED_USER
        return lc.EXIT_OK
    finally:
        log.close()


if __name__ == "__main__":
    sys.exit(main())
