# -*- coding: utf-8 -*-
"""intake.py —— 启动时的任务采集（关键词 / 提示词 / 筛查方向 / 检索平台 / 首次登录 / 检索设置）

用途：检索开始前把用户意图固化成 检索任务.json，后续每一步都按该文件执行，不再自行发挥。
四种入口：
    1) 终端向导（会逐项提示，可选项以编号选择）：
         python scripts\\intake.py --out task.json
    2) 宿主已问过用户，用参数落盘：
         python scripts\\intake.py --keywords "斑马鱼,微塑料" --direction "..." \\
             --platforms "pubmed,cnki,sinomed" --first-login ready --out task.json
    3) 宿主写好草稿，只做校验与补默认值：
         python scripts\\intake.py --from-json draft.json --out task.json
    4) 只要问题清单（宿主据此弹选择框）：
         python scripts\\intake.py --print-questionnaire
参数：
    --out FILE           输出任务文件（默认 <output_root>\\_任务\\检索任务_<时间>.json）
    --platforms IDS      检索平台，逗号分隔（platforms.py --list 可看）
    --platform-url URL   补充网址（可重复；未登记自动当自定义平台）
    --school-list PATH|URL 学校公布的数据库清单（txt/html 或图书馆页面网址）
    --first-login ready|first_time|unknown  是否已登录过这些平台并保存机构设置
    --login-pending IDS  需要首次登录的平台
    --yes                跳过最终确认（终端向导里用）
    --print-questionnaire 打印问题清单 JSON 后退出
退出码：0 成功 / 2 用户取消或无法交互 / 3 输入错误
依赖：标准库 + platforms.py
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import litcommon as lc  # noqa: E402
import platforms as pf  # noqa: E402

CURRENT_YEAR = datetime.now().year

# 期望相关度：直接决定筛选阈值（越高越严格，纳入篇数越少）
RELEVANCE = {
    "loose": {"label": "宽松（宁多勿漏，只要沾边就收）", "min_score": 40, "band": 20},
    "standard": {"label": "标准（主题相关且方法/指标对得上）", "min_score": 55, "band": 15},
    "strict": {"label": "严格（只要高度相关，可接受漏检）", "min_score": 70, "band": 10},
}
DEFAULT_DOC_EXCLUDE = ["会议", "征稿", "稿约", "Erratum", "Correction", "撤稿"]
# 这些词在方向上几乎必然出现，作为纳入要点会稀释评分，识别时跳过
GENERIC_CONCEPTS = {"暴露", "生长", "行为", "组织学", "肠道", "菌群", "毒性"}

QUESTIONNAIRE = {
    "title": "文献检索任务采集",
    "intro": (
        "开始检索前需要确认四类信息。所有答案会写入 检索任务.json，"
        "筛选阶段严格按「筛查方向」执行，不会自行扩大范围。"
    ),
    "questions": [
        {
            "id": "vpn_channel",
            "header": "VPN 通道",
            "question": "你的学校 VPN 用哪种方式连？（程序不代连，只按你说的方法去验证）",
            "required": True,
            "type": "choice",
            "options": [
                {"label": "深信服 EasyConnect", "value": "easyconnect"},
                {"label": "其他客户端（Cisco / OpenVPN 等）", "value": "other_client"},
                {"label": "学校 WebVPN（在浏览器里）", "value": "webvpn"},
                {"label": "还没连，我先去连", "value": "not_connected"},
                {"label": "不确定", "value": "unknown"},
            ],
            "default": "not_connected",
        },
        {
            "id": "keywords",
            "header": "关键词",
            "question": "这次要检索哪些关键词？（中文，逗号分隔，至少 1 个）",
            "required": True,
            "type": "text",
            "example": "斑马鱼, 微塑料",
        },
        {
            "id": "keywords_en",
            "header": "英文关键词",
            "question": "对应的英文关键词？（与中文一一对应，用于 PubMed；不知道可留空由词表补）",
            "required": False,
            "type": "text",
            "example": "zebrafish, microplastic",
        },
        {
            "id": "search_prompt",
            "header": "搜索提示词",
            "question": "用一句话说明你要解决的问题（会写进任务文件，辅助判断相关性）",
            "required": False,
            "type": "text",
            "example": "聚丙烯微塑料暴露对斑马鱼肠道菌群与消化功能的影响",
        },
        {
            "id": "direction",
            "header": "筛查方向",
            "question": "只保留什么样的文献？（必填；这是逐篇筛选的硬规则）",
            "required": True,
            "type": "text",
            "example": "斑马鱼活体暴露实验，关注肠道菌群、消化酶、炎症与抗氧化指标；排除材料吸附/降解与生态风险评估",
        },
        {
            "id": "include",
            "header": "纳入要点",
            "question": "命中哪些要点算符合方向？（逗号分隔；默认使用关键词）",
            "required": False,
            "type": "text",
            "example": "肠道菌群, 消化酶, 炎症, 氧化应激",
        },
        {
            "id": "exclude",
            "header": "排除要点",
            "question": "出现哪些词就排除？（标题/关键词命中即排除，摘要命中只扣分）",
            "required": False,
            "type": "text",
            "example": "综述, 吸附, 降解, 生态风险",
        },
        {
            "id": "must_have",
            "header": "必含条件",
            "question": "每篇必须包含的条件？（缺失直接排除，可留空）",
            "required": False,
            "type": "text",
            "example": "斑马鱼",
        },
        {
            "id": "years",
            "header": "文献时间",
            "question": "检索哪段时间的文献？",
            "required": True,
            "type": "choice",
            "options": [
                {"label": "近 3 年", "years": 3},
                {"label": "近 5 年", "years": 5},
                {"label": "近 10 年", "years": 10},
                {"label": "自定义年份区间", "years": None},
            ],
            "default": "近 5 年",
        },
        {
            "id": "relevance",
            "header": "期望相关度",
            "question": "希望筛得多严？",
            "required": True,
            "type": "choice",
            "options": [
                {"label": "宽松（宁多勿漏）", "value": "loose"},
                {"label": "标准", "value": "standard"},
                {"label": "严格（只要高度相关）", "value": "strict"},
            ],
            "default": "标准",
        },
        {
            "id": "limit",
            "header": "每库条数",
            "question": "每个数据库最多取多少条候选？（10/20/30/50）",
            "required": False,
            "type": "choice",
            "options": [{"label": "10 条"}, {"label": "20 条"}, {"label": "30 条"}, {"label": "50 条"}],
            "default": "20 条",
        },
        {
            "id": "platforms",
            "header": "检索平台",
            "question": (
                "查哪些平台？（可多选；先按学校公布的数据库清单勾选，清单外的网址在下一题补充）"
            ),
            "required": True,
            "type": "multi_choice",
            "options": pf.questionnaire_options() or [
                {"label": "PubMed｜公开", "value": "pubmed"},
                {"label": "知网｜校园 IP", "value": "cnki"},
            ],
            "default": ["pubmed", "cnki"],
        },
        {
            "id": "school_list",
            "header": "学校清单",
            "question": "学校公布的数据库清单在哪里？（本地 txt/html 路径或图书馆页面网址，可留空）",
            "required": False,
            "type": "text",
            "example": "https://library.example.edu.cn/ 或 D:\\数据库清单.html",
        },
        {
            "id": "custom_urls",
            "header": "补充网址",
            "question": "清单里没有、但你知道能查文献的网址？（逗号分隔，可留空；会先探测再检索）",
            "required": False,
            "type": "text",
            "example": "http://www.sinomed.ac.cn/, https://www.sciencedirect.com",
        },
        {
            "id": "login_setup",
            "header": "首次登录",
            "question": (
                "这些平台里，有没有你第一次在这台电脑上用的？"
                "（学校 VPN/机构登录必须先手动登录一次并保存好登录设置，否则脚本拿不到文献和全文）"
            ),
            "required": True,
            "type": "choice",
            "options": [
                {"label": "都登录过，登录设置也保存好了", "value": "ready"},
                {"label": "有平台是第一次用（先带我登录）", "value": "first_time"},
                {"label": "不确定，逐个检查", "value": "unknown"},
            ],
            "default": "不确定，逐个检查",
        },
        {
            "id": "login_pending",
            "header": "首次平台",
            "question": "哪些平台需要首次登录？（平台名或 id，逗号分隔；留空=除 PubMed 外逐个检查）",
            "required": False,
            "type": "text",
            "example": "SciFindern, SinoMed",
        },
        {
            "id": "download",
            "header": "是否下载",
            "question": "筛选后是否下载纳入文献的全文？",
            "required": True,
            "type": "choice",
            "options": [{"label": "下载全文"}, {"label": "只要清单，不下载"}],
            "default": "下载全文",
        },
        {
            "id": "max_items",
            "header": "纳入上限",
            "question": "纳入清单最多保留多少篇？（超出部分转待定）",
            "required": False,
            "type": "choice",
            "options": [{"label": "30 篇"}, {"label": "60 篇"}, {"label": "100 篇"}],
            "default": "60 篇",
        },
    ],
}

# ---------------------------------------------------------------- 询问协议
# 取信息的顺序与方式：一次一题、每题带推荐答案、能查的事实自己查、未共识不动手。
PROTOCOL = {
    "mode": "grilling",
    "rules": [
        "一次一题：问完等回答，再问下一题；不要一次性甩整张问卷。",
        "每题带推荐答案：先说「我建议……」，再给一句理由；用户可以直接采纳。",
        "能查的事实自己查：config.json、platforms.json、vpn_check 结果、上次运行目录、"
        "平台登录/探测缓存——只有「决策」才问用户。",
        "未达成共识不动手：关键词、筛查方向、平台、登录状态、VPN 通道没齐之前不检索、不下载；"
        "缺项一律停在退出码 2，不猜、不默认放行。",
    ],
    "order": [
        "vpn_channel", "keywords", "keywords_en", "direction", "years",
        "platforms", "login_setup", "limit", "relevance", "download",
    ],
    "lookup_first": [
        "config.json 里已填的学校、门户、输出目录（有就不问）",
        "platforms.json 注册表与上次探测/登录缓存（有就报现状，不重新问）",
        "vpn_check 实测的客户端/网卡/路由（事实，不问用户）",
        "上次运行目录里的 检索任务.json（续跑时复用，只问变化项）",
    ],
    "ask_only": [
        "你学校叫什么、用什么方式连 VPN（程序不猜、不代连）",
        "这次查什么、只保留什么样的文献（筛查方向必须由用户给）",
        "要不要全文、有没有平台的账号（账号密码永远由用户自己输）",
    ],
}

WHY = {
    "vpn_channel": "连 VPN 的方式决定预检怎么验；程序只检测、不启动客户端、不碰账号密码",
    "keywords": "没有关键词就没有检索式，必须由用户给",
    "keywords_en": "词表覆盖不到的主题没有英文就会拼出空检索式，宁可补一次",
    "search_prompt": "一句话研究问题，写进任务供判定参考，不填也能跑但相关性会差一些",
    "direction": "逐篇纳入/排除的硬规则，缺了只能按关键词匹配，结果会不可信",
    "include": "命中这些要点加分；不填就从方向与关键词自动识别",
    "exclude": "命中标题/关键词直接排除；比事后人工删更省事",
    "must_have": "缺它直接排除，用来卡住「必须含某物种/方法」这类硬条件",
    "years": "默认近 5 年，用户说「不限」时改 0",
    "platforms": "默认 PubMed + 知网；学校订阅清单里有别的库再加",
    "login_setup": "没登录过的平台打开只有登录页，先问清能少跑一趟",
    "limit": "每库 20 条是准确率与耗时的平衡点，上限 50",
    "relevance": "标准档：主题相关且方法/指标对得上；严格档会把方法不同的也筛掉",
    "download": "默认只下「纳入」；待定/排除不下，省时间也少惹付费墙",
    "max_items": "纳入上限，超出转待定，避免一次下太多",
    "school_list": "给一次清单就能知道你学校有哪些库，省得逐个试",
    "custom_urls": "清单里没认出的网址需要用户确认是不是学校订阅的库",
    "login_pending": "只列需要首次登录的平台，其余按已就绪处理",
}


def _recommendation(item: dict) -> str:
    """每题给一个可直接采纳的推荐答案。"""
    if item.get("default") is not None:
        return str(item["default"])
    return str(item.get("example") or "")


for _q in QUESTIONNAIRE["questions"]:
    _q.setdefault("recommended", _recommendation(_q))
    _q.setdefault("why", WHY.get(_q["id"], ""))
QUESTIONNAIRE["protocol"] = PROTOCOL


# ---------------------------------------------------------------- 基础工具

def _split(text: str) -> list[str]:
    """按中英文逗号/顿号/分号切分，去空。"""
    return [x.strip() for x in re.split(r"[,，、;；/|]", str(text or "")) if x.strip()]


def _ask(prompt: str, default: str = "", required: bool = False) -> str | None:
    """读一行输入；返回 None 表示用户中断（EOF）。"""
    tip = prompt
    if default:
        tip += f"\n  （直接回车用默认值：{default}）"
    print(tip)
    try:
        answer = input("> ").strip()
    except EOFError:
        return None
    if not answer:
        if default:
            return default
        if required:
            print("  ! 这一项必填，请重新输入。")
            return _ask(prompt, default, required)
        return ""
    return answer


def _ask_choice(prompt: str, options: list[str], default_index: int = 1) -> str | None:
    """编号选择，返回所选项文本。"""
    print("\n" + prompt)
    for index, label in enumerate(options, 1):
        mark = "（默认）" if index == default_index else ""
        print(f"  [{index}] {label}{mark}")
    raw = _ask("输入编号", str(default_index))
    if raw is None:
        return None
    if not raw.isdigit() or not (1 <= int(raw) <= len(options)):
        print("  ! 编号无效，按默认处理。")
        return options[default_index - 1]
    return options[int(raw) - 1]


def _ask_yes(prompt: str, default: bool = True) -> bool | None:
    raw = _ask(prompt + " [y/n]", "y" if default else "n")
    if raw is None:
        return None
    return str(raw).strip().lower() in ("y", "yes", "是", "1", "对", "要")


# ---------------------------------------------------------------- 数据规范化

def _as_list(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return _split(value)
    return [str(x).strip() for x in value if str(x).strip()]


def _int_or(value, fallback: int) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return fallback


def relevance_preset(name: str) -> dict:
    return RELEVANCE.get(str(name or "").strip().lower(), RELEVANCE["standard"])


def suggest_include(direction: str, keywords: list[str]) -> list[str]:
    """从筛查方向里识别领域概念，作为「纳入要点」的默认值。

    例：「关注肠道菌群、消化功能、炎症与抗氧化指标」→ 肠道菌群/消化/炎症/抗氧化。
    只做词表匹配，不猜词；识别不到就返回空，由调用方退回关键词。
    """
    import plan_query  # 延迟导入，避免与 screen 形成环

    text = str(direction or "")
    if not text:
        return []
    found: list[str] = []
    for concept in sorted(plan_query.CONCEPTS, key=lambda c: len(c["cn"]), reverse=True):
        term = concept["cn"]
        if term in GENERIC_CONCEPTS:
            continue
        if term in text and term not in found:
            found.append(term)
    # 长词优先：已命中「肠道菌群」就不再保留「肠道」
    found = [t for t in found if not any(t != other and t in other for other in found)]
    found.sort(key=lambda t: text.find(t))
    kw_norm = {str(k).strip().lower() for k in keywords}
    return [t for t in found if t.strip().lower() not in kw_norm]


PLACEHOLDER_ONLY = {
    "", "...", "…", "待定", "待补充", "待确认", "todo", "tbd", "xxx", "无", "不知道",
}


def _is_placeholder(text: str) -> bool:
    """识别「...」「待定」这类占位符。

    实测坑：--direction "..." 不报错，会把字面量写进任务，筛选时照它打分，
    结果全错还没人察觉。宁可报错，也不收占位符。
    """
    core = re.sub(r"[\s\W_]+", "", str(text or "")).lower()
    return not core or core in {re.sub(r"[\s\W_]+", "", x) for x in PLACEHOLDER_ONLY}


def normalize_task(data: dict) -> dict:
    """把任意来源的草稿补全成完整任务；缺关键词或筛查方向直接报错。"""
    keywords = _as_list(data.get("keywords"))
    if not keywords:
        raise ValueError("缺少关键词（keywords）：至少给 1 个")
    if all(_is_placeholder(k) for k in keywords):
        raise ValueError(
            "关键词只有占位符（如「...」）：请给出真实检索词，例如「斑马鱼, 微塑料」"
        )
    keywords_en = _as_list(data.get("keywords_en"))
    if not keywords_en:
        keywords_en = [""] * len(keywords)
    while len(keywords_en) < len(keywords):
        keywords_en.append("")

    scr = dict(data.get("screening") or {})
    direction = str(scr.get("direction") or "").strip()
    if not direction:
        raise ValueError(
            "缺少筛查方向（screening.direction）：必须说明「只保留什么样的文献」，"
            "否则无法按方向严格筛选"
        )
    if _is_placeholder(direction):
        raise ValueError(
            "筛查方向像是占位符（例如「...」「待定」）：请写成具体的「只保留什么样的文献」，"
            "例如「斑马鱼活体暴露，关注肠道菌群与消化酶；排除综述与生态风险评估」"
        )

    relevance = str(scr.get("relevance") or "standard").strip().lower()
    if relevance not in RELEVANCE:
        relevance = "standard"
    preset = relevance_preset(relevance)

    search = dict(data.get("search") or {})
    vpn_raw = dict(data.get("vpn") or {})
    vpn_channel = str(vpn_raw.get("channel") or search.get("vpn_channel") or "").strip().lower()
    if vpn_channel not in ("easyconnect", "other_client", "webvpn", "not_connected", "unknown"):
        vpn_channel = "unknown"
    vpn_block = {"channel": vpn_channel, "note": str(vpn_raw.get("note") or "").strip()}
    default_years = _int_or(search.get("years"), 5)
    years_min = _int_or(
        search.get("years_min") or scr.get("years_min"),
        CURRENT_YEAR - default_years + 1,
    )
    years_max = _int_or(search.get("years_max"), CURRENT_YEAR)
    if years_min > years_max:
        years_min, years_max = years_max, years_min
    years_min = max(1900, min(years_min, CURRENT_YEAR))
    years_max = max(years_min, min(years_max, CURRENT_YEAR))

    include = _as_list(scr.get("include")) or suggest_include(direction, keywords) or keywords
    exclude = _as_list(scr.get("exclude"))
    must_have = _as_list(scr.get("must_have"))
    prefer = _as_list(scr.get("prefer"))
    doc_types = _as_list(scr.get("doc_types_exclude"))
    if not doc_types:
        doc_types = []  # 默认不排除综述，除非用户在向导里勾选

    min_score = scr.get("min_score")
    if min_score is None:
        min_score = preset["min_score"]
    min_score = max(0, min(100, _int_or(min_score, preset["min_score"])))

    custom_platforms: list[dict] = []
    for item in search.get("custom_platforms") or []:
        if not isinstance(item, dict) or not item.get("id"):
            continue
        normalized = dict(item)
        normalized.setdefault("name", normalized["id"])
        normalized.setdefault("short", str(normalized["name"])[:16])
        normalized.setdefault("home", normalized.get("entry") or "")
        normalized.setdefault("entry", normalized.get("home") or "")
        normalized.setdefault("adapter", "generic")
        normalized.setdefault("selectors", {})
        normalized.setdefault("kind", "unknown")
        normalized.setdefault("custom", True)
        custom_platforms.append(normalized)

    raw_platforms = _as_list(search.get("platforms")) or _as_list(search.get("databases"))
    if not raw_platforms:
        raw_platforms = ["pubmed", "cnki"]
    platform_ids: list[str] = []
    custom_ids = {c["id"] for c in custom_platforms}
    for raw in raw_platforms:
        found = pf.get(raw, customs=custom_platforms)
        pid = found["id"] if found else str(raw).strip().lower()
        if pid and pid not in platform_ids:
            platform_ids.append(pid)
    unknown = [p for p in platform_ids if not pf.get(p) and p not in custom_ids]
    if unknown:
        raise ValueError(
            "未知平台：" + "、".join(unknown)
            + "（用 python scripts\\platforms.py --add <网址> 登记，或从任务里删掉）"
        )
    databases = [p for p in platform_ids if p in ("pubmed", "cnki")]

    limit = max(1, min(_int_or(search.get("limit"), 20), 50))
    max_items = max(1, min(_int_or(scr.get("max_items"), 60), 500))

    login_raw = dict(search.get("login_setup") or {})
    login_status = str(login_raw.get("status") or "unknown").strip().lower()
    if login_status not in ("ready", "first_time", "unknown"):
        login_status = "unknown"
    pending_ids: list[str] = []
    for raw in _as_list(login_raw.get("pending")):
        found = pf.get(raw, customs=custom_platforms)
        pid = found["id"] if found else str(raw).strip().lower()
        if pid in platform_ids and pid not in pending_ids:
            pending_ids.append(pid)
    login_setup = {
        "status": login_status,
        "pending": pending_ids,
        "note": str(login_raw.get("note") or "").strip(),
    }

    task = {
        "version": 1,
        "created_at": str(data.get("created_at") or datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
        "vpn": vpn_block,
        "keywords": keywords,
        "keywords_en": keywords_en[: len(keywords)],
        "search_prompt": str(data.get("search_prompt") or "").strip(),
        "notes": str(data.get("notes") or "").strip(),
        "screening": {
            "mode": "strict",
            "direction": direction,
            "include": include,
            "exclude": exclude,
            "must_have": must_have,
            "prefer": prefer,
            "doc_types_exclude": doc_types,
            "relevance": relevance,
            "min_score": min_score,
            "band": _int_or(scr.get("band"), preset["band"]),
            "max_items": max_items,
            "years_min": years_min,
            "years_max": years_max,
        },
        "search": {
            "years": years_max - years_min + 1,
            "years_min": years_min,
            "years_max": years_max,
            "limit": limit,
            "databases": databases,
            "platforms": platform_ids,
            "custom_platforms": custom_platforms,
            "login_setup": login_setup,
            "probe": bool(search.get("probe", True)),
            "download": bool(search.get("download", True)),
        },
    }
    return task


def build_from_flags(args) -> dict:
    """宿主用参数直接落盘。"""
    search = {
        "years": args.years,
        "years_min": args.years_min,
        "years_max": args.years_max,
        "limit": args.limit,
        "download": not args.no_download,
        "vpn_channel": getattr(args, "vpn_channel", None),
    }
    platform_args = _as_list(getattr(args, "platforms", "") or "")
    if platform_args:
        search["platforms"] = platform_args
    elif args.databases:
        search["databases"] = _as_list(args.databases)

    custom: list[dict] = []
    for url in getattr(args, "platform_url", None) or []:
        custom.append(pf.inline_custom(url))

    school_list = str(getattr(args, "school_list", "") or "").strip()
    if school_list:
        result = pf.import_school_list(school_list, customs=custom)
        if result.get("error"):
            raise ValueError(f"读取学校清单失败：{result['error']}")
        merged = list(dict.fromkeys(_as_list(search.get("platforms")) + result["matched"]))
        if merged:
            search["platforms"] = merged
        for url in result["unknown"]:
            custom.append(pf.inline_custom(url))
        print(f"[学校清单] 识别到平台：{'、'.join(result['names']) or '（无）'}；"
              f"未识别网址 {len(result['unknown'])} 个（将逐个探测）")
    if custom:
        search["custom_platforms"] = custom

    if getattr(args, "first_login", None):
        search["login_setup"] = {
            "status": args.first_login,
            "pending": _as_list(getattr(args, "login_pending", "") or ""),
        }

    data = {
        "keywords": args.keywords,
        "keywords_en": args.keywords_en,
        "search_prompt": args.prompt,
        "screening": {
            "direction": args.direction,
            "include": args.include,
            "exclude": args.exclude,
            "must_have": args.must,
            "prefer": args.prefer,
            "relevance": args.relevance,
            "min_score": args.min_score,
            "max_items": args.max_items,
            "doc_types_exclude": args.doc_exclude,
        },
        "search": search,
    }
    return normalize_task(data)


def build_from_keyword(keyword: str, direction: str = "") -> dict:
    """旧参数兼容：只有关键词时构造任务；无方向则退化为关键词匹配并标记。"""
    data = {"keywords": keyword, "screening": {"direction": direction or ""}}
    if not direction:
        # 允许运行，但明确降级：报告与日志都会标明
        task = normalize_task(
            {"keywords": keyword, "screening": {"direction": keyword, "include": _split(keyword)}}
        )
        task["screening"]["mode"] = "keyword_only"
        task["screening"]["direction"] = f"（未提供筛查方向，仅按关键词匹配：{keyword}）"
        return task
    return normalize_task(data)


def groups_from_task(task: dict) -> list[dict] | None:
    """中英关键词一一对齐时，直接作为概念组覆盖词表。

    用户给的英文通常只是简写，这里再并上领域词表里的同义词与 MeSH 主题词，
    避免 PubMed 检索式过窄（筛选阶段仍严格按方向判定）。
    """
    import plan_query

    keywords = task.get("keywords") or []
    ens = task.get("keywords_en") or []
    if len(ens) != len(keywords) or not all(str(e).strip() for e in ens):
        return None
    concept_map = {c["cn"].strip(): c for c in plan_query.CONCEPTS}
    groups = []
    for cn, en in zip(keywords, ens):
        terms: list[str] = []
        seen: set[str] = set()

        def add(term: str) -> None:
            term = str(term).strip()
            if not term:
                return
            if " " in term and not term.startswith('"') and not term.endswith("*"):
                term = f'"{term}"'
            key = term.lower().strip('"')
            if key not in seen:
                seen.add(key)
                terms.append(term)

        for piece in _split(en):
            add(piece)
        concept = concept_map.get(cn.strip())
        if concept:
            for extra in concept.get("en") or []:
                add(extra)
        groups.append({
            "cn": cn,
            "en": terms,
            "mesh": list((concept or {}).get("mesh") or []),
        })
    return groups


# ---------------------------------------------------------------- 展示与向导

def platform_label(pid: str, task: dict | None = None) -> str:
    customs = ((task or {}).get("search") or {}).get("custom_platforms") or []
    platform = pf.get(pid, customs=customs)
    return (platform.get("short") or platform.get("name") or pid) if platform else pid


def print_task(task: dict) -> None:
    scr, search = task["screening"], task["search"]
    print("\n" + "=" * 62)
    print("检索任务（后续步骤严格按此执行）")
    print("-" * 62)
    print(f"  关键词      ：{'、'.join(task['keywords'])}")
    if any(task.get("keywords_en") or []):
        print(f"  英文关键词  ：{'、'.join(x or '（空）' for x in task['keywords_en'])}")
    print(f"  搜索提示词  ：{task.get('search_prompt') or '（未填）'}")
    print(f"  筛查方向    ：{scr['direction']}")
    print(f"  纳入要点    ：{'、'.join(scr['include']) or '（无）'}")
    print(f"  排除要点    ：{'、'.join(scr['exclude']) or '（无）'}")
    print(f"  必含条件    ：{'、'.join(scr['must_have']) or '（无）'}")
    if scr.get("doc_types_exclude"):
        print(f"  排除类型    ：{'、'.join(scr['doc_types_exclude'])}")
    print(f"  期望相关度  ：{relevance_preset(scr['relevance'])['label']}"
          f"（纳入线 {scr['min_score']} 分，待定带 ±{scr['band']}）")
    print(f"  文献时间    ：{scr['years_min']}–{scr['years_max']}（{search['years']} 年）")
    platform_names = [platform_label(pid, task) for pid in
                      (search.get("platforms") or search.get("databases") or [])]
    print(f"  检索平台    ：{'、'.join(platform_names)}   每库条数：{search['limit']}")
    channel = str((task.get("vpn") or {}).get("channel") or "unknown")
    channel_label = {
        "easyconnect": "深信服 EasyConnect",
        "other_client": "其他客户端（Cisco/OpenVPN 等）",
        "webvpn": "学校 WebVPN（浏览器内）",
        "not_connected": "还没连（程序不代连，先连上再实测）",
        "unknown": "未确认（按实测结果判断）",
    }.get(channel, "未确认")
    print(f"  VPN 通道    ：{channel_label}")
    login_setup = search.get("login_setup") or {}
    login_status = login_setup.get("status") or "unknown"
    login_text = {
        "ready": "用户确认都登录过、登录设置已保存",
        "first_time": "有平台首次使用，检索前要先手动登录",
        "unknown": "未确认，检索前逐个检查登录状态",
    }.get(login_status, login_status)
    print(f"  登录准备    ：{login_text}")
    print(f"  纳入上限    ：{scr['max_items']} 篇   下载全文：{'是' if search['download'] else '否'}")
    if scr.get("mode") == "keyword_only":
        print("  ! 注意：未提供筛查方向，本次只做关键词匹配。")
    print("=" * 62)


def default_out_path() -> Path:
    cfg = lc.load_config(None, require=False) or {}
    root = Path(cfg.get("output_root") or (lc.SKILL_ROOT / "输出"))
    return root / "_任务" / f"检索任务_{datetime.now():%Y%m%d_%H%M%S}.json"


def _choose_platforms() -> list[str] | None:
    items = pf.selectable()
    print("\n  可选平台（规则 6 内置 + 公共库）：")
    for index, platform in enumerate(items, 1):
        print(f"  [{index}] {platform.get('short', platform['id']):<14}"
              f"{(platform.get('access_label') or ''):<24}"
              f"{(platform.get('fulltext_label') or '')}")
    raw = _ask("  输入编号（逗号分隔；a=全部；默认 1,2 = PubMed + 知网）", default="1,2")
    if raw is None:
        return None
    if str(raw).strip().lower() in ("a", "all", "全部"):
        return [platform["id"] for platform in items]
    selected: list[str] = []
    for piece in _split(raw):
        if piece.isdigit() and 1 <= int(piece) <= len(items):
            pid = items[int(piece) - 1]["id"]
            if pid not in selected:
                selected.append(pid)
    return selected or ["pubmed", "cnki"]


def wizard() -> dict | None:
    """终端向导：4 步 + 检索设置 + 确认。返回 None 表示用户退出。"""
    print("\n" + "=" * 62)
    print("文献检索任务向导")
    print("=" * 62)
    print("先收集你要什么，再检查 VPN 与学校账号，最后才开始检索。\n")

    keywords = _ask("[1/5] 关键词（中文，逗号分隔，必填）", required=True)
    if keywords is None:
        return None
    if not _split(keywords):
        print("  ! 关键词不能为空。")
        return None

    keywords_en = _ask("[2/5] 英文关键词（与中文一一对应，可留空由词表补）")
    if keywords_en is None:
        return None

    prompt = _ask("[3/5] 文献搜索提示词（一句话说明要解决的问题，可留空）")
    if prompt is None:
        return None

    print("\n[4/5] 筛查方向 —— 决定哪些文献能进清单，请写具体")
    direction = _ask("  只保留什么样的文献？（必填）", required=True)
    if direction is None or not str(direction).strip():
        return None

    print("\n  细化规则（可留空，直接回车用默认）")
    suggested = suggest_include(direction, _split(keywords))
    include_default = "、".join(suggested) if suggested else keywords
    hint = "从方向识别到" if suggested else "默认=关键词"
    include = _ask(f"  纳入要点（{hint}：{include_default}）", default=include_default)
    if include is None:
        return None
    exclude = _ask("  排除要点（标题/关键词命中即排除，如：综述, 吸附, 生态风险）", default="")
    if exclude is None:
        return None
    must = _ask("  必含条件（缺失直接排除，如：斑马鱼）", default="")
    if must is None:
        return None

    print("\n[5/5] 检索设置")
    time_choice = _ask_choice(
        "  文献时间范围：",
        ["近 3 年", "近 5 年", "近 10 年", "自定义年份区间"],
        default_index=2,
    )
    if time_choice is None:
        return None
    if time_choice.startswith("自定义"):
        span = _ask(f"  输入年份区间（如 2018-{CURRENT_YEAR}）", default=f"{CURRENT_YEAR - 5}-{CURRENT_YEAR}")
        if span is None:
            return None
    else:
        years = int(re.search(r"\d+", time_choice).group(0))
        span = f"{CURRENT_YEAR - years + 1}-{CURRENT_YEAR}"

    rel_choice = _ask_choice(
        "  期望相关度：",
        [f"宽松（纳入线 {RELEVANCE['loose']['min_score']} 分，宁多勿漏）",
         f"标准（纳入线 {RELEVANCE['standard']['min_score']} 分）",
         f"严格（纳入线 {RELEVANCE['strict']['min_score']} 分，只要高度相关）"],
        default_index=2,
    )
    if rel_choice is None:
        return None
    relevance = "loose" if rel_choice.startswith("宽松") else ("strict" if rel_choice.startswith("严格") else "standard")

    limit_choice = _ask_choice("  每个库最多取多少条候选：", ["10 条", "20 条", "30 条", "50 条"], default_index=2)
    if limit_choice is None:
        return None

    platforms_selected = _choose_platforms()
    if platforms_selected is None:
        return None

    print("\n  按学校公布的数据库清单补充（可留空跳过）")
    school_source = _ask("  清单文件（txt/html）或图书馆页面网址", default="")
    if school_source is None:
        return None
    extra_urls = _ask("  清单外的其他网址（逗号分隔）", default="")
    if extra_urls is None:
        return None

    custom_platforms: list[dict] = []
    if str(school_source).strip():
        result = pf.import_school_list(school_source)
        if result.get("error"):
            print(f"  ! {result['error']}（已跳过；稍后可 platforms.py --add 逐个登记）")
        else:
            print(f"  识别到平台：{'、'.join(result['names']) or '（无）'}")
            for pid in result["matched"]:
                if pid not in platforms_selected:
                    platforms_selected.append(pid)
            for url in result["unknown"]:
                item = pf.inline_custom(url)
                custom_platforms.append(item)
                platforms_selected.append(item["id"])
                print(f"  + 未识别网址，登记为自定义平台：{item['short']}｜{url}")
    for url in _split(extra_urls):
        platform = pf.resolve(url, customs=custom_platforms)
        if platform:
            if platform["id"] not in platforms_selected:
                platforms_selected.append(platform["id"])
        else:
            item = pf.inline_custom(url)
            custom_platforms.append(item)
            platforms_selected.append(item["id"])
            print(f"  + 自定义平台：{item['short']}｜{url}")
    if not platforms_selected:
        print("  ! 至少选一个平台。")
        return None

    print("\n  首次登录确认（不能省：第一次用必须先手动登录一次，否则脚本拿不到文献和全文）")
    first_time = _ask_yes("  这些平台里有没有第一次在这台电脑上用的？", default=False)
    if first_time is None:
        return None
    login_status = "first_time" if first_time else "ready"
    login_pending: list[str] = []
    if first_time:
        defaults = [platform_label(pid) for pid in platforms_selected if pid != "pubmed"]
        raw = _ask(f"  哪些需要首次登录（默认：{'、'.join(defaults) or '无'}）",
                   default="、".join(defaults))
        if raw is None:
            return None
        for name in _split(raw):
            platform = pf.get(name, customs=custom_platforms)
            pid = platform["id"] if platform else name
            if pid in platforms_selected and pid not in login_pending:
                login_pending.append(pid)

    download = _ask_yes("  筛选后下载纳入文献的全文？", default=True)
    if download is None:
        return None

    exclude_reviews = _ask_yes("  是否排除综述/会议/征稿类？", default=False)
    if exclude_reviews is None:
        return None

    max_items = _ask("  纳入清单最多保留多少篇（超出转待定）", default="60")
    if max_items is None:
        return None

    data = {
        "keywords": keywords,
        "keywords_en": keywords_en,
        "search_prompt": prompt,
        "screening": {
            "direction": direction,
            "include": include,
            "exclude": exclude,
            "must_have": must,
            "relevance": relevance,
            "doc_types_exclude": DEFAULT_DOC_EXCLUDE + (["综述", "Review"] if exclude_reviews else []),
            "max_items": max_items,
        },
        "search": {
            "years_min": int(span.split("-")[0]),
            "years_max": int(span.split("-")[-1]),
            "limit": int(re.search(r"\d+", limit_choice).group(0)),
            "platforms": platforms_selected,
            "custom_platforms": custom_platforms,
            "login_setup": {"status": login_status, "pending": login_pending},
            "download": download,
        },
    }
    try:
        task = normalize_task(data)
    except ValueError as exc:
        print(f"\n[输入有误] {exc}")
        return None

    print_task(task)
    choice = _ask_choice("确认无误？", ["保存任务并退出", "重新填写", "退出"], default_index=1)
    if choice is None or choice == "退出":
        return None
    if choice == "重新填写":
        return wizard()
    return task


def main() -> int:
    lc.setup_console()
    parser = argparse.ArgumentParser(description="采集检索任务（关键词 / 提示词 / 筛查方向 / 检索设置）")
    parser.add_argument("--out", help="输出任务 JSON")
    parser.add_argument("--from-json", help="从草稿 JSON 读取并规范化")
    parser.add_argument("--print-questionnaire", action="store_true", help="打印问题清单 JSON 后退出")
    parser.add_argument("--keywords", help="中文关键词，逗号分隔")
    parser.add_argument("--vpn-channel", choices=["easyconnect", "other_client", "webvpn",
                                                  "not_connected", "unknown"],
                        help="VPN 连接方式（程序不代连，只按它验证）")
    parser.add_argument("--keywords-en", help="英文关键词，逗号分隔（与中文一一对应）")
    parser.add_argument("--prompt", help="文献搜索提示词")
    parser.add_argument("--direction", help="筛查方向")
    parser.add_argument("--include", help="纳入要点，逗号分隔")
    parser.add_argument("--exclude", help="排除要点，逗号分隔")
    parser.add_argument("--must", help="必含条件，逗号分隔")
    parser.add_argument("--prefer", help="加分项，逗号分隔")
    parser.add_argument("--doc-exclude", help="排除文献类型，逗号分隔")
    parser.add_argument("--relevance", choices=list(RELEVANCE), help="期望相关度 loose/standard/strict")
    parser.add_argument("--min-score", type=int, help="纳入最低分 0-100")
    parser.add_argument("--max-items", type=int, help="纳入上限篇数")
    parser.add_argument("--years", type=int, help="近 N 年")
    parser.add_argument("--years-min", type=int, help="起始年份")
    parser.add_argument("--years-max", type=int, help="截止年份")
    parser.add_argument("--limit", type=int, help="每库条数")
    parser.add_argument("--databases", help="旧参数：pubmed,cnki（等价于 --platforms 的子集）")
    parser.add_argument("--platforms", help="检索平台 id，逗号分隔（platforms.py --list 可看）")
    parser.add_argument("--platform-url", action="append", help="补充网址（可重复；未登记会自动当自定义平台）")
    parser.add_argument("--school-list", help="学校公布的数据库清单（txt/html 路径或网址）")
    parser.add_argument("--first-login", choices=["ready", "first_time", "unknown"],
                        help="是否已登录过这些平台并保存了登录设置")
    parser.add_argument("--login-pending", help="需要首次登录的平台，逗号分隔")
    parser.add_argument("--no-download", action="store_true", help="只出清单不下载")
    parser.add_argument("--yes", action="store_true", help="跳过向导确认（配合参数使用）")
    args = parser.parse_args()
    lc.print_env()

    if args.print_questionnaire:
        print(json.dumps(QUESTIONNAIRE, ensure_ascii=False, indent=2))
        return lc.EXIT_OK

    try:
        if args.from_json:
            draft = json.loads(lc.read_text_auto(args.from_json))
            task = normalize_task(draft)
        elif args.keywords:
            task = build_from_flags(args)
        else:
            if not sys.stdin.isatty():
                print(
                    "\n[需要用户输入] 当前会话不可交互，无法弹向导。\n"
                    "请让宿主先问清 关键词 / 搜索提示词 / 筛查方向 / 文献时间 / 期望相关度，\n"
                    "再用 --keywords --direction --years-min --relevance 等参数重新运行；\n"
                    "或先执行 --print-questionnaire 取得问题清单。"
                )
                return lc.EXIT_NEED_USER
            task = wizard()
            if task is None:
                print("\n[已取消] 未生成任务文件。")
                return lc.EXIT_NEED_USER
    except (ValueError, json.JSONDecodeError) as exc:
        print(f"\n[输入有误] {exc}", file=sys.stderr)
        return lc.EXIT_CONFIG

    print_task(task)
    out = Path(args.out) if args.out else default_out_path()
    lc.write_json(out, task)
    print(f"\n任务文件：{out}")
    print("下一步（复制上面这行路径）：")
    print(f'  python scripts\\run_all.py --task "{out}" '
          "--confirm-vpn --confirm-account --confirm-platform-login --yes")
    print("或一条命令走完（会重新问一遍）：python scripts\\run_all.py --interactive")
    return lc.EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
