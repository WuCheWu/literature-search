# -*- coding: utf-8 -*-
"""make_excel.py —— 生成 23 列文献清单

用途：读 records_pubmed.json + records_cnki.json，写 文献清单.xlsx（格式与状态着色）。
      列里包含筛选结论：判定 / 匹配分 / 关键词匹配 / 方向匹配 / 选择依据。
参数：
    --run-dir DIR    含 records_*.json 的目录
    --output FILE    输出路径（默认 <run-dir>/文献清单.xlsx）
    --json           打印统计
退出码：0 成功 / 1 无记录或写入失败 / 3 配置错误
依赖：openpyxl
说明：空字段必须留空字符串，不填“无”“N/A”；行数必须等于记录数。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import litcommon as lc  # noqa: E402

COLUMNS = [
    ("序号", 6), ("判定", 8), ("匹配分", 8), ("标题", 60), ("作者", 24), ("期刊/来源", 22),
    ("年份", 8), ("卷", 8), ("期", 8), ("页码", 12), ("DOI", 28), ("PMID", 12),
    ("来源库", 14), ("文献类型", 14), ("摘要", 80), ("关键词", 30),
    ("关键词匹配", 40), ("方向匹配", 40), ("选择依据", 60),
    ("全文状态", 12), ("本地文件", 40), ("原文链接", 42), ("备注", 18),
]
WRAP_COLUMNS = {
    "标题", "作者", "摘要", "关键词", "备注", "关键词匹配", "方向匹配", "选择依据",
}
RIGHT_COLUMNS = {"序号", "年份", "匹配分"}
DECISION_ORDER = {"纳入": 0, "待定": 1, "排除": 2}

GREEN = "C6EFCE"
YELLOW = "FFEB9C"
RED = "FFC7CE"
GRAY = "EDEDED"
STATUS_FILL = {
    "已下载": GREEN,
    "仅摘要": YELLOW, "需付费": YELLOW, "无权限": YELLOW, "仅CAJ": YELLOW, "未下载": YELLOW,
    "需登录": YELLOW,
    "下载失败": RED, "失败": RED,
}
DECISION_FILL = {"纳入": GREEN, "待定": YELLOW, "排除": GRAY}


def _cell_text(rec: dict, column: str) -> str:
    value = rec.get(column, "")
    if value is None:
        return ""
    return str(value).strip()



def build(run_dir: Path, output: Path | None = None) -> tuple[int, Path, int]:
    lc.require_deps("openpyxl")
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    records = lc.read_records(run_dir)
    if any(rec.get("判定") for rec in records):
        records.sort(key=lambda r: (DECISION_ORDER.get(r.get("判定"), 3), -int(r.get("匹配分") or 0)))
    target = Path(output) if output else run_dir / "文献清单.xlsx"
    wb = Workbook()
    ws = wb.active
    ws.title = "文献清单"

    header_font = Font(bold=True)
    header_fill = PatternFill("solid", start_color="D9D9D9", end_color="D9D9D9")
    for index, (name, width) in enumerate(COLUMNS, 1):
        cell = ws.cell(row=1, column=index, value=name)
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = Alignment(horizontal="center", vertical="center")
        ws.column_dimensions[get_column_letter(index)].width = width

    for row_index, rec in enumerate(records, start=2):
        for col_index, (name, _) in enumerate(COLUMNS, 1):
            if name == "序号":
                value = row_index - 1
            elif name == "匹配分" and str(rec.get("匹配分", "")).strip().isdigit():
                value = int(str(rec["匹配分"]).strip())
            else:
                value = _cell_text(rec, name)
            cell = ws.cell(row=row_index, column=col_index, value=value)
            horizontal = "right" if name in RIGHT_COLUMNS else "left"
            cell.alignment = Alignment(
                horizontal=horizontal, vertical="top", wrap_text=name in WRAP_COLUMNS
            )
            if name == "全文状态":
                color = STATUS_FILL.get(str(value).strip())
                if color:
                    cell.fill = PatternFill("solid", start_color=color, end_color=color)
            if name == "判定":
                color = DECISION_FILL.get(str(value).strip())
                if color:
                    cell.fill = PatternFill("solid", start_color=color, end_color=color)

    last_row = max(1, len(records) + 1)
    ws.freeze_panes = "D2"
    ws.auto_filter.ref = f"A1:{get_column_letter(len(COLUMNS))}{last_row}"
    ws.row_dimensions[1].height = 22
    try:
        wb.save(target)
    except Exception as exc:  # noqa: BLE001
        print(f"[Excel] 写入失败：{type(exc).__name__}: {exc}", file=sys.stderr)
        return lc.EXIT_PARTIAL, target, len(records)
    return lc.EXIT_OK, target, len(records)


def main() -> int:
    lc.setup_console()
    parser = argparse.ArgumentParser(description="生成 23 列文献清单 Excel")
    parser.add_argument("--run-dir", required=True, help="含 records_*.json 的目录")
    parser.add_argument("--output", help="输出 xlsx 路径")
    parser.add_argument("--json", action="store_true", help="打印统计")
    args = parser.parse_args()
    lc.print_env()
    run_dir = Path(args.run_dir)
    if not run_dir.exists():
        lc.die(lc.EXIT_CONFIG, f"目录不存在：{run_dir}")
    code, target, count = build(run_dir, Path(args.output) if args.output else None)
    print(f"\nExcel：{target}（{count} 行数据）")
    if args.json:
        import json

        print(json.dumps({"output": str(target), "rows": count}, ensure_ascii=False))
    return code


if __name__ == "__main__":
    sys.exit(main())
