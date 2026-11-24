---
name: literature-search
description: >
  校园 VPN 下的多平台文献检索与全文下载：先弹选择框收集关键词/搜索提示词/筛查方向/文献时间/期望相关度/检索平台，
  按学校公布的数据库清单设置平台（SinoMed、知网新版/旧版/CIPD、ScienceDirect、CAS SciFindern/Methods/Formulus，支持自定义网址），
  确认 VPN、学校账号与平台首次登录后，先探测每个网址能否检索/下载，再逐平台检索、按方向筛选、下载纳入文献，
  输出带原文标题/摘要/选择依据的结构化清单。
  触发：查文献、检索文献、按方向筛文献、按学校数据库清单找、SinoMed/ScienceDirect/SciFindern 找文献。
  宿主无关；不绕付费墙、不猜账号密码、不自动连 VPN；滑块/验证码/首次登录必须人工完成。
---

# 校园 VPN 多平台文献检索与全文下载

## 0. 一句话

说「检索 XX 文献」，技能按固定顺序完成：**采集任务（关键词/方向/平台/首次登录）→ 确认 VPN 与学校账号
→ 确认平台首次登录与机构设置 → 探测每个平台能否检索/下载 → 多平台检索 → 按方向筛文献
→ 下载纳入文献 → 结构化清单 → 汇报**；任何一关拿不到确认就停下说清楚，不硬跑、不假装成功。

四类交付物：`文献列表.md`（每篇含原文标题/摘要/选择依据）、`文献清单.xlsx`（23 列）、`全文/`（PDF）、
`日志/run.log`；另有 `平台探测.json`（能力证据）、`登录准备.json`（首次登录确认）、`平台待处理.json`。

### 0.1 三条新增规则的落点

| 规则 | 要求 | 落点 |
| --- | --- | --- |
| 规则 5 | 按学校公布的文件设置平台并检索；查询失败后询问账号密码；自助探测用户给的中/外网网址能否下载 | `intake.py --school-list` / `platforms.py --parse`；`platform_search.py` 登录墙返回 2；`probe_platform.py` 能力探测 |
| 规则 6 | 可用平台：SinoMed、知网新版/旧版/CIPD、ScienceDirect、CAS SciFindern、Analytical Methods、Formulus；另加 PubScholar | `platforms.json` 内置 8 个平台（6 家族/8 规定网址 + PubMed + PubScholar），脚本只认注册表 + 用户网址 |
| 规则 7 | 有些平台第一次要登录并设置机构/学校，否则拿不到文献；必须先问用户 | `intake.py` 采集 `login_setup`；`platform_login.py` 逐平台确认/等待登录；`run_all.py` Step 3 未确认即暂停 |

## 1. 铁律（不可协商）

0. **VPN 硬闸（最高优先级）**：
   - 检测 VPN 前必须先在宿主界面弹确认（同款审批框）：
     「需要调用 PowerShell 读取网卡与进程，检测 VPN 状态」；
   - **未获允许 → 不检测、不检索、不下载**，退出码 2，告诉用户怎么允许；
   - 运行中任何阶段（PubMed / 知网 / 其他平台 / 下载途中）检测不到 VPN → **立即停**，
     提示「检测到 VPN 接入可能已断开（阶段），请重连后重跑」，已完成的记录与下载保留；
   - `--skip-vpn` 已废除（传了直接退出码 3）；`--allow-vpn-detect` 表示「用户已允许检测」。
1. **先问再做**：启动必须先拿到「关键词 + 文献搜索提示词 + 筛查方向 + 检索平台 + 首次登录状态」；
   没有筛查方向不得筛选，没有平台不得检索，缺项就问，不猜。
2. **三关确认，缺一不检索**：
   ① VPN 已连 + 学校账号已登录（`precheck.py`）；
   ② 平台首次登录/机构登录设置已确认（`platform_login.py`，规则 7）；
   ③ 至少一个平台可用（探测可达、有检索入口）。
   任一关无法确认 → 退出码 2，明确告诉用户「已暂停」，不检索、不下载。
3. **平台来源限定**：只从 `platforms.json`（规则 6 内置 + 已登记自定义）与用户本次提供的网址里选；
   不擅自换库、不偷偷加 Sci-Hub/盗版站、不把 PubMed 当成学校订阅的替代。
4. **首次登录必须问**：对每个非公开平台都要问「是否第一次在这台电脑用？是否已在登录页设置好学校/机构？」。
   用户答「第一次/不确定」→ 先带他登录一次，再检索；**不能假设浏览器里有登录态**。
5. **账号密码协议**：优先让用户在可见浏览器里**手动登录**（验证码/SSO/二次验证都走人工）；
   只有用户明确要求保存时才写 `creds.py`。不猜密码、不共享账号、不把密码写进任务/日志/清单。
   检索失败（登录墙、无权限）→ 停下问用户，给出「手动登录」和「保存账号」两条命令。
6. **探测先行**：非 PubMed/知网平台，检索前先跑 `probe_platform.py`，确认可达、有检索入口、
   有 PDF 入口/是否付费墙；结论写 `平台探测.json`，截图留证。探测结论只描述事实，不推断未看到的权限。
7. **筛选严格照方向**：纳入/排除只看 `检索任务.json` 的规则，脚本不加戏；每篇必须写出选择依据。
8. **网卡有 IP ≠ 已连接**：断线后 IPv4 会残留；门户 200 ≠ 已连接；`SangforPWEx` 在跑 ≠ 已连接。
9. **首页不能判机构身份**：知网首页只显示「充值/会员/机构登录」，必须进检索结果页看校名。
10. **滑块/验证码只能人过**：打印指引并阻塞等待，超时截图 + 退出码 2；不破解、不用 headless。
11. **不点购买/充值/试用**，不绕付费墙，不用 Sci-Hub/盗版站/第三方代下。
12. **单篇失败不拖垮整批**：记录状态继续，最后单列；平台整体不可用则跳过并汇报。
13. **频率克制**：翻页/详情/下载间隔 ≥2.5 秒，单次上限 `max_per_run`。
14. **数字必须对得上**：汇报的纳入/待定/排除、PDF 篇数必须等于清单与文件；摘要不改写，取不到就留空注明。

## 1.5 询问协议（一次一题，带推荐答案）

Step 0 与 Step 3 向用户取信息时，**按下面的方式问**，不一次性甩问卷：

1. **一次一题**：问完等回答，再问下一题。
2. **每题带推荐答案**：先说「我建议……」，再给一句理由；用户可直接采纳。
3. **能查的事实自己查**：`config.json`、`platforms.json`、`vpn_check` 实测、上次运行目录、
   探测/登录缓存——只有「决策」才问用户。
4. **未达成共识不动手**：关键词、筛查方向、平台、登录状态、VPN 通道没齐之前，
   不检索、不下载；缺项停在退出码 2，不猜、不默认放行。

问题清单已内置这些字段：`python scripts\intake.py --print-questionnaire` 每题的
`recommended`（推荐答案）、`why`（为什么要问）、顶层 `protocol`（规则与顺序）。宿主照它逐题问即可。

### 1.5.1 决策树（父决策先定，再展开子决策）

```text
VPN 通道 ──┬─ 学校叫什么
           ├─ 用什么客户端/门户（EasyConnect 只是其中一种）
           └─ 现在连没连（不靠口头确认，靠机构识别实测）
   ↓
检索范围 ──┬─ 关键词（中/英）
           ├─ 筛查方向（只保留什么、排除什么）
           └─ 时间范围 / 相关度 / 每库条数
   ↓
平台与登录 ┬─ 查哪些库（学校清单里有就按清单）
           └─ 上次登录过没有、机构设置存没存
   ↓
全文范围 ──┬─ 要不要全文（只要清单就跳过下载）
           └─ 待定文献要不要也下
```

### 1.5.2 自查 vs 必问

| 自己查（别问用户） | 必须问（决策归用户） |
| --- | --- |
| `config.json` 是否已有学校/门户/输出目录 | 学校叫什么（首次配置） |
| `platforms.json` 有哪些平台、别名 | 用什么方式连 VPN（程序不代连、不猜客户端） |
| `vpn_check` 的客户端/网卡/路由事实 | 这次查什么、只保留什么样的文献 |
| 上次运行目录里的 `检索任务.json` | 要不要全文、待定要不要也下 |
| 探测/登录缓存的状态与有效期 | 有没有 CAS 等平台账号（密码永远用户自己输） |

### 1.5.3 VPN 与文献信息的推荐答案

| 问题 | 推荐答案 | 理由 |
| --- | --- | --- |
| 用什么方式连 VPN | 用你现有的客户端自己连，程序不启动客户端、不碰账号密码 | 登录由人完成，程序只验证结果 |
| 连没连上 | 不口头确认，直接做机构识别实测（知网结果页出现校名） | 网卡有 IP ≠ 已连接；门户 200 ≠ 已连接 |
| 查哪些关键词 | 用户原话里的名词；英文由用户提供，程序不自动翻译 | 缺英文会拼出空检索式，宁可停下问 |
| 筛查方向 | 一句话说清「只保留什么 + 排除什么」，不接受「待定」「...」 | 占位符会被当规则写进任务 |
| 时间 / 条数 / 相关度 | 近 5 年 / 每库 20 条 / 标准档 | 准确率与耗时的平衡点 |
| 要不要全文 | 默认只下「纳入」；没有 playwright 或邮箱时降级并说明 | 缺依赖不该拖垮整轮，但必须告知 |

### 1.5.4 英文词的专业性（硬要求）

中文→英文**必须用生物学/化学的学科术语**，不用日常义译；拿不准就给 2–3 个候选让用户挑，
最终以用户确认为准（写进 `keywords_en` 或 `screening.terms_en`）。
`expose`/`effect`/`study` 这类泛词不能单独充当检索词。

## 2. Step 0：启动交互（必做，不能跳）

### 2.1 三种采集方式

| 场景 | 做法 |
| --- | --- |
| 宿主有选择框（WorkBuddy `AskUserQuestion`） | 读 `python scripts/intake.py --print-questionnaire`，按 `questions[]` 逐条问；再 `--from-json` 落盘 |
| 用户自己在终端跑 | `python scripts/intake.py` 走向导（编号选择 + 文本输入） |
| 用户一次性说全了 | `run_all.py --keywords ... --direction ... --platforms "pubmed,cnki,sinomed" --first-login ready ...` |
| 只给关键词 | 允许但会降级为关键词匹配并在清单标注；应主动追问方向 |

### 2.2 必问项

| 项 | 必填 | 说明 | 选项/示例 |
| --- | --- | --- | --- |
| **VPN 通道** | **是** | 你学校用哪种方式连 VPN（EasyConnect / 其他客户端 / WebVPN）；程序只检测、不代连、不启动客户端 | 默认「还没连，我先去连」；落盘为 `task.vpn.channel` |
| 关键词 | 是 | 中文，逗号分隔；英文必须由用户给（程序不翻译） | `斑马鱼, 微塑料` |
| 英文关键词 | 否 | 与中文一一对应，用于 PubMed；**由用户提供、用生物/化学专业术语**，程序不内置词表、不自动翻译 | `zebrafish, microplastic` |
| 关键词同义词 | 否 | 用户给的复数/学名/旧称，以 OR 并入检索式，补漏检 | `斑马鱼=danio rerio; 微塑料=microplastics,nanoplastic*` |
| 方向要点门槛 | 否 | 纳入须命中的方向要点数（默认 5，超过要点数时封顶） | `5` |
| 文献搜索提示词 | 否 | 一句话研究问题 | `聚丙烯微塑料暴露对斑马鱼肠道菌群的影响` |
| 筛查方向 | 是 | 只保留什么样的文献；逐篇判定的硬规则 | `斑马鱼活体暴露，关注肠道菌群/炎症/抗氧化；排除吸附降解` |
| 纳入/排除/必含 | 否 | 纳入要点可从方向自动识别；排除词命中标题即排除；必含缺失即排除 | `肠道菌群, 炎症` / `综述, 吸附` / `斑马鱼` |
| 文献时间 | 是 | 近 3/5/10 年或自定义 | 默认近 5 年 |
| 期望相关度 | 是 | 宽松 40 / 标准 55 / 严格 70 分 | 默认标准 |
| 每库条数 | 否 | 10/20/30/50 | 默认 20 |
| **检索平台** | **是** | 多选：PubMed、知网、SinoMed、ScienceDirect、SciFindern、CAS Methods、CAS Formulus；可加自定义网址 | 默认 PubMed + 知网 |
| **学校数据库清单** | 否 | 学校公布的清单文件（txt/html）或图书馆页面网址；自动识别平台、未识别网址登记为自定义 | `https://library.example.edu.cn/` 或 `D:\数据库清单.html` |
| **补充网址** | 否 | 清单外的中/外网网址，逗号分隔；先探测再检索 | `http://www.sinomed.ac.cn/` |
| **首次登录** | **是** | 「都登录过且设置好机构」「有平台第一次用」「不确定逐个检查」 | 默认逐个检查 |
| **首次登录平台** | 否 | 用户指定哪些平台需要首次登录；留空=除 PubMed 外逐个检查 | `SciFindern, SinoMed` |
| 是否下载 | 是 | 只出清单 or 下载纳入文献 | 默认下载 |
| 纳入上限 | 否 | 超出转待定 | 默认 60 |

问的时候要**给选项而不是空问**；平台项要把「访问方式 + 全文能力」写在选项里，让用户知道为什么要登录。

### 2.3 任务文件（`检索任务.json`）

```json
{
  "keywords": ["斑马鱼", "微塑料"],
  "keywords_en": ["zebrafish", "microplastic"],
  "search_prompt": "聚丙烯微塑料暴露对斑马鱼肠道菌群与消化功能的影响",
  "screening": {
    "mode": "strict",
    "direction": "斑马鱼活体暴露实验，关注肠道菌群、消化、炎症与抗氧化",
    "include": ["肠道菌群", "消化", "炎症", "抗氧化"],
    "exclude": ["综述", "吸附", "降解"],
    "must_have": ["斑马鱼"],
    "prefer": [],
    "doc_types_exclude": ["会议", "征稿"],
    "relevance": "standard",
    "min_score": 55,
    "band": 15,
    "max_items": 60,
    "years_min": 2022,
    "years_max": 2026
  },
  "search": {
    "years": 5, "years_min": 2022, "years_max": 2026,
    "limit": 20,
    "platforms": ["pubmed", "cnki", "sinomed", "sciencedirect"],
    "databases": ["pubmed", "cnki"],
    "custom_platforms": [
      {"id": "custom-xxx-edu-cn", "name": "学校自建库", "entry": "https://xxx.edu.cn/search",
       "adapter": "generic", "kind": "unknown", "selectors": {}}
    ],
    "login_setup": {"status": "first_time", "pending": ["scifinder-n"]},
    "probe": true,
    "download": true
  }
}
```

`intake.py` 会补全并校验：缺 `keywords` / `screening.direction` / 未知平台 → 退出码 3。
`databases` 只是 PubMed/知网的向后兼容子集，真正执行看 `platforms`。

## 3. Step 1–3：检索前的四道门槛

### 3.1 VPN + 学校账号（`precheck.py`）

1. **技术预检**：EasyConnect.exe、进程、虚拟网卡 `Status=Up`、知网路由 `<知网网段>/24`（字段留空则跳过此项）。
2. **用户确认**（两项都要 y）：① EasyConnect 显示已连接且是本人学校账号；② 浏览器已用学校账号登录/从图书馆入口进。
   非交互会话必须由宿主先问过，再带 `--confirm-vpn --confirm-account`；**不能因为看起来像连上了就自己加参数**。
3. **机构身份实测**：可见 Edge 打开知网 → 人工过滑块 → 检索一次 → **结果页出现校名**才算通过。
   只查 PubMed 或已 `--skip-institution` 时跳过，但日志要注明「未验证学校订阅权限」。

### 3.2 平台首次登录/机构设置（`platform_login.py`，规则 7）

对每个非公开平台逐个确认：

- 用户说「都登录过且设置好了」→ 记为 `ready`，不打开浏览器；
- 用户说「有平台第一次用」→ 对指定平台打开登录页，等用户完成登录/机构选择，最多 `--wait 180` 秒；
- 用户说「不确定」→ 逐个问；没确认就 **退出码 2，不进入探测/检索**。

```powershell
# 宿主逐项问（返回 JSON 问题清单）
python scripts\platform_login.py --print-questions

# 带用户登录一次（完成 SSO/验证码/选学校）
python scripts\platform_login.py --platform scifinder-n --wait 180

# 用户确认都登录过：只记录，不打开浏览器
python scripts\platform_login.py --ready --platform pubmed --platform cnki --platform sinomed

# 只读缓存
python scripts\platform_login.py --check
```

登录状态缓存在 `<output_root>\_平台登录\`，默认 30 天；**未就绪不缓存**，下次继续问，避免一次失败后长期被挡。
缓存与 `browser.profile_dir` 绑定，换 profile 会自动失效。

### 3.3 平台能力探测（`probe_platform.py`，规则 5）

对每个非 PubMed/知网平台打开入口，实测：

| 结论 | 含义 | 后续动作 |
| --- | --- | --- |
| `fulltext_ok` | 有检索框、有结果、详情页有 PDF 入口且未见付费墙 | 正常检索 + 尝试下载 |
| `oa_partial` | 有 PDF 入口但也有 Get access/无权限提示 | 检索；能下的下，其余标 `需付费` |
| `metadata_only` | 能检索，但只有题录/无 PDF 入口 | 检索题录；全文换其他订阅库 |
| `login_required` | 有登录墙（密码框/机构登录） | 停下问用户：手动登录或保存账号 |
| `no_search` | 页面能开，但找不到检索入口 | 换入口网址/人工确认；不硬抓 |
| `unreachable` | 打不开（VPN/域名/证书） | 跳过该平台，汇报里单列 |

```powershell
# 探测一个用户给的网址；--add 先登记为自定义平台
python scripts\probe_platform.py --url "https://xxx.edu.cn/library" --add
python scripts\probe_platform.py --platform custom-xxx-edu-cn --keyword "微塑料" --save

# 探测已登记平台 / 全部
python scripts\probe_platform.py --platform sinomed --keyword "微塑料"
python scripts\probe_platform.py --all --quick
```

结果写 `<run_dir>\平台探测.json`，截图在 `平台探测\截图\`；缓存默认 7 天（`platforms.cache_ttl_days`）。
`--save` 把学到的检索框/结果链接选择器写入 `platform_learned.json`，通用适配器下次优先用。

## 4. 平台与权限（规则 5/6）

### 4.1 内置平台（`platforms.json`）

| 平台 | 规定网址 | 访问方式 | 全文能力 | 适配器 |
| --- | --- | --- | --- | --- |
| SinoMed | `http://www.sinomed.ac.cn/` | 校园 IP/个人账号 | 题录为主，部分全文链接 | 通用 |
| 知网（新版） | `https://www.cnki.net/` | 校园 IP | 学校订阅 PDF/CAJ | 专用 |
| 知网（旧版） | `https://www.cnki.net/old` | 校园 IP | 同上，尽力抓取 | 通用 |
| CNKI 专利（CIPD） | `http://epub.cnki.net/kns/brief/result.aspx?dbPrefix=CIPD` | 校园 IP | 专利全文 | 通用 |
| ScienceDirect | `http://www.sciencedirect.com` | 校园 IP/机构登录 | 学校订阅 PDF | 通用 |
| CAS SciFindern | `https://scifinder-n.cas.org/` | CAS 个人账号 | 订阅全文/专利链接 | 通用（JS 应用） |
| CAS Analytical Methods | `https://methods.cas.org` | CAS 个人账号 | 方法全文 | 通用（JS 应用） |
| CAS Formulus | `https://formulus.cas.org` | CAS 个人账号 | 配方全文 | 通用（JS 应用） |
| PubMed | `https://pubmed.ncbi.nlm.nih.gov/` | 公开 | OA/出版社页面 | 专用 API |
| PubScholar | `https://pubscholar.cn/explore` | 公开 | 题录 + 全文链接（下载看权限） | 通用（可见浏览器） |
| 马研库 | `https://marxism.ssap.com.cn/` | 公开（检索免登录） | 全文需机构/个人登录 | 通用（可见浏览器） |
| 马理论平台 | `https://mks.cnki.net/index.html` | CNKI 机构账号（需登录） | 学校订阅 | 通用（可见浏览器） |

> 规则 6 的 8 个网址全部在上面；PubMed 与 PubScholar 是公共库，保留但不替代学校订阅库。
> PubScholar 禁止第三方直连 API（`/hky` 返回 403「第三方应用独立请求时，无此操作权限」），
> 只能像知网一样用可见浏览器检索，选择器首次运行需校准。

### 4.2 从学校清单设置平台

```powershell
# 先解析清单，看识别到哪些平台、哪些网址没认出来
python scripts\platforms.py --parse "D:\数据库清单.html"
python scripts\platforms.py --parse "https://library.example.edu.cn/"

# 未识别的网址逐个登记（同一主机只登记一次）
python scripts\platforms.py --add "https://xxx.edu.cn/search" --name "学校自建库"

# 采集/开跑时直接带清单
python scripts\run_all.py --school-list "D:\数据库清单.html" --keywords ... --direction ...
```

清单支持 txt/html 文件或网页；PDF/Word 会明确拒绝（提示另存为 txt/html 或给页面网址），**不猜内容**。
匹配依据：网址主机 + 平台名/别名；未识别网址作为自定义平台随任务走，检索前先探测。
拉丁别名按**词边界**匹配且至少 3 个字符（避免「CAS 号」「SD 卡」误判成 SciFindern / ScienceDirect），
输出里会标「命中：xxx」——清单把 MEDLINE 当独立库时会算作 PubMed，把「生物医学文献」算作 SinoMed，请跟用户核对后再跑。

### 4.3 登录、账号与密码

1. **默认手动登录**：脚本用与检索相同的持久化 profile 打开登录页，用户自己完成 SSO/验证码/机构选择；
   登录一次后 Cookie 留在 profile，后续复用。这是最稳、也最不涉及密码保管的方式。
2. **用户明确要求保存账号**时才用 `creds.py`：

```powershell
python scripts\creds.py --set scifinder-n --username 你的账号
python scripts\creds.py --list          # 密码显示 ******
python scripts\creds.py --delete scifinder-n
```

- 默认位置 `%USERPROFILE%\.literature-search\credentials.json`（明文，仅本机；可 `LIT_CREDS_PATH` 覆盖）。
- 不写 `检索任务.json`、`平台探测.json`、`run.log`、`文献列表.md`；日志只记录「已用保存账号登录」。
- 主流程不带密码参数时，`platform_search.py` 在登录墙处返回退出码 2，并打印两条处理命令；
  宿主必须把这两条命令转达给用户，不能自己编账号。

3. **为什么必须问首次登录**：VPN 只解决网络，不等于平台已登录。第一次用 ScienceDirect/SinoMed/CAS 时，
   若没在登录页选学校/机构或完成统一认证，页面只给摘要或登录页，脚本拿不到全文。
   所以 `login_setup` 是硬门槛，不能跳。

### 4.4 自定义网址的边界

- 允许：学校图书馆页面、学校自建库、用户明确知道能查文献的中/外网网址。
- 探测只回答「能不能检索、有没有 PDF 入口、是否有付费墙」，不承诺一定能下全文。
- 通用适配器靠 meta（citation_* / DC.*）、JSON-LD、页面选择器抓字段；JS 应用（CAS 三库）可能只拿到标题/摘要，
  报告里要如实标 `metadata_only` 或 `仅摘要`，**不编造摘要/作者/年份**。
- 不把「探测到登录页」当成失败：它是需要用户登录的信号，返回 2 并提示。

## 5. 配置（唯一要改的地方）

`config.json`（从 `config.example.json` 复制）：

| 字段 | 含义 | 默认 |
| --- | --- | --- |
| `vpn.portal` / `client_dir` / `vnic_desc_match` / `cnki_probe_route` | VPN 门户/客户端/网卡/路由 | 见示例 |
| `institution.name` / `library_url` | 机构名（结果页判据）/图书馆入口 | 示例大学 |
| `databases.pubmed.retmax/years/email` | PubMed 条数/年限/礼貌邮箱 | 20/5/空 |
| `databases.cnki.retmax/years` | 知网条数/年限 | 20/5 |
| `browser.channel` / `profile_dir` / `verify_wait_sec` | Edge / **持久化 profile 别删** / 滑块等待 | msedge / `D:\literature-search\.lit-profile` / 180 |
| `platforms.cache_dir` | 平台能力缓存 | `<output_root>\_平台能力` |
| `platforms.login_state_dir` | 首次登录状态 | `<output_root>\_平台登录` |
| `platforms.cache_ttl_days` / `login_state_ttl_days` | 能力/登录缓存天数 | 7 / 30 |
| `platforms.probe_detail` | 探测是否进详情页 | true |
| `platforms.wait_login_sec` / `interval_sec` / `limit` | 登录等待/平台检索间隔/每平台条数 | 180 / 2.5 / 20 |
| `download.max_per_run/interval_sec/timeout_sec/retry/min_bytes` | 下载控制 | 30/3/90/1/10240 |
| `download.selected_only` / `include_pending` | 只下纳入 / 待定也下 | true / false |
| `output_root` | 产物根目录 | `D:\literature-search\输出` |

换学校只改 `vpn.portal`、`institution.name`、`institution.library_url`；平台清单用 `platforms.json` 管。

## 6. 执行流程（Step 0–9）

### Step 0 任务采集

见第 2 节。`run_all` 取任务优先级：`--task` > 参数 > 旧 `--keyword` > 交互向导 > 打印问题清单并退出码 2。

### Step 1 检索式确认（`plan_query.py`）

输出概念组（用户给的中英关键词对）、PubMed 式、CNKI 字段式、CNKI 实际输入；缺英文 → 退出码 2，提示补 `--keywords-en`，不猜译、不查内置词表。

### Step 2 预检（`precheck.py`）

见 3.1；未通过直接返回，后面的登录/探测/检索/筛选/下载/出表都不执行。

### Step 3 平台登录准备（`platform_login.py`）

见 3.2；未确认 → 退出码 2，不打开探测/检索。

### Step 4 平台能力探测（`probe_platform.py`）

只对非 PubMed/知网平台；缓存有效则复用。`login_required` 不阻塞其他平台，但会写入 `平台待处理.json`。

### Step 5 多平台检索

- **PubMed**：E-utilities（esearch + efetch）→ `records_pubmed.json`；429/5xx 退避重试。
- **知网**：可见 Edge → 滑块人工 → 列表 `a.fz14` → 详情页取字段 → `records_cnki.json`。
- **其他平台**：`platform_search.py` → 登录处理 → 检索框 → 结果列表 → 逐条详情 → `records_<平台>.json`。
  - 英文平台用英文关键词，中文平台用中文关键词（`platform.lang`）。
  - 登录墙且无账号 → 返回 2 + `平台待处理.json`，其他平台继续。
- **跨库去重**：DOI/标题相同只留一条（PubMed > 知网 > 平台），其余写 `其他来源` 与备注。
- 断点续跑：已有非空 `records_<平台>.json` 直接复用，`--refresh` 强制重检。

### Step 6 筛选（`screen.py`）

读 `检索任务.json`，为每篇算匹配分与判定，写回 `records_*.json` 与 `筛选结果.json`。
规则见第 7 节。

### Step 7 下载（`download_fulltext.py`）

只下**纳入**（待定看 `download.include_pending`，`--download-pending` 可开）；排除不下。

- PubMed：PMC/Europe PMC → Unpaywall；PMC 直连 403 时走「可见 Edge 过盾 + `api/getPdf`」。
- 知网：`#pdfDown` → `expect_download`；只有 `#cajDown` → `.caj` 标 `仅CAJ`。
- 其他平台：详情页 `pdf_button`/`.pdf` 直链 → 带 Cookie 请求；否则点按钮/新标签页取 PDF。
  登录墙 → `需登录`；付费墙 → `需付费`；无 PDF 入口 → `仅摘要`/`无权限`。
- 统一命名 `第一作者_年份_标题前40字.pdf`，重名加 `_2`，校验 `size >= min_bytes` 且前 4 字节 `%PDF`。

### Step 8 清单 + 汇报

`make_excel.py` 生成 23 列 Excel（判定/匹配分/关键词匹配/方向匹配/选择依据；冻结 D2、自动筛选、状态着色）；
`make_report.py` 生成 `文献列表.md`，含「任务与确认信息 → 检索平台与权限 → 筛选统计 → 纳入/待定/排除」。
随后按记录统计汇报，数字必须与文件一致。

## 7. 筛选规则（`screen.py`，写死在代码里）

### 7.1 先判硬条件（命中即排除）

| 条件 | 说明 |
| --- | --- |
| 必含条件缺失 | `must_have` 里一个都没出现（中英别名都算） |
| 排除要点命中标题/关键词 | 摘要出现只扣分，不直接排除 |
| 排除文献类型 | `doc_types_exclude` 命中标题/文献类型/期刊 |
| 年份早于下限 | 网络首发年份晚于上限只标注，不排除 |

### 7.2 再算分（0–100）

| 维度 | 权重 | 计分方式 |
| --- | --- | --- |
| 关键词 | 40 | 每个关键词取最佳命中位置：标题 1.0 / 关键词 0.85 / 摘要 0.6，按概念数平均 |
| 筛查方向要点 | 35 | 命中的 `include` 条数 ÷ 总数 |
| 加分项 | 15 | 命中的 `prefer` 条数 ÷ 总数（未配置则剔除） |
| 时效 | 10 | 在下限之后 1.0，无年份 0.5 |

摘要命中排除词每条扣 8 分。

### 7.3 判定

- `score ≥ min_score`（标准 55 / 宽松 40 / 严格 70）→ **纳入**
- **方向要点硬规则**：纳入还须命中 `screening.required_hits` 个方向要点（默认 5，
  上限为该任务的方向要点数：要点只有 4 个时按 4 执行并提示）。不足 → **最高待定**，
  依据写「方向要点命中 X/N（纳入硬规则 ≥N，未达硬规则已降为待定）」。
- `min_score - band ≤ score < min_score` → **待定**
- 其余 → **排除**；摘要缺失导致信息不足时从排除上调为待定
- 纳入超过 `max_items` 的部分转待定，依据里写「超出上限」

降级过多时（运行日志会统计条数），给用户三选一：
A 降低 `--required-hits` 重筛（不重检索）；B 补充 `include` / `terms_en` 要点；
C 把筛查方向写窄。**不要替用户自动放宽。**

### 7.4 中英互译

**不使用内置领域词表，也不调用翻译服务。** 中文要点按任务里的 `screening.terms_en` 扩展
（用户自己填的「中文=英文」对照，如 `肠道菌群=gut microbiota; 炎症=inflammation`）；
没给英文对照时，中文要点只能匹配中文题录，日志会提示「纳入/排除可能偏宽」。

### 7.5 选择依据必须包含

1. 命中哪些关键词、命中在标题/关键词/摘要；
2. 符合哪些筛查方向要点（同样标位置）；
3. 必含条件是否满足（按哪个别名匹配）；
4. 触发的排除项/扣分项；
5. 匹配分与最终判定。

例：`关键词命中：斑马鱼=zebrafish（标题/关键词/摘要）、微塑料（摘要）；符合筛查方向要点：肠道菌群=intestinal microbiota（摘要）、炎症（关键词）；必含条件「斑马鱼」满足（按zebrafish）；匹配度 68/100 → 判定：纳入`

## 8. 输出物

`<output_root>\<关键词1>_<关键词2>_<YYYYMMDD>\`

| 文件 | 内容 |
| --- | --- |
| `文献列表.md` | **主交付**：任务/确认 → 平台与权限 → 统计 → 纳入/待定/排除，每篇含原文标题、作者、年卷期、DOI/PMID、链接、全文状态、**原文摘要**、关键词匹配、方向匹配、**选择依据** |
| `文献清单.xlsx` | 23 列，含判定/匹配分/关键词匹配/方向匹配/选择依据；状态着色（含 `需登录`） |
| `筛选结果.json` | 全量记录 + 匹配关系，供复核 |
| `检索任务.json` | 本次采集的规则（含平台/登录设置） |
| `预检确认.json` | VPN 事实、用户确认、机构实测 |
| `平台探测.json` | 每个平台的可达性/登录/检索/PDF/付费墙结论 + 截图路径 |
| `登录准备.json` | 每个平台的首次登录/机构设置确认 |
| `平台待处理.json` | 需要账号、未检索或只拿到题录的平台 |
| `records_<平台>.json` | 各平台原始记录（PubMed/知网/其他） |
| `全文/` | PDF/CAJ，`第一作者_年份_标题.pdf` |
| `日志/run.log` | 每步时间、检索式、每篇判定、异常原文 |

重跑同一关键词：默认断点续跑；**换筛查方向不需要重检索**；换平台/首次登录后重跑会补检新平台。

## 9. 异常协议

| 级别 | 场景 | 动作 | 对用户的话 |
| --- | --- | --- | --- |
| 致命 | 未提供关键词/筛查方向/平台 | 停，打印问题清单，退出码 2 | 「先告诉我关键词、方向和平台」 |
| 致命 | VPN 未连、学校账号未确认、机构未识别、滑块超时 | 停，退出码 2，保留截图 | 「请先连 EasyConnect / 用学校账号登录 / 拖滑块」+ 截图 |
| 致命 | 平台首次登录未确认 | 停，退出码 2，写 `登录准备.json` | 「第一次用要先登录一次并选好学校/机构」+ 命令 |
| 需用户 | 平台登录墙且无保存账号 | 该平台返回 2，写 `平台待处理.json`，其他平台继续 | 「这个平台要登录：手动登录或保存账号，二选一」 |
| 平台级 | 网址打不开/无检索入口 | 跳过该平台，退出码 1 | 「该平台不可用：VPN/网址/改版，见截图」 |
| 单篇 | 下载失败、无权限、需付费、需登录 | 记状态继续 | 汇报单列，不解释成「已下」 |
| 改版 | 检索框/列表/详情选择器全不命中 | 截图 + 退出码 1 | 「页面可能改版，需校准 platforms.json / learned 选择器」 |
| 环境 | 缺依赖、配置缺失、浏览器起不来 | 退出码 3 或 1，原文写日志 | 给出缺失项与安装命令 |

## 10. 合法边界

只下载学校已订阅或开放获取内容；账号必须是用户本人的学校/CAS 账号，不共享、不代注册、不伪造机构参数；
不整刊批量抓取；遵守数据库条款与频率限制；不点购买/试用，不绕付费墙。

## 11. 常见失败与处理（本机实测）

| 坑 | 现象 | 处理 |
| --- | --- | --- |
| 会话代理 | requests 超时、浏览器 `ERR_PROXY_CONNECTION_FAILED` | `trust_env=False`；启动前清 `*_PROXY`；`--no-proxy-server` |
| 代理端口会变 | 每次会话不同 | 代码不写死端口，一律清环境变量 |
| 知网反爬 | curl/headless 跳 `/verify/` | 可见窗口 + 持久化 profile，人工拖一次 |
| 首页判身份 | 首页只有「充值/会员/机构登录」 | 必须进结果页 |
| 知网详情页 `h1` | 第一个 `h1` 是登录弹窗 | 用 `.wx-tit h1` |
| 摘要取不到 | 摘要是 `span#ChDivSummary` | 选择器用 `#ChDivSummary`，`text_content()` |
| 作者混入机构 | `#authorpart` 带「XX大学XX学院」 | `#authorpart a` + 过滤机构关键词 |
| 英文文献被判缺必含 | 中文必含词匹配不到 zebrafish | 在任务里填 `screening.terms_en`（中文=英文），或补必含词的英文 |
| 网络首发年份超前 | 知网显示 2027 | 下限硬排除，上限只标注 |
| PMC 403 | requests 直连全 403 | 浏览器过盾后 `api/getPdf` |
| 登录后又被判登录 | 登录成功后又 goto 登录入口，页面仍是登录表单 | 登录成功后先检查当前页；仍是登录页才回入口（已修） |
| 静态登录页 | 表单提交后回首页，但入口 URL 仍是 login.html | 同上：留在落地页，不盲目回入口 |
| SPA 登录 | CAS 三库登录在 iframe/JS 里 | `detect_login` 遍历 `page.frames`；失败则人工登录 |
| 通用抽取不准 | JS 应用只给标题/摘要 | 如实标 `metadata_only`/`仅摘要`，不编造 |
| 多平台重复 | 同一篇在 PubMed 和 ScienceDirect 各一条 | 跨库去重，保留字段最全的，其他写「同文见」 |
| 登录态过期 | 探测/检索又见密码框 | 重新 `platform_login.py --platform X --wait 180` |
| PowerShell 5.1 | `??`、三目、`&&` 不可用 | 不用 `.ps1`，统一 Python |
| 控制台编码 | PowerShell GBK，强写 UTF-8 变乱码 | 跟随终端编码，只设 `errors="replace"` |
| bash/PowerShell python 不同 | 3.13.12 vs 3.12.6 | 脚本启动打印解释器，以打印为准 |

## 12. 验收清单

- [ ] `intake.py --print-questionnaire` 含 `platforms`（8 个平台选项）、`school_list`、`custom_urls`、`login_setup`、`login_pending`
- [ ] 任务缺 `screening.direction` → 退出码 3；未知平台 → 退出码 3 并提示 `platforms.py --add`
- [ ] `platforms.py --parse` 能识别清单里的知网/SinoMed/ScienceDirect/SciFindern，未识别网址单列
- [ ] `platforms.json` 含规则 6 的全部 8 个网址（知网 3 个 + SinoMed + ScienceDirect + CAS 3 个）+ PubMed + PubScholar
- [ ] 用户说「第一次登录/不确定」且未确认 → `run_all` 退出码 2，不探测、不检索
- [ ] `platform_login.py --print-questions` 逐平台给出「是否登录过/是否设置机构」
- [ ] `probe_platform.py` 对可检索+有 PDF 的站点判 `fulltext_ok`；只有题录判 `metadata_only`；登录墙判 `login_required`
- [ ] 探测报告含 final_url、结果条数、PDF 入口数、付费墙提示、截图路径
- [ ] 登录墙且无账号 → `platform_search` 退出码 2，打印「手动登录 / 保存账号」两条命令
- [ ] `creds.py --list` 密码显示 `******`；密码不出现在 run.log / 检索任务.json / 文献列表.md
- [ ] 用户提供的中/外网网址能 `--add` 后探测并检索；不擅自换库
- [ ] 只下载纳入文献；`排除` 保持「未下载」并备注；平台无 PDF 标 `仅摘要/需付费/需登录`
- [ ] 跨库重复按 DOI/标题合并，`其他来源` 与备注可见
- [ ] Excel 23 列、状态着色含 `需登录`；行数与清单一致
- [ ] 报告有「检索平台与权限」区，列出登录状态、能力结论、待处理平台
- [ ] `日志/run.log` 能还原：平台选择、登录确认、探测结论、检索式、每篇判定、下载结果、异常原文

## 附录 A：脚本用法

| 脚本 | 命令 | 退出码 |
| --- | --- | --- |
| `doctor.py` | `--config` `--json` `--vpn`（环境自检：依赖/Edge/EasyConnect/配置/目录/注册表） | 0/1/3 |
| `intake.py` | `--out` `--keywords` `--keywords-en` `--direction` `--platforms` `--platform-url` `--school-list` `--first-login` `--login-pending` `--relevance` `--years` `--limit` `--from-json` `--print-questionnaire` | 0/2/3 |
| `platforms.py` | `--list` `--show ID` `--add URL --name` `--parse 文件或网址` `--learn 平台探测.json` `--json` | 0/1/3 |
| `platform_login.py` | `--platform` `--all` `--wait` `--first-time` `--ready` `--print-questions` `--check` `--refresh` | 0/2/3 |
| `probe_platform.py` | `--platform` `--url` `--all` `--keyword` `--quick` `--wait-login` `--refresh` `--save` `--add` | 0/1/2/3 |
| `platform_search.py` | `--platform` `--url` `--entry` `--query` `--limit` `--run-dir` `--task` `--wait-login` `--username` `--password-stdin` `--password-env` `--save-creds` `--no-detail` | 0/1/2/3 |
| `creds.py` | `--list` `--set ID --username` `--password-stdin` `--password-env` `--delete ID` `--path` | 0/2/3 |
| `precheck.py` | `--run-dir` `--keyword` `--confirm-vpn` `--confirm-account` `--skip-institution` | 0/2/3 |
| `plan_query.py` | `--text` `--keyword` `--groups` `--yes` | 0/2/3 |
| `vpn_check.py` | `--config` `--json` | 0/2/3 |
| `inst_check.py` | `--keyword` `--run-dir` `--json` | 0/2/3 |
| `pubmed_search.py` | `--query` `--limit` `--years` `--run-dir` `--json` | 0/1 |
| `cnki_search.py` | `--query` `--limit` `--no-detail` `--run-dir` `--selectors` `--config` `--json` | 0/1/2/3 |
| `screen.py` | `--run-dir` `--task` `--judgements` | 0/1/3 |
| `download_fulltext.py` | `--run-dir` `--max` `--all` `--skip-pubmed` `--skip-cnki` `--skip-platforms` `--task` | 0/1/2/3 |
| `make_excel.py` | `--run-dir` `--output` `--json` | 0/1/3 |
| `make_report.py` | `--run-dir` `--task` `--output` | 0/1/3 |
| `run_all.py` | 见文件头 / `--help` | 0/1/2/3 |

公共模块：`litcommon.py`（配置/日志/路径/退出码/合并 records）、`browser_util.py`（可见 Edge/profile/清代理/滑块）、
`platforms.py`（注册表/清单解析/选择器学习）、`platform_util.py`（登录识别/检索框/结果抽取/meta 解析/PDF 入口）、
`plan_query.py`（只用用户给的关键词拼检索式，不内置任何领域词表）。

## 附录 B：选择器校准

- 知网：`selectors.json`（摘要 `#ChDivSummary`、作者 `#authorpart a`、标题 `.wx-tit h1`、PDF `#pdfDown`、CAJ `#cajDown`）。
- 其他平台：`platforms.json` 的 `selectors`；探测 `--save` 会写 `platform_learned.json`，下次优先用。
- 通用抽取顺序：平台选择器 → meta（citation_* / DC.*）→ JSON-LD → 页面启发式；取不到留空，绝不编造。

## 附录 C：滑块接管规则

1. 打开知网后 URL 含 `/verify/` → 打印指引，阻塞轮询（2 秒一次，上限 `verify_wait_sec`）。
2. 用户完成拼图 → 自动继续；不做任何自动点击/破解。
3. 超时 → 截图 `日志/verify_timeout_<时间>.png` → 退出码 2。
4. 复用同一 `profile_dir`，通过后不再拦；**profile 删了就要重新拖**。

## 附录 D：已知边界

- 通用适配器不是万能的：CAS 三库是 JS 应用，可能只拿到标题/摘要；SinoMed 全文常跳到知网/万方，可能只有题录。
- 知网旧版/CIPD 走通用适配器，能力以探测结果为准；知网高级检索 `SU=` 仍只作为表达式输出。
- `--list` 清单模式只对 PubMed 逐条精确检索；其他平台标题检索未校准。
- `.caj` 只下载不转换；不做 UI 自动导出。
- 筛选是**规则打分**，不读语义；方向太抽象（如「相关研究」）时请拆成纳入/排除要点。
- 账号密码若保存，是明文（Windows 无 POSIX 0600），仅限本机；优先手动登录。
- 未内置万方/维普/Web of Science；用户给出网址可探测 + 通用检索，但不保证字段完整。
