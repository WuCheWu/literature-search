# literature-search：在校园 VPN 里批量找文献、下全文

给它**关键词**和**「你要什么样的文献」**，它在学校数据库里检索、逐篇判断相关性、把能下的全文下下来，
输出 **Excel（23 列）+ 可读报告 + 全文文件夹**。

拿不到确认（VPN、机构身份、登录、验证码、付费墙）就停下说清卡在哪——不硬跑，不假装成功。

---

## 1. 运行环境（硬件 / 系统 / 组件 + 下载地址）

### 硬件与系统

| 项 | 最低 | 建议 | 说明 |
| --- | --- | --- | --- |
| 系统 | Windows 10 x64 | Windows 11 x64 | 脚本要调 PowerShell 读网卡与进程 |
| 内存 | 4 GB | 8 GB | 同时开 Edge 可见窗口 + Excel |
| 磁盘 | 1 GB 空闲 | 10 GB+ | 全文 PDF 按篇累加，单篇约 1–10 MB |
| 网络 | 能连校园网/VPN | 稳定有线 | 知网对网络抖动敏感，断了要重连 |

### 组件与下载地址

| 组件 | 版本 | 下载 / 安装 | 检查 |
| --- | --- | --- | --- |
| Python | ≥ 3.10 | https://www.python.org/downloads/windows/ · 安装勾选 **Add python.exe to PATH** | `python --version` |
| pip 依赖 | — | `python -m pip install -r requirements.txt` · 国内慢加 `-i https://mirrors.aliyun.com/pypi/simple/ --trusted-host mirrors.aliyun.com` | `python -c "import requests,openpyxl,playwright"` |
| Microsoft Edge | 任意近期版本 | https://www.microsoft.com/zh-cn/edge/download （Win10/11 一般自带） | 能正常开网页 |
| Playwright | 随 pip 装 | https://playwright.dev/python/docs/intro · **用 Edge 通道不用再下浏览器内核** | `python -c "import playwright"` |
| VPN | 学校下发 | 用户自己预先连接好VPN并提前有过登录 | 客户端显示「已连接」 |
| PowerShell | 5.1 | 系统自带 | `powershell -Command $PSVersionTable.PSVersion` |
| CAJViewer | 可选 | https://cajviewer.cnki.net/ | 下载到 `.caj` 时用它打开 |

装完跑一次自检（只做一次，之后随时可跑）：

```powershell
cd D:\literature-search
python -m pip install -r requirements.txt
python scripts\doctor.py
```

`doctor.py` 逐项检查 Python 版本、三个依赖、Edge、EasyConnect、`config.json` 字段、输出目录可写、平台注册表；
全通过返回 0，有警告返回 1（能跑但可能降级），关键项缺失返回 3 并打印修复命令。

> VPN 只支持**深信服 EasyConnect**（脚本按 `EasyConnect.exe` + 网卡描述 `Sangfor SSL VPN` + 知网网段路由判定）。
> 用 Cisco AnyConnect / WebVPN 的学校过不了预检，需要改 `vpn_check.py`。

---

## 2. 运行逻辑（Agent 按这个走）

### 主流程：一条命令，九步

```text
doctor.py —通过→ run_all.py
                 ├ Step 0 采集任务（关键词/方向/平台/首次登录）
                 ├ Step 1 生成检索式 → 确认（--yes 可跳过）
                 ├ Step 2 VPN + 学校账号预检（硬门槛）
                 ├ Step 3 平台首次登录/机构设置确认
                 ├ Step 4 其他平台能力探测（能否检索/能否下 PDF）
                 ├ Step 5 PubMed + 知网 + 其他平台检索
                 ├ Step 6 跨库去重 + 按方向逐篇判定（纳入/待定/排除）
                 ├ Step 7 下载纳入文献的全文
                 ├ Step 8 生成 文献清单.xlsx + 文献列表.md
                 └ Step 9 汇报（数字与 Excel、文件数必须一致）
```

### Agent 决策规则（5 条）

1. 有**关键词 + 筛查方向** → 直接 `run_all.py --keywords ... --direction ... --confirm-vpn --confirm-account --confirm-platform-login --yes`。
2. 没有筛查方向 → **先问**，别自己编；问题清单：`python scripts\intake.py --print-questionnaire`。
3. 退出码 2 → 把「哪一步、看到什么、请你做什么、做完回什么」原样转达，等用户回复；**别自动重试**。
4. 同关键词重跑 → 默认断点续跑（复用已检索/已下载）；只换筛查方向不必重检索，**别加 `--refresh`**。
5. 只出清单不下载 → `--no-download`；只查某几个库 → `--platforms "pubmed,cnki"`。

### 脚本分工（可单步跑，排错时用）

| 脚本 | 什么时候用 | 命令 |
| --- | --- | --- |
| `doctor.py` | 开跑前、报错后 | `python scripts\doctor.py` |
| `run_all.py` | 一条命令走完 | `--interactive` 或 `--keywords --direction ...` |
| `intake.py` | 只要任务文件 / 看问题清单 | `--keywords "a,b" --direction "..." --out 任务.json`；`--print-questionnaire` |
| `platforms.py` | 看平台 / 解析学校清单 / 登记网址 | `--list`；`--parse 文件或网址`；`--add URL --name 名称` |
| `platform_login.py` | 平台首次登录与机构设置 | `--print-questions`；`--ready --platform X`；`--platform X --wait 180` |
| `probe_platform.py` | 试某网址能否检索/下载 | `--url U --keyword K`；`--url U --add`；`--all --quick` |
| `precheck.py` / `vpn_check.py` | 单独验 VPN 与机构身份 | `precheck.py --confirm-vpn --confirm-account`；`vpn_check.py --json` |
| `pubmed_search.py` / `cnki_search.py` | 单库检索 | `--query "..." --limit 20 --run-dir 目录` |
| `screen.py` | 只重跑筛选（换方向） | `--run-dir 目录` |
| `download_fulltext.py` | 只重跑下载 | `--run-dir 目录 [--all]` |
| `make_excel.py` / `make_report.py` | 只重出表 | `--run-dir 目录` |
| `creds.py` | 保存/删除账号（默认不保存） | `--list`；`--delete cnki` |

### 退出码

| 码 | 含义 | 动作 |
| --- | --- | --- |
| 0 | 全部成功 | 打开 `文献列表.md` |
| 1 | 部分失败 | 看 Excel「备注」列与 `日志\run.log` |
| 2 | 需要人处理（VPN/登录/验证码/缺参数/空检索式） | 按提示做完再重跑 |
| 3 | 配置或环境错误（缺依赖、配置缺字段、占位符参数） | 按提示修，或跑 `doctor.py` |

---

## 3. 快速开始

### 3.1 你自己在终端跑

```powershell
cd D:\literature-search
python scripts\doctor.py                  # ① 自检
python scripts\run_all.py --interactive   # ② 一条命令走完，它会问你 5 件事
```

会问：关键词、一句话说明要解决什么、**筛查方向**（必填）、文献时间、期望相关度。
答完先给你看检索式（回车通过），再依次跑 VPN 预检 → 平台登录 → 检索 → 筛选 → 下载 → 出表。
中途要你动手的只有三件：**连 VPN、在浏览器里登录一次、拖一次滑块**。

只想先落一份任务文件：

```powershell
python scripts\intake.py            # 结尾会打印「任务文件：<路径>」和下一步命令，复制即可
```

### 3.2 让 AI 助手跑（DSH 里就是这么用）

> 帮我查「斑马鱼 微塑料」的文献，只要斑马鱼活体暴露、看肠道菌群和消化功能的，近五年，每个库 20 条，能下的都下下来。

它会把 5 个问题问清，然后用自己的命令跑；遇到 VPN、登录、验证码会停下来问你。
你确认后回「都好了」——它用 `--confirm-vpn --confirm-account --confirm-platform-login` 记录你的确认。

---

## 4. 产物在哪

```text
D:\literature-search\输出\<关键词1>_<关键词2>_<日期>\
├─ 文献列表.md      ← 先看这个：任务、平台权限、统计、纳入/待定/排除，每篇带摘要与选择依据
├─ 文献清单.xlsx    ← 23 列，可排序筛选；状态列按结果着色
├─ 全文\            ← PDF / CAJ，文件名 = 第一作者_年份_标题
├─ 检索任务.json    ← 本次规则（关键词/方向/平台/年限），重跑直接复用
├─ 日志\run.log     ← 每步时间、检索式、判定、异常原文
└─ records_*.json   ← 各库原始记录
```

探测与登录状态另有缓存：`D:\literature-search\输出\_平台能力\`、`_平台登录\`（各自有独立 TTL）。

---

## 5. 卡住了怎么办

| 现象 | 意思 | 处理 |
| --- | --- | --- |
| `缺少 Python 依赖：openpyxl` | 没装包 | `python -m pip install -r requirements.txt` |
| `找不到配置文件` | 配置缺失 | `Copy-Item config.example.json config.json` |
| `未检测到描述含 Sangfor SSL VPN 的虚拟网卡` | VPN 没连 | EasyConnect → `vpn.example.edu.cn` → 学号登录 → 显示「已连接」 |
| 网卡 Up 但 IP 是 `169.254.x.x` | 断线残留 | 客户端断开再重连 |
| 结果页没校名，只有「登录/充值」 | 没落到校内出口 | 断开重连；仍不行换网络 |
| 知网跳 `/verify/` 拼图 | 风控 | 在打开的浏览器里拖一次滑块，脚本自动继续 |
| 某平台提示要登录 | 该平台要账号 | `platform_login.py --platform X --wait 180` 带登录一次，再用**原命令**重跑 |
| `需付费` / `无权限` | 学校没订这篇 | 走图书馆文献传递 / 馆际互借 |
| 下到 `.caj` 打不开 | 需要 CAJViewer | 装 https://cajviewer.cnki.net/ |
| PDF 只有几 KB | 下到登录页/错误页 | 脚本会删掉并记「下载失败」，重跑通常能过 |
| 换了学校/账号后仍判定旧状态 | 缓存没过期 | 加 `--refresh-probe` 重跑 |
| 控制台中文乱码 | 终端不是 UTF-8 | PowerShell 先 `chcp 65001`，或换 Windows Terminal |
| `[阻塞] 检索式为空` | 主题不在内置词表 | 补 `--keywords-en`（与中文一一对应）重跑 |

---

## 6. 平台与权限

`platforms.json` 注册 **7 个平台**（`--platforms` 用这些 id）：

| id | 平台 | 网址 | 访问方式 | 全文能力 |
| --- | --- | --- | --- | --- |
| `pubmed` | PubMed | https://pubmed.ncbi.nlm.nih.gov/ | 公开 | 开放获取或出版社页面 |
| `cnki` | 中国知网 | https://www.cnki.net/ | 校园 IP | 学校订阅 PDF/CAJ |
| `sinomed` | SinoMed | http://www.sinomed.ac.cn/ | 校园 IP / 个人账号 | 题录为主，部分全文链接 |
| `sciencedirect` | ScienceDirect | http://www.sciencedirect.com | 校园 IP / 机构登录 | 学校订阅 PDF |
| `scifinder-n` | CAS SciFindern | https://scifinder-n.cas.org/ | CAS 账号 | 订阅全文/专利链接 |
| `cas-methods` | CAS Analytical Methods | https://methods.cas.org | CAS 账号 | 方法全文 |
| `cas-formulus` | CAS Formulus | https://formulus.cas.org | CAS 账号 | 配方全文 |

知网名下另有两个入口网址，**同属 `cnki`，不是独立 id**（要直接查它们用 `platform_search.py --entry`）：
旧版 https://www.cnki.net/old · 专利 CIPD http://epub.cnki.net/kns/brief/result.aspx?dbPrefix=CIPD 。

探测结论（`平台探测.json`）：`fulltext_ok` 能检索能下 PDF / `oa_partial` 部分开放 /
`metadata_only` 只有题录 / `login_required` 需登录 / `no_search` 无检索入口 / `unreachable` 打不开。

---

## 7. 学校清单 / 自定义网址

```powershell
python scripts\platforms.py --parse "D:\数据库清单.html"    # 认平台，列出没认出的网址并给登记命令
python scripts\platforms.py --add "https://xxx.edu.cn/search" --name "学校自建库"
python scripts\probe_platform.py --url "https://xxx.edu.cn/search" --keyword "微塑料"

python scripts\run_all.py --school-list "D:\数据库清单.html" --keywords "关键词" `
  --direction "筛查方向" --confirm-vpn --confirm-account --confirm-platform-login --yes
```

清单支持 txt / html 文件或网页；PDF / Word 会提示先另存为文本——不猜内容。

---

## 8. 账号、密码、换学校

默认**手动登录**，不保存密码。需要才用：`python scripts\creds.py --list` / `--delete cnki`。
保存位置 `%USERPROFILE%\.literature-search\credentials.json`（**明文**，仅本机）；密码不进任务、日志、清单。

换学校改 `config.json` 这 **5 个键**（`config.json` 已随附，要重置就从 `config.example.json` 复制）：

| 键 | 改成 |
| --- | --- |
| `vpn.portal` | 你学校的 VPN 门户 |
| `vpn.client_dir` | 你机器上 EasyConnect 安装目录 |
| `vpn.cnki_probe_route` | 你学校 VPN 下访问知网的网段；**不知道就留空**（留空=跳过该检查，只验网卡 Up） |
| `institution.name` | 学校全名（脚本拿它去知网结果页找校名） |
| `institution.library_url` | 图书馆网址 |

改完加 `--refresh-probe` 跑一次，否则会沿用旧学校的登录状态与探测缓存。

其他常用：`output_root`（产物放哪）、`browser.profile_dir`（登录态存这，删了要重登）、
`browser.channel`（`msedge`/`chrome`）、`download.max_per_run`、`platforms.cache_ttl_days`。

---

## 9. 边界与术语

**它不做**：绕付费墙、点购买/试用、Sci-Hub/盗版站、代你登录、伪造机构身份、写综述、做统计分析。
**它做不到**：订阅范围外下全文；JS 应用（CAS 三库）只保证尽力抓取；筛选是关键词加减分，不读语义。

| 词 | 意思 |
| --- | --- |
| 平台 / 库 | 要检索的网站（PubMed、知网、ScienceDirect……） |
| 题录 | 只有标题、作者、期刊、摘要，没有全文 |
| 检索式 | 真正发给数据库的查询语句 |
| 纳入 / 待定 / 排除 | 逐篇判定结果，默认只下「纳入」 |
| 探测 | 先小规模试一次：能否检索、能否下 PDF |
| 机构身份 | 数据库认出你是学校用户（靠校园网出口 IP，不用个人账号） |
| 宿主 | 跑这些脚本的 AI 助手（在 DSH 里就是我） |

---

执行细节见 [`SKILL.md`](SKILL.md)，本次改动见 [`修改日志.md`](修改日志.md)。
开始前先跑 `python scripts\doctor.py`。

## 这是一个臭不要脸的赞助码

如果对你有帮助，可以先收藏，如有反馈，请留言

<img width="708" height="782" alt="赞助码" src="https://github.com/user-attachments/assets/db9c665a-08ed-44b1-b365-14f97ffc2010" />





