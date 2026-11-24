# 体验券与跨馆兑现记录

`data/` 为离线合成业务记录。文件均为 UTF-8 的 Tab 分隔表，首行是列名，逗号分隔集合，`-` 表示不适用，`?` 表示未取得，不把它们当作零。标识符只在各表声明的归属范围内有效。

## 运行与资料核对

使用 Python 3.11。只需 Python 3.11 标准库。

```sh
python3 inspect_packet.py
python3 -m unittest discover -s tests
```

前一条命令只检查表头和记录形状并显示行数；可传入另一目录读取同格式记录。当前除资料核对外，还提供券生命周期、试算区间、准备金核对和签准方案工件。

## 常用命令

```sh
# 原有形状检查
python3 inspect_packet.py

# 运行全部单测
python3 -m unittest discover -s tests

# 生成三次关账试算、补齐复盘、前台券状态和维度核对
python3 -I reserve_report.py report --data data --out reports

# 经办人提交一次自定义试算
python3 -I reserve_report.py trial \
  --data data --exit-date 71 --site A --as-of 67 \
  --slots S4,S5,S6

# 财务签准后写出不可重复的方案工件（示例命令，ID/签准日由财务给）
python3 -I reserve_report.py approve-complete \
  --data data --approval-id AP-NEW-001 --signed-day 73
```

`--provision-rate R` 可传入财务提交的准备金费率；`coverage` 仍取自 `closures.tsv` 或自定义试算参数。费率点值只作为额外财务公式列，不会把外推点值改称已认列负债。

## 实现口径

- 以原券 `voucher` 为主键串联发券、转赠、预约、撤约、签到和退款；转赠只改变持券类型，不重置 `issue` 起算的券龄。
- `received <= as_of` 才进入当时关账视图；`happened` 保留业务发生日。订正记录按 `rectifications.event` 撤销指向的事件，不改原事件的 `received`。
- `pause.tsv` 中同一 `segment` 的多条顺延记录全部累加，得到 `adjusted_expiry`。
- 预约只形成待兑现占用；`attend` 与有效 `cash_refund` 才终结权益。已签准的 `rebook`、`refund_due`、`reserve` 单独进入不可释放/已登记准备金额。
- 跨店重复预约在未收到 void 订正前保留为多个 `possible_attributions`，由同一张券的互斥方案枚举处理，不把一张券同时分到两个馆。
- 新改约场次必须满足：点位在 `terms.sites` 内、非退出点、不晚于顺延到期日，并且不超过 `exit_date + hold_days`。已存在预约作为独立的 `retain_booking` 选项，不受新改约等待窗口限制。
- 场次可分配名额保守取 `capacity - max(sealed, approved_rebook_count)`。因此 `S5` 的 `2-2=0` 个可自由分配名额，不会被推荐。
- 枚举所有满足名额约束的互斥方案，得到各结算周退款、兑现费用及合计的联合区间。各周 `gross_min/gross_max` 是边际边界，不能把所有周上界相加当作一个方案；JSON 另给 `minimum_total` 和 `maximum_total` 的连贯方案。
- 未观察到的弃券不记为零。没有成熟弃券证据时，硬区间下界来自“所有未承诺券均有可行兑现路径”的最低成本，上界来自“均选择全额退款”；无可行改约时自然落入退款/未决准备。

## 本包关键结果（金额单位：分）

| 视图 | 未承诺区间 | 含已签准记录的硬区间 | 审慎留足上界 |
|---|---:|---:|---:|
| 退出日 64 / 资料截点 60 | 13,600–20,700 | 26,500–33,600 | 33,600 |
| 退出日 68 / 资料截点 65 | 13,600–20,700 | 26,500–33,600 | 33,600 |
| 退出日 71 / 资料截点 67 | 16,400–24,900 | 29,300–37,800 | 37,800 |
| 补齐记录复盘（含 received 72） | 10,800–16,500 | 23,700–29,400 | 29,400 |

已签准记录合计 12,900：第 64 周 `AP1+AP2=4,800` 为改约兑现；第 71 周 `AP3=3,900` 为实付退款义务、`AP4=4,200` 为签准未决准备。其中不可释放的支付义务为 8,700，另有签准准备 4,200。

第三次试算相对第二次，因 R2 撤销 V10 退款而恢复其权益，未承诺区间整体增加 2,800–4,200；审慎口径建议在第 71 周补提 4,200。R1 只消除 V7 的跨店归属分歧，不改变合同金额区间。补齐记录中的 E18/E19 确认 V7 签到（2,800）和 V8 退款（4,200）；相对截点视图为这两张券预留的 8,400，实际结果为 7,000，其中 2,800 签到兑现需补结算、4,200 迟收实付退款用于对账，仅 1,400 为有利估计变化。补齐后剩余审慎准备为 29,400；若把新确认实际额与剩余准备合并作终身口径备忘，则为 36,400，不能把这个备忘数与资产负债表准备混称。

最新补齐复盘的可行最低成本方案为：V10→S4、V12→S4、V16→S4、V15→S6；`S5` 因封存名额无可分配余量而不使用。该方案的连贯周需求为第 64 周 4,800、第 71 周 16,100、第 78 周 2,800（合计 23,700 分）；审慎准备仍取区间上界 29,400 分。该方案是可签准建议，不是已经发生的签准事实。

## 输出文件

- `reports/trials.json`：三次 `closures.tsv` 试算的完整结果。
- `reports/complete_review.json`：纳入迟收记录后的复盘和历史支持证据。
- `reports/proposed_plan.json`：当前最低总成本、满足名额约束的可签准建议。
- `reports/rollforward.json`：相邻关账视图的补提、估计变化和不可释放义务。
- `reports/post_cutoff_review.json`：最后截点视图与补齐记录复盘之间的迟收签到/退款、补结算和估计差异。
- `reports/vouchers_*.tsv`：前台券状态、当前有效预约/签准去向及保留原因。
- `reports/reconciliation_*.tsv`：财务按发券批、持券类型、承担馆的核对表。


## 输入表
- `vouchers.tsv`：日为相对演练起点的天数；条款号关联原券可用点和退款条件。 列为 `voucher`、`batch`、`issue`、`expiry`、`original_site`、`terms`。
- `terms.tsv`：退款从发券后 refund_day 天开放；hold_days 为顾客接受改约的最长等待；金额单位分。 列为 `terms`、`sites`、`hold_days`、`refund_day`、`refundable`、`fulfil_cost`。
- `actions.tsv`：同一预约以 slot 和券号关联；received 是当时收到日期；以追加订正表确认重复竞争记录。 列为 `event`、`voucher`、`happened`、`received`、`kind`、`party`、`site`、`slot`。
- `rectifications.tsv`：void 表示撤销被指向的重复记录，不能改原记录收件日。 列为 `record`、`event`、`received`、`kind`、`competing_event`。
- `pause.tsv`：结束日不含；extend 说明对 segment 发券批顺延天数，同一批记录可以跨点。 列为 `site`、`start`、`end`、`extend`、`segment`。
- `slots.tsv`：sealed 是签准名额中的锁定名额；历史场次也列在名额表内。 列为 `slot`、`site`、`day`、`capacity`、`sealed`。
- `closures.tsv`：as_of 是作出方案时资料截点；coverage 是准备金目标覆盖比例；结算周从 week_start 顺延七天。 列为 `date`、`site`、`as_of`、`coverage`、`week_start`。
- `approved.tsv`：既有签准记录为外部事实，金额单位分；支付、保留及改约指令分别标记。 列为 `approval`、`voucher`、`signed`、`operation`、`destination`、`amount`、`settlement_week`。
