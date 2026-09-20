# 真丝体验材料履约

门店把裁衣剩下的真丝交给文化体验课（手链、发簪、耳饰）使用后，需要一条不中断的责任链：
同一块面料被拆到不同场次、临时换老师、取消活动、断网补录，都不能说不清材料、讲解和授权由谁负责。
本服务把面料批次、裁片流转、活动方案、讲师资质、参与者选择与授权期限关联起来，提供体验履约后端。

运行 `python3 service.py --check` 核对服务配置；`python3 service.py --port 8000` 启动后访问 `/health` 确认服务身份。
`npm test` 运行全部测试（基础契约 + 领域流程 + HTTP 接口）。

## 领域模型

- **面料批次 FabricBatch**：裁衣余料入库，记录来源裁衣单、设计师、剩余尺寸、染色与清洁注意事项、再利用估值。
- **裁片 MaterialPiece**：批次拆出的树状结构（`parent_id` 指回母片，`batch_id` 一路继承）。
  状态：`stock 在库 → reserved 已预留 → issued 已领用 → consumed 已入作品`，
  以及 `returned 退回工坊`、`display 展示品`、`split 已拆分（母片终结）`。
- **流转台账 Movement**：追加式，永不修改。拆分、退回、改作展示品都写台账，来源永不丢失。
- **活动方案 ActivityPlan**：工艺类型、人均用料、工艺讲解（带版本号）、讲师资质要求、报名费与讲师分成。
- **场次 Session**：排期时保存**原承诺快照**（时间、讲师、费用、用料标准、讲解版本），改期不修改快照。
- **授权 Consent**：按主体（参与者/作品）、类别（未成年人影像/顾客故事/作品照片）、用途、期限分别取得。
- **费用台账 FeeEntry**：报名费 → 讲师报酬 / 材料再利用估值 / 门店留存，讲师付款另记。

## 关键规则

1. **幂等补录**：所有写接口接受 `Idempotency-Key` 请求头（或 body 的 `idempotency_key`）。
   断网补录领料、成品时重复提交返回首次结果，不重复扣减；成品另支持 `offline_ref` 凭据去重。
   幂等键绑定请求指纹，跨请求复用会被拒绝（409）。
2. **面积守恒**：拆分面积不得超过母片，不足部分自动生成"余料"子片；部分用料自动拆出消耗子片与余料子片。
3. **改期**：保留原承诺快照，只改排期，并重新核算材料是否够用——
   被退回/转走的预留列入 `broken_reservations`，缺口给出在库替代建议（优先同批次）。
4. **换老师**：校验新讲师资质，生成交接单（在手材料、讲解版本、待跟进授权），已领裁片经手人同步变更。
5. **取消场次**：必须说明材料/讲解/授权接手人；裁片按处置方案退回在库、退回工坊、改作展示品或转入其他场次。
6. **授权**：未成年人影像须监护人授权；展示必须落在有效授权（类别+用途+期限）内；
   撤回后立即阻止新的展示，已有展示列入下架复核。
7. **追溯**：`GET /works/{id}/trace` 从一件作品查到所用真丝（批次、染色/清洁注意事项、裁片链）、
   经手人（台账 + 交接单）、授权状态与费用去向。

## 主要接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/batches` | 登记面料批次（生成整批根裁片） |
| GET | `/batches/{id}` | 批次详情 + 裁片 + 再利用汇总 |
| POST | `/pieces/{id}/split` | 拆分裁片（自动余料，面积守恒） |
| GET | `/pieces/{id}` | 裁片来源链与流转台账 |
| POST | `/pieces/return-to-workshop` | 退回工坊 |
| POST | `/pieces/convert-to-display` | 改作展示品 |
| POST | `/plans` / `/plans/{id}/briefing` | 活动方案 / 更新讲解（版本递增） |
| POST | `/instructors` | 讲师与资质 |
| POST | `/sessions` | 排期（校验资质，保存原承诺快照） |
| POST | `/sessions/{id}/reserve` | 预留裁片 |
| POST | `/sessions/{id}/enroll` | 报名（记录参与者选择，按原承诺价计费） |
| POST | `/sessions/{id}/issue` | 领料（幂等） |
| POST | `/sessions/{id}/works` | 登记成品并扣减用料（幂等 + offline_ref） |
| POST | `/sessions/{id}/reschedule` | 改期（保留承诺，重算材料） |
| POST | `/sessions/{id}/reassign-instructor` | 换老师（交接单） |
| POST | `/sessions/{id}/cancel` | 取消（接手人 + 裁片处置） |
| POST | `/sessions/{id}/complete` / `/settle` | 完成 / 结算（费用去向） |
| POST | `/consents` / `/consents/{id}/withdraw` | 取得授权 / 撤回 |
| POST | `/publications` | 发起展示（校验授权，拒绝即 403） |
| GET | `/works/{id}/trace` | 作品全链路追溯 |
| GET | `/reports/reuse?designer_id=` | 设计师边角料再利用量 |
| GET | `/reports/instructor-pay?instructor_id=` | 讲师应付/已付/未付报酬 |

错误统一为 `{"error": {"code", "message"}}`：404 不存在、409 状态冲突、422 参数校验、403 授权不足。

## 代码结构

- `service.py`：服务入口与健康检查（保持原有契约）。
- `silkworkshop/models.py`：数据结构；`silkworkshop/store.py`：进程内存储与台账。
- `silkworkshop/domain.py`：领域规则（材料、场次、授权、费用、追溯）。
- `silkworkshop/api.py`：HTTP 路由。
- `service_contract.py`：基础契约测试；`test_fulfillment.py`：领域流程与接口测试。
