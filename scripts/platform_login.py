# -*- coding: utf-8 -*-
"""platform_login.py —— 平台首次登录与机构登录设置（规则 7）

为什么需要：
    ScienceDirect / SinoMed / CAS 三库等平台，第一次在这台电脑上用时必须先手动登录一次，
    并在登录页选对「机构/学校」或完成学校统一身份认证；否则后续脚本打开只有登录页/摘要，
    拿不到文献与全文。VPN 只解决网络，不等于平台已登录。

做法：
    用与检索完全相同的持久化浏览器 profile（config.browser.profile_dir）打开平台入口，
    让用户自己完成登录（含验证码、二次验证、机构选择），脚本只等待并检查登录框是否消失。
    登录一次后 Cookie 留在 profile 里，后续检索直接复用。

参数：
    --platform ID          平台 id（可重复）
    --all                  全部非公开平台
    --wait SEC             每个平台等待人工登录的秒数（默认 180）
    --first-time           用户已确认「有平台第一次用」——逐个打开等待登录
    --ready                用户已确认「都登录过且设置好了」——只记录，不打开浏览器
    --print-questions      只打印问题清单（宿主据此问用户），不打开浏览器
    --check                只读缓存状态，不打开浏览器
    --run-dir DIR          输出目录（写 登录准备.json）
    --refresh              忽略缓存重新检查
    --config PATH          配置文件
    --json                 输出 JSON
退出码：0 全部就绪 / 2 仍有平台需要用户登录 / 1 有平台打不开 / 3 配置错误
依赖：playwright（msedge）
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
import litcommon as lc  # noqa: E402
import platform_util as pu  # noqa: E402
import platforms as pf  # noqa: E402

QUESTIONS = {
    "title": "平台首次登录确认（规则 7）",
    "intro": (
        "以下平台如果不是「已登录过且机构/学校登录设置已保存」，脚本打开只会看到登录页或摘要，"
        "拿不到文献和全文。请逐项确认；需要首次登录的，会先在浏览器里带你登录一次。"
    ),
    "options": [
        {"label": "已登录过，机构/学校选项也设置好了", "value": "ready"},
        {"label": "登录过，但没选机构/学校", "value": "need_setup"},
        {"label": "第一次用，还没登录", "value": "first_time"},
        {"label": "不确定", "value": "unknown"},
    ],
}


def _state_dir(cfg: dict) -> Path:
    configured = (cfg.get("platforms") or {}).get("login_state_dir")
    return Path(configured) if configured else Path(cfg["output_root"]) / "_平台登录"


def _state_path(cfg: dict, pid: str) -> Path:
    return _state_dir(cfg) / f"{lc.sanitize_filename(pid, 40)}.json"


def load_state(cfg: dict, pid: str, refresh: bool = False) -> dict | None:
    if refresh:
        return None
    data = lc.read_json(_state_path(cfg, pid), default=None)
    if not isinstance(data, dict):
        return None
    if data.get("profile_dir") != cfg["browser"]["profile_dir"]:
        return None
    if not data.get("ready"):
        return None  # 未就绪不缓存，下次继续问/继续等，避免一次失败后 30 天都被挡住
    ttl_days = float((cfg.get("platforms") or {}).get("login_state_ttl_days", 30) or 0)
    if ttl_days > 0:
        try:
            checked = datetime.strptime(data.get("checked_at", ""), "%Y-%m-%d %H:%M:%S")
            if (datetime.now() - checked).total_seconds() > ttl_days * 86400:
                return None
        except ValueError:
            return None
    data["cached"] = True
    return data


def save_state(cfg: dict, info: dict) -> Path:
    info["profile_dir"] = cfg["browser"]["profile_dir"]
    path = _state_path(cfg, info["platform"])
    lc.write_json(path, info)
    return path


def questions_for(platforms_list: list[dict]) -> dict:
    questions = []
    for platform in platforms_list:
        if (platform.get("access") or "open") == "open":
            continue
        questions.append({
            "id": platform["id"],
            "header": (platform.get("short") or platform["id"])[:12],
            "question": (
                f"{platform.get('name') or platform['id']}：你是否已在这台电脑上登录过，"
                "并在登录页选好了学校/机构（或完成学校统一身份认证）？"
            ),
            "type": "choice",
            "options": QUESTIONS["options"],
            "required": True,
        })
    return {"title": QUESTIONS["title"], "intro": QUESTIONS["intro"], "questions": questions}


def check_one(cfg: dict, platform: dict, wait: int, run_dir: Path, log,
              first_time: bool = False) -> dict:
    pid = platform.get("id") or "custom"
    name = platform.get("short") or platform.get("name") or pid
    entry = platform.get("entry") or platform.get("home") or ""
    shot_dir = run_dir / "日志"
    info = {
        "platform": pid,
        "name": name,
        "entry": entry,
        "ready": False,
        "needs_user": True,
        "reason": "",
        "method": "",
        "first_time": first_time,
        "login_required": False,
        "institution_seen": False,
        "checked_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    if not entry:
        info["reason"] = "平台没有入口网址"
        return info

    pw, context, page = bu.open_context(cfg, log)
    try:
        if not bu.goto(page, entry, cfg, log):
            info["reason"] = "入口打不开（先确认 VPN/网络）"
            bu.screenshot(page, shot_dir, f"login_{pid}_fail")
            return info
        time.sleep(2)
        if "/verify/" in page.url and not bu.wait_for_verify(page, cfg, shot_dir, log):
            info["reason"] = "滑块未完成"
            return info

        login = pu.detect_login(page)
        info["login_required"] = bool(login["required"])
        info["institution_seen"] = pu.institution_seen(page, cfg["institution"]["name"])

        if not login["required"]:
            info["ready"] = True
            info["needs_user"] = False
            info["method"] = "already-logged-in" if platform.get("access") == "account" else "no-login-wall"
            log(f"[登录准备] {name} 无需登录/已有登录状态（机构名可见={info['institution_seen']}）")
            return info

        print("\n" + "=" * 62)
        print(f"{name} 需要登录。已用检索专用浏览器打开：{page.url}")
        print("请在这个窗口里完成：① 登录（验证码/二次验证）；② 选择学校/机构；③ 记住登录。")
        print(f"完成后脚本会自动继续，最多等 {wait} 秒。")
        print("=" * 62)
        if pu.wait_manual_login(page, cfg, log, wait, name):
            if not pu.detect_login(page)["required"]:
                info["ready"] = True
                info["needs_user"] = False
                info["method"] = "manual"
                info["institution_seen"] = pu.institution_seen(page, cfg["institution"]["name"])
                log(f"[登录准备] {name} 登录完成（机构名可见={info['institution_seen']}）")
                return info
        info["reason"] = "等待登录超时"
        bu.screenshot(page, shot_dir, f"login_{pid}_timeout")
        return info
    except Exception as exc:  # noqa: BLE001
        info["reason"] = f"{type(exc).__name__}: {exc}"
        log(f"[登录准备] {name} 异常：{info['reason']}")
        return info
    finally:
        bu.close_context(pw, context, log)


def ensure(cfg: dict, pids: list[str], run_dir: Path, log,
           platforms_map: dict | None = None, status: str = "unknown",
           pending: list[str] | None = None, assume_ready: bool = False,
           assume_ip_ready: bool = False, wait: int = 0,
           refresh: bool = False) -> tuple[int, dict[str, dict]]:
    """逐平台确认登录准备。返回 (退出码, {pid: info})。

    status: ready（用户说都登录过）/ first_time（有平台首次）/ unknown（逐个检查）
    """
    platforms_map = platforms_map or {}
    pending = pending or []
    results: dict[str, dict] = {}
    for pid in pids:
        platform = platforms_map.get(pid) or pf.get(pid)
        if not platform:
            continue
        platform = pf.merge_learned(platform)
        access = platform.get("access") or "unknown"
        if access == "open":
            results[pid] = {
                "platform": pid, "name": platform.get("short") or pid, "ready": True,
                "needs_user": False, "method": "not-needed", "reason": "",
                "checked_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            }
            continue

        cached = load_state(cfg, pid, refresh=refresh)
        if cached:
            cached["needs_user"] = not cached.get("ready")
            results[pid] = cached
            log(f"[登录准备] {cached.get('name')} 用缓存："
                f"{'就绪' if cached.get('ready') else '未就绪'}（{cached.get('checked_at')}）")
            continue

        must_check = (pid in pending) if pending else (status == "first_time")
        # --confirm-platform-login 只是「我确认过」，不能覆盖「这台机器第一次用」；
        # 否则会写 ready 并缓存 30 天，把本该做的登录检查永久跳过。
        if must_check:
            assume_ready = False
        if (status == "ready" or assume_ready
                or (status == "first_time" and pending and pid not in pending)
                or (assume_ip_ready and access == "ip")):
            info = {
                "platform": pid, "name": platform.get("short") or pid, "ready": True,
                "needs_user": False, "method": "user-confirmed", "reason": "",
                "first_time": False, "login_required": False,
                "checked_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            }
        elif must_check and wait > 0:
            info = check_one(cfg, platform, wait, run_dir, log, first_time=True)
        else:
            info = {
                "platform": pid, "name": platform.get("short") or pid, "ready": False,
                "needs_user": True, "method": "", "reason": "未确认是否第一次登录/是否已设置机构",
                "first_time": must_check, "login_required": False,
                "checked_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            }
            log(f"[登录准备] {info['name']} 未确认：检索前需要用户回答"
                "「是否已登录并设置好学校/机构」")
        save_state(cfg, info)
        results[pid] = info

    ready = [pid for pid, info in results.items() if info.get("ready")]
    not_ready = [pid for pid, info in results.items() if not info.get("ready")]
    lc.write_json(run_dir / "登录准备.json", list(results.values()))
    log(f"[登录准备] 就绪 {len(ready)} / 待登录 {len(not_ready)}")
    if not_ready:
        print("\n[需要用户确认登录] 以下平台还没确认登录/机构设置，检索会拿不到全文：")
        for pid in not_ready:
            info = results[pid]
            print(f"  - {info['name']}：{info.get('reason') or '未确认'}")
        print("  处理方式：")
        print("    A. 已登录过 → 带 --confirm-platform-login 重跑（表示用户已确认）")
        print("    B. 首次登录 → 运行  python scripts\\platform_login.py --platform "
              f"{not_ready[0]} --wait 180")
        print("    C. 让宿主逐项问 → python scripts\\platform_login.py --print-questions")
        return lc.EXIT_NEED_USER, results
    return lc.EXIT_OK, results


def main() -> int:
    lc.setup_console()
    parser = argparse.ArgumentParser(description="平台首次登录与机构登录设置")
    parser.add_argument("--platform", action="append", help="平台 id（可重复）")
    parser.add_argument("--all", action="store_true", help="全部非公开平台")
    parser.add_argument("--wait", type=int, default=180, help="每个平台等待登录秒数")
    parser.add_argument("--first-time", action="store_true", help="用户已确认有平台首次使用")
    parser.add_argument("--ready", action="store_true", help="用户已确认都登录过且设置好了")
    parser.add_argument("--print-questions", action="store_true", help="只打印问题清单")
    parser.add_argument("--check", action="store_true", help="只读缓存，不打开浏览器")
    parser.add_argument("--run-dir", help="输出目录")
    parser.add_argument("--refresh", action="store_true", help="忽略缓存")
    parser.add_argument("--config", help="配置文件")
    parser.add_argument("--platforms-file", help="覆盖 platforms.json")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    args = parser.parse_args()
    lc.print_env()

    cfg = lc.load_config(args.config)
    registry_path = args.platforms_file
    targets = []
    if args.all:
        targets = [p for p in pf.selectable(registry_path) if (p.get("access") or "open") != "open"]
    for pid in args.platform or []:
        platform = pf.get(pid, registry_path)
        if not platform:
            lc.die(lc.EXIT_CONFIG, f"没有这个平台：{pid}（先用 platforms.py --add 登记）")
        targets.append(platform)
    if not targets:
        targets = [p for p in pf.selectable(registry_path) if (p.get("access") or "open") != "open"]

    if args.print_questions:
        print(json.dumps(questions_for(targets), ensure_ascii=False, indent=2))
        return lc.EXIT_OK

    run_dir = Path(args.run_dir) if args.run_dir else (
        Path(cfg["output_root"]) / "_平台登录" / f"{datetime.now():%Y%m%d_%H%M%S}"
    )
    run_dir.mkdir(parents=True, exist_ok=True)
    log = lc.RunLog(run_dir / "日志" / "login.log")
    try:
        if args.check:
            results = {}
            for platform in targets:
                cached = load_state(cfg, platform["id"], refresh=args.refresh)
                if cached:
                    results[platform["id"]] = cached
            print(f"已就绪：{sum(1 for v in results.values() if v.get('ready'))} / {len(targets)}")
            if args.json:
                print(json.dumps(results, ensure_ascii=False, indent=2))
            return lc.EXIT_OK if len(results) == len(targets) else lc.EXIT_NEED_USER

        status = "ready" if args.ready else ("first_time" if args.first_time else "unknown")
        if args.ready:
            # 用户已确认：只记录，不打开浏览器
            results = {}
            for platform in targets:
                info = {
                    "platform": platform["id"], "name": platform.get("short") or platform["id"],
                    "ready": True, "needs_user": False, "method": "user-confirmed",
                    "reason": "", "first_time": False, "login_required": False,
                    "institution_seen": False,
                    "checked_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                }
                save_state(cfg, info)
                results[platform["id"]] = info
            lc.write_json(run_dir / "登录准备.json", list(results.values()))
            print(f"已记录 {len(results)} 个平台为「已登录/已设置」；检索时会直接复用浏览器登录状态。")
            return lc.EXIT_OK

        code = lc.EXIT_OK
        results = {}
        for platform in targets:
            info = check_one(cfg, platform, args.wait, run_dir, log,
                             first_time=args.first_time)
            save_state(cfg, info)
            results[platform["id"]] = info
            if not info.get("ready"):
                code = lc.EXIT_NEED_USER
        lc.write_json(run_dir / "登录准备.json", list(results.values()))
        if args.json:
            print(json.dumps(list(results.values()), ensure_ascii=False, indent=2))
        print(f"\n就绪：{sum(1 for v in results.values() if v.get('ready'))} / {len(results)}")
        return code
    finally:
        log.close()


if __name__ == "__main__":
    sys.exit(main())
