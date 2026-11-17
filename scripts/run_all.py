# -*- coding: utf-8 -*-
"""run_all.py —— 串起完整流程

流程（顺序固定，不可颠倒）：
    Step 0 采集任务：关键词 / 搜索提示词 / 筛查方向 / 文献时间 / 期望相关度 / 检索平台 / 首次登录
    Step 1 生成并确认检索式
    Step 2 确认 VPN 与学校账号（技术预检 + 用户确认 + 机构身份实测），未确认即暂停
    Step 3 确认平台首次登录/机构登录设置（规则 7），未确认即暂停
    Step 4 探测其他平台能不能检索、能不能下载（规则 5）
    Step 5 PubMed + 知网 + 其他平台检索
    Step 6 跨库去重 + 按筛查方向逐篇判定（纳入 / 待定 / 排除）
    Step 7 下载纳入文献全文（待定/排除默认不下）
    Step 8 生成 23 列 Excel 与 文献列表.md（原文标题/摘要/选择依据）
    Step 9 汇报（数字必须与 Excel、清单一致）

参数：
    --task FILE             检索任务.json（推荐：宿主先问清再落盘）
    --interactive           启动终端向导采集任务
    --keywords "a,b"        中文关键词
    --keywords-en "x,y"     英文关键词，与中文一一对应
    --prompt "..."          文献搜索提示词
    --direction "..."       筛查方向（必填项，缺失会拒绝检索）
    --include/--exclude/--must/--prefer "a,b"
    --relevance             期望相关度 loose/standard/strict
    --min-score N / --max-items N
    --years-min/--years-max 文献时间范围
    --platforms "pubmed,cnki,sinomed,sciencedirect"
    --platform-url URL      补充网址（可重复；未登记自动当自定义平台）
    --school-list PATH|URL  学校公布的数据库清单（txt/html 或图书馆页面网址）
    --first-login ready|first_time|unknown
    --login-pending "SciFindern,SinoMed"
    --confirm-platform-login 用户已确认所选平台都登录过、机构设置已保存
    --wait-login SEC        首次登录时等人工登录的秒数（0=不等待，提示先登录）
    --no-probe / --refresh-probe
    --databases "pubmed,cnki" 旧参数，等价于 --platforms 的子集
    --keyword "关键词"      旧参数（会降级为关键词匹配）
    --list FILE             已知文献清单（每行 DOI / PMID / 标题）；仅 PubMed 分支
    --years N / --limit N   覆盖任务里的年限与每库条数
    --no-download / --no-screen / --judgements FILE / --download-pending
    --refresh               忽略已有记录重新检索
    --yes                   跳过检索式确认
    --confirm-vpn / --confirm-account
    --skip-institution / --skip-vpn（后者仅调试）
    --skip-pubmed / --skip-cnki
    --config PATH
退出码：0 全部成功 / 1 部分失败 / 2 需用户处理 / 3 配置错误
依赖：其余全部脚本
"""
from __future__ import annotations

import argparse
import re
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import browser_util as bu  # noqa: E402
import cnki_search  # noqa: E402
import download_fulltext  # noqa: E402
import intake  # noqa: E402
import litcommon as lc  # noqa: E402
import make_excel  # noqa: E402
import make_report  # noqa: E402
import plan_query  # noqa: E402
import platform_login  # noqa: E402
import platform_search  # noqa: E402
import platforms as pf  # noqa: E402
import precheck  # noqa: E402
import probe_platform  # noqa: E402
import pubmed_search  # noqa: E402
import screen  # noqa: E402

DOI_RE = re.compile(r"^10\.\d{4,9}/\S+$")
PMID_RE = re.compile(r"^\d{7,9}$")


def _read_list(path: Path) -> list[dict]:
    entries = []
    for line in lc.read_text_auto(path).splitlines():
        text = line.strip().strip(",")
        if not text or text.startswith("#"):
            continue
        if DOI_RE.match(text):
            entries.append({"type": "doi", "value": text, "term": f'"{text}"[DOI]'})
        elif PMID_RE.match(text):
            entries.append({"type": "pmid", "value": text, "term": f"{text}[uid]"})
        else:
            entries.append({"type": "title", "value": text, "term": f'"{text}"[Title]'})
    return entries


def _search_list(cfg: dict, entries: list[dict], limit: int, log) -> tuple[int, list[dict]]:
    """清单模式：逐条精确检索 PubMed，合并去重。"""
    records, seen, code = [], set(), lc.EXIT_OK
    for index, entry in enumerate(entries, 1):
        item_code, found, _ = pubmed_search.search_pubmed(cfg, entry["term"], 1, 0, None)
        if item_code != lc.EXIT_OK:
            code = lc.EXIT_PARTIAL
            log(f"[清单] {index}/{len(entries)} 检索失败：{entry['value'][:50]}")
            continue
        for rec in found:
            key = rec.get("DOI") or rec.get("PMID") or rec.get("标题")
            if key in seen:
                continue
            seen.add(key)
            rec["备注"] = f"清单模式：{entry['type']}"
            records.append(rec)
        if len(records) >= limit:
            break
    log(f"[清单] 共解析 {len(records)} 条（输入 {len(entries)} 条）")
    return code, records


def _merge_prior(records: list[dict], prior: list[dict]) -> None:
    """重检时保留上次的下载成果：按 DOI → PMID → 标题 匹配，文件还在就沿用状态。"""
    def key(rec):
        return (rec.get("DOI") or "").lower().strip() or (rec.get("PMID") or "").strip() \
            or (rec.get("标题") or "").strip()

    old = {key(r): r for r in prior if key(r)}
    for rec in records:
        prev = old.get(key(rec))
        if not prev:
            continue
        local = Path(prev.get("本地文件") or "")
        if local and local.exists():
            rec["全文状态"] = prev.get("全文状态") or "已下载"
            rec["本地文件"] = str(local)
            rec["备注"] = prev.get("备注") or rec.get("备注") or ""


def _report(records: list[dict], run_dir: Path, excel: Path, md: Path, stats: dict, log) -> None:
    counts = lc.summarize(records)
    pdf = sum(1 for r in records if (r.get("本地文件") or "").lower().endswith(".pdf"))
    caj = sum(1 for r in records if (r.get("本地文件") or "").lower().endswith(".caj"))
    pubmed_n = sum(1 for r in records if r.get("source_db") == "pubmed")
    cnki_n = sum(1 for r in records if r.get("source_db") == "cnki")
    others: dict[str, int] = {}
    for rec in records:
        if rec.get("source_db") in ("pubmed", "cnki"):
            continue
        label = rec.get("来源库") or rec.get("source_db") or "其他"
        others[label] = others.get(label, 0) + 1
    pending = lc.read_json(run_dir / "平台待处理.json", default=[]) or []

    print("\n" + "=" * 62)
    print("检索完成")
    print(f"  PubMed：{pubmed_n} 条   知网：{cnki_n} 条   合计：{len(records)} 条")
    if others:
        print("  其他平台：" + "   ".join(f"{name} {count} 条" for name, count in others.items()))
    if stats:
        print(f"  筛选：纳入 {stats.get('纳入', 0)} / 待定 {stats.get('待定', 0)}"
              f" / 排除 {stats.get('排除', 0)}")
    print(f"  全文：PDF {pdf} 篇，CAJ {caj} 篇")
    for status in ("需付费", "无权限", "需登录", "仅摘要", "仅CAJ", "下载失败", "未下载"):
        if counts.get(status):
            print(f"  {status}：{counts[status]} 条")
    if pending:
        print("  待处理平台（未检索/需登录）：")
        for item in pending:
            print(f"    - {item.get('name') or item.get('platform')}："
                  f"{item.get('reason') or '需要登录'}")
    print(f"  输出目录：{run_dir}")
    print(f"  文献列表（含摘要与选择依据）：{md}")
    print(f"  文献清单（Excel）：{excel}")
    print(f"  日志：{run_dir / '日志' / 'run.log'}")
    print("=" * 62)
    log(f"[汇报] PubMed={pubmed_n} 知网={cnki_n} 其他={others} 合计={len(records)} "
        f"PDF={pdf} CAJ={caj} 状态={counts} 筛选={stats} 待处理={len(pending)}")


def _load_or_search(cfg, name: str, run_dir: Path, refresh: bool) -> list[dict] | None:
    """有非空记录才算续跑；空文件（上次跳过该库留下的）应重新检索。"""
    path = run_dir / name
    if path.exists() and not refresh:
        data = lc.read_json(path, default=[]) or []
        if data:
            return data
    return None


def _platforms_map(task: dict) -> dict[str, dict]:
    """平台 id → 合并了探测选择器的平台定义（含任务里的自定义平台）。"""
    search = task.get("search") or {}
    customs = {
        item["id"]: item for item in search.get("custom_platforms") or []
        if isinstance(item, dict) and item.get("id")
    }
    result: dict[str, dict] = {}
    for pid in search.get("platforms") or []:
        platform = customs.get(pid) or pf.get(pid)
        if platform:
            result[pid] = pf.merge_learned(platform)
    return result


def _apply_platform_args(task: dict, args) -> dict:
    """把 --platforms / --platform-url / --school-list / --first-login 合进任务。"""
    search = task["search"]
    ids = list(search.get("platforms") or [])
    customs = list(search.get("custom_platforms") or [])

    if getattr(args, "platforms", None):
        ids = []
        for raw in intake._split(args.platforms):
            found = pf.get(raw, customs=customs)
            pid = found["id"] if found else str(raw).strip().lower()
            if pid and pid not in ids:
                ids.append(pid)
    if getattr(args, "databases", None):
        legacy = [x for x in intake._split(args.databases) if x in ("pubmed", "cnki")]
        if legacy:
            ids = list(dict.fromkeys(ids + legacy)) if getattr(args, "platforms", None) else legacy

    for url in getattr(args, "platform_url", None) or []:
        item = pf.inline_custom(url)
        if not any(c.get("id") == item["id"] for c in customs):
            customs.append(item)
        if item["id"] not in ids:
            ids.append(item["id"])

    school_list = getattr(args, "school_list", None)
    if school_list:
        result = pf.import_school_list(school_list, customs=customs)
        if result.get("error"):
            lc.die(lc.EXIT_CONFIG, f"读取学校清单失败：{result['error']}")
        for pid in result["matched"]:
            if pid not in ids:
                ids.append(pid)
        for url in result["unknown"]:
            item = pf.inline_custom(url)
            if not any(c.get("id") == item["id"] for c in customs):
                customs.append(item)
            if item["id"] not in ids:
                ids.append(item["id"])
        print(f"[学校清单] 识别到平台：{'、'.join(result['names']) or '（无）'}；"
              f"未识别网址 {len(result['unknown'])} 个（将逐个探测）")

    search["platforms"] = ids
    search["databases"] = [p for p in search["platforms"] if p in ("pubmed", "cnki")]
    search["custom_platforms"] = customs
    if getattr(args, "vpn_channel", None):
        task.setdefault("vpn", {})["channel"] = args.vpn_channel
    if getattr(args, "first_login", None):
        search["login_setup"] = {
            "status": args.first_login,
            "pending": intake._as_list(getattr(args, "login_pending", "") or ""),
        }
    return task


def _dedupe_records(run_dir: Path, log) -> None:
    """跨库去重：DOI/标题相同只留一条（PubMed > 知网 > 平台），其余并入备注。"""
    records = lc.read_records(run_dir)
    if not records:
        return
    priority = {"pubmed": 0, "cnki": 1}
    records.sort(key=lambda rec: priority.get(rec.get("source_db"), 2))
    seen: dict[str, dict] = {}
    keep: list[dict] = []
    for rec in records:
        key = (rec.get("DOI") or "").lower().strip() or \
            re.sub(r"\W+", "", (rec.get("标题") or "").lower())[:120]
        if not key:
            keep.append(rec)
            continue
        first = seen.get(key)
        if first is None:
            seen[key] = rec
            keep.append(rec)
            continue
        other = rec.get("来源库") or rec.get("source_db") or ""
        if other:
            first["其他来源"] = "; ".join(x for x in [first.get("其他来源"), other] if x)
            note = (first.get("备注") or "").strip("；; ")
            first["备注"] = f"{note}；同文见 {other}" if note else f"同文见 {other}"
        log(f"[去重] {(rec.get('标题') or '')[:30]} 与 {first.get('来源库') or '已有记录'} 重复，已合并来源")

    groups: dict[str, list[dict]] = {}
    for rec in keep:
        key = lc.sanitize_filename(str(rec.get("source_db") or "unknown"), 40)
        groups.setdefault(key, []).append(rec)
    for path in run_dir.glob("records_*.json"):
        key = lc.sanitize_filename(path.stem[len("records_"):], 40)
        if key not in groups:
            path.unlink()
    for key, items in groups.items():
        lc.write_json(run_dir / f"records_{key}.json", items)
    if len(keep) != len(records):
        log(f"[去重] {len(records)} → {len(keep)} 条")


def _build_task(args, entries: list[dict]) -> dict | None:
    """按优先级取任务：--task > 参数 > 旧 --keyword > 交互向导 > None（需宿主去问）。"""
    try:
        if args.task:
            data = lc.read_json(args.task, default=None)
            if not data:
                lc.die(lc.EXIT_CONFIG, f"任务文件不存在或不是合法 JSON：{args.task}")
            task = intake.normalize_task(data)
        elif args.keywords:
            task = intake.build_from_flags(args)
        elif args.keyword or args.text:
            if not (args.direction or "").strip() and not args.allow_keyword_only:
                return None  # 缺筛查方向：不降级，先让宿主去问
            task = intake.build_from_keyword(args.keyword or args.text, args.direction or "")
        elif entries:
            # 清单模式：没给任务时用文件名当关键词，仅做关键词匹配
            task = intake.build_from_keyword(Path(args.list).stem, args.direction or "")
        elif args.interactive or (not entries and sys.stdin.isatty()):
            task = intake.wizard()
            if task is None:
                return None
        else:
            return None
    except ValueError as exc:
        lc.die(lc.EXIT_CONFIG, f"任务参数有误：{exc}")

    # 命令行覆盖项
    if args.years is not None:
        years_min = datetime.now().year - int(args.years) + 1
        task["screening"]["years_min"] = years_min
        task["screening"]["years_max"] = datetime.now().year
        task["search"]["years_min"] = years_min
        task["search"]["years_max"] = datetime.now().year
        task["search"]["years"] = int(args.years)
    if args.limit is not None:
        task["search"]["limit"] = max(1, min(int(args.limit), 50))
    if args.min_score is not None:
        task["screening"]["min_score"] = max(0, min(int(args.min_score), 100))
    if args.max_items is not None:
        task["screening"]["max_items"] = max(1, int(args.max_items))
    if args.no_download:
        task["search"]["download"] = False
    return _apply_platform_args(task, args)


def _print_need_task() -> None:
    import json

    print("\n[需要用户先设定检索任务] 还缺「关键词」或「筛查方向」，不能直接开跑。")
    print("筛查方向是逐篇判定纳入/排除的硬规则，不能省略；只给关键词不会自动降级。")
    print("请让用户确认：关键词、文献搜索提示词、筛查方向、文献时间、期望相关度、")
    print("检索平台（按学校公布的数据库清单）、是否第一次登录，然后二选一：")
    print("  A. 终端里跑向导：python scripts\\intake.py")
    print("  B. 带参数开跑：  python scripts\\run_all.py --keywords \"A,B\" --direction \"...\" "
          "--platforms \"pubmed,cnki\" --first-login ready --confirm-vpn --confirm-account "
          "--confirm-platform-login")
    print("\n问题清单（可用选择框逐条问用户）：")
    print(json.dumps(intake.QUESTIONNAIRE, ensure_ascii=False, indent=2))


def main() -> int:
    lc.setup_console()
    parser = argparse.ArgumentParser(description="文献检索：采集 → 确认 → 检索 → 筛选 → 下载 → 清单")
    parser.add_argument("--task", help="检索任务.json")
    parser.add_argument("--interactive", action="store_true", help="启动终端向导")
    parser.add_argument("--keywords", help="中文关键词，逗号分隔")
    parser.add_argument("--vpn-channel", choices=["easyconnect", "other_client", "webvpn",
                                                  "not_connected", "unknown"],
                        help="VPN 连接方式（程序不代连，只按它验证）")
    parser.add_argument("--keywords-en", help="英文关键词，逗号分隔")
    parser.add_argument("--prompt", help="文献搜索提示词")
    parser.add_argument("--direction", help="筛查方向")
    parser.add_argument("--include", help="纳入要点")
    parser.add_argument("--exclude", help="排除要点")
    parser.add_argument("--must", help="必含条件")
    parser.add_argument("--prefer", help="加分项")
    parser.add_argument("--doc-exclude", help="排除文献类型")
    parser.add_argument("--relevance", choices=list(intake.RELEVANCE), help="期望相关度")
    parser.add_argument("--min-score", type=int, help="纳入最低分")
    parser.add_argument("--max-items", type=int, help="纳入上限")
    parser.add_argument("--years-min", type=int, help="起始年份")
    parser.add_argument("--years-max", type=int, help="截止年份")
    parser.add_argument("--keyword", help="旧参数：关键词")
    parser.add_argument("--text", help="同 --keyword")
    parser.add_argument("--list", help="已知文献清单文件")
    parser.add_argument("--years", type=int, help="近 N 年")
    parser.add_argument("--limit", type=int, help="每库条数上限")
    parser.add_argument("--databases", help="旧参数：pubmed,cnki")
    parser.add_argument("--platforms", help="平台 id，逗号分隔（platforms.py --list 可看）")
    parser.add_argument("--platform-url", action="append", help="补充网址（可重复；未登记自动当自定义平台）")
    parser.add_argument("--school-list", help="学校公布的数据库清单（txt/html 路径或网址）")
    parser.add_argument("--first-login", choices=["ready", "first_time", "unknown"],
                        help="用户是否已登录过这些平台并保存了机构登录设置")
    parser.add_argument("--login-pending", help="需要首次登录的平台，逗号分隔")
    parser.add_argument("--confirm-platform-login", action="store_true",
                        help="用户已确认所选平台都登录过、机构登录设置已保存")
    parser.add_argument("--wait-login", type=int, default=0,
                        help="首次登录时等人工登录的秒数（0=不等待，提示用户先登录）")
    parser.add_argument("--no-probe", action="store_true", help="跳过平台能力探测")
    parser.add_argument("--refresh-probe", action="store_true", help="忽略平台探测/登录缓存")
    parser.add_argument("--no-download", action="store_true", help="不下载全文")
    parser.add_argument("--no-screen", action="store_true", help="跳过筛选")
    parser.add_argument("--allow-keyword-only", action="store_true",
                        help="允许没有筛查方向时只按关键词匹配（调试用）")
    parser.add_argument("--judgements", help="复核结论 JSON")
    parser.add_argument("--download-pending", action="store_true", help="待定文献也下载")
    parser.add_argument("--refresh", action="store_true", help="忽略已有记录重新检索")
    parser.add_argument("--yes", action="store_true", help="跳过检索式确认")
    parser.add_argument("--confirm-vpn", action="store_true", help="已确认 VPN 已连接")
    parser.add_argument("--confirm-account", action="store_true", help="已确认学校账号已登录")
    parser.add_argument("--skip-institution", action="store_true", help="跳过机构身份实测")
    parser.add_argument("--skip-vpn", action="store_true", help="跳过检索前确认（仅调试）")
    parser.add_argument("--skip-pubmed", action="store_true")
    parser.add_argument("--skip-cnki", action="store_true")
    parser.add_argument("--config", help="配置文件")
    args = parser.parse_args()
    lc.print_env()
    # 开跑先查依赖：缺包时给安装命令，别等检索到一半才炸
    lc.require_deps("requests", "openpyxl")

    entries = _read_list(Path(args.list)) if args.list else []
    task = _build_task(args, entries)
    if task is None:
        _print_need_task()
        return lc.EXIT_NEED_USER

    keyword = "_".join(task["keywords"][:2]) or (Path(args.list).stem if args.list else "检索")
    cfg = lc.load_config(args.config)
    run_dir = lc.make_run_dir(cfg, keyword)
    log = lc.RunLog(run_dir / "日志" / "run.log")
    lc.write_json(run_dir / "检索任务.json", task)
    intake.print_task(task)
    log(f"[任务] 关键词={'、'.join(task['keywords'])} 方向={task['screening']['direction'][:40]!r} "
        f"年限={task['screening']['years_min']}-{task['screening']['years_max']} "
        f"相关度={task['screening']['relevance']} 纳入线={task['screening']['min_score']} "
        f"平台={'、'.join(task['search'].get('platforms') or [])} "
        f"登录准备={(task['search'].get('login_setup') or {}).get('status')} "
        f"下载={task['search']['download']} 清单模式={bool(entries)}")

    try:
        selectors = bu.load_selectors(None, log)
        worst = lc.EXIT_OK

        # Step 1 检索式
        if entries:
            print(f"\n[清单模式] 共 {len(entries)} 条，逐条精确检索 PubMed。")
            plan = {"pubmed": "per-item", "cnki_plain": "", "groups": [], "need_manual": []}
        else:
            groups = intake.groups_from_task(task)
            plan = plan_query.build(" ".join(task["keywords"]), groups)
            plan_query.print_plan(plan)
            if plan["need_manual"]:
                log(f"[检索式] 缺英文概念：{plan['need_manual']}")
                print("\n[阻塞] 以下概念缺英文：" + "、".join(plan["need_manual"]))
                print("  请在任务里补 --keywords-en（与中文一一对应），或改关键词后重跑。")
                return lc.EXIT_NEED_USER
            if not args.yes:
                if not plan_query.confirm():
                    log("[检索式] 用户取消或无法交互")
                    return lc.EXIT_NEED_USER
            log(f"[检索式] PubMed: {plan['pubmed']} | CNKI: {plan['cnki_plain']}")

        platform_ids = task["search"].get("platforms") or task["search"].get("databases") or []
        platforms_map = _platforms_map(task)
        cnki_on = (
            "cnki" in platform_ids
            and bool(cfg["databases"]["cnki"].get("enabled"))
            and not args.skip_cnki
        )
        pubmed_on = (
            "pubmed" in platform_ids
            and bool(cfg["databases"]["pubmed"].get("enabled"))
            and not args.skip_pubmed
        )
        other_ids = [pid for pid in platform_ids if pid not in ("pubmed", "cnki") and pid in platforms_map]
        if not cnki_on:
            log("[配置] 本次不查知网")
        if not pubmed_on:
            log("[配置] 本次不查 PubMed")
        if other_ids:
            log(f"[配置] 其他平台：{'、'.join(other_ids)}")

        if not platform_ids:
            log("[配置] 任务里没有检索平台，已终止")
            print("\n[配置错误] 任务里没有检索平台（search.platforms 为空）。")
            return lc.EXIT_CONFIG
        if not pubmed_on and not cnki_on and not other_ids:
            log("[配置] 没有可检索的平台（任务选择与 --skip-* 冲突），已终止")
            print("\n[配置错误] 本次没有任何可检索平台，请检查 --platforms 与 --skip-* 参数。")
            return lc.EXIT_CONFIG

        # 词表外的主题会拼出空检索式：宁可不跑，也不要给用户一份看着正常的"0 篇"
        if not entries:
            empty = []
            if pubmed_on and not (plan.get("pubmed") or "").strip():
                empty.append("PubMed")
            if cnki_on and not (plan.get("cnki_plain") or "").strip():
                empty.append("知网")
            if empty:
                log(f"[检索式] 空检索式：{'、'.join(empty)}（内置词表未覆盖该主题）")
                print(f"\n[阻塞] 检索式为空，没有真正查询任何库：{'、'.join(empty)}")
                print("  原因：内置中英词表只覆盖常见环境/生物医学概念，你的主题不在其中。")
                print("  处理：用 --keywords-en 给出英文关键词（与中文一一对应），再重跑。")
                print('  例：--keywords "阿尔茨海默病,认知功能" '
                      '--keywords-en "alzheimer disease,cognitive function"')
                return lc.EXIT_NEED_USER

        # Step 2 VPN + 学校账号确认（硬门槛）
        pc_info: dict = {}
        if not args.skip_vpn:
            pc_code, pc_info = precheck.run(
                cfg, run_dir, plan.get("cnki_plain") or keyword, log, selectors,
                assume_confirmed=args.confirm_vpn and args.confirm_account,
                skip_institution=args.skip_institution or not cnki_on,
            )
            if pc_code != lc.EXIT_OK:
                log("[预检] 未通过，终止（不检索、不下载、不生成清单）")
                return lc.EXIT_NEED_USER
        else:
            log("[预检] 已按 --skip-vpn 跳过（正式流程不应使用；账号未确认）")

        # Step 2.5 平台首次登录/机构登录设置（规则 7）
        login_setup = task["search"].get("login_setup") or {}
        pc_account = bool((pc_info.get("user_confirm") or {}).get("account"))
        login_code, _ = platform_login.ensure(
            cfg, platform_ids, run_dir, log, platforms_map,
            status=login_setup.get("status") or "unknown",
            pending=login_setup.get("pending") or [],
            assume_ready=args.confirm_platform_login,
            assume_ip_ready=pc_account,
            wait=args.wait_login,
            refresh=args.refresh_probe,
        )
        if login_code != lc.EXIT_OK:
            log("[登录准备] 有平台未确认登录/机构设置，流程暂停（不检索、不下载）")
            return lc.EXIT_NEED_USER

        # Step 2.6 平台能力探测（能不能检索、能不能下载）
        if other_ids and not args.no_probe and task["search"].get("probe", True):
            caps = probe_platform.ensure(
                cfg, other_ids, run_dir, log,
                plan.get("cnki_plain") or keyword, platforms_map,
                refresh=args.refresh_probe, wait_login=args.wait_login,
            )
            for pid in other_ids:
                cap = caps.get(pid) or {}
                log(f"[探测] {cap.get('name') or pid} → {cap.get('verdict_label') or '未探测'}")
                if cap.get("verdict") == "login_required":
                    log(f"[探测] {cap.get('name') or pid} 仍有登录墙，检索阶段会再次请求账号")
        else:
            log("[探测] 已跳过平台能力探测")

        limit = int(task["search"]["limit"])
        years = int(task["search"]["years"])

        # Step 3 PubMed 检索
        if pubmed_on:
            cached = _load_or_search(cfg, "records_pubmed.json", run_dir, args.refresh)
            if cached is not None and not entries:
                log(f"[PubMed] 断点续跑，复用 {len(cached)} 条")
            elif entries:
                prior = lc.read_json(run_dir / "records_pubmed.json", default=[]) or []
                pub_code, cached = _search_list(cfg, entries, limit, log)
                _merge_prior(cached, prior)
                worst = max(worst, pub_code)
                lc.write_json(run_dir / "records_pubmed.json", cached)
            else:
                prior = lc.read_json(run_dir / "records_pubmed.json", default=[]) or []
                pub_code, cached = pubmed_search.run(cfg, plan["pubmed"], limit, years, run_dir, log)
                _merge_prior(cached, prior)
                lc.write_json(run_dir / "records_pubmed.json", cached)
                worst = max(worst, pub_code)
        elif not entries:
            log("[PubMed] 已跳过，写入空记录占位")
            lc.write_json(run_dir / "records_pubmed.json", [])

        # Step 4 知网检索
        if cnki_on and not entries:
            cached_cnki = _load_or_search(cfg, "records_cnki.json", run_dir, args.refresh)
            if cached_cnki is not None:
                log(f"[知网] 断点续跑，复用 {len(cached_cnki)} 条")
            else:
                prior_cnki = lc.read_json(run_dir / "records_cnki.json", default=[]) or []
                cnki_code, cnki_records = cnki_search.search_cnki(
                    cfg, plan.get("cnki_plain") or keyword, limit, run_dir, log, selectors
                )
                _merge_prior(cnki_records, prior_cnki)
                worst = max(worst, cnki_code)
                lc.write_json(run_dir / "records_cnki.json", cnki_records)
                if cnki_code == lc.EXIT_NEED_USER:
                    log("[知网] 需要人工处理（登录/滑块），继续用已有记录出表；"
                        "处理完可用原命令续跑")

        # Step 4.5 其他平台检索（SinoMed / ScienceDirect / CAS / 自定义网址）
        platform_pending = []
        if other_ids and not entries:
            for pid in other_ids:
                platform = platforms_map.get(pid) or {}
                name = platform.get("short") or pid
                cached_platform = _load_or_search(
                    cfg, f"records_{lc.sanitize_filename(pid, 40)}.json", run_dir, args.refresh
                )
                if cached_platform is not None:
                    log(f"[平台] {name} 断点续跑，复用 {len(cached_platform)} 条")
                    continue
                query_zh = plan.get("cnki_plain") or keyword
                query_en = " ".join(x for x in task.get("keywords_en") or [] if x) or query_zh
                query = query_en if platform.get("lang") == "en" else query_zh
                p_code, p_records, p_info = platform_search.run_platform(
                    cfg, platform, query, int(task["search"]["limit"]), run_dir, log,
                    cnki_selectors=selectors, wait_login=args.wait_login, detail=True,
                )
                worst = max(worst, p_code)
                if p_code == lc.EXIT_NEED_USER:
                    platform_pending.append(p_info)
                    log(f"[平台] {name} 需要账号/登录，已跳过（结果里会列为待处理）")
                elif p_code == lc.EXIT_PARTIAL:
                    log(f"[平台] {name} 部分失败：{p_info.get('reason')}")
                else:
                    log(f"[平台] {name} 完成 {len(p_records)} 条")
            # 本轮只要检索过其他平台就覆盖写（哪怕是空列表），避免上一轮的残留误导用户
            lc.write_json(run_dir / "平台待处理.json", platform_pending)

        # Step 4.6 跨库去重（PubMed 与知网之间同样要去重，不只"其他平台"）
        filled = [p for p in run_dir.glob("records_*.json") if lc.read_json(p, default=[])]
        if len(filled) > 1:
            _dedupe_records(run_dir, log)
        else:
            log("[去重] 只有单一来源，跳过跨库去重")

        # Step 5 按方向筛选
        stats: dict = {}
        if not args.no_screen:
            judgements = []
            if args.judgements:
                judgements = lc.read_json(args.judgements, default=[]) or []
                if isinstance(judgements, dict):
                    judgements = judgements.get("records") or judgements.get("items") or []
            scr_code, _ = screen.run(run_dir, task, log, judgements)
            worst = max(worst, scr_code)
            stats = screen._stats(lc.read_records(run_dir))
        else:
            log("[筛选] 已按 --no-screen 跳过（清单将没有判定与选择依据）")

        # Step 6 下载（默认只下纳入）
        if task["search"]["download"] and not args.no_download:
            dl_cfg = cfg["download"]
            try:
                dl_code, records = download_fulltext.run(
                    cfg, run_dir, log, None,
                    do_pubmed=pubmed_on, do_cnki=cnki_on and not entries,
                    do_platforms=bool(other_ids) and not entries,
                    selectors=selectors,
                    selected_only=bool(stats) and bool(dl_cfg.get("selected_only", True)),
                    include_pending=args.download_pending or bool(dl_cfg.get("include_pending", False)),
                    platforms_map=platforms_map,
                )
            except SystemExit as exc:
                # 依赖缺失等致命退出在这里兜住：检索与筛选的成果不能丢
                log(f"[下载] 下载阶段提前中止（退出码 {getattr(exc, 'code', 1)}），继续出表")
                dl_code, records = lc.EXIT_PARTIAL, lc.read_records(run_dir)
            worst = max(worst, dl_code)
            if dl_code == lc.EXIT_NEED_USER:
                log("[下载] 有平台需要人工处理，已先出表；处理完可单跑 download_fulltext.py 补下")
        else:
            log("[下载] 本次不下载全文")

        # Step 7 Excel + 结构化清单
        records = lc.read_records(run_dir)
        excel_code, excel, rows = make_excel.build(run_dir)
        worst = max(worst, excel_code)
        log(f"[Excel] {excel} 行数={rows}")
        rep_code, md, stats = make_report.build(run_dir, task)
        worst = max(worst, rep_code)
        log(f"[清单] {md} 纳入={stats.get('纳入', 0)} 待定={stats.get('待定', 0)} 排除={stats.get('排除', 0)}")

        # Step 8 汇报
        _report(records, run_dir, excel, md, stats, log)
        return worst
    except Exception as exc:  # noqa: BLE001 —— 未预期异常必须带上下文落盘
        import traceback

        log(f"[异常] {type(exc).__name__}: {exc}")
        log.raw(traceback.format_exc())
        return lc.EXIT_PARTIAL
    finally:
        log.close()


if __name__ == "__main__":
    sys.exit(main())
