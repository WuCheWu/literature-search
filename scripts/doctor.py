# -*- coding: utf-8 -*-
"""doctor.py —— 开跑前的环境自检

用途：一条命令看清「这台机器现在能不能跑」：Python 版本、三个依赖、Edge、EasyConnect、
      配置文件、输出目录可写、平台注册表可读。不通过时直接给修复命令。
参数：
    --config PATH   配置文件（默认 <技能根>/config.json）
    --json          输出 JSON（供宿主解析）
    --vpn           顺带做一次 VPN 预检（会等 PowerShell，约 2-5 秒）
退出码：0 全部通过 / 1 有警告（能跑但可能降级） / 3 有关键项不通过
依赖：标准库（可选：vpn_check 需要 PowerShell）
说明：只检查、不安装、不改配置；装不装由用户决定。
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import litcommon as lc  # noqa: E402

MIN_PYTHON = (3, 10)
DEPS = {
    "requests": "联网检索（PubMed / 学校清单网址 / Unpaywall）",
    "openpyxl": "生成 文献清单.xlsx",
    "playwright": "打开可见 Edge 检索知网等平台",
}
EDGE_PATHS = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
]


def _check(name: str, ok: bool, detail: str, fix: str = "", level: str = "关键") -> dict:
    return {"name": name, "ok": bool(ok), "detail": detail, "fix": fix, "level": level}


def _edge_path() -> str:
    for raw in EDGE_PATHS:
        if Path(raw).exists():
            return raw
    return ""


def run_checks(cfg_path: str | None, cfg: dict | None) -> list[dict]:
    checks: list[dict] = []

    version = ".".join(str(x) for x in sys.version_info[:3])
    checks.append(_check(
        "Python 版本", sys.version_info[:2] >= MIN_PYTHON,
        f"{version}（需要 ≥ {MIN_PYTHON[0]}.{MIN_PYTHON[1]}）",
        "到 https://www.python.org/downloads/windows/ 装 3.10+，安装时勾选 Add python.exe to PATH",
    ))

    for module, why in DEPS.items():
        installed = importlib.util.find_spec(module) is not None
        checks.append(_check(
            f"依赖 {module}", installed,
            "已安装" if installed else f"未安装（{why}）",
            "python -m pip install requests openpyxl playwright",
        ))

    edge = _edge_path()
    checks.append(_check(
        "Microsoft Edge", bool(edge), edge or "没在默认位置找到 msedge.exe",
        "到 https://www.microsoft.com/zh-cn/edge/download 安装（脚本用它开可见窗口）",
        level="警告",
    ))

    config_file = Path(cfg_path) if cfg_path else lc.SKILL_ROOT / "config.json"
    checks.append(_check(
        "配置文件", config_file.exists(), str(config_file),
        "Copy-Item config.example.json config.json",
    ))

    missing: list[str] = []
    if cfg:
        for key in lc.REQUIRED_KEYS:
            try:
                lc._dig(cfg, key)
            except KeyError:
                missing.append(key)
    checks.append(_check(
        "配置字段完整", bool(cfg) and not missing,
        "全部必填字段都在" if cfg and not missing else ("缺少：" + "、".join(missing) if missing else "未读到配置"),
        "对照 config.example.json 补齐缺的字段",
    ))

    if cfg:
        root = Path(cfg["output_root"])
        try:
            root.mkdir(parents=True, exist_ok=True)
            probe = root / ".doctor_write_test"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
            detail = f"{root}（可写）"
            writable = True
        except Exception as exc:  # noqa: BLE001
            detail = f"{root}（不可写：{type(exc).__name__}: {exc}）"
            writable = False
        checks.append(_check("输出目录", writable, detail, "改 config.json 的 output_root 到有写权限的盘"))

        exe = Path(str(cfg["vpn"]["client_dir"])) / "EasyConnect.exe"
        checks.append(_check(
            "EasyConnect 客户端", exe.exists(), str(exe),
            "从学校 VPN 门户（config.json 的 vpn.portal）下载安装；只用 PubMed 时可忽略",
        ))
    else:
        checks.append(_check("输出目录", False, "没读到配置，无法判断", "先修好 config.json"))
        checks.append(_check("EasyConnect 客户端", False, "没读到配置，无法判断", "先修好 config.json"))

    registry = lc.read_json(lc.SKILL_ROOT / "platforms.json", default={}) or {}
    items = registry.get("platforms") if isinstance(registry, dict) else registry
    checks.append(_check(
        "平台注册表", bool(items), f"platforms.json 里 {len(items or [])} 个平台",
        "重新检出 platforms.json，或从 platforms.example.json 复制",
    ))
    return checks


def main() -> int:
    lc.setup_console()
    parser = argparse.ArgumentParser(description="开跑前的环境自检（依赖 / Edge / VPN / 配置 / 目录）")
    parser.add_argument("--config", help="配置文件")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    parser.add_argument("--vpn", action="store_true", help="顺带做 VPN 预检")
    args = parser.parse_args()
    lc.print_env()

    try:
        cfg = lc.load_config(args.config)
    except SystemExit:
        cfg = None

    checks = run_checks(args.config, cfg)

    if args.vpn:
        import vpn_check

        facts = vpn_check.collect_facts(cfg or {"vpn": {}})
        ok, reasons = vpn_check.judge(facts)
        checks.append(_check(
            "VPN 预检", ok,
            "虚拟网卡 Up、知网路由存在" if ok else "；".join(reasons),
            "打开 EasyConnect 连上学校 VPN 后重跑：python scripts\\vpn_check.py",
        ))

    failed = [c for c in checks if not c["ok"] and c["level"] == "关键"]
    warned = [c for c in checks if not c["ok"] and c["level"] != "关键"]

    if args.json:
        print(json.dumps(
            {"ok": not failed, "checks": checks, "failed": [c["name"] for c in failed],
             "warned": [c["name"] for c in warned]},
            ensure_ascii=False, indent=2,
        ))
    else:
        print("\n" + "=" * 62)
        print("环境自检")
        print("=" * 62)
        for item in checks:
            mark = "[通过]" if item["ok"] else ("[缺]" if item["level"] == "关键" else "[注意]")
            print(f"  {mark} {item['name']}：{item['detail']}")
        for item in failed + warned:
            if item["fix"]:
                print(f"    -> {item['fix']}")
        print("-" * 62)
        if failed:
            print(f"结论：{len(failed)} 项关键问题，先修好再跑。")
        elif warned:
            print(f"结论：可以跑，但有 {len(warned)} 项警告（功能可能降级）。")
        else:
            print("结论：全部通过，可以跑 python scripts\\run_all.py --interactive")
        print("=" * 62)

    return lc.EXIT_CONFIG if failed else (lc.EXIT_PARTIAL if warned else lc.EXIT_OK)


if __name__ == "__main__":
    sys.exit(main())
