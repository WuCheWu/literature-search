# -*- coding: utf-8 -*-
"""platforms.py —— 文献平台注册表（规则 5/6）

用途：
    1) 维护内置平台（SinoMed / CNKI 新版·旧版·CIPD / ScienceDirect / CAS 三库 / PubMed）；
    2) 解析学校公布的数据库清单（文本、HTML、网址），把能对上的平台勾出来，对不上的网址登记为自定义平台；
    3) 保存探测学到的选择器（platform_learned.json），供通用适配器复用。

参数（CLI）：
    --list                    列出全部平台（表格）
    --show ID                 查看单个平台完整 JSON
    --add URL [--name 名称]   登记自定义平台（同一主机只登记一次）
    --parse 文件或网址        解析学校清单，输出匹配到的平台与未识别网址
    --learn FILE              把 平台探测.json 里的 selectors_learned 写入 platform_learned.json
    --json                    机器可读输出
    --platforms-file PATH     覆盖 platforms.json（测试用；也可用环境变量 LIT_PLATFORMS_FILE）
退出码：0 成功 / 1 有未识别项 / 3 配置错误
依赖：标准库（解析网址用 requests，沿用 litcommon.build_session，trust_env=False）
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))
import litcommon as lc  # noqa: E402

URL_RE = re.compile(r"https?://[^\s<>\"'）)，,；;、]+", re.I)
TAG_RE = re.compile(r"<script.*?</script>|<style.*?</style>", re.I | re.S)
ANY_TAG_RE = re.compile(r"<[^>]+>")
DEFAULT_IDS = ["pubmed", "cnki"]
MINIMAL = [
    {
        "id": "pubmed", "name": "PubMed", "short": "PubMed", "aliases": ["pubmed"],
        "home": "https://pubmed.ncbi.nlm.nih.gov/", "entry": "https://pubmed.ncbi.nlm.nih.gov/",
        "entries": [{"label": "PubMed", "url": "https://pubmed.ncbi.nlm.nih.gov/"}],
        "lang": "en", "kind": "index", "access": "open", "access_label": "公开",
        "fulltext": "oa", "fulltext_label": "开放获取", "adapter": "pubmed",
        "selectors": {}, "order": 1, "selectable": True, "default": True, "note": "",
    },
    {
        "id": "cnki", "name": "中国知网 CNKI", "short": "知网", "aliases": ["cnki", "知网"],
        "home": "https://www.cnki.net/", "entry": "https://kns.cnki.net/",
        "entries": [{"label": "知网首页", "url": "https://www.cnki.net/"}],
        "lang": "zh", "kind": "fulltext", "access": "ip", "access_label": "校园 IP",
        "fulltext": "subscription", "fulltext_label": "学校订阅", "adapter": "cnki",
        "selectors": {}, "order": 2, "selectable": True, "default": True, "note": "",
    },
]


def registry_path(path: str | Path | None = None) -> Path:
    if path:
        return Path(path)
    env = os.environ.get("LIT_PLATFORMS_FILE")
    return Path(env) if env else lc.SKILL_ROOT / "platforms.json"


def learned_path(path: str | Path | None = None) -> Path:
    if path:
        return Path(path)
    env = os.environ.get("LIT_PLATFORMS_LEARNED")
    return Path(env) if env else lc.SKILL_ROOT / "platform_learned.json"


def local_path() -> Path:
    """本机专属平台（机构 appId、内网入口等），默认不进版本库。"""
    env = os.environ.get("LIT_PLATFORMS_LOCAL")
    return Path(env) if env else lc.SKILL_ROOT / "platforms.local.json"


def _load_local() -> list[dict]:
    path = local_path()
    if not path.exists():
        return []
    try:
        data = json.loads(lc.read_text_auto(path))
    except json.JSONDecodeError:
        return []
    items = data.get("platforms") if isinstance(data, dict) else data
    return [p for p in (items or []) if isinstance(p, dict) and p.get("id")]


def _ensure_registry(path: Path) -> Path:
    """缺失时从 example 复制；再缺就写一份最小注册表，保证主流程不因配置崩掉。"""
    if path.exists():
        return path
    example = path.with_name("platforms.example.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    if example.exists():
        shutil.copyfile(example, path)
    else:
        path.write_text(
            json.dumps({"version": 1, "platforms": MINIMAL}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    return path


def load(path: str | Path | None = None) -> dict:
    target = _ensure_registry(registry_path(path))
    try:
        data = json.loads(lc.read_text_auto(target))
    except json.JSONDecodeError as exc:
        raise SystemExit(f"[平台] {target} 不是合法 JSON：{exc}")
    if not isinstance(data.get("platforms"), list) or not data["platforms"]:
        data = {"version": 1, "platforms": list(MINIMAL)}
    if path is None:
        merged = {p["id"]: p for p in data["platforms"] if isinstance(p, dict) and p.get("id")}
        for item in _load_local():
            merged[item["id"]] = {**merged.get(item["id"], {}), **item}
        data = {"version": data.get("version", 1), "platforms": list(merged.values())}
    data["_path"] = str(target)
    return data


def save(data: dict, path: str | Path | None = None) -> Path:
    target = registry_path(path)
    payload = {k: v for k, v in data.items() if not k.startswith("_")}
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return target


def all_platforms(path: str | Path | None = None) -> list[dict]:
    items = [p for p in load(path)["platforms"] if isinstance(p, dict) and p.get("id")]
    return sorted(items, key=lambda p: (p.get("order", 99), p.get("short") or p["id"]))


def selectable(path: str | Path | None = None) -> list[dict]:
    return [p for p in all_platforms(path) if p.get("selectable", True)]


def _norm(text: str) -> str:
    return re.sub(r"\s+", "", str(text or "")).lower()


def get(pid: str, path: str | Path | None = None, customs: list[dict] | None = None) -> dict | None:
    key = str(pid or "").strip()
    if not key:
        return None
    for p in (customs or []):
        if p.get("id") == key:
            return p
    for p in all_platforms(path):
        if p["id"] == key:
            return p
    low = key.lower()
    for p in all_platforms(path):
        names = [p.get("id", ""), p.get("name", ""), p.get("short", "")] + list(p.get("aliases") or [])
        if any(low == str(n).lower() for n in names if n):
            return p
    return None


def host_of(url: str) -> str:
    text = str(url or "").strip()
    if not text:
        return ""
    if "://" not in text:
        text = "http://" + text
    netloc = urlparse(text).netloc.lower()
    if netloc.startswith("www."):
        netloc = netloc[4:]
    return netloc.split(":")[0]


def resolve(text: str, path: str | Path | None = None, customs: list[dict] | None = None) -> dict | None:
    """按 id / 名称 / 别名 / 网址主机 解析平台。"""
    found = get(text, path, customs)
    if found:
        return found
    host = host_of(text)
    if not host:
        return None
    for p in (customs or []) + all_platforms(path):
        hosts = {host_of(p.get("home", "")), host_of(p.get("entry", ""))}
        for entry in p.get("entries") or []:
            hosts.add(host_of(entry.get("url", "")))
        hosts.discard("")
        for candidate in hosts:
            if host == candidate or host.endswith("." + candidate) or candidate.endswith("." + host):
                return p
    return None


def questionnaire_options(path: str | Path | None = None) -> list[dict]:
    return [
        {
            "label": f"{p.get('short') or p['id']}｜{p.get('access_label', '')}｜{p.get('fulltext_label', '')}",
            "value": p["id"],
        }
        for p in selectable(path)
    ]


def inline_custom(url: str, name: str = "", note: str = "用户提供的网址，能力以探测结果为准") -> dict:
    """把任意网址当场变成一个平台定义；不写注册表，随任务 JSON 走。"""
    url = str(url or "").strip()
    host = host_of(url) or "custom"
    slug = re.sub(r"[^a-z0-9]+", "-", host.lower()).strip("-") or "site"
    short = name.strip() or host
    return {
        "id": f"custom-{slug[:32]}",
        "name": name.strip() or host,
        "short": short[:16],
        "aliases": [],
        "home": url,
        "entry": url,
        "entries": [{"label": "用户提供", "url": url}],
        "lang": "zh" if host.endswith(".cn") or host.endswith(".edu.cn") else "en",
        "kind": "unknown",
        "access": "unknown",
        "access_label": "以探测为准",
        "fulltext": "unknown",
        "fulltext_label": "以探测为准",
        "adapter": "generic",
        "selectors": {},
        "order": 90,
        "selectable": False,
        "custom": True,
        "note": note,
    }


def add_custom(url: str, name: str = "", path: str | Path | None = None,
               local: bool = False) -> tuple[dict, bool]:
    """登记自定义平台。返回 (平台, 是否新建)；同主机已存在则直接返回已有条目。

    local=True 写入 platforms.local.json（不进版本库），用于机构专属入口。
    """
    existing = resolve(url, path)
    if existing:
        return existing, False
    item = inline_custom(url, name)
    if local:
        target = local_path()
        items = _load_local()
        used = {p["id"] for p in items} | {p["id"] for p in load(path)["platforms"]}
        base, index = item["id"], 2
        while item["id"] in used:
            item["id"] = f"{base}-{index}"
            index += 1
        items.append(item)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps({"version": 1, "platforms": items}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return item, True
    data = load(path)
    used = {p["id"] for p in data["platforms"]}
    base, index = item["id"], 2
    while item["id"] in used:
        item["id"] = f"{base}-{index}"
        index += 1
    data["platforms"].append(item)
    save(data, path)
    return item, True


def learn(pid: str, selectors: dict, path: str | Path | None = None, source: str = "") -> Path:
    """保存探测学到的选择器；按平台 id 合并，不覆盖人工写的 platforms.json。"""
    target = learned_path()
    data = {}
    if target.exists():
        try:
            data = json.loads(lc.read_text_auto(target))
        except json.JSONDecodeError:
            data = {}
    data.setdefault("platforms", {})[pid] = {
        "selectors": selectors or {},
        "source": source,
        "learned_at": __import__("datetime").datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    target.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return target


def merge_learned(platform: dict, path: str | Path | None = None) -> dict:
    """把 platform_learned.json 里的选择器并入平台定义（平台自带优先）。"""
    merged = dict(platform)
    merged["selectors"] = dict(platform.get("selectors") or {})
    target = learned_path(path)
    if not target.exists():
        return merged
    try:
        data = json.loads(lc.read_text_auto(target))
    except json.JSONDecodeError:
        return merged
    learned = ((data.get("platforms") or {}).get(platform.get("id")) or {}).get("selectors") or {}
    for key, value in learned.items():
        if key not in merged["selectors"] or not merged["selectors"].get(key):
            merged["selectors"][key] = value
    return merged


def _alias_hit(name: str, lowered: str) -> bool:
    """别名是否真的出现在清单里。

    拉丁别名按词边界匹配，且至少 3 个字符——否则 "SD"/"CAS" 会在任何页面蒙中
    （"CAS 号"、"SD 卡"）。CJK 别名按子串匹配。
    """
    s = str(name or "").strip().lower()
    if not s:
        return False
    if re.fullmatch(r"[a-z0-9 ._-]+", s):
        if len(s) < 3:
            return False
        pattern = r"(?<![a-z0-9])" + re.escape(s) + r"(?![a-z0-9])"
        return re.search(pattern, lowered) is not None
    return s in lowered


def parse_school_list(text: str, path: str | Path | None = None,
                      customs: list[dict] | None = None) -> dict:
    """从学校清单文本里识别平台与网址。

    返回 {"matched": [平台id], "unknown": [网址], "hits": {平台id: 命中词}}。
    识别依据：① 清单里的网址主机；② 平台名/别名按词边界出现在文本里。
    """
    raw = str(text or "")
    lowered = raw.lower()
    matched: list[str] = []
    unknown: list[str] = []
    hits: dict[str, str] = {}
    registry = all_platforms(path)

    for url in dict.fromkeys(URL_RE.findall(raw)):
        platform = resolve(url, path, customs)
        if platform:
            if platform["id"] not in matched:
                matched.append(platform["id"])
                hits[platform["id"]] = url
        elif url not in unknown:
            unknown.append(url.rstrip(".,;"))

    for platform in registry:
        names = [platform.get("name"), platform.get("short")] + list(platform.get("aliases") or [])
        for name in names:
            if _alias_hit(name, lowered) and platform["id"] not in matched:
                matched.append(platform["id"])
                hits[platform["id"]] = name
                break

    # 自定义平台也参与名称匹配
    for platform in customs or []:
        for name in [platform.get("name"), platform.get("short")]:
            if _alias_hit(name, lowered) and platform["id"] not in matched:
                matched.append(platform["id"])
                hits[platform["id"]] = name
                break

    names = []
    for pid in matched:
        platform = get(pid, path, customs)
        if platform:
            names.append(platform.get("short") or pid)
    return {"matched": matched, "unknown": unknown, "names": names, "hits": hits}


def _strip_html(text: str) -> str:
    return ANY_TAG_RE.sub(" ", TAG_RE.sub(" ", text))


def read_school_list(source: str, timeout: int = 30) -> tuple[str, str]:
    """读学校清单：支持 http(s) 网址、txt/html/htm 文件。返回 (文本, 错误说明)。"""
    source = str(source or "").strip().strip('"')
    if not source:
        return "", "空路径"
    if source.lower().startswith(("http://", "https://")):
        try:
            session = lc.build_session()
            resp = session.get(source, timeout=timeout)
            if resp.status_code >= 400:
                return "", f"HTTP {resp.status_code}"
            resp.encoding = resp.apparent_encoding or resp.encoding
            return _strip_html(resp.text), ""
        except Exception as exc:  # noqa: BLE001
            return "", f"{type(exc).__name__}: {exc}"
    path = Path(source)
    if not path.exists():
        return "", f"文件不存在：{path}"
    suffix = path.suffix.lower()
    if suffix in (".pdf", ".doc", ".docx"):
        return "", "暂不支持直接读 PDF/Word，请把数据库清单页另存为 txt/html，或直接给图书馆页面的网址"
    for encoding in ("utf-8-sig", "utf-8", "gbk", "gb18030"):
        try:
            text = path.read_text(encoding=encoding)
            return (_strip_html(text) if suffix in (".html", ".htm") else text), ""
        except UnicodeDecodeError:
            continue
        except OSError as exc:
            return "", f"{type(exc).__name__}: {exc}"
    return "", "编码无法识别（试过 utf-8/gbk）"


def import_school_list(source: str, path: str | Path | None = None,
                       customs: list[dict] | None = None) -> dict:
    text, error = read_school_list(source)
    if error:
        return {"matched": [], "unknown": [], "names": [], "hits": {},
                "error": error, "source": source}
    result = parse_school_list(text, path, customs)
    result["error"] = ""
    result["source"] = source
    return result


def _table(items: list[dict]) -> None:
    print(f"{'ID':<16}{'名称':<28}{'访问':<22}{'全文':<30}")
    print("-" * 98)
    for p in items:
        print(
            f"{p['id'][:15]:<16}{(p.get('short') or '')[:26]:<28}"
            f"{(p.get('access_label') or '')[:20]:<22}{(p.get('fulltext_label') or '')[:28]:<30}"
        )


def main() -> int:
    lc.setup_console()
    parser = argparse.ArgumentParser(description="文献平台注册表（内置规则 6 平台 + 自定义网址）")
    parser.add_argument("--list", action="store_true", help="列出平台")
    parser.add_argument("--show", help="查看平台完整定义")
    parser.add_argument("--add", help="登记自定义平台网址")
    parser.add_argument("--name", help="自定义平台名称（配合 --add）")
    parser.add_argument("--local", action="store_true",
                        help="写入 platforms.local.json（机构专属入口，不进版本库）")
    parser.add_argument("--parse", help="解析学校公布的数据库清单（文件或网址）")
    parser.add_argument("--learn", help="从 平台探测.json 学习选择器")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    parser.add_argument("--platforms-file", help="覆盖 platforms.json 路径")
    args = parser.parse_args()
    lc.print_env()

    path = args.platforms_file
    if args.list or not any([args.show, args.add, args.parse, args.learn]):
        items = all_platforms(path)
        if args.json:
            print(json.dumps(items, ensure_ascii=False, indent=2))
        else:
            _table(items)
        return lc.EXIT_OK

    if args.show:
        item = get(args.show, path)
        if not item:
            lc.die(lc.EXIT_CONFIG, f"没有这个平台：{args.show}")
        print(json.dumps(item, ensure_ascii=False, indent=2))
        return lc.EXIT_OK

    if args.add:
        item, created = add_custom(args.add, args.name or "", path, local=args.local)
        where = "platforms.local.json（本机，不入库）" if args.local else "platforms.json"
        print(f"{'已登记' if created else '已存在'}：{item['id']}｜{item['name']}｜{item['entry']}")
        print(f"写入：{where}")
        if created:
            print("下一步：python scripts\\probe_platform.py --platform "
                  f"{item['id']} --keyword \"你的关键词\" --save")
        return lc.EXIT_OK

    if args.parse:
        result = import_school_list(args.parse, path)
        if result["error"]:
            lc.die(lc.EXIT_CONFIG, f"读取学校清单失败：{result['error']}")
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            print(f"清单来源：{result['source']}")
            print(f"识别到平台：{'、'.join(result['names']) or '（无）'}")
            for pid in result["matched"]:
                item = get(pid, path)
                short = (item.get("short") if item else "") or ""
                hit = (result.get("hits") or {}).get(pid) or ""
                same = hit.lower() in (pid.lower(), short.lower())
                mark = f"（命中：{hit}）" if hit and not same else ""
                print(f"  + {pid:<16}{short}{mark}")
            if result["unknown"]:
                print(f"未识别的网址 {len(result['unknown'])} 个（可逐个 --add 登记）：")
                for url in result["unknown"]:
                    print(f"  ? {url}")
                print("先登记第一个（id 用下面打印出来的，后续 --platforms / --platform 都用它）：")
                print(f'  python scripts\\platforms.py --add "{result["unknown"][0]}" --name "学校自建库"')
        return lc.EXIT_OK if not result["unknown"] else lc.EXIT_PARTIAL

    if args.learn:
        data = lc.read_json(args.learn, default={}) or {}
        items = data if isinstance(data, list) else [data]
        count = 0
        for item in items:
            pid = item.get("platform") or item.get("id")
            selectors = item.get("selectors_learned") or {}
            if pid and selectors:
                learn(pid, selectors, source=str(args.learn))
                print(f"已学习：{pid}（{len(selectors)} 组选择器）")
                count += 1
        if not count:
            print("没有可学习的 selectors_learned；先跑 probe_platform.py --save")
            return lc.EXIT_PARTIAL
        return lc.EXIT_OK
    return lc.EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
