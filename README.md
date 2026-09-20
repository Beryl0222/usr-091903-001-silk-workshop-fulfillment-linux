# 真丝体验材料履约

服务用于记录文化体验活动中的真丝余料、作品流转与使用授权，使门店、工坊和讲师共享一致的材料责任链。

运行 `python3 service.py --check` 可核对服务配置；执行 `python3 service.py --port 8000` 后访问 `/health` 可确认服务身份。生产或现场使用时加 `--db fulfillment.db` 落盘（SQLite 单文件，断网可用）。

## 解决的业务问题

- **来源不丢**：面料批次入库生成根裁片；拆分、部分领用、退回工坊、改作展示都通过 `parent_id` 串成谱系，任何一块料都能回溯到批次、染色/清洁注意事项和经手人。
- **责任可交接**：场次可登记 `material`（材料）、`process`（工艺讲解）、`photo`（照片授权）三类交接；临时换老师可自动转交工艺讲解责任，取消活动时可查接手人。
- **断网补录不重复扣减**：领料、成品、再利用、费用等写操作携带 `client_ref`，同一编号重试只生效一次并回放首次结果（响应带 `replayed: true`）；批量通道 `POST /sync` 逐条执行，单条失败不影响其他条。
- **改期保留承诺并重算材料**：首次排期固化承诺快照，改期只追加修订；报名、改选择、改期都会按「方案单位用量 × 参与者选择」重算缺口（cm²）。
- **授权分用途分期限**：未成年人影像（须监护人）、顾客故事、作品照片分别授权，可指定用途（display/promotion/archive…）、渠道（any/store_screen/web…）和起止时间。撤回立即生效，`/displays/check` 对撤回、过期、用途或渠道不符一律拒绝并留痕；未成年参与者的作品照片还须影像授权同时有效。
- **端到端追溯与核算**：从一件作品可查所用真丝批次与裁片链、经手人、授权状态、费用净额；另提供边角料再利用量报表和讲师课酬报表（在册人头 × 单人课酬，扣已付）。

## 主要接口

| 方法 路径 | 说明 |
| --- | --- |
| `POST /batches` · `GET /batches` · `GET /batches/{id}` | 面料批次（含染色/清洁注意事项、剩余尺寸） |
| `POST /pieces/split` | 裁片拆分，一块面料可分到不同场次 |
| `POST /pieces/assign` | 裁片划归场次 |
| `POST /pieces/return` · `POST /pieces/display` | 退回工坊 / 改作展示品（保留谱系） |
| `POST /reuses` · `GET /reports/reuse` | 边角料再利用登记 / 设计师核量报表 |
| `POST /instructors` · `POST /plans` | 讲师与资质 / 活动方案（工艺讲解、单位用量、课酬） |
| `POST /events` · `GET /events/{id}` | 排期（响应含材料是否够用）/ 场次详情 |
| `POST /events/{id}/reschedule` | 改期：返回原承诺与重算结果 |
| `POST /events/{id}/instructor` | 换讲师，可同时交接工艺讲解 |
| `POST /events/{id}/cancel` · `/complete` | 取消 / 结项 |
| `GET /events/{id}/sufficiency` · `/payout` | 材料缺口 / 讲师应得报酬 |
| `POST /handoffs` | 材料/工艺/照片责任交接 |
| `POST /participants` · `POST /participants/choices` | 报名（未成年人须监护人）/ 当场改选择 |
| `POST /allocations` · `POST /allocations/reverse` | 领料（支持 `client_ref` 幂等）/ 冲销退回 |
| `POST /artworks` · `GET /artworks/{id}/trace` | 成品登记（成品品类须与选择一致）/ 全链路追溯 |
| `POST /consents` · `POST /consents/withdraw` · `GET /consents` | 授权 / 撤回 / 查询 |
| `POST /displays/check` | 展示前授权校验并留痕 |
| `POST /fees` | 费用流水（报名费/材料成本/工坊退回/讲师课酬） |
| `POST /sync` | 断网离线队列批量补录 |

时间统一使用 ISO 8601（建议带时区，如 `2026-10-01T10:00:00+00:00`）。

## 结构

- `storage.py` — SQLite 表结构与连接（写操作串行化 + 唯一约束）
- `domain.py` — 全部业务规则与报表
- `service.py` — 标准库 HTTP 入口与路由
- `service_contract.py` / `test_domain.py` / `test_api.py` — 合约与业务测试

## 测试

```bash
npm test          # 依次运行三套 unittest（37 个用例）
python3 service.py --check
```
