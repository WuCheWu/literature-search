# -*- coding: utf-8 -*-
"""make_report.py —— 生成结构化文献列表 文献列表.md

用途：把筛选结果按「纳入 / 待定 / 排除」分区输出，每篇都带原文标题、原文摘要、选择依据，
      并说明与关键词、筛查方向的匹配关系。
参数：
    --run-dir DIR    含 records_*.json / 筛选结果.json 的目录
    --task FILE      检索任务.json（缺省读 <run-dir>/检索任务.json）
    --output FILE    输出 md 路径（默认 <run-dir>/文献列表.md）
    --json           打印统计
退出码：0 成功 / 1 无记录 / 3 配置错误
依赖：标准库
说明：摘要、标题一律保留原文，不改写、不压缩；排序与 Excel 一致（判定 → 匹配分）。
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import litcommon as lc  # noqa: E402
import platforms as pf  # noqa: E402

DECISION_ORDER = {"纳入": 0, "待定": 1, "排除": 2}


def _load_task(run_dir: Path, path: str | None) -> dict:
    target = Path(path) if path else run_dir / "检索任务.json"
    if target.exists():
        try:
            return json.loads(lc.read_text_auto(target))
        except json.JSONDecodeError:
            return {}
    data = lc.read_json(run_dir / "筛选结果.json", default={}) or {}
    return data.get("task") or {}


def _precheck(run_dir: Path) -> dict:
    return lc.read_json(run_dir / "预检确认.json", default={}) or {}


def _records(run_dir: Path) -> list[dict]:
    data = lc.read_json(run_dir / "筛选结果.json", default={}) or {}
    records = data.get("records") or []
    if records:
        return records
    return lc.read_records(run_dir)


def _year_volume(rec: dict) -> str:
    bits = []
    if rec.get("年份"):
        bits.append(str(rec["年份"]))
    tail = ""
    if rec.get("卷"):
        tail += str(rec["卷"])
    if rec.get("期"):
        tail += f"({rec['期']})"
    if tail:
        bits.append(tail)
    text = ", ".join(bits)
    if rec.get("页码"):
        text += f": {rec['页码']}"
    return text


def _block(index: int, rec: dict) -> list[str]:
    title = (rec.get("标题") or "（无标题）").strip()
    decision = rec.get("判定") or "未筛选"
    score = rec.get("匹配分")
    headline = f"### {index}. 【{decision}{f' {score} 分' if score is not None else ''}】{title}"
    lines = [headline, ""]
    lines.append(f"- **原文标题**：{title}")
    if rec.get("作者"):
        lines.append(f"- **作者**：{rec['作者']}")
    source = rec.get("期刊/来源") or ""
    detail = _year_volume(rec)
    if source or detail:
        lines.append(f"- **期刊/来源**：{source}{'，' if source and detail else ''}{detail}")
    ids = []
    if rec.get("DOI"):
        ids.append(f"DOI: {rec['DOI']}")
    if rec.get("PMID"):
        ids.append(f"PMID: {rec['PMID']}")
    if rec.get("来源库"):
        ids.append(f"来源库: {rec['来源库']}")
    if ids:
        lines.append("- **标识**：" + " ｜ ".join(ids))
    if rec.get("原文链接"):
        lines.append(f"- **原文链接**：{rec['原文链接']}")
    status = rec.get("全文状态") or "未下载"
    local = rec.get("本地文件") or ""
    lines.append(f"- **全文**：{status}{f' → `{local}`' if local else ''}")

    abstract = (rec.get("摘要") or "").strip()
    lines.append("- **原文摘要**：")
    lines.append("")
    if abstract:
        for para in abstract.splitlines():
            if para.strip():
                lines.append(f"  > {para.strip()}")
    else:
        lines.append("  > （该条未取到摘要：数据库页面无摘要或详情页抓取失败，未编造）")
    lines.append("")

    if rec.get("关键词匹配"):
        lines.append(f"- **关键词匹配**：{rec['关键词匹配']}")
    else:
        lines.append("- **关键词匹配**：（标题/关键词/摘要均未出现检索词）")
    if rec.get("方向匹配"):
        lines.append(f"- **筛查方向匹配**：{rec['方向匹配']}")
    elif rec.get("判定") == "排除":
        lines.append("- **筛查方向匹配**：未命中方向要点")
    lines.append(f"- **选择依据**：{rec.get('选择依据') or '（无）'}")
    if rec.get("备注"):
        lines.append(f"- **备注**：{rec['备注']}")
    lines.append("")
    return lines


METHOD_LABELS = {
    "not-needed": "无需登录",
    "user-confirmed": "用户确认已登录",
    "already-logged-in": "已有登录状态",
    "no-login-wall": "无需登录",
    "manual": "手动登录完成",
    "stored-credentials": "保存账号登录",
    "login-required": "需要登录",
}
RELEVANCE_LABELS = {"loose": "宽松", "standard": "标准", "strict": "严格"}


def _platform_status(run_dir: Path, task: dict) -> tuple[list[dict], list[dict]]:
    caps = lc.read_json(run_dir / "平台探测.json", default=[]) or []
    logins = lc.read_json(run_dir / "登录准备.json", default=[]) or []
    pending = lc.read_json(run_dir / "平台待处理.json", default=[]) or []
    if isinstance(caps, dict):
        caps = caps.get("platforms") or []
    if isinstance(logins, dict):
        logins = logins.get("platforms") or []
    cap_map = {item.get("platform"): item for item in caps if item.get("platform")}
    login_map = {item.get("platform"): item for item in logins if item.get("platform")}
    pending_map = {item.get("platform"): item for item in pending if item.get("platform")}

    search = (task or {}).get("search") or {}
    customs = {item["id"]: item for item in search.get("custom_platforms") or [] if item.get("id")}
    rows = []
    for pid in search.get("platforms") or search.get("databases") or []:
        platform = customs.get(pid) or pf.get(pid) or {"id": pid, "name": pid, "short": pid}
        cap = cap_map.get(pid) or {}
        login = login_map.get(pid) or {}
        wait = pending_map.get(pid) or {}
        if pid == "pubmed":
            capability = "E-utilities 专用适配器（无需登录）"
        elif pid == "cnki":
            capability = cap.get("verdict_label") or "专用适配器（校园 IP）"
        else:
            capability = cap.get("verdict_label") or "未探测"
        if login.get("ready"):
            method = METHOD_LABELS.get(login.get("method"), login.get("method") or "")
            login_text = "就绪" + (f"（{method}）" if method else "")
        elif login:
            login_text = "未就绪"
        else:
            login_text = "—"
        note = cap.get("note") or wait.get("reason") or ""
        rows.append({
            "id": pid,
            "name": platform.get("short") or platform.get("name") or pid,
            "login": login_text,
            "capability": capability,
            "note": note,
        })
    todos = [
        {"name": item.get("name") or item.get("platform"), "reason": item.get("reason") or ""}
        for item in pending
    ]
    return rows, todos


def build(run_dir: Path, task: dict | None = None, output: Path | None = None) -> tuple[int, Path, dict]:
    records = _records(run_dir)
    target = Path(output) if output else run_dir / "文献列表.md"
    task = task if task is not None else _load_task(run_dir, None)
    precheck = _precheck(run_dir)

    stats = {"合计": len(records), "纳入": 0, "待定": 0, "排除": 0}
    for rec in records:
        decision = rec.get("判定") or "未筛选"
        stats[decision] = stats.get(decision, 0) + 1

    scr = (task or {}).get("screening") or {}
    search = (task or {}).get("search") or {}
    institution = (precheck.get("institution") or {})
    vpn = (precheck.get("vpn") or {})

    lines: list[str] = []
    lines.append("# 文献检索结果清单")
    lines.append("")
    lines.append(
        "> 本清单严格按任务文件中的「关键词 + 筛查方向」生成：纳入区是可以直接用的文献，"
        "待定区需要人工再确认，排除区同样保留原文标题、摘要与判定依据，便于逐条核对。"
    )
    lines.append("")

    # ---- 任务信息
    lines.append("## 一、任务与确认信息")
    lines.append("")
    lines.append("| 项目 | 内容 |")
    lines.append("| --- | --- |")
    lines.append(f"| 生成时间 | {datetime.now():%Y-%m-%d %H:%M:%S} |")
    lines.append(f"| 关键词 | {'、'.join(task.get('keywords') or []) or '（未记录）'} |")
    if any(task.get("keywords_en") or []):
        lines.append(f"| 英文关键词 | {'、'.join(x or '（空）' for x in task['keywords_en'])} |")
    lines.append(f"| 文献搜索提示词 | {task.get('search_prompt') or '（未填）'} |")
    lines.append(f"| 筛查方向 | {scr.get('direction') or '（未记录）'} |")
    lines.append(f"| 纳入要点 | {'、'.join(scr.get('include') or []) or '（无）'} |")
    lines.append(f"| 排除要点 | {'、'.join(scr.get('exclude') or []) or '（无）'} |")
    lines.append(f"| 必含条件 | {'、'.join(scr.get('must_have') or []) or '（无）'} |")
    lines.append(
        f"| 文献时间 | {scr.get('years_min') or '?'}–{scr.get('years_max') or '?'} 年 |"
    )
    lines.append(
        f"| 期望相关度 | {RELEVANCE_LABELS.get(scr.get('relevance'), scr.get('relevance') or '标准')}"
        f"（纳入线 {scr.get('min_score')} 分，待定下界 {int(scr.get('min_score') or 55) - int(scr.get('band') or 15)} 分） |"
    )
    platform_rows, todos = _platform_status(run_dir, task)
    platform_names = "、".join(row["name"] for row in platform_rows) or "（未记录）"
    lines.append(f"| 检索平台与条数 | {platform_names}，每库上限 {search.get('limit') or '?'} 条 |")
    if vpn.get("ok") is not None:
        lines.append(
            f"| VPN 确认 | {'通过' if vpn.get('ok') else '未通过'}（{precheck.get('checked_at') or '—'}） |"
        )
    if institution.get("ok"):
        lines.append(f"| 机构身份 | {institution.get('institution') or '—'}（结果页已核对） |")
    elif institution.get("skipped"):
        lines.append("| 机构身份 | 本次未核对（按参数跳过） |")
    lines.append("")

    # ---- 平台与权限
    if platform_rows:
        lines.append("## 二、检索平台与权限")
        lines.append("")
        lines.append(
            "| 平台 | 登录/机构设置 | 能力探测 | 说明 |"
        )
        lines.append("| --- | --- | --- | --- |")
        for row in platform_rows:
            lines.append(
                f"| {row['name']} | {row['login']} | {row['capability']} | {row['note'] or '—'} |"
            )
        lines.append("")
        if todos:
            lines.append("**待处理平台**（本次未检索或只拿到题录）：")
            lines.append("")
            for item in todos:
                lines.append(f"- {item['name']}：{item['reason'] or '需要登录'}")
            lines.append("")

    # ---- 统计
    lines.append("## 三、筛选统计")
    lines.append("")
    lines.append("| 判定 | 篇数 | 含义 |")
    lines.append("| --- | --- | --- |")
    lines.append(f"| 纳入 | {stats.get('纳入', 0)} | 同时满足关键词与筛查方向，可直接使用 |")
    lines.append(f"| 待定 | {stats.get('待定', 0)} | 部分匹配或信息不足，需人工确认 |")
    lines.append(f"| 排除 | {stats.get('排除', 0)} | 命中排除项、缺必含条件或相关度不足 |")
    lines.append(f"| 合计 | {stats.get('合计', 0)} | 本次检索入库的候选文献 |")
    lines.append("")

    grouped = {"纳入": [], "待定": [], "排除": []}
    for rec in records:
        grouped.setdefault(rec.get("判定") or "未筛选", []).append(rec)
    for key in grouped:
        grouped[key].sort(key=lambda r: -int(r.get("匹配分") or 0))

    sections = [
        ("四、纳入文献", "纳入", "按匹配分降序；这些文献的原文摘要与选择依据均逐条列出。"),
        ("五、待定文献", "待定", "相关度接近阈值或摘要缺失，建议人工看一眼再决定。"),
        ("六、排除文献", "排除", "每篇都写明为什么排除，若你不同意可直接据此复核。"),
    ]
    if grouped.get("未筛选"):
        sections.append(("七、未筛选文献", "未筛选", "本次未执行筛选（清单里没有判定与匹配分）。"))
    counter = 0
    for heading, decision, note in sections:
        items = grouped.get(decision) or []
        lines.append(f"## {heading}（{len(items)} 篇）")
        lines.append("")
        lines.append(note)
        lines.append("")
        if not items:
            lines.append("_（无）_")
            lines.append("")
            continue
        for rec in items:
            counter += 1
            lines.extend(_block(counter, rec))

    # ---- 产物
    lines.append("## 附：产物位置")
    lines.append("")
    lines.append(f"- 结构化清单：`{target}`")
    lines.append(f"- 表格清单：`{run_dir / '文献清单.xlsx'}`")
    lines.append(f"- 筛选明细：`{run_dir / '筛选结果.json'}`")
    lines.append(f"- 任务文件：`{run_dir / '检索任务.json'}`")
    lines.append(f"- 预检记录：`{run_dir / '预检确认.json'}`")
    if (run_dir / "平台探测.json").exists():
        lines.append(f"- 平台能力探测：`{run_dir / '平台探测.json'}`")
    if (run_dir / "登录准备.json").exists():
        lines.append(f"- 平台登录准备：`{run_dir / '登录准备.json'}`")
    lines.append(f"- 全文目录：`{run_dir / '全文'}`")
    lines.append(f"- 运行日志：`{run_dir / '日志' / 'run.log'}`")
    lines.append("")

    try:
        target.write_text("\n".join(lines), encoding="utf-8")
    except OSError as exc:
        print(f"[清单] 写入失败：{type(exc).__name__}: {exc}", file=sys.stderr)
        return lc.EXIT_PARTIAL, target, stats
    return lc.EXIT_OK, target, stats


def main() -> int:
    lc.setup_console()
    parser = argparse.ArgumentParser(description="生成结构化文献列表（原文标题/摘要/选择依据）")
    parser.add_argument("--run-dir", required=True)
    parser.add_argument("--task", help="检索任务.json")
    parser.add_argument("--output", help="输出 md 路径")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    lc.print_env()
    run_dir = Path(args.run_dir)
    if not run_dir.exists():
        lc.die(lc.EXIT_CONFIG, f"目录不存在：{run_dir}")
    code, target, stats = build(run_dir, _load_task(run_dir, args.task), Path(args.output) if args.output else None)
    print(f"\n文献列表：{target}")
    extra = f" / 未筛选 {stats['未筛选']}" if stats.get("未筛选") else ""
    print(f"  纳入 {stats.get('纳入', 0)} / 待定 {stats.get('待定', 0)} / 排除 {stats.get('排除', 0)}"
          f"{extra}（合计 {stats.get('合计', 0)}）")
    if args.json:
        print(json.dumps(stats, ensure_ascii=False))
    return code


if __name__ == "__main__":
    sys.exit(main())
