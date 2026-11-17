# -*- coding: utf-8 -*-
"""platform_util.py —— 通用平台操作层（探测 / 检索 / 下载共用）

不绑定具体数据库，靠「平台自带选择器 → 全局启发式」两级策略工作：
    登录识别与填表、检索框定位、提交检索、结果列表抽取、详情字段解析（meta/JSON-LD/选择器）、
    PDF 入口与付费墙识别、CSS 路径学习。

实测教训（勿改）：
  1. 判断登录只看「可见的 password 输入框」，不要看页面里有没有“登录”两个字——
     几乎所有中文库的导航条都有“登录”。
  2. 有些平台在 iframe 里登录，必须遍历 page.frames。
  3. 结果列表优先用平台选择器；通用启发式只做兜底，宁少不滥（阈值 3 分）。
  4. 详情字段优先 meta（citation_* / DC.*）与 JSON-LD，其次页面选择器；取不到留空，绝不编造。
  5. 所有元素操作都要 try/except：SPA 重渲染会让 ElementHandle 失效。
"""
from __future__ import annotations

import json
import re
import time
from urllib.parse import urljoin

import litcommon as lc

YEAR_RE = re.compile(r"(?:19|20)\d{2}")
DOI_RE = re.compile(r"10\.\d{4,9}/[^\s\"'<>,;]+")
SPACE_RE = re.compile(r"\s+")

LOGIN_TEXTS = (
    "sign in", "log in", "login", "登录", "用户名", "密码", "password",
    "institutional sign in", "机构登录",
)
HARD_LOGIN_TEXTS = (
    "请先登录", "登录后查看", "登录后下载", "sign in to continue", "sign in to access",
    "please sign in", "log in to access", "institutional sign in",
)
PAYWALL_TEXTS = (
    "get access", "purchase pdf", "buy article", "buy now", "purchase article",
    "subscribe now", "无权限", "立即购买", "付费下载", "购买全文", "订阅后查看",
    "access denied", "not entitled", "暂无权限",
)
NAV_TEXTS = {
    "首页", "登录", "注册", "搜索", "检索", "高级检索", "更多", "查看全部", "期刊导航",
    "帮助", "关于我们", "联系我们", "隐私政策", "使用条款", "english", "中文",
    "next", "previous", "help", "about", "contact", "privacy", "terms", "home",
}

GENERIC_SEARCH_INPUTS = [
    "input#qs",
    "input[type='search']",
    "input[name='q']",
    "input[name='query']",
    "input[name='search']",
    "input[name='searchText']",
    "input[name='keyword']",
    "input[id='q']",
    "input[id*='search']",
    "input[id*='Search']",
    "input[placeholder*='搜索']",
    "input[placeholder*='检索']",
    "input[placeholder*='Search']",
    "input[placeholder*='search']",
    "textarea[placeholder*='检索']",
    "textarea[placeholder*='搜索']",
    "form input[type='text']",
]
GENERIC_SEARCH_BUTTONS = [
    "button[type='submit']",
    "input[type='submit']",
    "button:has-text('搜索')",
    "button:has-text('检索')",
    "button:has-text('Search')",
    "a:has-text('搜索')",
    "[class*='search-btn']",
    "[class*='searchBtn']",
]
USER_INPUT_SELECTORS = [
    "input[type='email']",
    "input[name='username']",
    "input[name='user']",
    "input[name='email']",
    "input[name='login']",
    "input[id*='user']",
    "input[id*='email']",
    "input[autocomplete='username']",
    "input[type='text']",
]
SUBMIT_SELECTORS = [
    "button[type='submit']",
    "input[type='submit']",
    "button:has-text('登录')",
    "button:has-text('登 录')",
    "button:has-text('Sign in')",
    "button:has-text('Log in')",
    "a:has-text('登录')",
]


# ---------------------------------------------------------------- 基础

def clean(text: str) -> str:
    return SPACE_RE.sub(" ", str(text or "")).strip()


def is_visible(element) -> bool:
    try:
        return bool(element and element.is_visible())
    except Exception:
        return False


def sel_list(selectors: dict | None, key: str) -> list[str]:
    """兼容两种写法：{"pdf_button": [...]} 与 {"detail": {"pdf_button": [...]}}。"""
    selectors = selectors or {}
    value = selectors.get(key)
    if isinstance(value, list):
        return [str(x) for x in value if x]
    detail = selectors.get("detail") or {}
    value = detail.get(key)
    if isinstance(value, list):
        return [str(x) for x in value if x]
    if isinstance(value, str):
        return [value]
    return []


def find_first(page, selectors: list[str], require_visible: bool = True):
    for selector in selectors or []:
        try:
            elements = page.query_selector_all(selector)
        except Exception:
            continue
        for element in elements:
            if not require_visible or is_visible(element):
                return element
    return None


def find_first_with_selector(page, selectors: list[str]):
    for selector in selectors or []:
        try:
            elements = page.query_selector_all(selector)
        except Exception:
            continue
        for element in elements:
            if is_visible(element):
                return element, selector
    return None, ""


def page_signature(page) -> tuple[str, str]:
    try:
        return page.url, page.title()
    except Exception:
        return "", ""


def css_path(element) -> str:
    """给探测学到的元素生成稳定 CSS 路径；失败返回空串。"""
    script = """
    el => {
      const esc = (s) => (window.CSS && CSS.escape) ? CSS.escape(s) : s.replace(/[^a-zA-Z0-9_-]/g, '\\\\$&');
      if (!el || el.nodeType !== 1) return '';
      if (el.id) return '#' + esc(el.id);
      const parts = [];
      let node = el;
      while (node && node.nodeType === 1 && parts.length < 5) {
        let sel = node.tagName.toLowerCase();
        if (node.getAttribute && node.getAttribute('name')) {
          sel += '[name="' + node.getAttribute('name').replace(/"/g, '') + '"]';
        } else if (node.className && typeof node.className === 'string') {
          const cls = node.className.trim().split(/\\s+/).slice(0, 2).filter(Boolean);
          if (cls.length) sel += cls.map(c => '.' + esc(c)).join('');
        }
        const parent = node.parentElement;
        if (parent) {
          const same = Array.from(parent.children).filter(c => c.tagName === node.tagName);
          if (same.length > 1) sel += ':nth-of-type(' + (Array.prototype.indexOf.call(parent.children, node) + 1) + ')';
        }
        parts.unshift(sel);
        if (node.id) break;
        node = node.parentElement;
      }
      return parts.join(' > ');
    }
    """
    try:
        return element.evaluate(script) or ""
    except Exception:
        return ""


# ---------------------------------------------------------------- 登录

def detect_login(page) -> dict:
    """返回 {required, password_input, frame, signals, url}。frame 仅内部使用。"""
    frames = []
    try:
        frames = [page.main_frame] + [f for f in page.frames if f != page.main_frame]
    except Exception:
        frames = [page]
    for frame in frames:
        try:
            inputs = frame.query_selector_all("input[type='password']")
        except Exception:
            continue
        for element in inputs:
            if is_visible(element):
                return {
                    "required": True,
                    "password_input": True,
                    "frame": frame,
                    "signals": ["password-input"],
                    "url": page.url,
                }
    try:
        html = page.content().lower()
    except Exception:
        html = ""
    hard = [t for t in HARD_LOGIN_TEXTS if t in html]
    soft = [t for t in LOGIN_TEXTS if t in html]
    return {
        "required": bool(hard),
        "password_input": False,
        "frame": None,
        "signals": hard or soft,
        "url": page.url,
    }


def _find_user_input(frame):
    for selector in USER_INPUT_SELECTORS:
        try:
            elements = frame.query_selector_all(selector)
        except Exception:
            continue
        for element in elements:
            if not is_visible(element):
                continue
            try:
                placeholder = (element.get_attribute("placeholder") or "").lower()
                input_type = (element.get_attribute("type") or "text").lower()
            except Exception:
                continue
            if input_type in ("password", "hidden", "checkbox", "radio", "submit"):
                continue
            if any(word in placeholder for word in ("搜索", "检索", "search")):
                continue
            return element
    return None


def login_with_credentials(page, creds: dict, log=None) -> tuple[bool, str]:
    """用保存的账号密码登录。返回 (成功, 方式/原因)。"""
    info = detect_login(page)
    if not info["required"]:
        return True, "not-needed"
    username = (creds or {}).get("username") or ""
    password = (creds or {}).get("password") or ""
    if not username or not password:
        return False, "no-credentials"

    frame = info.get("frame") or page
    try:
        pwd_inputs = [e for e in frame.query_selector_all("input[type='password']") if is_visible(e)]
    except Exception:
        pwd_inputs = []
    if not pwd_inputs:
        return False, "no-password-field"
    pwd_input = pwd_inputs[0]
    user_input = _find_user_input(frame)
    if user_input is None:
        return False, "no-username-field"

    try:
        user_input.click()
        user_input.fill("")
        user_input.fill(username)
        pwd_input.click()
        pwd_input.fill("")
        pwd_input.fill(password)
    except Exception as exc:  # noqa: BLE001
        if log:
            log(f"[登录] 填表失败：{type(exc).__name__}: {exc}")
        return False, "fill-failed"

    button = find_first(frame, SUBMIT_SELECTORS)
    try:
        if button is not None:
            button.click()
        else:
            pwd_input.press("Enter")
    except Exception as exc:  # noqa: BLE001
        if log:
            log(f"[登录] 提交失败：{type(exc).__name__}: {exc}")
        return False, "submit-failed"

    deadline = time.time() + 35
    while time.time() < deadline:
        time.sleep(2)
        if not detect_login(page)["required"]:
            if log:
                log("[登录] 已通过（保存的账号）")
            return True, "stored-credentials"
    try:
        html = page.content().lower()
    except Exception:
        html = ""
    for hint in ("incorrect", "invalid", "错误", "失败", "验证码", "captcha", "locked"):
        if hint in html:
            return False, f"login-rejected:{hint}"
    return False, "login-timeout"


def wait_manual_login(page, cfg: dict, log, seconds: int = 0, platform_name: str = "") -> bool:
    """让人在可见浏览器里手动登录；轮询直到登录框消失或超时。"""
    wait_sec = int(seconds or (cfg.get("platforms") or {}).get("wait_login_sec", 180))
    if wait_sec <= 0:
        return False
    print("\n" + "=" * 62)
    print(f"{platform_name or '该平台'} 需要登录。请在弹出的浏览器窗口里完成登录"
          "（含验证码/二次验证），脚本会自动继续。")
    print(f"最多等 {wait_sec} 秒；完成后无需回车。")
    print("=" * 62)
    deadline = time.time() + wait_sec
    while time.time() < deadline:
        time.sleep(2)
        try:
            if not detect_login(page)["required"]:
                if log:
                    log("[登录] 人工登录完成，继续。")
                return True
        except Exception:
            continue
    if log:
        log(f"[登录] 人工登录等待超时（{wait_sec} 秒）")
    return False


# ---------------------------------------------------------------- 检索

def find_search_box(page, selectors: dict | None = None):
    selectors = selectors or {}
    order = sel_list(selectors, "search_input") + GENERIC_SEARCH_INPUTS
    seen = set()
    for selector in order:
        if selector in seen:
            continue
        seen.add(selector)
        try:
            elements = page.query_selector_all(selector)
        except Exception:
            continue
        for element in elements:
            if not is_visible(element):
                continue
            try:
                input_type = (element.get_attribute("type") or "text").lower()
                disabled = element.get_attribute("disabled")
            except Exception:
                continue
            if input_type in ("password", "hidden", "checkbox", "radio", "submit") or disabled:
                continue
            return element, selector
    return None, ""


def submit_search(context, page, box, query: str, selectors: dict | None = None, log=None):
    """填检索词并提交；返回实际承载结果页的 page（可能新开标签页）。"""
    selectors = selectors or {}
    before_pages = list(context.pages)
    before_sig = page_signature(page)
    try:
        box.scroll_into_view_if_needed()
        box.click()
        box.fill("")
        box.fill(str(query))
    except Exception as exc:  # noqa: BLE001
        if log:
            log(f"[检索] 填写检索词失败：{type(exc).__name__}: {exc}")
        return page
    try:
        page.keyboard.press("Enter")
    except Exception:
        pass

    time.sleep(2.5)
    new_pages = [p for p in context.pages if p not in before_pages]
    if new_pages:
        target = new_pages[-1]
        try:
            target.wait_for_load_state("domcontentloaded", timeout=20000)
        except Exception:
            pass
        time.sleep(1.5)
        if log:
            log(f"[检索] 结果在新标签页打开：{target.url[:100]}")
        return target

    try:
        page.wait_for_load_state("domcontentloaded", timeout=20000)
    except Exception:
        pass
    time.sleep(1.5)
    if page_signature(page) != before_sig:
        return page

    # 回车没反应：点检索按钮
    button = find_first(page, sel_list(selectors, "search_button") + GENERIC_SEARCH_BUTTONS)
    if button is not None:
        try:
            button.click()
            try:
                page.wait_for_load_state("domcontentloaded", timeout=20000)
            except Exception:
                pass
            time.sleep(2)
            new_pages = [p for p in context.pages if p not in before_pages]
            if new_pages:
                return new_pages[-1]
        except Exception as exc:  # noqa: BLE001
            if log:
                log(f"[检索] 点击检索按钮失败：{type(exc).__name__}: {exc}")
    return page


# ---------------------------------------------------------------- 结果列表

def _items_from_elements(elements, base_url: str, limit: int) -> list[dict]:
    items, seen = [], set()
    for element in elements:
        try:
            href = element.get_attribute("href") or ""
            text = clean(element.inner_text())
        except Exception:
            continue
        if not href or not text or href.startswith(("javascript:", "#", "mailto:")):
            continue
        url = urljoin(base_url, href)
        if url in seen:
            continue
        seen.add(url)
        items.append({"标题": text, "url": url})
        if len(items) >= limit:
            break
    return items


def collect_results(page, selectors: dict | None, limit: int, log=None) -> list[dict]:
    """先平台选择器，再通用启发式。返回 [{标题, url}]。"""
    selectors = selectors or {}
    for selector in sel_list(selectors, "item_link"):
        try:
            elements = page.query_selector_all(selector)
        except Exception:
            continue
        items = _items_from_elements(elements, page.url, limit)
        if items:
            if log:
                log(f"[检索] 用平台选择器 {selector} 抓到 {len(items)} 条")
            return items

    try:
        anchors = page.query_selector_all("a[href]")
    except Exception:
        return []
    scored = []
    seen = set()
    for order, anchor in enumerate(anchors[:800]):
        try:
            href = anchor.get_attribute("href") or ""
            text = clean(anchor.inner_text())
            if not is_visible(anchor):
                continue
        except Exception:
            continue
        if not href or href.startswith(("#", "javascript:", "mailto:")):
            continue
        if len(text) < 8 or len(text) > 300:
            continue
        low = text.lower()
        if low in NAV_TEXTS:
            continue
        if len(text) < 20 and any(w in low for w in ("pdf", "全文", "下载", "download")):
            continue
        url = urljoin(page.url, href)
        if url in seen:
            continue
        try:
            parent_text = anchor.evaluate(
                "el => { const p = el.closest('li,article,tr'); "
                "if (p) return p.innerText; "
                "const d = el.closest('div'); return d ? d.innerText : el.innerText; }"
            ) or ""
        except Exception:
            parent_text = text
        parent_text = clean(parent_text)[:600]
        score = 0
        if YEAR_RE.search(parent_text):
            score += 3
        if re.search(r"article|detail|abstract|abs/|record|doi|view|paper|pdf", href.lower()):
            score += 2
        if len(text) >= 15:
            score += 1
        if len(text) >= 30:
            score += 1
        if re.search(r"[,，、;；]", parent_text) and re.search(r"[A-Za-z\u4e00-\u9fa5]{2,}", parent_text):
            score += 1
        if any(word in parent_text for word in ("摘要", "Abstract", "作者", "Author")):
            score += 1
        if len(re.findall(r"[A-Za-z\u4e00-\u9fa5]{2,}", text)) >= 3:
            score += 1
        if score < 3:
            continue
        seen.add(url)
        scored.append((score, order, text, url))

    scored.sort(key=lambda row: (-row[0], row[1]))
    items = [{"标题": row[2], "url": row[3]} for row in scored[:limit]]
    if log:
        log(f"[检索] 通用启发式抓到 {len(items)} 条（候选 {len(scored)}）")
    return items


# ---------------------------------------------------------------- 详情

META_KEYS = {
    "citation_title": "标题",
    "citation_author": "作者",
    "citation_journal_title": "期刊/来源",
    "citation_publication_date": "年份",
    "citation_doi": "DOI",
    "citation_abstract": "摘要",
    "citation_volume": "卷",
    "citation_issue": "期",
    "citation_firstpage": "页码",
    "citation_lastpage": "尾页",
    "citation_article_type": "文献类型",
    "dc.title": "标题",
    "dc.creator": "作者",
    "dc.description": "摘要",
    "dc.date": "年份",
    "dc.type": "文献类型",
    "description": "摘要",
    "keywords": "关键词",
}


def meta_tags(page) -> dict:
    script = """
    () => Array.from(document.querySelectorAll('meta')).map(m => ({
      key: (m.getAttribute('name') || m.getAttribute('property') || '').toLowerCase().trim(),
      content: (m.getAttribute('content') || '').trim()
    })).filter(x => x.key && x.content)
    """
    result: dict[str, list[str]] = {}
    try:
        for item in page.evaluate(script) or []:
            result.setdefault(item["key"], []).append(item["content"])
    except Exception:
        pass
    return result


def jsonld(page) -> dict:
    """从 JSON-LD 抓 headline/description/author/datePublished，兜底 meta 缺失。"""
    try:
        blocks = page.query_selector_all("script[type='application/ld+json']")
    except Exception:
        return {}
    for block in blocks:
        try:
            data = json.loads(block.inner_text() or "{}")
        except Exception:
            continue
        candidates = data if isinstance(data, list) else [data]
        for item in candidates:
            if isinstance(item, dict) and item.get("@graph"):
                candidates.extend(item["@graph"])
            if not isinstance(item, dict):
                continue
            if item.get("headline") or item.get("name") or item.get("description"):
                authors = item.get("author") or []
                if isinstance(authors, dict):
                    authors = [authors]
                names = []
                for author in authors:
                    if isinstance(author, dict) and author.get("name"):
                        names.append(author["name"])
                    elif isinstance(author, str):
                        names.append(author)
                return {
                    "标题": clean(item.get("headline") or item.get("name") or ""),
                    "摘要": clean(item.get("description") or ""),
                    "作者": "; ".join(names[:20]),
                    "年份": clean(str(item.get("datePublished") or ""))[:4],
                    "期刊/来源": clean((item.get("isPartOf") or {}).get("name") if isinstance(item.get("isPartOf"), dict) else ""),
                }
    return {}


def _first_meta(meta: dict, *keys: str) -> str:
    for key in keys:
        values = meta.get(key) or []
        if values:
            return clean(values[0])
    return ""


def _all_meta(meta: dict, *keys: str) -> list[str]:
    values = []
    for key in keys:
        values.extend(meta.get(key) or [])
    return [clean(v) for v in values if clean(v)]


def extract_detail(page, selectors: dict | None, platform: dict | None = None, log=None) -> dict:
    selectors = selectors or {}
    platform = platform or {}
    meta = meta_tags(page)
    ld = jsonld(page)

    title = (_first_meta(meta, "citation_title", "dc.title") or ld.get("标题") or "")
    if not title:
        element = find_first(page, sel_list(selectors, "title") + ["h1", ".article-title", "h1.title"])
        if element:
            try:
                title = clean(element.inner_text())
            except Exception:
                title = ""
    if not title:
        try:
            title = clean(page.title())
        except Exception:
            title = ""

    authors = "; ".join(_all_meta(meta, "citation_author", "dc.creator")[:20]) or ld.get("作者") or ""
    if not authors:
        elements = []
        for selector in sel_list(selectors, "authors") or ["[class*='author'] a"]:
            try:
                elements = page.query_selector_all(selector)
            except Exception:
                continue
            if elements:
                break
        names = []
        for element in elements[:20]:
            try:
                name = clean(element.inner_text())
            except Exception:
                continue
            if name:
                names.append(name)
        authors = "; ".join(names)

    journal = _first_meta(meta, "citation_journal_title") or ld.get("期刊/来源") or ""
    if not journal:
        element = find_first(page, sel_list(selectors, "source"))
        if element:
            try:
                journal = clean(element.inner_text())
            except Exception:
                journal = ""

    date_text = _first_meta(meta, "citation_publication_date", "dc.date") or ld.get("年份") or ""
    year_match = YEAR_RE.search(date_text)
    year = year_match.group(0) if year_match else ""

    abstract = _first_meta(meta, "citation_abstract", "dc.description") or ld.get("摘要") or ""
    if len(abstract) < 80:
        fallback = _first_meta(meta, "description")
        if len(fallback) > len(abstract) and len(fallback) >= 80:
            abstract = fallback
    if len(abstract) < 40:
        element = find_first(
            page,
            sel_list(selectors, "abstract")
            + ["#ChDivSummary", ".abstract-text", "section.abstract", "[class*='abstract']", "#abstract"],
        )
        if element:
            try:
                abstract = clean(element.text_content() or element.inner_text())
            except Exception:
                abstract = ""

    keywords = "; ".join(_all_meta(meta, "keywords"))
    if not keywords:
        elements = []
        for selector in sel_list(selectors, "keywords") or [".keywords", "[class*='keyword']"]:
            try:
                elements = page.query_selector_all(selector)
            except Exception:
                continue
            if elements:
                break
        words = []
        for element in elements:
            try:
                raw = clean(element.text_content() or element.inner_text())
            except Exception:
                continue
            for word in re.split(r"[;；,，]", raw):
                word = word.strip()
                if word and word not in words:
                    words.append(word)
        keywords = "; ".join(words)

    doi = _first_meta(meta, "citation_doi")
    if not doi:
        try:
            match = DOI_RE.search(page.content())
            doi = match.group(0).rstrip(".") if match else ""
        except Exception:
            doi = ""

    volume = _first_meta(meta, "citation_volume")
    issue = _first_meta(meta, "citation_issue")
    pages = _first_meta(meta, "citation_firstpage")
    last = _first_meta(meta, "citation_lastpage")
    if pages and last:
        pages = f"{pages}-{last}"
    doc_type = _first_meta(meta, "citation_article_type", "dc.type")

    return {
        "标题": title,
        "作者": authors,
        "期刊/来源": journal,
        "年份": year,
        "卷": volume,
        "期": issue,
        "页码": pages,
        "DOI": doi,
        "PMID": "",
        "来源库": platform.get("short") or platform.get("name") or "",
        "文献类型": doc_type,
        "摘要": abstract,
        "关键词": keywords,
    }


def pdf_targets(page, selectors: dict | None = None) -> tuple[list[dict], list]:
    """返回 (targets, elements)。targets 里 href 为空表示必须点按钮。"""
    selectors = selectors or {}
    targets, elements, seen = [], [], set()

    def add(element, href: str, text: str, selector: str):
        href = urljoin(page.url, href) if href else ""
        key = href or f"btn::{text}"
        if key in seen:
            return
        low = text.lower()
        if any(word in low for word in ("purchase", "buy ", "购买", "订阅")):
            return
        seen.add(key)
        targets.append({
            "kind": "link" if href else "button",
            "href": href,
            "text": text,
            "selector": selector,
        })
        elements.append(element)

    for selector in sel_list(selectors, "pdf_button"):
        try:
            found = page.query_selector_all(selector)
        except Exception:
            continue
        for element in found:
            if not is_visible(element):
                continue
            try:
                href = element.get_attribute("href") or ""
                text = clean(element.inner_text()) or clean(element.get_attribute("aria-label") or "") or "PDF"
            except Exception:
                continue
            add(element, href, text, selector)

    try:
        anchors = page.query_selector_all("a[href]")
    except Exception:
        anchors = []
    for element in anchors[:600]:
        if not is_visible(element):
            continue
        try:
            href = element.get_attribute("href") or ""
            text = clean(element.inner_text())
        except Exception:
            continue
        low_href = href.lower()
        low_text = text.lower()
        if any(word in low_text for word in ("purchase", "buy ", "购买", "订阅")):
            continue
        hit = (
            ".pdf" in low_href
            or "pdfft" in low_href
            or "pdfdownload" in low_href
            or (re.search(r"\bpdf\b", low_text) is not None and len(text) <= 30)
            or low_text in ("全文", "下载全文", "全文下载", "download pdf", "view pdf", "open pdf")
        )
        if hit:
            add(element, href, text or "PDF", "generic")

    return targets[:8], elements[:8]


def paywall_hint(page) -> str:
    try:
        html = page.content().lower()
    except Exception:
        return ""
    for hint in PAYWALL_TEXTS:
        if hint in html:
            return hint
    return ""


def institution_seen(page, name: str) -> bool:
    if not name:
        return False
    try:
        html = page.content()
    except Exception:
        return False
    return name in html or name.lower() in html.lower()
