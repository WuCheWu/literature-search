# -*- coding: utf-8 -*-
"""precheck.py —— 检索前的硬门槛：VPN 已连接 + 学校账号已登录

用途：三道确认全部通过才允许检索；任何一道无法确认就暂停并提示用户。
    ① 技术预检：虚拟网卡 Up、知网网段路由存在（vpn_check）
    ② 用户确认：本人学校 VPN 账号已连、已用学校账号登录/从图书馆入口进
    ③ 机构实测：打开知网结果页，页面出现机构名（inst_check，唯一可靠判据）
参数：
    --run-dir DIR       输出目录（写 预检确认.json 与截图）
    --keyword "词"      触发机构实测用的检索词
    --confirm-vpn       宿主已确认「VPN 已连接」
    --confirm-account   宿主已确认「学校账号已登录」
    --skip-institution  跳过机构实测（仅 PubMed 场景；日志会记录）
    --selectors FILE    选择器文件
    --config PATH       配置文件
    --json              输出 JSON
退出码：0 全部确认 / 2 任一项无法确认（已暂停） / 3 配置错误
依赖：vpn_check、inst_check、playwright（msedge）
注意：机构识别走 IP/VPN，不需要在知网登录个人账号；「学校账号」指 VPN 与图书馆入口的账号。
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import browser_util as bu  # noqa: E402
import inst_check  # noqa: E402
import litcommon as lc  # noqa: E402
import vpn_check  # noqa: E402

PAUSE_BANNER = (
    "\n" + "=" * 62 + "\n"
    "已暂停：未能确认「VPN 已连接 + 学校账号已登录」。\n"
    "后续的检索、筛选、下载、出表都没有执行，也没有打开任何数据库页面（机构实测除外）。\n"
    + "=" * 62
)


def _ask_yes(prompt: str) -> bool | None:
    print(prompt)
    try:
        raw = input("  确认请输入 y（取消直接回车）：").strip().lower()
    except EOFError:
        return None
    return raw in ("y", "yes", "是", "1")


def confirm_interactively(facts: dict) -> tuple[bool, bool, str]:
    """两个问题都必须回答 y，才认为用户确认。"""
    print("\n=== 第 2 步：请本人确认（两项都要是） ===")
    print("  说明：知网的机构识别依靠 VPN 出口 IP，不需要在知网登录个人账号；")
    print("        「学校账号」指 VPN 客户端与图书馆入口使用的本人账号。")
    if not sys.stdin.isatty():
        print("  ! 当前会话不可交互，无法取得人工确认。")
        return False, False, "non-interactive"

    vpn_ok = _ask_yes("\n  ① EasyConnect 现在显示「已连接」，且用的是你本人的学校 VPN 账号？")
    if vpn_ok is None:
        return False, False, "eof"
    if not vpn_ok:
        return False, False, "user-denied-vpn"

    account_ok = _ask_yes("\n  ② 浏览器已用学校账号登录（或已从图书馆入口进入数据库，页面能看到校名）？")
    if account_ok is None:
        return False, False, "eof"
    if not account_ok:
        return False, False, "user-denied-account"
    return True, True, "interactive"


def run(cfg: dict, run_dir: Path, keyword: str, log,
        selectors: dict | None = None, assume_confirmed: bool = False,
        skip_institution: bool = False) -> tuple[int, dict]:
    """返回 (退出码, 预检记录)。所有事实写入 <run_dir>/预检确认.json。"""
    sel = selectors or bu.load_selectors(None, log)
    info = {
        "checked_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "ok": False,
        "vpn": {},
        "user_confirm": {},
        "institution": {},
        "reason": "",
    }

    # ① 技术预检
    log("[预检] 第 1 步：检查 EasyConnect、虚拟网卡与知网路由")
    facts = vpn_check.collect_facts(cfg)
    vpn_ok, reasons = vpn_check.judge(facts)
    vpn_check.print_summary(facts)
    info["vpn"] = {"ok": vpn_ok, "reasons": reasons, "facts": facts}
    if not vpn_ok:
        print("\n=== 预判：未连接 ===")
        for reason in reasons:
            print(f"  x {reason}")
        print("\n" + vpn_check.GUIDE)
        info["reason"] = "VPN 未连接：" + "；".join(reasons)
        lc.write_json(run_dir / "预检确认.json", info)
        log("[预检] VPN 未连接，流程暂停（不打开数据库、不生成清单）")
        print(PAUSE_BANNER)
        return lc.EXIT_NEED_USER, info
    print("\n  ✓ 虚拟网卡 Up、知网路由存在（这只是技术预检，账号还要确认）")

    # ② 用户确认
    if assume_confirmed:
        vpn_confirmed = account_confirmed = True
        method = "flags"
        log("[预检] 第 2 步：宿主已代为确认 VPN 与学校账号（--confirm-vpn --confirm-account）")
    else:
        log("[预检] 第 2 步：请用户确认 VPN 与学校账号")
        vpn_confirmed, account_confirmed, method = confirm_interactively(facts)
    info["user_confirm"] = {
        "vpn": vpn_confirmed, "account": account_confirmed, "method": method,
    }
    if not (vpn_confirmed and account_confirmed):
        info["reason"] = f"用户未能确认（{method}）"
        lc.write_json(run_dir / "预检确认.json", info)
        log(f"[预检] 用户确认未通过（{method}），流程暂停")
        if method == "non-interactive":
            print(
                "\n[需要用户确认] 当前会话不能弹交互确认。\n"
                "请让用户明确回答「VPN 已连接、学校账号已登录」后，\n"
                "带 --confirm-vpn --confirm-account 重新运行；否则不会开始检索。"
            )
        elif method.startswith("user-denied"):
            print("\n  你刚才选择了「否」。请先连好 VPN / 登录学校账号，再重新运行。")
        print(PAUSE_BANNER)
        return lc.EXIT_NEED_USER, info
    print("\n  ✓ 用户已确认 VPN 与学校账号")

    # ③ 机构实测
    if skip_institution:
        info["institution"] = {"skipped": True}
        log("[预检] 第 3 步：按 --skip-institution 跳过机构实测（未验证学校订阅权限）")
    else:
        log("[预检] 第 3 步：打开知网结果页核对机构身份（一次探测性检索，不下载）")
        code, result = inst_check.run(cfg, keyword or "文献", run_dir, log, sel)
        info["institution"] = result
        if code != lc.EXIT_OK:
            info["reason"] = f"机构身份未确认：{result.get('reason')}"
            lc.write_json(run_dir / "预检确认.json", info)
            log("[预检] 机构身份未确认，流程暂停")
            print(PAUSE_BANNER)
            return lc.EXIT_NEED_USER, info
        print(f"\n  ✓ 结果页出现「{cfg['institution']['name']}」，学校订阅身份有效")

    info["ok"] = True
    info["reason"] = ""
    lc.write_json(run_dir / "预检确认.json", info)
    log("[预检] 三项确认全部通过，允许开始检索")
    return lc.EXIT_OK, info


def main() -> int:
    lc.setup_console()
    parser = argparse.ArgumentParser(description="检索前确认：VPN + 学校账号 + 机构身份")
    parser.add_argument("--run-dir", help="输出目录")
    parser.add_argument("--keyword", default="文献", help="机构实测用的检索词")
    parser.add_argument("--confirm-vpn", action="store_true", help="宿主已确认 VPN 已连接")
    parser.add_argument("--confirm-account", action="store_true", help="宿主已确认学校账号已登录")
    parser.add_argument("--skip-institution", action="store_true", help="跳过机构实测")
    parser.add_argument("--selectors", help="selectors.json")
    parser.add_argument("--config", help="配置文件")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    args = parser.parse_args()
    lc.print_env()
    cfg = lc.load_config(args.config)
    run_dir = Path(args.run_dir) if args.run_dir else lc.make_run_dir(cfg, args.keyword)
    log = lc.RunLog(run_dir / "日志" / "run.log")
    try:
        code, info = run(
            cfg, run_dir, args.keyword, log,
            bu.load_selectors(args.selectors, log),
            assume_confirmed=args.confirm_vpn and args.confirm_account,
            skip_institution=args.skip_institution,
        )
        if args.json:
            print(json.dumps(info, ensure_ascii=False, indent=2, default=str))
        return code
    finally:
        log.close()


if __name__ == "__main__":
    sys.exit(main())
