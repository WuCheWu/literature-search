# -*- coding: utf-8 -*-
"""vpn_check.py —— 环境与 VPN 预检（只报事实与预判，不做唯一判定）

用途：检查 EasyConnect 客户端、相关进程、Sangfor 虚拟网卡、知网路由。
参数：
    --config PATH   配置文件（默认 <技能根>/config.json）
    --json          只输出 JSON（供 run_all 解析）
退出码：
    0 预检通过（网卡 Up 且知网路由存在）
    2 预判未连接（网卡缺失/未 Up/路由缺失）
    3 配置错误
依赖：标准库 + Windows PowerShell 5.1（只调用 cmdlet，不用 .ps1）
注意：网卡有 IP、门户返回 200、SangforPWEx 在跑，都不等于已连接；
      本脚本的结论只是预检，最终机构身份由 inst_check.py 判定。
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import litcommon as lc  # noqa: E402


PS_TEMPLATE = r"""
$ErrorActionPreference = 'SilentlyContinue'
$outPath = '__OUT__'
$desc = '__DESC__'
$exe = '__EXE__'
$cnkiPrefix = '__CNKI__'

$adapter = $null
$ad = Get-NetAdapter | Where-Object { $_.InterfaceDescription -like $desc } | Select-Object -First 1
if ($ad) {
    $ips = @(Get-NetIPAddress -InterfaceIndex $ad.ifIndex -AddressFamily IPv4 | ForEach-Object { $_.IPAddress })
    $routes = @(Get-NetRoute -InterfaceIndex $ad.ifIndex -AddressFamily IPv4)
    if ([string]::IsNullOrWhiteSpace($cnkiPrefix)) {
        $cnkiRoute = $true
    } else {
        $cnki = @($routes | Where-Object { $_.DestinationPrefix -eq $cnkiPrefix })
        $cnkiRoute = ($cnki.Count -gt 0)
    }
    $adapter = [ordered]@{
        name        = [string]$ad.Name
        description = [string]$ad.InterfaceDescription
        status      = [string]$ad.Status
        if_index    = [int]$ad.ifIndex
        ipv4        = $ips
        route_count = $routes.Count
        cnki_route  = $cnkiRoute
        cnki_prefix = [string]$cnkiPrefix
    }
}

$names = @('EasyConnect','SangforCSClient','SangforPWEx','SangforSP','SangforServiceClient')
$procs = [ordered]@{}
foreach ($n in $names) {
    $p = Get-Process -Name $n -ErrorAction SilentlyContinue
    if ($p) { $procs[$n] = $true } else { $procs[$n] = $false }
}

$result = [ordered]@{
    client_exe   = [bool](Test-Path -LiteralPath $exe)
    client_path  = $exe
    adapter      = $adapter
    processes    = $procs
    checked_at   = (Get-Date).ToString('yyyy-MM-dd HH:mm:ss')
}
$json = $result | ConvertTo-Json -Depth 6 -Compress
[IO.File]::WriteAllText($outPath, $json, (New-Object System.Text.UTF8Encoding($false)))
"""


def collect_facts(cfg: dict) -> dict:
    """调用 PowerShell 收集事实，返回字典。失败时返回带 error 的最小结构。"""
    vpn = cfg["vpn"]
    exe = str(Path(vpn["client_dir"]) / "EasyConnect.exe")
    desc = "*" + str(vpn["vnic_desc_match"]).strip("*") + "*"
    # 留空 = 跳过路由检查（全隧道 VPN 没有单独的知网网段，硬查会误判）
    cnki = str(vpn.get("cnki_probe_route") or "").strip()

    fd, tmp = tempfile.mkstemp(suffix=".json", prefix="lit_vpn_")
    os.close(fd)  # mkstemp 会占着句柄，Windows 下不关就删不掉
    Path(tmp).unlink(missing_ok=True)
    script = (
        PS_TEMPLATE.replace("__OUT__", tmp.replace("'", "''"))
        .replace("__DESC__", desc.replace("'", "''"))
        .replace("__EXE__", exe.replace("'", "''"))
        .replace("__CNKI__", cnki.replace("'", "''"))
    )
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
             "-Command", script],
            capture_output=True,
            timeout=90,
        )
        raw = Path(tmp).read_text(encoding="utf-8") if Path(tmp).exists() else ""
        if not raw.strip():
            return {
                "error": "PowerShell 未返回数据",
                "stderr": proc.stderr.decode("utf-8", "replace")[-500:],
                "client_path": exe,
            }
        return json.loads(raw)
    except Exception as exc:  # noqa: BLE001 —— 预检失败要如实上报
        return {"error": f"{type(exc).__name__}: {exc}", "client_path": exe}
    finally:
        Path(tmp).unlink(missing_ok=True)


def judge(facts: dict) -> tuple[bool, list[str]]:
    """预判是否值得继续。任一条不满足即视为未连接。"""
    reasons: list[str] = []
    if "error" in facts:
        return False, [f"预检脚本执行失败：{facts['error']}"]
    if not facts.get("client_exe"):
        reasons.append(f"未找到 EasyConnect 客户端：{facts.get('client_path')}")
    adapter = facts.get("adapter")
    if not adapter:
        reasons.append("未检测到描述含 Sangfor SSL VPN 的虚拟网卡")
        return False, reasons
    if str(adapter.get("status", "")).lower() != "up":
        reasons.append(
            f"虚拟网卡「{adapter.get('name')}」状态为 {adapter.get('status')}（断线后 IPv4 会残留，不能当已连接）"
        )
    if not adapter.get("cnki_route"):
        prefix = adapter.get("cnki_prefix") or facts.get("cnki_prefix") or "未配置"
        reasons.append(f"虚拟网卡路由表里没有知网网段（{prefix}），隧道未建立")
    return (not reasons), reasons


def print_summary(facts: dict) -> None:
    print("\n=== VPN 预检（事实） ===")
    if "error" in facts:
        print(f"  执行失败：{facts['error']}")
        return
    print(f"  客户端 EasyConnect.exe：{'存在' if facts.get('client_exe') else '缺失'}")
    print(f"    路径：{facts.get('client_path')}")
    procs = facts.get("processes") or {}
    running = [k for k, v in procs.items() if v]
    print(f"  相关进程：{'、'.join(running) if running else '（无）'}")
    adapter = facts.get("adapter")
    if not adapter:
        print("  虚拟网卡：未找到")
        return
    print(f"  虚拟网卡：{adapter.get('name')}（ifIndex={adapter.get('if_index')}）")
    print(f"    描述：{adapter.get('description')}")
    print(f"    状态：{adapter.get('status')}")
    print(f"    IPv4：{', '.join(adapter.get('ipv4') or []) or '（无）'}")
    print(f"    路由条数：{adapter.get('route_count')}")
    prefix = adapter.get("cnki_prefix") or ""
    label = prefix if prefix else "未配置（跳过）"
    print(f"    知网路由 {label}：{'存在' if adapter.get('cnki_route') else '缺失'}")


GUIDE = (
    "请先连接 EasyConnect（开始菜单 → EasyConnect，或\n"
    "  C:\\Program Files (x86)\\Sangfor\\SSL\\EasyConnect\\EasyConnect.exe），\n"
    "输账号密码点连接，等客户端显示“已连接”后重新运行本技能。\n"
    "（脚本不会代填账号密码，也不会自动启动客户端。）"
)


PAUSE_TEXT = (
    "\n" + "=" * 62 + "\n"
    "【需要你处理】未检测到 VPN 接入，已停止，没有检索任何网络资源。\n"
    "判定：{reasons}\n"
    "请：\n"
    "  1) 打开你学校的 VPN 客户端（EasyConnect / 其他）并连上；\n"
    "  2) 确认客户端显示「已连接」；\n"
    "  3) 回来重跑（已检索/已下载的结果会保留）。\n"
    "=" * 62
)

MIDRUN_TEXT = (
    "\n" + "=" * 62 + "\n"
    "【需要你处理】检测到 VPN 接入可能已断开（{stage}），已停止检索。\n"
    "判定：{reasons}\n"
    "请重新连接 VPN 后重跑；已完成的记录与下载不会丢。\n"
    "=" * 62
)


def check(cfg: dict) -> tuple[bool, list[str]]:
    """VPN 是否可用。

    配置 `vpn.client` 不是 easyconnect 时（Cisco / WebVPN / 其他），客户端检测不适用，
    返回通过；断线改由请求失败识别。
    """
    client = str(((cfg or {}).get("vpn") or {}).get("client") or "easyconnect").strip().lower()
    if client in ("other", "webvpn", "none"):
        return True, [f"按配置跳过客户端检测（vpn.client={client}）"]
    try:
        facts = collect_facts(cfg)
    except Exception as exc:  # noqa: BLE001
        return False, [f"预检执行失败：{type(exc).__name__}: {exc}"]
    return judge(facts)


def require(cfg: dict, log=None, stage: str = "检索前") -> None:
    """硬闸：不通过就带提示退出码 2，调用方不必再判断。"""
    ok, reasons = check(cfg)
    if ok:
        return
    # 「…检索前 / 下载前」= 还没开始，用未检测到文案；「…中 / …后」= 中途断线
    midrun = ("中" in stage) or ("后" in stage) or ("中断" in stage)
    template = MIDRUN_TEXT if midrun else PAUSE_TEXT
    text = template.format(reasons="；".join(reasons) or "未知", stage=stage)
    if log:
        log(f"[VPN] {stage} 未通过：{'；'.join(reasons)}")
    print(text, file=sys.stderr)
    sys.exit(lc.EXIT_NEED_USER)


DETECT_PROMPT = (
    "\n" + "-" * 62 + "\n"
    "需要调用 PowerShell 读取网卡与进程，检测 VPN 状态。\n"
    "（只读，不改任何配置；拒绝则本次不检测、也不检索。）\n"
    + "-" * 62
)

DETECT_NEED_USER = (
    "\n【需要你处理】检测 VPN 需要运行 PowerShell 读取网卡与进程。\n"
    "请回复「允许检测 VPN」，再用 --allow-vpn-detect 重跑；\n"
    "未获允许时不会检测，也不会检索任何网络资源。"
)


def confirm_detect(force: bool = False, log=None) -> None:
    """检测前要一次显式许可；拒绝或无法询问 → 退出码 2。"""
    if force:
        if log:
            log("[VPN] 已按 --allow-vpn-detect 允许运行检测")
        return
    if not sys.stdin.isatty():
        print(DETECT_NEED_USER, file=sys.stderr)
        sys.exit(lc.EXIT_NEED_USER)
    print(DETECT_PROMPT)
    try:
        raw = input("  输入 y 允许本次检测，直接回车拒绝：").strip().lower()
    except EOFError:
        raw = ""
    if raw not in ("y", "yes", "是", "1"):
        print(DETECT_NEED_USER, file=sys.stderr)
        sys.exit(lc.EXIT_NEED_USER)
    if log:
        log("[VPN] 用户已允许运行 PowerShell 检测")


def run(cfg: dict, as_json: bool = False) -> tuple[int, dict]:
    facts = collect_facts(cfg)
    ok, reasons = judge(facts)
    if as_json:
        print(json.dumps({"ok": ok, "reasons": reasons, "facts": facts}, ensure_ascii=False, indent=2))
    else:
        print_summary(facts)
        print("\n=== 预判 ===")
        if ok:
            print("  通过：网卡 Up、知网路由存在。注意这只是预检，机构身份仍需 inst_check.py 确认。")
        else:
            for reason in reasons:
                print(f"  × {reason}")
            print("\n" + GUIDE)
    return (lc.EXIT_OK if ok else lc.EXIT_NEED_USER), facts


def main() -> int:
    lc.setup_console()
    parser = argparse.ArgumentParser(description="VPN 与环境预检（只报事实，不做最终判定）")
    parser.add_argument("--config", help="配置文件路径，默认 <技能根>/config.json")
    parser.add_argument("--json", action="store_true", help="只输出 JSON")
    parser.add_argument("--yes", action="store_true", help="已获用户允许，直接运行检测")
    args = parser.parse_args()
    lc.print_env()
    cfg = lc.load_config(args.config)
    confirm_detect(args.yes)
    code, _ = run(cfg, args.json)
    return code


if __name__ == "__main__":
    sys.exit(main())
