# -*- coding: utf-8 -*-
"""screen.py —— 按用户给的筛查方向逐篇判定（纳入 / 待定 / 排除）

用途：读 records_*.json + 检索任务.json，为每篇算出匹配分与选择依据，回写记录并生成 筛选结果.json。
判定规则（全部来自任务文件，脚本不自行放宽）：
    · 必含条件缺失             → 排除
    · 排除要点出现在标题/关键词 → 排除；只出现在摘要 → 扣分
    · 排除文献类型（综述/会议…）→ 排除
    · 年份早于下限             → 排除（晚于上限只标注，多为网络首发）
    · 匹配分 ≥ min_score       → 纳入
    · min_score-band ≤ 分 < min_score → 待定
    · 低于 min_score-band      → 排除
参数：
    --run-dir DIR       含 records_*.json 的目录
    --task FILE         检索任务.json（缺省读 <run-dir>/检索任务.json）
    --judgements FILE   人工/宿主复核结果（对「待定」逐条给结论）
    --json              打印统计
退出码：0 成功 / 1 无记录 / 3 配置或任务错误
依赖：标准库 + plan_query 的领域词表（中英互译）
说明：中文关键词/要点会自动扩展英文同义词（斑马鱼 ↔ zebrafish），否则英文文献会被漏判。
      「选择依据」必须能复述出命中了什么、为什么收/不收。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import unicodedata
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import intake  # noqa: E402
import litcommon as lc  # noqa: E402
import plan_query  # noqa: E402

FIELD_ORDER = ("标题", "关键词", "摘要")
LOC_WEIGHT = {"标题": 1.0, "关键词": 0.85, "摘要": 0.6}
ASCII_TERM = re.compile(r"^[a-z0-9][a-z0-9\-\s]*$")
YEAR_RE = re.compile(r"(19|20)\d{2}")
DECISION_ORDER = {"纳入": 0, "待定": 1, "排除": 2}

# 筛选常用词的中英对照（词表里没有的补充在这里）
EXTRA_VOCAB = {
    "综述": ["review", "systematic review", "meta-analysis", "research progress"],
    "研究进展": ["research progress", "advances"],
    "述评": ["commentary", "editorial"],
    "会议": ["conference", "proceedings", "symposium"],
    "征稿": ["call for papers"],
    "稿约": ["instructions for authors"],
    "吸附": ["adsorption", "adsorb", "adsorbent"],
    "降解": ["degradation", "degrade", "biodegradation"],
    "生态风险": ["ecological risk", "risk assessment"],
    "肠道菌群": ["gut microbiota", "gut microbiome", "intestinal flora", "intestinal microbiota"],
    "消化": ["digestion", "digestive", "digestive enzyme"],
    "炎症": ["inflammation", "inflammatory"],
    "抗氧化": ["antioxidant", "antioxidative"],
    "氧化应激": ["oxidative stress"],
    "肠道屏障": ["intestinal barrier", "gut barrier"],
    "肝脏": ["liver", "hepatic"],
    "毒性": ["toxicity", "toxic"],
}


def _norm(value) -> str:
    """全角转半角 + 小写 + 折叠空白，保证中英文都能稳定比较。"""
    if value is None:
        return ""
    text = unicodedata.normalize("NFKC", str(value))
    return re.sub(r"\s+", " ", text).strip().lower()


def _clean_term(term: str) -> str:
    return str(term).strip().strip('"').strip("*").strip()


def _unique(terms: list[str]) -> list[str]:
    seen, out = set(), []
    for term in terms:
        key = _norm(_clean_term(term))
        if key and key not in seen:
            seen.add(key)
            out.append(_clean_term(term))
    return out


def _build_vocab() -> dict[str, list[str]]:
    """中英双向词表：复用 plan_query 的领域词表，再补筛选常用词。"""
    table: dict[str, list[str]] = {}

    def link(left: str, right: str) -> None:
        left, right = _clean_term(left), _clean_term(right)
        if not left or not right:
            return
        table.setdefault(_norm(left), [])
        if _norm(right) not in {_norm(x) for x in table[_norm(left)]}:
            table[_norm(left)].append(right)
        table.setdefault(_norm(right), [])
        if _norm(left) not in {_norm(x) for x in table[_norm(right)]}:
            table[_norm(right)].append(left)

    for concept in plan_query.CONCEPTS:
        for en in concept.get("en") or []:
            link(concept["cn"], en)
    for cn, ens in EXTRA_VOCAB.items():
        for en in ens:
            link(cn, en)
    return table


VOCAB = _build_vocab()


def _expand(term: str) -> list[str]:
    base = _clean_term(term)
    out = [base]
    for extra in VOCAB.get(_norm(base), []):
        if _norm(extra) not in {_norm(x) for x in out}:
            out.append(extra)
    return out


def _hit(text: str, term: str) -> bool:
    """英文按词首边界匹配（允许 -s/-es 等词尾），中文直接子串匹配。"""
    needle = _norm(_clean_term(term))
    if not needle or not text:
        return False
    if ASCII_TERM.match(needle):
        pattern = rf"(?<![a-z0-9]){re.escape(needle)}[a-z0-9]{{0,4}}(?![a-z0-9])"
        return re.search(pattern, text) is not None
    return needle in text


def _locate(term: str, fields: dict) -> list[str]:
    return [name for name in FIELD_ORDER if _hit(fields[name], term)]


def _match_group(variants: list[str], fields: dict) -> tuple[list[str], list[str]]:
    """一组同义词（斑马鱼 / zebrafish）命中情况合并。"""
    locs: list[str] = []
    used: list[str] = []
    for variant in variants:
        found = _locate(variant, fields)
        if not found:
            continue
        used.append(variant)
        for name in found:
            if name not in locs:
                locs.append(name)
    return locs, used


def _best_weight(locs: list[str]) -> float:
    return max((LOC_WEIGHT[name] for name in locs), default=0.0)


def _hits(terms: list[str], fields: dict) -> tuple[dict, dict]:
    hits, variants = {}, {}
    for term in terms:
        locs, used = _match_group(_expand(term), fields)
        if locs:
            hits[term] = locs
            variants[term] = used
    return hits, variants


def _fmt_hits(hits: dict, variants: dict | None = None) -> str:
    bits = []
    for term, locs in hits.items():
        alias = ""
        if variants:
            others = [v for v in variants.get(term) or [] if _norm(v) != _norm(term)]
            if others:
                alias = "=" + "/".join(others[:2])
        bits.append(f"{term}{alias}（{'/'.join(locs)}）")
    return "、".join(bits)


def _year_of(rec: dict) -> int | None:
    match = YEAR_RE.search(str(rec.get("年份") or ""))
    return int(match.group(0)) if match else None


def _keyword_groups(task: dict) -> list[list[str]]:
    """关键词按「概念」分组：中文 + 英文对应词，避免同一概念被算两遍。"""
    groups = []
    keywords = task.get("keywords") or []
    ens = task.get("keywords_en") or []
    for index, cn in enumerate(keywords):
        group = [cn]
        if index < len(ens) and str(ens[index]).strip():
            group += intake._split(ens[index])
        else:
            group += VOCAB.get(_norm(cn), [])
        groups.append(_unique(group))
    return [g for g in groups if g]


def screen_record(rec: dict, task: dict) -> dict:
    """返回追加到记录上的筛选字段。"""
    scr = task["screening"]
    fields = {name: _norm(rec.get(name)) for name in FIELD_ORDER}
    empty_abstract = not fields["摘要"]

    kw_groups = _keyword_groups(task)
    inc_terms = _unique(scr.get("include") or [])
    inc_terms = [t for t in inc_terms if _norm(t) not in {_norm(g[0]) for g in kw_groups}]
    exc_terms = _unique(scr.get("exclude") or [])
    must_terms = _unique(scr.get("must_have") or [])
    pref_terms = _unique(scr.get("prefer") or [])
    doc_terms = _unique(scr.get("doc_types_exclude") or [])

    # 关键词：按概念组求最佳命中位置
    kw_hits, kw_variants, kw_ratio_sum = {}, {}, 0.0
    for group in kw_groups:
        locs, used = _match_group(group, fields)
        if locs:
            kw_hits[group[0]] = locs
            kw_variants[group[0]] = used
            kw_ratio_sum += _best_weight(locs)
    kw_ratio = kw_ratio_sum / len(kw_groups) if kw_groups else 0.0

    inc_hits, inc_variants = _hits(inc_terms, fields)
    pref_hits, pref_variants = _hits(pref_terms, fields)
    exc_hits, exc_variants = _hits(exc_terms, fields)
    must_hits, must_variants = _hits(must_terms, fields)
    must_missing = [t for t in must_terms if t not in must_hits]

    hard: list[str] = []
    soft: list[str] = []
    for term, locs in exc_hits.items():
        if any(name in ("标题", "关键词") for name in locs):
            hard.append(f"排除要点「{term}」命中{'/'.join(locs)}")
        else:
            soft.append(term)
    for term in doc_terms:
        if (
            _hit(fields["标题"], term)
            or _hit(_norm(rec.get("文献类型")), term)
            or _hit(_norm(rec.get("期刊/来源")), term)
        ):
            hard.append(f"文献类型命中「{term}」")
    if must_missing:
        hard.append("缺少必含条件「" + "、".join(must_missing) + "」")

    year = _year_of(rec)
    years_min = int(scr.get("years_min") or 0)
    years_max = int(scr.get("years_max") or 0)
    if year and years_min and year < years_min:
        hard.append(f"年份 {year} 早于下限 {years_min}")
    late_year = bool(year and years_max and year > years_max)

    # ---- 打分：关键词 40 / 方向要点 35 / 加分项 15 / 时效 10，缺项自动归一
    inc_ratio = len(inc_hits) / len(inc_terms) if inc_terms else 0.0
    pref_ratio = len(pref_hits) / len(pref_terms) if pref_terms else 0.0
    if year and years_min:
        recency = 1.0 if year >= years_min else 0.6
    elif year:
        recency = 0.8
    else:
        recency = 0.5

    parts = [(40, kw_ratio)]
    if inc_terms:
        parts.append((35, inc_ratio))
    if pref_terms:
        parts.append((15, pref_ratio))
    parts.append((10, recency))
    total_weight = sum(weight for weight, _ in parts) or 1
    score = round(100 * sum(weight * ratio for weight, ratio in parts) / total_weight)
    score = max(0, min(100, score - 8 * len(soft)))

    min_score = int(scr.get("min_score") or 55)
    band = int(scr.get("band") or 15)
    if hard:
        decision = "排除"
    elif score >= min_score:
        decision = "纳入"
    elif score >= min_score - band:
        decision = "待定"
    elif empty_abstract:
        decision = "待定"
        hard.append("摘要缺失，信息不足，无法判定")
    else:
        decision = "排除"

    # ---- 选择依据：同时说清关键词与筛查方向的匹配关系
    segments: list[str] = []
    if kw_hits:
        segments.append("关键词命中：" + _fmt_hits(kw_hits, kw_variants))
    else:
        segments.append("未命中任何关键词（仅主题相近）")
    if inc_hits:
        segments.append("符合筛查方向要点：" + _fmt_hits(inc_hits, inc_variants))
    elif inc_terms:
        segments.append("未命中方向要点")
    if pref_hits:
        segments.append("加分项：" + _fmt_hits(pref_hits, pref_variants))
    if must_terms:
        segments.append(
            "必含条件「" + "、".join(must_terms) + "」"
            + ("全部满足" if not must_missing else "未满足")
            + (f"（按{'/'.join(must_variants.get(must_terms[0]) or [])}匹配）"
               if not must_missing and must_variants.get(must_terms[0]) else "")
        )
    if hard:
        segments.append("硬性排除：" + "；".join(hard))
    if soft:
        segments.append("摘要提及排除词（扣分）：" + "、".join(soft))
    if late_year:
        segments.append(f"标注年份 {year} 晚于检索上限（多为网络首发，未据此排除）")
    if empty_abstract and decision != "排除":
        segments.append("摘要缺失，信息不足")
    segments.append(f"匹配度 {score}/100")
    reason = "；".join(segments) + f" → 判定：{decision}"

    kw_text = _fmt_hits(kw_hits, kw_variants)
    direction_bits = []
    if inc_hits:
        direction_bits.append(_fmt_hits(inc_hits, inc_variants))
    if must_terms:
        direction_bits.append("必含" + ("✓" if not must_missing else "✗") + "：" + "、".join(must_terms))

    return {
        "判定": decision,
        "匹配分": score,
        "关键词匹配": kw_text,
        "方向匹配": "；".join(direction_bits),
        "选择依据": reason,
        "匹配关系": {
            "关键词": kw_hits,
            "关键词别名": kw_variants,
            "筛查方向": inc_hits,
            "方向别名": inc_variants,
            "加分项": pref_hits,
            "必含条件": {"要求": must_terms, "缺失": must_missing, "别名": must_variants},
            "排除项": exc_hits,
            "硬性排除": hard,
            "得分": score,
            "判定": decision,
            "阈值": {"纳入": min_score, "待定下界": min_score - band},
        },
    }


def _key(rec: dict) -> str:
    doi = _norm(rec.get("DOI"))
    if doi:
        return "doi:" + doi
    pmid = _norm(rec.get("PMID"))
    if pmid:
        return "pmid:" + pmid
    return "title:" + _norm(rec.get("标题"))


def apply_judgements(records: list[dict], items: list[dict], log=None) -> int:
    """把人工/宿主复核结论并进记录；返回命中条数。"""
    index = {_key(rec): rec for rec in records}
    applied = 0
    for item in items or []:
        key = ""
        if item.get("doi"):
            key = "doi:" + _norm(item["doi"])
        elif item.get("pmid"):
            key = "pmid:" + _norm(item["pmid"])
        elif item.get("key"):
            key = str(item["key"]).strip()
            if not key.startswith(("doi:", "pmid:", "title:")):
                key = "title:" + _norm(key)
        elif item.get("标题"):
            key = "title:" + _norm(item["标题"])
        rec = index.get(key)
        if rec is None:
            if log:
                log(f"[筛选] 复核条目未匹配到记录：{item}")
            continue
        decision = str(item.get("判定") or "").strip()
        if decision in DECISION_ORDER:
            rec["判定"] = decision
        if item.get("分") is not None:
            try:
                rec["匹配分"] = int(item["分"])
            except (TypeError, ValueError):
                pass
        reason = str(item.get("理由") or "").strip()
        if reason:
            rec["选择依据"] = f"{rec.get('选择依据') or ''}；【复核】{reason}"
            rec.setdefault("匹配关系", {})["复核"] = {"判定": decision, "理由": reason}
        rec["复核"] = True
        applied += 1
    return applied


def _cap(records: list[dict], max_items: int) -> None:
    included = [r for r in records if r.get("判定") == "纳入"]
    for rec in included[max_items:]:
        rec["判定"] = "待定"
        rec["选择依据"] = (
            f"{rec.get('选择依据') or ''}；超出纳入上限 {max_items} 篇，转为待定"
        )
        rec.setdefault("匹配关系", {})["判定"] = "待定"


def _stats(records: list[dict]) -> dict:
    stats = {"合计": len(records), "纳入": 0, "待定": 0, "排除": 0}
    for rec in records:
        decision = rec.get("判定") or "待定"
        stats[decision] = stats.get(decision, 0) + 1
    stats["PubMed"] = sum(1 for r in records if r.get("source_db") == "pubmed")
    stats["知网"] = sum(1 for r in records if r.get("source_db") == "cnki")
    return stats


def run(run_dir: Path, task: dict, log=None, judgements: list[dict] | None = None) -> tuple[int, list[dict]]:
    records = lc.read_records(run_dir)
    if not records:
        if log:
            log("[筛选] 没有可筛选的记录，先跑检索。")
        return lc.EXIT_PARTIAL, []

    for rec in records:
        rec.update(screen_record(rec, task))

    if judgements:
        applied = apply_judgements(records, judgements, log)
        if log:
            log(f"[筛选] 复核结论已应用 {applied} 条")

    _cap(records, int(task["screening"].get("max_items") or 60))
    records.sort(key=lambda r: (DECISION_ORDER.get(r.get("判定"), 1), -int(r.get("匹配分") or 0)))

    # 按来源分组回写：其他平台（SinoMed / ScienceDirect / CAS / 自定义）也要带上判定，
    # 否则 Excel 与报告里这些行的「判定/匹配分/选择依据」会是空的。
    groups: dict[str, list[dict]] = {}
    for rec in records:
        key = lc.sanitize_filename(str(rec.get("source_db") or "unknown"), 40)
        groups.setdefault(key, []).append(rec)
    for key in ("pubmed", "cnki"):
        groups.setdefault(key, [])
    for key, items in groups.items():
        lc.write_json(run_dir / f"records_{key}.json", items)
    stats = _stats(records)
    lc.write_json(run_dir / "筛选结果.json", {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "task": task,
        "stats": stats,
        "records": records,
    })
    if log:
        log(f"[筛选] 合计 {stats['合计']} 条 → 纳入 {stats['纳入']} / 待定 {stats['待定']} / 排除 {stats['排除']}")
    return lc.EXIT_OK, records


def main() -> int:
    lc.setup_console()
    parser = argparse.ArgumentParser(description="按筛查方向逐篇判定（纳入/待定/排除）")
    parser.add_argument("--run-dir", required=True)
    parser.add_argument("--task", help="检索任务.json")
    parser.add_argument("--judgements", help="复核结论 JSON")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    lc.print_env()

    run_dir = Path(args.run_dir)
    if not run_dir.exists():
        lc.die(lc.EXIT_CONFIG, f"目录不存在：{run_dir}")
    task_path = Path(args.task) if args.task else run_dir / "检索任务.json"
    if not task_path.exists():
        lc.die(lc.EXIT_CONFIG, f"找不到任务文件：{task_path}（先运行 intake.py）")
    try:
        task = intake.normalize_task(json.loads(lc.read_text_auto(task_path)))
    except (ValueError, json.JSONDecodeError) as exc:
        lc.die(lc.EXIT_CONFIG, f"任务文件无效：{exc}")

    judgements = []
    if args.judgements:
        judgements = json.loads(lc.read_text_auto(args.judgements))
        if isinstance(judgements, dict):
            judgements = judgements.get("records") or judgements.get("items") or []

    log = lc.RunLog(run_dir / "日志" / "run.log")
    try:
        code, records = run(run_dir, task, log, judgements)
        if args.json:
            print(json.dumps(_stats(records), ensure_ascii=False, indent=2))
        return code
    finally:
        log.close()


if __name__ == "__main__":
    sys.exit(main())
