# -*- coding: utf-8 -*-
"""plan_query.py —— 检索式生成与人工确认

用途：把中文主题/关键词拆成概念组，生成 PubMed 式与 CNKI 式，打印后等待用户确认。
参数：
    --text "自然语言主题"      与 --keyword 等价，二选一
    --keyword "关键词组"
    --groups "中文=en1,en2;中文2=en3"   手工指定概念组（覆盖自动识别）
    --groups-file groups.json          同上的 JSON 形式
    --json                             输出 JSON（供 run_all 使用）
    --yes                              跳过交互确认（仅 run_all 在用户已确认后使用）
退出码：0 成功 / 2 用户取消或无法交互 / 3 输入错误
依赖：标准库
说明：不调用任何翻译 API。词表覆盖本实验室常见主题；未识别词会明确列出来，
      由宿主模型用 --groups 补齐——绝不猜译。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import litcommon as lc  # noqa: E402

# 概念词表：cn 为主词，en 为同义词（* 为截词），mesh 为 MeSH 主题词
CONCEPTS = [
    {"cn": "聚丙烯微塑料", "en": ["polypropylene", "microplastic*"], "mesh": ["Microplastics"]},
    {"cn": "聚乙烯微塑料", "en": ["polyethylene", "microplastic*"], "mesh": ["Microplastics"]},
    {"cn": "聚苯乙烯微塑料", "en": ["polystyrene", "microplastic*"], "mesh": ["Microplastics"]},
    {"cn": "纳米塑料", "en": ["nanoplastic*"], "mesh": ["Microplastics"]},
    {"cn": "微塑料", "en": ["microplastic*", "microplastics"], "mesh": ["Microplastics"]},
    {"cn": "聚丙烯", "en": ["polypropylene"], "mesh": []},
    {"cn": "聚乙烯", "en": ["polyethylene"], "mesh": []},
    {"cn": "聚苯乙烯", "en": ["polystyrene"], "mesh": []},
    {"cn": "斑马鱼", "en": ["zebrafish", '"danio rerio"'], "mesh": ["Zebrafish"]},
    {"cn": "肠道菌群", "en": ['"gut microbiota"', '"gut microbiome"', '"intestinal flora"'],
     "mesh": ["Gastrointestinal Microbiome"]},
    {"cn": "肠道微生物", "en": ['"gut microbiota"', '"intestinal microbiota"'],
     "mesh": ["Gastrointestinal Microbiome"]},
    {"cn": "菌群", "en": ["microbiota", "microbiome"], "mesh": []},
    {"cn": "抗氧化", "en": ["antioxidant*", '"oxidative stress"'], "mesh": ["Antioxidants"]},
    {"cn": "氧化应激", "en": ['"oxidative stress"'], "mesh": ["Oxidative Stress"]},
    {"cn": "炎症", "en": ["inflammat*", '"inflammatory response"'], "mesh": ["Inflammation"]},
    {"cn": "消化", "en": ["digest*", '"digestive function"'], "mesh": ["Digestion"]},
    {"cn": "肠道", "en": ["intestin*", "gut"], "mesh": ["Intestines"]},
    {"cn": "肝脏", "en": ["liver", "hepat*"], "mesh": ["Liver"]},
    {"cn": "毒性", "en": ["toxic*", "toxicity"], "mesh": []},
    {"cn": "暴露", "en": ["exposure", "exposed"], "mesh": []},
    {"cn": "免疫", "en": ["immun*", '"immune response"'], "mesh": []},
    {"cn": "生长", "en": ["growth"], "mesh": []},
    {"cn": "生殖", "en": ["reproduct*"], "mesh": ["Reproduction"]},
    {"cn": "代谢", "en": ["metabol*"], "mesh": ["Metabolism"]},
    {"cn": "转录组", "en": ["transcriptom*"], "mesh": ["Transcriptome"]},
    {"cn": "蛋白质组", "en": ["proteom*"], "mesh": ["Proteome"]},
    {"cn": "基因表达", "en": ['"gene expression"'], "mesh": ["Gene Expression"]},
    {"cn": "生物富集", "en": ["bioaccumulat*"], "mesh": []},
    {"cn": "水环境", "en": ['"aquatic environment"', "water"], "mesh": []},
    {"cn": "沉积物", "en": ["sediment*"], "mesh": ["Geologic Sediments"]},
    {"cn": "食物链", "en": ['"food chain"', '"trophic transfer"'], "mesh": ["Food Chain"]},
    {"cn": "肠道屏障", "en": ['"intestinal barrier"', '"gut barrier"'], "mesh": []},
    {"cn": "组织学", "en": ["histolog*"], "mesh": []},
    {"cn": "行为", "en": ["behavio*"], "mesh": ["Behavior, Animal"]},
]
STOPWORDS = [
    "的影响", "影响", "研究", "作用", "机制", "指标", "分析", "进展", "综述", "初探",
    "探讨", "关系", "基于", "不同", "效应", "评估", "应用", "以及", "及其", "一种",
    "对", "和", "与", "及", "的", "在", "中", "下", "上", "为", "以", "了",
]
CNKI_FIELD = "SU"


def _load_groups(spec: str) -> list[dict]:
    """解析 '中文=en1,en2;中文2=en3' —— 手工补词通道。"""
    groups = []
    for chunk in re.split(r"[;；]", spec):
        chunk = chunk.strip()
        if not chunk:
            continue
        if "=" not in chunk:
            raise ValueError(f"概念组缺少 '='：{chunk}")
        cn, en = chunk.split("=", 1)
        terms = [t.strip() for t in re.split(r"[,，]", en) if t.strip()]
        groups.append({"cn": cn.strip(), "en": terms, "mesh": []})
    return groups


def _match(text: str) -> tuple[list[dict], str]:
    """最长优先匹配，返回命中概念与未识别片段。"""
    concepts = sorted(CONCEPTS, key=lambda c: len(c["cn"]), reverse=True)
    taken = [False] * len(text)
    hits: list[tuple[int, dict]] = []
    for concept in concepts:
        start = 0
        while True:
            idx = text.find(concept["cn"], start)
            if idx < 0:
                break
            span = range(idx, idx + len(concept["cn"]))
            if not any(taken[i] for i in span):
                for i in span:
                    taken[i] = True
                hits.append((idx, concept))
            start = idx + len(concept["cn"])
    hits.sort(key=lambda item: item[0])

    leftovers, buf = [], ""
    for char, used in zip(text, taken):
        if used:
            if buf:
                leftovers.append(buf)
                buf = ""
        else:
            buf += char
    if buf:
        leftovers.append(buf)

    unknown = []
    for chunk in leftovers:
        cleaned = chunk
        for word in STOPWORDS:
            cleaned = cleaned.replace(word, " ")
        for piece in re.split(r"[\s，。、；：,.;:()（）\[\]【】]+", cleaned):
            if len(re.findall(r"[\u4e00-\u9fff]", piece)) >= 2:
                unknown.append(piece)
    return [h[1] for h in hits], "、".join(dict.fromkeys(unknown))


def _pubmed_clause(group: dict) -> str:
    terms = list(group.get("en") or [])
    terms += [f'"{m}"[Mesh]' for m in group.get("mesh") or [] if m]
    return "(" + " OR ".join(terms) + ")" if terms else ""


def build(text: str, groups_override: list[dict] | None = None) -> dict:
    """生成三种表达式：PubMed 检索式、CNKI 字段式、CNKI 实际输入。"""
    if groups_override:
        groups, unknown = groups_override, ""
    else:
        groups, unknown = _match(text)
    pubmed = " AND ".join(c for c in (_pubmed_clause(g) for g in groups) if c)
    cnki_field = " AND ".join(f"{CNKI_FIELD}='{g['cn']}'" for g in groups)
    cnki_plain = " ".join(g["cn"] for g in groups)
    skipped = [g["cn"] for g in groups if not g.get("en")]
    return {
        "input": text,
        "groups": groups,
        "unknown": unknown,
        "pubmed": pubmed,
        "cnki_field": cnki_field,
        "cnki_plain": cnki_plain,
        "need_manual": skipped,
    }


def print_plan(plan: dict) -> None:
    print("\n=== 概念组 ===")
    for index, group in enumerate(plan["groups"], 1):
        terms = ", ".join(group.get("en") or []) or "（缺英文，需手工补）"
        mesh = "；MeSH: " + ", ".join(group.get("mesh") or []) if group.get("mesh") else ""
        print(f"  {index}. {group['cn']:<10} → {terms}{mesh}")
    if plan["unknown"]:
        print(f"\n  ! 未识别片段：{plan['unknown']}（不会进检索式，请用 --groups 补英文）")
    print("\n=== PubMed 检索式 ===")
    print("  " + (plan["pubmed"] or "（空）"))
    print("\n=== CNKI 检索式（记录用） ===")
    print("  " + (plan["cnki_field"] or "（空）"))
    print("\n=== CNKI 实际输入（简单检索框） ===")
    print("  " + (plan["cnki_plain"] or "（空）"))


def confirm() -> bool:
    print("\n检索式确认：[1] 直接执行   [2] 取消，改关键词/英文/方向后重跑")
    try:
        choice = input("输入 1 或 2：").strip()
    except EOFError:
        print("当前会话不可交互：确认后加 --yes 重新运行，或把这段检索式交给用户看一眼。")
        return False
    return choice == "1"


def main() -> int:
    lc.setup_console()
    parser = argparse.ArgumentParser(description="生成 PubMed / CNKI 检索式并等待确认")
    parser.add_argument("--text", help="自然语言主题")
    parser.add_argument("--keyword", help="关键词组（与 --text 等价）")
    parser.add_argument("--groups", help='手工概念组："中文=en1,en2;中文2=en3"')
    parser.add_argument("--groups-file", help="概念组 JSON 文件")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    parser.add_argument("--yes", action="store_true", help="跳过确认")
    args = parser.parse_args()
    lc.print_env()

    text = (args.text or args.keyword or "").strip()
    if not text and not (args.groups or args.groups_file):
        lc.die(lc.EXIT_CONFIG, "必须提供 --text/--keyword，或 --groups/--groups-file")
    try:
        override = _load_groups(args.groups) if args.groups else None
        if args.groups_file:
            override = json.loads(lc.read_text_auto(args.groups_file))
    except (ValueError, json.JSONDecodeError) as exc:
        lc.die(lc.EXIT_CONFIG, f"概念组格式错误：{exc}")

    plan = build(text or "handmade", override)
    if args.json:
        print(json.dumps(plan, ensure_ascii=False, indent=2))
        return lc.EXIT_OK
    print_plan(plan)
    if plan["need_manual"]:
        print(f"\n[阻塞] 以下概念组缺英文：{'、'.join(plan['need_manual'])}；补齐后再执行。")
        return lc.EXIT_NEED_USER
    if args.yes:
        print("\n[--yes] 已跳过确认。")
        return lc.EXIT_OK
    return lc.EXIT_OK if confirm() else lc.EXIT_NEED_USER


if __name__ == "__main__":
    sys.exit(main())
