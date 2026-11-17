# -*- coding: utf-8 -*-
"""creds.py —— 平台账号密码的本地保管（只在用户明确提供后使用）

定位：
    有些平台（CAS SciFindern / Analytical Methods / Formulus，以及部分学校的 SinoMed 个人账号）
    必须登录才能检索或下载。技能不会去猜、不会共享账号；流程是：
        检索失败/被登录墙拦住 → 明确告诉用户 → 用户选择「在浏览器里手动登录」或「保存账号」→ 重试。

存放：
    默认 %USERPROFILE%\\.literature-search\\credentials.json
    可用环境变量 LIT_CREDS_PATH 或 --path 覆盖；该文件不在技能目录里，不会被同步/打包。
    文件内含明文密码，仅作本机自动化用；不写日志、不出现在 检索任务.json / 平台探测.json。
    Windows 无法像 POSIX 一样 chmod 600，请确保这是你自己的用户目录；用完可 --delete。

参数：
    --list                       列出已保存的平台（密码一律显示 ******）
    --set ID                     保存/更新账号（密码交互输入，或 --password-stdin）
    --username U                 账号
    --password-stdin             从标准输入读密码（宿主自动化用；不落 shell 历史）
    --password-env NAME          从环境变量读密码
    --note TEXT                  备注（如「学校统一身份认证」）
    --delete ID                  删除
    --path FILE                  覆盖存储路径
    --json                       机器可读输出
退出码：0 成功 / 2 需要用户输入 / 3 用法或读写错误
依赖：标准库（getpass）
"""
from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import litcommon as lc  # noqa: E402


def default_path() -> Path:
    env = os.environ.get("LIT_CREDS_PATH")
    return Path(env) if env else Path.home() / ".literature-search" / "credentials.json"


def _path(path: str | Path | None = None) -> Path:
    return Path(path) if path else default_path()


def load(path: str | Path | None = None) -> dict:
    target = _path(path)
    if not target.exists():
        return {"version": 1, "platforms": {}}
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"version": 1, "platforms": {}}
    if not isinstance(data.get("platforms"), dict):
        data["platforms"] = {}
    return data


def _save(data: dict, path: str | Path | None = None) -> Path:
    target = _path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        os.chmod(target, 0o600)
    except OSError:
        pass
    return target


def get(pid: str, path: str | Path | None = None) -> dict | None:
    entry = (load(path).get("platforms") or {}).get(str(pid or "").strip())
    if not entry or not entry.get("username") or not entry.get("password"):
        return None
    return entry


def set_entry(pid: str, username: str, password: str, note: str = "",
              path: str | Path | None = None) -> Path:
    if not pid or not username or password is None:
        raise ValueError("平台 id、账号、密码都不能为空")
    data = load(path)
    data.setdefault("platforms", {})[pid] = {
        "username": str(username),
        "password": str(password),
        "note": str(note or ""),
        "saved_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    return _save(data, path)


def delete(pid: str, path: str | Path | None = None) -> bool:
    data = load(path)
    existed = (data.get("platforms") or {}).pop(pid, None) is not None
    _save(data, path)
    return existed


def describe(path: str | Path | None = None) -> list[dict]:
    """给日志/汇报用：绝不含密码。"""
    items = []
    for pid, entry in sorted((load(path).get("platforms") or {}).items()):
        items.append({
            "platform": pid,
            "username": entry.get("username") or "",
            "password": "******",
            "note": entry.get("note") or "",
            "saved_at": entry.get("saved_at") or "",
        })
    return items


def _read_password(args) -> str | None:
    if args.password_env:
        value = os.environ.get(args.password_env)
        if not value:
            print(f"环境变量 {args.password_env} 为空。", file=sys.stderr)
            return None
        return value
    if args.password_stdin:
        line = sys.stdin.readline()
        return line.rstrip("\r\n") if line else None
    if not sys.stdin.isatty():
        print("当前会话不可交互：请用 --password-stdin 或 --password-env 提供密码。", file=sys.stderr)
        return None
    try:
        first = getpass.getpass("密码（输入不回显）：")
        second = getpass.getpass("再输一次：")
    except (EOFError, KeyboardInterrupt):
        return None
    if first != second:
        print("两次输入不一致。", file=sys.stderr)
        return None
    return first


def main() -> int:
    lc.setup_console()
    parser = argparse.ArgumentParser(description="平台账号密码本地保管（只保存用户明确提供的）")
    parser.add_argument("--list", action="store_true", help="列出已保存的平台（掩码）")
    parser.add_argument("--set", help="保存/更新平台账号")
    parser.add_argument("--username", help="账号")
    parser.add_argument("--password-stdin", action="store_true", help="从标准输入读密码")
    parser.add_argument("--password-env", help="从环境变量读密码")
    parser.add_argument("--note", help="备注")
    parser.add_argument("--delete", help="删除平台账号")
    parser.add_argument("--path", help="覆盖存储路径")
    parser.add_argument("--json", action="store_true", help="机器可读输出")
    args = parser.parse_args()
    lc.print_env()

    if args.delete:
        existed = delete(args.delete, args.path)
        print(f"{'已删除' if existed else '本来就没有'}：{args.delete}")
        return lc.EXIT_OK

    if args.set:
        username = (args.username or "").strip()
        if not username:
            print("缺少 --username。", file=sys.stderr)
            return lc.EXIT_CONFIG
        password = _read_password(args)
        if not password:
            print("没有拿到密码，未保存。", file=sys.stderr)
            return lc.EXIT_NEED_USER
        target = set_entry(args.set, username, password, args.note or "", args.path)
        print(f"已保存：{args.set}｜账号 {username}｜密码 ******")
        print(f"位置：{target}")
        print("提示：该文件是明文，仅本机使用；用完可 python scripts\\creds.py --delete "
              f"{args.set}")
        return lc.EXIT_OK

    items = describe(args.path)
    if args.json:
        print(json.dumps({"path": str(_path(args.path)), "platforms": items}, ensure_ascii=False, indent=2))
        return lc.EXIT_OK
    print(f"凭据文件：{_path(args.path)}")
    if not items:
        print("（还没有保存任何平台账号）")
        return lc.EXIT_OK
    print(f"{'平台':<18}{'账号':<24}{'保存时间':<22}备注")
    print("-" * 78)
    for item in items:
        print(f"{item['platform'][:16]:<18}{item['username'][:22]:<24}"
              f"{item['saved_at'][:20]:<22}{item['note'][:24]}")
    return lc.EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
