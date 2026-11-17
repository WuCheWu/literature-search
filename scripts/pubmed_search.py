# -*- coding: utf-8 -*-
"""pubmed_search.py —— PubMed E-utilities 检索与解析

用途：esearch 取 PMID，efetch 取详情，写 records_pubmed.json。
参数：
    --query "检索式"       必填
    --limit N              覆盖配置 retmax
    --years N              覆盖配置 years（近 N 年）
    --run-dir DIR          输出目录（默认按 output_root/关键词_日期 生成）
    --out FILE             直接指定输出 JSON
    --config PATH          配置文件
    --json                 把结果打到 stdout（不写文件）
退出码：0 成功 / 1 网络或接口失败 / 3 配置错误
依赖：requests（必须 trust_env=False）、标准库
注意：curl / Invoke-WebRequest 探测 PubMed 会因证书吊销检查卡 60 秒，禁止使用。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import litcommon as lc  # noqa: E402

EUTILS = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
TIMEOUT = 30
RETRIES = 3


def _request(session, url: str, params: dict):
    """429/5xx/网络错误退避重试：2/4/8 秒。"""
    last_exc = None
    for attempt in range(RETRIES):
        try:
            resp = session.get(url, params=params, timeout=TIMEOUT)
            if resp.status_code in (429, 500, 502, 503, 504):
                raise RuntimeError(f"HTTP {resp.status_code}")
            resp.raise_for_status()
            return resp
        except Exception as exc:  # noqa: BLE001 —— 要保留原始异常
            last_exc = exc
            if attempt < RETRIES - 1:
                time.sleep(2 * (2 ** attempt))
    raise RuntimeError(f"{type(last_exc).__name__}: {last_exc}")


def _text(node) -> str:
    return "".join(node.itertext()).strip() if node is not None else ""


def _authors(article) -> str:
    names = []
    for author in article.findall(".//AuthorList/Author"):
        collective = author.findtext("CollectiveName")
        if collective:
            names.append(collective.strip())
            continue
        last = (author.findtext("LastName") or "").strip()
        initials = (author.findtext("Initials") or "").strip()
        if last:
            names.append(f"{last} {initials}".strip())
    if not names:
        return ""
    return ", ".join(names[:10]) + (", et al" if len(names) > 10 else "")


def _pubdate(article) -> tuple[str, str, str]:
    issue = article.find(".//JournalIssue")
    if issue is None:
        return "", "", ""
    volume = (issue.findtext("Volume") or "").strip()
    issue_no = (issue.findtext("Issue") or "").strip()
    year = (issue.findtext("PubDate/Year") or "").strip()
    if not year:
        medline = (issue.findtext("PubDate/MedlineDate") or "").strip()
        year = medline[:4] if medline[:4].isdigit() else ""
    return year, volume, issue_no


def _abstract(article) -> str:
    parts = []
    for node in article.findall(".//Abstract/AbstractText"):
        text = _text(node)
        label = (node.get("Label") or "").strip()
        if text:
            parts.append(f"{label}: {text}" if label else text)
    return "\n".join(parts)


def _ids(node) -> tuple[str, str, str]:
    doi = pmcid = ""
    for item in node.findall(".//ArticleIdList/ArticleId"):
        id_type = (item.get("IdType") or "").lower()
        if id_type == "doi" and not doi:
            doi = _text(item)
        elif id_type in ("pmc", "pmcid") and not pmcid:
            pmcid = _text(item)
    if not doi:
        for eloc in node.findall(".//ELocationID"):
            if (eloc.get("EIdType") or "").lower() == "doi":
                doi = _text(eloc)
                break
    return doi, pmcid, ""


def parse_article(node) -> dict:
    citation = node.find(".//MedlineCitation")
    article = node.find(".//Article")
    if citation is None or article is None:
        return {}
    pmid = (citation.findtext("PMID") or "").strip()
    doi, pmcid, _ = _ids(node)
    year, volume, issue = _pubdate(article)
    pages = (article.findtext(".//Pagination/MedlinePgn") or "").strip()
    keywords = [_text(k) for k in node.findall(".//KeywordList/Keyword")]
    pubtypes = [_text(t) for t in node.findall(".//PublicationTypeList/PublicationType")]
    return {
        "标题": _text(article.find("ArticleTitle")).rstrip("."),
        "作者": _authors(article),
        "期刊/来源": (article.findtext(".//Journal/Title") or "").strip(),
        "年份": year,
        "卷": volume,
        "期": issue,
        "页码": pages,
        "DOI": doi,
        "PMID": pmid,
        "来源库": "PubMed",
        "文献类型": "; ".join(t for t in pubtypes if t),
        "摘要": _abstract(article),
        "关键词": "; ".join(k for k in keywords if k),
        "全文状态": "未下载",
        "本地文件": "",
        "原文链接": f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/" if pmid else "",
        "备注": "",
        "pmcid": pmcid,
        "source_db": "pubmed",
    }


def search_pubmed(cfg: dict, query: str, limit: int, years: int, log=None) -> tuple[int, list[dict], int]:
    """返回 (退出码, 记录, 命中总数)。"""
    session = lc.build_session()
    pubmed_cfg = cfg["databases"]["pubmed"]
    params = {
        "db": "pubmed",
        "term": query,
        "retmode": "json",
        "sort": "relevance",
        "retmax": max(1, min(int(limit), 50)),
        "tool": "literature-search",
    }
    if years and years > 0:
        params["datetype"] = "pdat"
        params["reldate"] = int(years) * 365
    if pubmed_cfg.get("email"):
        params["email"] = pubmed_cfg["email"]

    try:
        search_resp = _request(session, f"{EUTILS}/esearch.fcgi", params).json()
        result = search_resp.get("esearchresult", {})
        pmids = result.get("idlist", [])
        total = int(result.get("count", "0") or 0)
    except Exception as exc:  # noqa: BLE001
        if log:
            log(f"[PubMed] esearch 失败：{type(exc).__name__}: {exc}")
        return lc.EXIT_PARTIAL, [], 0

    if log:
        log(f"[PubMed] 命中 {total} 条，取 {len(pmids)} 条")
    if not pmids:
        return lc.EXIT_OK, [], total

    try:
        xml_resp = _request(
            session,
            f"{EUTILS}/efetch.fcgi",
            {"db": "pubmed", "id": ",".join(pmids), "retmode": "xml"},
        )
        root = ET.fromstring(xml_resp.content)
    except Exception as exc:  # noqa: BLE001
        if log:
            log(f"[PubMed] efetch 失败：{type(exc).__name__}: {exc}")
        return lc.EXIT_PARTIAL, [], total

    records = [rec for rec in (parse_article(n) for n in root.findall("PubmedArticle")) if rec]
    if log:
        log(f"[PubMed] 解析出 {len(records)} 条")
    return lc.EXIT_OK, records, total


def run(cfg, query, limit, years, run_dir: Path | None, log=None) -> tuple[int, list[dict]]:
    code, records, _ = search_pubmed(cfg, query, limit, years, log)
    if run_dir is not None:
        lc.write_json(run_dir / "records_pubmed.json", records)
        if log:
            log(f"[PubMed] 写入 {run_dir / 'records_pubmed.json'}")
    return code, records


def main() -> int:
    lc.setup_console()
    parser = argparse.ArgumentParser(description="PubMed 检索（esearch + efetch）")
    parser.add_argument("--query", required=True, help="PubMed 检索式")
    parser.add_argument("--limit", type=int, help="条数上限，默认读配置")
    parser.add_argument("--years", type=int, help="近 N 年，默认读配置")
    parser.add_argument("--run-dir", help="输出目录")
    parser.add_argument("--out", help="输出 JSON 路径")
    parser.add_argument("--config", help="配置文件")
    parser.add_argument("--json", action="store_true", help="结果打到 stdout")
    args = parser.parse_args()
    lc.print_env()
    cfg = lc.load_config(args.config)
    limit = args.limit or cfg["databases"]["pubmed"].get("retmax", 20)
    years = args.years if args.years is not None else cfg["databases"]["pubmed"].get("years", 5)

    run_dir = Path(args.run_dir) if args.run_dir else None
    code, records, total = search_pubmed(cfg, args.query, limit, years, print)
    if args.out:
        lc.write_json(args.out, records)
    elif run_dir:
        lc.write_json(run_dir / "records_pubmed.json", records)
    if args.json:
        print(json.dumps({"count": total, "records": records}, ensure_ascii=False, indent=2))
    print(f"\n共命中 {total} 条，取回 {len(records)} 条。")
    return code


if __name__ == "__main__":
    sys.exit(main())
