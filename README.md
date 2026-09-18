# 研学体验容量管家

面向浚县非遗产业（展示馆 + 大师工坊）的研学接待容量管理服务。维护课程版本、场地、
师傅技能、材料包与时段容量；接收团队需求后给出可执行的分组与替代方案；迟到、取消、
拆团、雨天转室、材料临时短缺都产生可解释状态；**已开始的体验不会被后来的排程夺走资源**。

纯 Python 3.11 标准库实现（`http.server` + `sqlite3`），无第三方依赖。

## 运行

```bash
python3 -m service.main          # 默认 :8000，数据落盘 ./data/app.db
PORT=8000 DB_PATH=data/app.db service   # 或安装后用入口脚本
python3 -m unittest discover -s tests   # 测试
```

## 核心规则

1. **资源锁定**：会话开始（`start`）后 `locked=true`，其场地容量、师傅、材料不可被
   后续排程、请假、短缺重分配、取消、拆团夺走；与之冲突的操作返回 `409 SESSION_LOCKED`。
2. **可解释状态**：每个需求/会话携带 `state_reasons: [{code, message, facts}]`，
   机器可读 + 人可读 + 支撑事实（如缺口数量、冲突时段）。
3. **名单最小信息**：每次确认必须提交名单，但只保存人数、年龄段（可选）、渠道侧
   名单引用（可选）与摘要哈希，不存姓名/证件等个人信息。
4. **幂等**：建单以 `(channel, external_ref)` 判重；所有写接口支持 `Idempotency-Key`
   请求头；离线补录名单以 `(booking_id, idem_key)` 判重。重试返回首次结果。
5. **可恢复**：全部状态落 SQLite。重启后 `GET /handover?date=` 给出当天未结束会话
   及其师傅/材料/待办状态，可继续执行。
6. **可复盘**：所有状态变化追加事件日志（含业务日期 `biz_date`），`GET /replay`
   回放容量、改派与结算依据。

## 主数据

| 接口 | 说明 |
|---|---|
| `POST/GET /courses` | 课程版本：时长、天数（跨日课）、单组上限、场地类型、技能需求、材料需求 |
| `POST/GET /venues` | 场地：indoor/outdoor |
| `POST/GET /slots` | 时段容量：场地 + 日期 + 起止 + 容量 |
| `POST/GET /masters` | 师傅与技能（如 docent 讲解、pottery 制坯） |
| `POST /masters/{id}/leave` | 师傅请假：自动改派同技能师傅；撞已开始会话则拒绝 |
| `POST/GET /materials` | 材料包库存（stock / reserved / available） |
| `POST /materials/{id}/adjust` | 库存调整；调减时按「已开始锁定优先、先到先得」分配，缺口会话标记 `MATERIAL_SHORTAGE`，补足后自动解除 |

## 团队需求与执行

| 接口 | 说明 |
|---|---|
| `POST /bookings` | 接收团队需求 → 首选方案 + 替代方案（放宽时间窗/改期，含调整解释）；无可行方案则 `needs_attention` 并附原因 |
| `POST /bookings/{id}/confirm` | 确认方案：保存名单最小信息，创建分组会话并占用容量/师傅/材料；方案过期（资源已被占）返回 `409 PLAN_STALE` |
| `POST /bookings/{id}/cancel` | 取消：释放未开始会话资源；含已开始会话则拒绝 |
| `POST /bookings/{id}/roster` | 离线补录名单（幂等） |
| `POST /sessions/{id}/start` `/complete` | 开始（锁定）/ 完结（按实到消耗材料、释放师傅，形成结算依据） |
| `POST /sessions/{id}/late` | 迟到：时段余量内顺延，否则给出压缩说明 |
| `POST /sessions/{id}/split` | 拆团：原组缩减，新组另找场地/师傅/材料，失败整体回滚 |
| `POST /sessions/{id}/transfer` | 雨天转室：迁往同时段室内场地，无可用场地时标记 `TRANSFER_FAILED` |

## 复盘与交接

| 接口 | 说明 |
|---|---|
| `GET /handover?date=` | 当天未结束会话交接视图（默认今天） |
| `GET /replay?from=&to=` | 事件时间线 + 逐时段容量核对（含超订检查）+ 改派记录 + 结算依据 |
| `GET /settlement?from=&to=` | 结算：按已完成会话汇总人数、材料消耗、师傅工时 |

错误格式统一为 `{"error": {"code", "message", "details"}}`。敏感配置放本地环境文件。
