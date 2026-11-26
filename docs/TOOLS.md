# Agent 工具接口

`tools/definitions.json` 定义 18 个工具；`ruc_capabilities` 合并专用能力、内置契约和当前扩展，返回具体参数。能力名为开放字符串，不随新增信息修改工具枚举。

## 传输和会话

- `scripts/ruc.py`：人使用的 CLI，每次调用一个新任务；`batch` 在一次任务内执行多项请求。
- `scripts/tool.py`：stdin 一个 `name/arguments` JSON，EOF 后执行，stdout 一个 JSON；适合无需后续状态的调用。
- `scripts/serve.py --transport jsonl`：每行一个请求与结果，进程内持续复用任务。
- `scripts/serve.py --transport mcp`：标准 MCP stdio，使用官方 SDK，兼容 `initialize/tools/list/tools/call`。Python 3.10+，安装 `requirements-mcp.txt`。
- Python：`ToolRuntime` 上下文的 `call`，或 `CampusSession` 上下文的 `query`。

凭据通过 `--credentials-file` 或 `RUC_CREDENTIALS_FILE` 传给宿主，不放在工具参数中。可选 `--extensions` 指定 JSON 配置目录。写任务须为持续服务启用 `--allow-writes`。

每个服务进程供一个账号的一次任务使用，不用于多个用户共享。`ruc_reset_session` 关闭当前任务；下一条查询才创建新认证。它不会自动重读扩展文件；修改磁盘配置后重启服务。

## 工具

| Tool | 参数和用途 |
|---|---|
| `ruc_capabilities` | 可选 keyword；能力、参数、来源、读写属性和验证范围 |
| `ruc_query` | capability、可选 params/all_pages/max_pages；专用和声明式读取共用入口 |
| `ruc_query_batch` | 1–20 个 capability/params；整批预校验，复用当次身份 |
| `ruc_discover` | 可选 platform/keyword；无参数返回 profile 和契约格式，academic 返回实时 API 映射，portal 返回实时应用 |
| `ruc_inspect_source` | url，可选 keyword/max_chars；当前公开 HTML/JS 的链接、哈希和上下文 |
| `ruc_register_contract` | document；原子登记当前任务的 JSON 扩展，不写磁盘 |
| `ruc_prepare_action` | capability、params；生成具体写操作预览 |
| `ruc_commit_action` | action_id；消费预览并执行一次 |
| `ruc_browser_open` | url；打开配置或本次门户发现的入口，返回页面 ID 和快照 |
| `ruc_browser_snapshot` | page，可选 max_chars；当前文字、iframe 和元素 ref |
| `ruc_browser_interact` | page/ref/action，可选 value/write；读模式交互或生成写动作预览 |
| `ruc_browser_network` | 可选 request_id 或 response_id；脱敏业务参数或已保留只读 JSON |
| `ruc_reset_session` | 清理当前会话，撤销本次 CAS 票据 |
| `ruc_check_login` | 验证静音舱认证和校区；不必在正常查询前调用 |
| `ruc_query_booths` | 兼容原静音舱查询参数 |
| `ruc_find_apps` | keyword、limit；离线历史入口，live=false |
| `ruc_platforms` | 默认查看本包维护状态；live=true 返回当前账号实时入口、权限计数和平台分组；CLI 使用 --live |
| `ruc_authenticate` | realm=legacy，可选 verification_code；独立微人大登录与验证码 |

动作预览、提交、浏览器和动态注册依赖持续会话，单次 `tool.py` 会明确拒绝这些调用，防止返回下一次无法使用的 actionId 或页面 ref。

## 读取能力

已有 `quiet_booths/library_seats/sports_venues/classrooms/grades/rankings/academic_options/applications` 保留原参数。新增：

| capability | 参数和结果 |
|---|---|
| `booths.notices` | type=0 公告、type=1 规则；data 为原始列表 |
| `booths.usage_types` | data 为当前用途目录 |
| `booths.required_fields` | roomId；data 为当前房间表单字段定义 |
| `booths.my_reservations` | currentPage/pageSize；本人预约记录、完整性元数据 |
| `booths.my_breaches` | 同上，本人违规记录 |
| `booths.my_scores` | 同上，本人积分记录 |
| `booths.my_blacklist` | 同上，本人限制记录 |
| `academic.calendar` | term；先取当前学期，返回教学日历原始结构 |

当前能力按平台由 `tools/contracts/*.json` 提供，完整目录通过 `ruc_capabilities` 获取。新增课程、借阅、校园卡、食堂、就业、办事和数据表能力见 [README](../README.md)。`ruc_query(all_pages=true)` 会按契约自动翻页；`max_pages` 上限 100，未收齐时保留 nextPage。HTML 结果的 scope=server_html 不包含尚未加载的 AJAX 数据。

写能力 `booths.reserve`、`forms.submit`、`forms.create`、`museum.cancel_reservation` 的完整字段从 `ruc_capabilities` 获取，具体工作流见 [EXTENDING](EXTENDING.md)。本次没有执行真实业务写入。

未来课堂使用 `future.*`，学务中心 `affairs.*`，掌上教务 `teaching.*`，公共计算云 `cloud.*`，党校 `party.*`，访客 `visitor.*`，博物馆 `museum.*`，哲学院会议室 `philosophy.*`，作品平台 `media.*`，创新研学 `innovation.*`，商学院职业发展 `business_career.*`，社科数据 `datasets.*`，虚拟实验 `virtual_labs.*`，研究生服务 `graduate.*`，红色创新 `red.*`。具体参数和已核验范围通过能力目录获取。

## 结果解释

每次业务查询附能力、平台、来源和北京时间。专用能力保留自己的语义结果；声明式能力返回 `data/schemaFingerprint`，声明分页时附 `pageItems/totalItems/complete`。新增字段原样保留。读取目录或接口失败会返回错误，不会转换为空列表或“已满”。

`date_not_open` 是日期未开放，`no_matching_catalog` 是目录不匹配，体育 `not_scheduled` 是未排时段；`partial/unknown/issues` 表示未核实范围。图书馆不指定时段时，结果只说明官网默认时段。

会话元数据包含 `savedStateRead=false`、`credentialSubmissions`、`authenticationRealms` 和 `restTicketRevoked`。CAS/legacy 各计一次密码提交。持续任务中撤销标记为 null，reset/任务退出后才有最终结果。`freshSession=true` 表示本任务从新身份启动，不代表每个工具调用都重新登录。

浏览器快照带 `blockedRequests` 时为 partial；页面文字仍可使用，但须说明未加载的业务范围。浏览器提交返回 `verification_required`，不能仅凭点击完成宣称预约成功。`outcome_unknown` 表示无法确认写操作结果，应先查回执，不能重发相同操作。

密码拒绝、认证策略改变或限流时停止，不自动重试密码，不回退历史 Cookie。精确 schema 和错误码来自实际返回。

## 验证码

微人大首次返回 `CAPTCHA_REQUIRED`。MCP content 含原生图片，JSONL details.imageDataUrl 为同一图片。识别后在同一持续进程调用 `ruc_authenticate({realm:"legacy",verification_code:"识别结果"})`，成功后重发原读取；不创建新会话，不把验证码当作账号密码。

体育 `query_not_open` 表示当天查询尚未开放，`availableAfter` 为北京时间；不能据此判断满额。已有业务会话返回 LOGIN_REQUIRED 时，读取会用原身份重建一次业务会话，不重新提交密码；失败继续返回错误。
