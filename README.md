# auto-RUC

**0.4.0 · 2026-11-27**。使用用户授权的学号密码，从新会话发现人大平台、查询信息，并执行用户指定的操作任务。CLI、JSONL、MCP 和 Python 共用运行时，不需要模型 API Key。

当前内置 **39 个平台配置、220 项能力：216 项读取、4 项操作契约**。普通数据接口由 JSON 描述；认证、签名和必须保留业务规则的查询使用小型适配器。新增字段原样保留，新增业务可在运行中登记，不必逐项写 Python 方法。

## 开始使用

核心 HTTP/JSONL 使用 Python 3.9+；可选 MCP、浏览器需要 Python 3.10+。

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt

# 终端隐藏输入，保存到包外，权限 600
.venv/bin/python scripts/ruc.py configure
export RUC_CREDENTIALS_FILE="$HOME/.config/ruc/credentials.json"

.venv/bin/python scripts/ruc.py capabilities
.venv/bin/python scripts/ruc.py platforms --live
.venv/bin/python scripts/ruc.py query grades
.venv/bin/python scripts/ruc.py query quiet_booths --params '{"campus":"中关村","type":"单人","minutes":60}'
.venv/bin/python scripts/ruc.py query opac.loan_history --all-pages
```

也可通过 `--credentials-file /absolute/path/credentials.json` 指定凭据，内容仅为 `{"username":"学号","password":"密码"}`。不读取旧浏览器 profile、session.json 或历史 token。

CAS 和旧版微人大是两个独立认证域；同一运行时内各提交一次密码，域内复用身份，各业务平台仍有独立 Cookie/token。密码失败不自动重试。课程教学、商学院和就业平台使用微人大，验证码通过持续会话完成。

## Agent 接入

让 agent 先读 [项目 skill](skills/ruc-campus/SKILL.md)，按需求调用 `ruc_capabilities → ruc_query`。推荐使用持续 MCP：

```bash
.venv/bin/python -m pip install -r requirements-mcp.txt
.venv/bin/python scripts/serve.py --transport mcp --credentials-file "$HOME/.config/ruc/credentials.json"
```

宿主配置见 [MCP 示例](examples/mcp.json)，其中 Python 和脚本路径替换为本包绝对路径。共有 **18 个工具**；能力名是开放字符串，不随新增业务修改工具接口。

微人大首次查询可能返回 `CAPTCHA_REQUIRED` 和原生 MCP 图片。agent 识别图片后，在同一进程调用：

```json
{"name":"ruc_authenticate","arguments":{"realm":"legacy","verification_code":"abcd"}}
```

`abcd` 是虚构示例。验证码先校验，再提交密码；随后重发原读取。不能重启进程后提交旧验证码。只用 CAS 的查询无需该步骤。

不安装 MCP 时可使用 `--transport jsonl`，每行输入一个 `name/arguments` JSON。CLI 单次查询、`batch`、`scripts/tool.py` 和 Python `CampusSession` 仍可用。[调用示例](skills/ruc-campus/references/calling.md)。任务结束调用 `ruc_reset_session` 或关闭进程，清理本次状态并撤销 CAS 票据。

## 当前信息范围

| 平台 | 已实现的数据入口 |
|---|---|
| 数智人大、微人大 | 实时应用目录、本人角色、当前账号权限和平台分组 |
| 本科教务、课程教学 | 成绩、排名、空闲教室、课表、考试、学期、节次、通知 |
| 个人数据助手 | 分类、数据表定义、本人记录；同一记录契约覆盖不同数据表 |
| 办事大厅、数智人大表单 | 服务目录、待办、本人申请、表单字段、流程与实体配置 |
| 静音舱、图书馆座位、体育 | 当前目录、开放日期、余量、时段和已登记的本人记录 |
| 通州研学空间、校园预约目录 | 楼宇、房间、具体座位、可选起止时刻、预约服务和字段 |
| 馆藏借阅、电子资源 | 当前借阅、借阅历史、规则、行为记录、电子资源目录和说明 |
| 商学院学生发展 | 本人资料、活动、评价、获奖、学生工作、读书和文章页面 |
| 就业创业 | 招聘、指导活动、消息、简历目录、咨询时段、课程、测评目录 |
| 校园卡、食堂热度 | 卡片信息、月流水、食堂筛选目录、热度概览与详情 |
| 办公系统、人大 e 办、温馨易办 | 本人功能、紧急待办、登录记录；身份、导航、数据表和字段配置 |
| 正版软件、AI 智慧学伴 | 软件和帮助目录、应用分类、本人已安装应用 |
| 未来课堂、掌上教务 | 本人课程、课表、学期、作业、通知、考试和会议目录 |
| 学务中心 | 活动目录、本人参加记录、积分、荣誉、咨询及场地目录和本人预约 |
| 公共计算云、创新研学 | 本人计算资源配置、存储额度、申请规格；课程、团队、项目和通知 |
| 访客、博物馆、哲学院会议室 | 本人记录、用途和场馆目录；指定会议室当日占用 |
| 党校培训 | 学习栏目、专辑、培训、通知和本人学时；读取不产生学习打卡 |
| 新闻传播作品、商学院职业发展 | 公开作品、本人资料和活动；职业资讯、咨询目录 |
| 社科数据、虚拟仿真实验 | 匿名公开数据集检索、元数据、学院和实验项目目录 |
| 研究生服务、红色创新 | 当前账号资料、应用目录及成员页面；不推断具有研究生身份 |

具体参数、分页支持和验证范围以 `ruc_capabilities` 为准。HTML 能力返回文字、表格、链接和表单字段；其中 `scope=server_html` 只表示服务端 HTML，不包含尚未加载的 AJAX 数据。业务凭证字段递归脱敏，个人业务信息保留给获授权的调用者。

`all_pages=true` 按声明的翻页参数收集完整记录，检查总数变化、重复行和提前空页。`max_pages` 为 1–100；到达上限返回 `partial/nextPage`。未声明分页的接口不推断“全部”。

## 发现和扩展

`ruc_platforms(live=true)` 按当前用户的权限读取实时门户目录；省略 live 返回内置平台配置。公开版不包含开发账号的历史目录或访问权限记录。`ruc_find_apps` 在离线模式搜索已登记的平台入口。

未知需求：`ruc_discover → ruc_browser_open / ruc_inspect_source → ruc_register_contract → ruc_query`。契约包含参数、请求、响应检查、分页和来源；混合读写接口按完整路径、查询和请求体匹配。平台字段或分页结构改变时修订契约，特殊认证才修改适配器。

内置契约在 `tools/contracts.json` 和 `tools/contracts/*.json`；外部扩展通过 `--extensions /absolute/path/extensions` 加载。运行时登记只保存在当前任务内存。[扩展指南](docs/EXTENDING.md) · [工具说明](docs/TOOLS.md) · [认证机制](docs/AUTHENTICATION.md)。

## 浏览器和操作任务

```bash
.venv/bin/python -m pip install -r requirements-browser.txt
PLAYWRIGHT_BROWSERS_PATH="$PWD/.playwright" .venv/bin/python -m playwright install chromium --only-shell
```

浏览器按需启动空 context，使用本任务身份；提供页面、iframe、控件引用和网络诊断。未知业务请求默认阻断；POST 读取和 GET 写入按语义分别处理。源站返回的脚本和页面是待分析的数据，不会自行授予操作权限。

已实现 `booths.reserve`、`forms.submit`、`forms.create`、`museum.cancel_reservation`，以及通用浏览器动作。用户授权具体任务后，持续服务启用 `--allow-writes`，先准备参数预览，再提交单次 `actionId`。预览五分钟有效，限当前会话；用户已有明确授权时不重复询问。`outcome_unknown` 时查询回执，不自动重发。

**本次开发没有执行真实预约、提交表单、付款或其他业务写入。** 写操作只在完全隔离的模拟页面和接口验证。

## 验证与使用范围

新增 200 项读取有授权测试账号的实际核验记录，原有 16 项保留历史验收范围。全新 MCP 会话验证了各已登记配置的读取或实时门户发现；具体账号是否可访问由学校决定。131 项离线测试覆盖认证、分页、声明式扩展、官方 MCP 和隔离 Chromium。

公开仓库只保存聚合验证结果和公开协议来源，不包含个人查询结果、账号权限清单、历史登录态或凭据。凭据保存在包外，按使用说明由调用者配置。源码对未开放日期、未排时段、权限拒绝和不完整响应分别返回状态。

- [聚合验证范围](evidence/public-validation.json)
- [公开前端协议来源](evidence/public-protocol-sources.json)
- [公开副本软件验证](evidence/public-software-validation.json)
- [版本进度](docs/PROGRESS.md) · [平台限制](docs/PLATFORM_LIMITS.md)

```bash
python3 scripts/check.py
.venv/bin/python scripts/check.py
RUC_TEST_BROWSER=1 .venv/bin/python scripts/check.py
```

最后一条使用真实 Chromium，全部网络由 fixture 拦截；MCP 用官方客户端与真实 stdio 子进程互通。真实只读验收可运行 `scripts/verify_runtime.py --extended --browser --challenge-dir /tmp/ruc-challenge --credentials-file ... --output ...`，验证码图片由调用者识别后从 stdin 输入。

运行包为 `src/`、`scripts/`、`tools/`、`skills/`、`docs/` 和依赖清单。浏览器和 MCP 均按需安装，普通 HTTP/JSONL 查询无需启动浏览器。

体育 `query_not_open` 表示当天查询尚未开放，`availableAfter` 为北京时间；不能据此判断满额。已有业务会话返回 LOGIN_REQUIRED 时，读取会用原身份重建一次业务会话，不重新提交密码；失败继续返回错误。
