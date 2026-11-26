# 在内存中查询和筛选

在项目根目录执行。`PYTHONPATH=src` 让 Python 找到本包；`-B` 避免生成字节码。凭据参数使用已经授权的包外路径。下面代码只展示调用形状，替换查询和筛选条件后运行。

## 参数已确定：一次批量查询

```bash
PYTHONPATH=src .venv/bin/python -B - /absolute/path/credentials.json <<'PY'
import json
import sys
from ruc_agent.tools import invoke

requests = [
    {"capability": "library_seats", "params": {"date": "2030-03-01"}},
    {"capability": "grades"},
    {"capability": "rankings"},
]
result = invoke("ruc_query_batch", {"requests": requests}, credentials_file=sys.argv[1])
for item in result["results"]:
    if item["status"] == "error":
        print(json.dumps(item, ensure_ascii=False))
        continue
    if item["capability"] == "library_seats":
        item["rooms"] = [r for r in item.get("rooms", []) if "待查询区域" in r.get("name", "")]
    elif item["capability"] == "grades":
        item["records"] = [r for r in item.get("records", [])
                           if r.get("rowType") == "course" and "待查询课程" in r.get("kcname", "")]
        item.pop("terms", None)
    print(json.dumps(item, ensure_ascii=False))
print(json.dumps({k: result[k] for k in ["freshSession", "savedStateRead", "credentialSubmissions", "restTicketRevoked"]}))
PY
```

用用户日期替换虚构日期；课程名和阅览室名按当次结果匹配。不确定官网名称时先在同一结果中打印课程名或阅览室名列表，不重新登录去重查同一目录。不必将完整个人结果写入临时文件。

通用返回结构：每项有 `status/capability/asOf/source`；图书馆是 `rooms`，静音舱是 `groups[].rooms`（舱名 `name`、连续窗口 `windows`），成绩和排名是 `records`。每项保持自己的状态，不能把筛选结果为空一律当成无余量。

## 后续参数依赖目录

```python
from ruc_agent.credentials import read_credentials
from ruc_agent.engine import CampusSession

with CampusSession(lambda: read_credentials(credentials_file, interactive=False)) as session:
    directory = session.query("library_seats", {"date": target_date})
    # 从 directory 的当前 rooms 中选出目标，再传其 id。
    detail = session.query("library_seats", {"date": target_date, "room_id": str(selected_room["id"])})
metadata = session.metadata()  # 离开 with 后才有最终撤销结果。
```

`credentials_file`、`target_date`、`selected_room` 由调用者根据授权和本次目录提供。需要共享认证的查询都放在同一 `with` 中；不要循环调用 `tools.invoke` 来代替一个批次。

## 分页与 HTML

```python
history = session.query("opac.loan_history", {}, all_pages=True, max_pages=20)
if history["complete"]:
    rows = history["data"]
else:
    # 保留已读取数据和 nextPage，不声称取全。
    rows, next_page = history["data"], history["nextPage"]
```

JSON 工具把 all_pages/max_pages 放在 arguments 顶层，与 capability/params 并列；CLI 使用 --all-pages/--max-pages。`CampusSession.batch` 的每项仍为 capability/params，需要自动翻页时单独调用 query 并复用 session。

HTML 能力 data 包含 title、text、links、tables、forms、fields。使用当前链接或控件继续探索动态数据；scope=server_html 不能解释为完整 SPA。表单中凭证字段值脱敏，普通个人资料可按用户授权读取。

`study.start_times` 返回的分钟数可能是数字字符串，调用 end_times 前转换为整数；seatId 来自 study.seats 对象的键，roomId 来自 study.rooms。
