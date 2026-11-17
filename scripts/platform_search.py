# -*- coding: utf-8 -*-
"""platform_search.py —— 通用平台检索（SinoMed / ScienceDirect / CAS / 自定义网址 …）

流程：
    打开入口 → 识别登录墙（已存凭据/人工登录）→ 找检索框 → 提交 → 抓结果列表
    → 逐条进详情页取字段（meta/JSON-LD/选择器）→ 写 records_<平台>.json。
    被登录墙挡住且没有账号时，返回退出码 2 并打印「手动登录 / 保存账号」两条命令。

参数：
    --platform ID        平台 id（注册表）
    --url URL            直接对一个网址检索（不登记也可用）
    --entry INDEX|文字   使用平台 entries 里的第 N 个入口（1 起）或标签包含的文字
    --query "检索词"     必填
    --limit N            条数上限（默认 20）
    --run-dir DIR        输出目录（写 records_<平台>.json 与 平台登录.json）
    --task FILE          检索任务.json（读其中的 custom_platforms）
    --wait-login SEC     遇到登录墙时等人手动登录的秒数
    --username U         本次登录账号（配合 --password-stdin / --password-env）
    --password-stdin     从标准输入读密码
    --password-env NAME  从环境变量读密码
    --save-creds         登录成功后保存账号（默认不保存）
    --no-detail          只抓列表，不进详情页
    --selectors FILE     CNKI 选择器文件（其他平台从 platforms.json 取）
    --config PATH        配置文件
    --platforms-file PATH 覆盖 platforms.json
    --json               输出记录 JSON
退出码：0 成功 / 1 部分失败（没抓到结果、详情页失败） / 2 需用户处理（登录/滑块） / 3 配置错误
依赖：playwright（msedge）、platform_util、creds
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


def _selectors_for(platform: dict, cnki_selectors: dict | None) -> dict:
    selectors = dict(pf.merge_learned(platform).get("selectors") or {})
    ref = platform.get("selectors_ref")
    if ref == "selectors.json" and cnki_selectors:
        merged = dict(cnki_selectors)
        merged.update(selectors)
        return merged
    return selectors


def _record_login(run_dir: Path, info: dict) -> None:
    path = run_dir / "平台登录.json"
    data = lc.read_json(path, default=[]) or []
    if isinstance(data, dict):
        data = data.get("platforms") or []
    data = [item for item in data if item.get("platform") != info.get("platform")]
    data.append(info)
    lc.write_json(path, data)


def _signature(rec: dict) -> str:
    return (rec.get("DOI") or "").lower().strip() or (rec.get("标题") or "").strip()


def _record(meta: dict, url: str, platform: dict, pdf_available: bool, pdf_url: str,
            note: str) -> dict:
    rec = {
        "标题": "", "作者": "", "期刊/来源": "", "年份": "", "卷": "", "期": "",
        "页码": "", "DOI": "", "PMID": "", "来源库": platform.get("short") or platform.get("id"),
        "文献类型": "", "摘要": "", "关键词": "", "全文状态": "未下载", "本地文件": "",
        "原文链接": url, "备注": note, "source_db": platform.get("id") or "custom",
        "url": url, "pdf_available": pdf_available, "pdf_url": pdf_url,
    }
    rec.update({k: v for k, v in meta.items() if v not in (None, "")})
    return rec


def _login_prompt(platform: dict, query: str, run_dir: Path, reason: str) -> str:
    pid = platform.get("id") or "custom"
    name = platform.get("short") or platform.get("name") or pid
    return (
        f"\n[需要账号] {name} 需要登录，本次未检索该平台（原因：{reason}）。\n"
        f"  二选一：\n"
        f"  A. 手动登录（推荐，推荐用自己账号，脚本只等不代填）：\n"
        f"     python scripts\\platform_search.py --platform {pid} --query \"{query}\" "
        f"--run-dir \"{run_dir}\" --wait-login 180\n"
        f"  B. 保存账号后重试：\n"
        f"     python scripts\\creds.py --set {pid} --username 你的账号\n"
        f"     python scripts\\platform_search.py --platform {pid} --query \"{query}\" "
        f"--run-dir \"{run_dir}\"\n"
        f"  说明：账号密码只保存在本机用户目录，不写日志、不写进任务文件。\n"
        f"  如果该平台只是题录库、没有全文，也可以不登录，改查其他已订阅平台。"
    )


def _resolve_entry(platform: dict, entry: str | None) -> str:
    entries = platform.get("entries") or []
    if entry:
        if str(entry).isdigit():
            index = int(entry) - 1
            if 0 <= index < len(entries):
                return entries[index]["url"]
        for item in entries:
            if str(entry).lower() in (item.get("label") or "").lower():
                return item["url"]
    return platform.get("entry") or platform.get("home") or (entries[0]["url"] if entries else "")


def _capacity_note(run_dir: Path, platform: dict) -> str:
    note = f"平台：{platform.get('name') or platform.get('id')}"
    data = lc.read_json(run_dir / "平台探测.json", default=[]) or []
    if isinstance(data, dict):
        data = data.get("platforms") or []
    for item in data:
        if item.get("platform") == platform.get("id"):
            note += f"；探测：{item.get('verdict_label') or item.get('verdict')}"
            break
    return note


def run_platform(cfg: dict, platform: dict, query: str, limit: int, run_dir: Path, log,
                 cnki_selectors: dict | None = None, wait_login: int = 0,
                 username: str = "", password: str = "", save_creds: bool = False,
                 detail: bool = True, entry: str | None = None) -> tuple[int, list[dict], dict]:
    pid = platform.get("id") or "custom"
    name = platform.get("short") or platform.get("name") or pid
    start_url = _resolve_entry(platform, entry)
    selectors = _selectors_for(platform, cnki_selectors)
    info = {
        "platform": pid,
        "name": name,
        "url": start_url,
        "ok": False,
        "needs_credentials": False,
        "reason": "",
        "method": "not-needed",
        "checked_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    if not start_url:
        info["reason"] = "平台没有入口网址"
        _record_login(run_dir, info)
        log(f"[平台] {name} 没有入口网址")
        return lc.EXIT_CONFIG, [], info

    shot_dir = run_dir / "日志"
    pw, context, page = bu.open_context(cfg, log)
    try:
        if not bu.goto(page, start_url, cfg, log):
            info["reason"] = "入口打不开"
            _record_login(run_dir, info)
            qu = bu.screenshot(page, shot_dir, f"platform_{pid}_home_fail")
            log(f"[平台] {name} 入口打不开：{start_url}（截图 {qu}）")
            return lc.EXIT_NEED_USER, [], info
        if "/verify/" in page.url and not bu.wait_for_verify(page, cfg, shot_dir, log):
            info["reason"] = "滑块未完成"
            _record_login(run_dir, info)
            return lc.EXIT_NEED_USER, [], info
        time.sleep(2)

        login = pu.detect_login(page)
        if login["required"]:
            info["method"] = "login-required"
            log(f"[平台] {name} 需要登录：{login.get('signals')}")
            credentials = None
            if username and password:
                credentials = {"username": username, "password": password}
            else:
                credentials = creds.get(pid)
            if credentials:
                ok, method = pu.login_with_credentials(page, credentials, log)
                info["method"] = method
                if ok:
                    if save_creds and username and password:
                        creds.set_entry(pid, username, password, note="平台检索自动保存")
                        log(f"[平台] 已保存 {pid} 账号（密码不落日志）")
                    time.sleep(1)
                    # 登录后若当前页仍是登录页，才回入口；否则留在落地页（很多 SSO 会跳到首页）
                    if pu.detect_login(page)["required"]:
                        if not bu.goto(page, start_url, cfg, log):
                            log("[平台] 登录后回入口失败，继续当前页")
                        time.sleep(2)
                    login = pu.detect_login(page)
                else:
                    log(f"[平台] 保存的账号登录失败：{method}")
            elif wait_login:
                ok = pu.wait_manual_login(page, cfg, log, wait_login, name)
                info["method"] = "manual" if ok else "manual-timeout"
                if ok:
                    time.sleep(1)
                    if pu.detect_login(page)["required"]:
                        if not bu.goto(page, start_url, cfg, log):
                            log("[平台] 登录后回入口失败，继续当前页")
                        time.sleep(2)
                    login = pu.detect_login(page)

            if login["required"]:
                info["needs_credentials"] = True
                info["reason"] = info["method"] or "login-required"
                info["login_url"] = page.url
                info["signals"] = list(login.get("signals") or [])
                _record_login(run_dir, info)
                bu.screenshot(page, shot_dir, f"platform_{pid}_login")
                print(_login_prompt(platform, query, run_dir, info["reason"]))
                return lc.EXIT_NEED_USER, [], info

        box, box_selector = pu.find_search_box(page, selectors)
        if box is None:
            time.sleep(3)
            box, box_selector = pu.find_search_box(page, selectors)
        if box is None:
            info["reason"] = "未找到检索框（页面可能改版或需要登录）"
            _record_login(run_dir, info)
            path = bu.screenshot(page, shot_dir, f"platform_{pid}_nosearch")
            log(f"[平台] {name} 未找到检索框，截图 {path}")
            return lc.EXIT_PARTIAL, [], info
        log(f"[平台] {name} 检索框：{box_selector}")

        target = pu.submit_search(context, page, box, query, selectors, log)
        if target is not page:
            page = target
        time.sleep(2)
        if "/verify/" in page.url and not bu.wait_for_verify(page, cfg, shot_dir, log):
            info["reason"] = "滑块未完成"
            _record_login(run_dir, info)
            return lc.EXIT_NEED_USER, [], info

        items = pu.collect_results(page, selectors, limit, log)
        info["result_count"] = len(items)
        log(f"[平台] {name} 抓到 {len(items)} 条（{page.url[:90]}）")
        if not items:
            path = bu.screenshot(page, shot_dir, f"platform_{pid}_noitems")
            info["reason"] = "结果列表为空（选择器未命中或该检索词无结果）"
            _record_login(run_dir, info)
            log(f"[平台] {name} 解析 0 条，截图 {path}")
            return lc.EXIT_PARTIAL, [], info

        note = _capacity_note(run_dir, platform)
        interval = float((cfg.get("platforms") or {}).get("interval_sec", 2.5))
        records: list[dict] = []
        seen: set[str] = set()
        for index, item in enumerate(items, 1):
            url = item["url"]
            key = url.lower()
            if key in seen:
                continue
            seen.add(key)
            meta = {"标题": item["标题"]}
            pdf_available, pdf_url = False, ""
            if detail:
                time.sleep(interval)
                if bu.goto(page, url, cfg, log):
                    time.sleep(1.5)
                    meta = pu.extract_detail(page, selectors, platform, log)
                    targets, _ = pu.pdf_targets(page, selectors)
                    pdf_available = bool(targets)
                    pdf_url = next((t["href"] for t in targets if t.get("href")), "")
                else:
                    log(f"[平台] {index}/{len(items)} 详情页打不开：{url[:80]}")
            rec = _record(meta, url, platform, pdf_available, pdf_url, note)
            records.append(rec)
            log(f"[平台] {index}/{len(items)} {(rec.get('标题') or '')[:40]}｜"
                f"摘要 {len(rec.get('摘要') or '')} 字｜PDF={pdf_available}")

        info["ok"] = True
        info["method"] = info["method"] if info["method"] != "login-required" else "not-needed"
        _record_login(run_dir, info)
        path = run_dir / f"records_{lc.sanitize_filename(pid, 40)}.json"
        lc.write_json(path, records)
        log(f"[平台] {name} 写入 {path}（{len(records)} 条）")
        return lc.EXIT_OK, records, info
    except Exception as exc:  # noqa: BLE001
        import traceback

        info["reason"] = f"{type(exc).__name__}: {exc}"
        _record_login(run_dir, info)
        log(f"[平台] {name} 异常：{info['reason']}")
        log.raw(traceback.format_exc())
        return lc.EXIT_PARTIAL, [], info
    finally:
        bu.close_context(pw, context, log)


def _platform_from_args(args) -> dict | None:
    if args.task:
        task = lc.read_json(args.task, default={}) or {}
        customs = (task.get("search") or {}).get("custom_platforms") or []
        if args.platform:
            for item in customs:
                if item.get("id") == args.platform:
                    return item
    if args.platform:
        return pf.get(args.platform, args.platforms_file)
    if args.url:
        found = pf.resolve(args.url, args.platforms_file)
        return found or pf.inline_custom(args.url)
    return None


def main() -> int:
    lc.setup_console()
    parser = argparse.ArgumentParser(description="通用平台检索（含登录处理）")
    parser.add_argument("--platform", help="平台 id")
    parser.add_argument("--url", help="直接检索一个网址")
    parser.add_argument("--entry", help="入口序号（1 起）或标签文字")
    parser.add_argument("--query", required=True, help="检索词")
    parser.add_argument("--limit", type=int, help="条数上限")
    parser.add_argument("--run-dir", help="输出目录")
    parser.add_argument("--task", help="检索任务.json（读 custom_platforms）")
    parser.add_argument("--wait-login", type=int, default=0, help="人工登录等待秒数")
    parser.add_argument("--username", help="本次登录账号")
    parser.add_argument("--password-stdin", action="store_true", help="从标准输入读密码")
    parser.add_argument("--password-env", help="从环境变量读密码")
    parser.add_argument("--save-creds", action="store_true", help="登录成功后保存账号")
    parser.add_argument("--no-detail", action="store_true", help="只抓列表")
    parser.add_argument("--selectors", help="CNKI 选择器文件")
    parser.add_argument("--config", help="配置文件")
    parser.add_argument("--platforms-file", help="覆盖 platforms.json")
    parser.add_argument("--json", action="store_true", help="输出记录 JSON")
    args = parser.parse_args()
    lc.print_env()

    cfg = lc.load_config(args.config)
    platform = _platform_from_args(args)
    if not platform:
        lc.die(lc.EXIT_CONFIG, "需要 --platform 或 --url（可先用 platforms.py --list 看有哪些）")

    password = ""
    if args.password_env:
        import os

        password = os.environ.get(args.password_env, "")
    elif args.password_stdin:
        password = sys.stdin.readline().rstrip("\r\n")

    limit = max(1, min(args.limit or int((cfg.get("platforms") or {}).get("limit", 20)), 50))
    run_dir = Path(args.run_dir) if args.run_dir else lc.make_run_dir(
        cfg, f"{platform.get('short') or platform['id']}_{args.query}"[:30]
    )
    run_dir.mkdir(parents=True, exist_ok=True)
    log = lc.RunLog(run_dir / "日志" / "run.log")
    try:
        cnki_selectors = bu.load_selectors(args.selectors, log)
        code, records, info = run_platform(
            cfg, platform, args.query, limit, run_dir, log, cnki_selectors,
            wait_login=args.wait_login, username=args.username or "", password=password,
            save_creds=args.save_creds, detail=not args.no_detail, entry=args.entry,
        )
        if args.json:
            print(json.dumps({"platform": info, "records": records}, ensure_ascii=False, indent=2))
        print(f"\n平台：{info['name']}｜状态：{'完成' if info['ok'] else info['reason']}｜"
              f"记录：{len(records)} 条")
        print(f"输出目录：{run_dir}")
        return code
    finally:
        log.close()


if __name__ == "__main__":
    sys.exit(main())
