# 研学体验容量管家

为村里的展示馆 / 大师工坊管理节假日学生团的**课程版本、场地、师傅技能、材料包与时段容量**。
团队需求进来后自动分组、配齐讲解员 + 制坯师 + 场地 + 材料，排不下时给出带原因码的方案与替代时段；
迟到、取消、拆团、雨天转室、材料临时短缺都产生**可解释状态**；已开始的体验享有硬锁定，
资源不会被后来的排程夺走。

仅使用 Python 标准库，无外部依赖。

## 设计要点

- **事件溯源**：所有变更都是不可变事件（`service/store.py`），当前状态由事件流回放得到。
  复盘可回放至任意事件序号（`GET /replay?until_seq=`）；崩溃后从 `data/events.jsonl` 重放恢复。
- **事务原子提交**：一个命令产生的所有事件（含幂等回执）整批赋号、一次 `os.replace` 落盘，
  失败回滚内存状态——渠道重试与离线补录凭 `Idempotency-Key` 必然幂等。
- **软预留 → 硬锁定**：确认方案时材料为软预留；开场后转消耗（硬锁定），取消释放，完成/终止按实结算。
- **材料覆盖顺序**：库存临时下降时按确认先后逐团给覆盖配额；先开场团缺席未领的包自动顺延给后团。
- **最小名单信息**：只存学生编号 + 是否到场，不存姓名。

## 目录

| 文件 | 职责 |
| --- | --- |
| `service/domain.py` | 常量、状态与主数据结构（课程版本、场地、师傅、库存、会话、名单条目） |
| `service/engine.py` | 纯排程引擎：占用视图、单组匹配、分组方案、替代时段、替补师傅/转室查找 |
| `service/store.py` | JSONL 事件存储、折叠器、任意时点回放、ID 计数器重建 |
| `service/app.py` | 应用服务：命令事务、幂等、生命周期、请假自愈、拆团、雨天、结算、交接 |
| `service/main.py` | HTTP 入口（标准库 `http.server`） |
| `tests/test_capacity.py` | 35 个用例覆盖全部业务情形与端到端复盘场景 |

## 运行

```bash
python3 -m service.main          # 默认事件文件 data/events.jsonl，端口 8000
PORT=8123 python3 -m service.main
python3 -m unittest discover -s tests
```

## HTTP 接口（写接口带 `Idempotency-Key` 头即幂等）

主数据：`POST /admin/courses` `/admin/venues` `/admin/masters`
`/admin/leaves` `/admin/leaves/cancel` `/admin/stock/receive` `/admin/stock/adjust`

团队：`POST /teams` → `POST /plans/{id}/confirm`；查询 `GET /plans/{id}`

现场：`POST /sessions/{id}/roster|late|start|complete|cancel|terminate|split`

天气：`POST /weather/rain`（未开始室外团按确认先后转室；已开始的不动，记 `RAIN_STARTED_PROTECTED`）

查询：`GET /sessions/{id}` `/sessions/{id}/incidents` `/schedule?date=`
`/stock` `/incidents` `/handover?date=` `/replay?until_seq=`

## 关键状态与异常码

- 会话：`CONFIRMED → LATE → IN_PROGRESS → COMPLETED`；旁路 `CANCELLED`（开始前，释放预留）、
  `TERMINATED`（开始后，按实结算）、`SPLIT`（父团终结，子团延续）。
- 可解释异常：`STAFF_SHORTAGE`、`STAFF_REASSIGNED`、`RAIN_RELOCATED`、`RAIN_NO_INDOOR_CAPACITY`、
  `RAIN_STARTED_PROTECTED`、`MATERIAL_SHORTAGE`、`PARTIAL_KITS_AT_START`。
- 排程原因码：`VENUE_FULL`、`NO_GUIDE`、`NO_MAKER`、`MATERIAL_SHORT`；同时返回最多 3 个替代时段/日期。
- 结算依据 `basis=ATTENDED`：按实际到场人数 × 课程版本单价，附已耗材料 SKU 与数量。
