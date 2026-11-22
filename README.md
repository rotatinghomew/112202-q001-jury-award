# 体验券与跨馆兑现记录

`data/` 为离线合成业务记录。文件均为 UTF-8 的 Tab 分隔表，首行是列名，逗号分隔集合，`-` 表示不适用，`?` 表示未取得，不把它们当作零。标识符只在各表声明的归属范围内有效。

## 运行与资料核对

使用 Python 3.11。只需 Python 3.11 标准库。

```sh
python3 inspect_packet.py
python3 -m unittest discover -s tests
```

前一条命令只检查表头和记录形状并显示行数。可传入另一目录读取同格式记录。当前没有分析、决策或业务处理结果。

## 输入表
- `vouchers.tsv`：日为相对演练起点的天数；条款号关联原券可用点和退款条件。 列为 `voucher`、`batch`、`issue`、`expiry`、`original_site`、`terms`。
- `terms.tsv`：退款从发券后 refund_day 天开放；hold_days 为顾客接受改约的最长等待；金额单位分。 列为 `terms`、`sites`、`hold_days`、`refund_day`、`refundable`、`fulfil_cost`。
- `actions.tsv`：同一预约以 slot 和券号关联；received 是当时收到日期；以追加订正表确认重复竞争记录。 列为 `event`、`voucher`、`happened`、`received`、`kind`、`party`、`site`、`slot`。
- `rectifications.tsv`：void 表示撤销被指向的重复记录，不能改原记录收件日。 列为 `record`、`event`、`received`、`kind`、`competing_event`。
- `pause.tsv`：结束日不含；extend 说明对 segment 发券批顺延天数，同一批记录可以跨点。 列为 `site`、`start`、`end`、`extend`、`segment`。
- `slots.tsv`：sealed 是签准名额中的锁定名额；历史场次也列在名额表内。 列为 `slot`、`site`、`day`、`capacity`、`sealed`。
- `closures.tsv`：as_of 是作出方案时资料截点；coverage 是准备金目标覆盖比例；结算周从 week_start 顺延七天。 列为 `date`、`site`、`as_of`、`coverage`、`week_start`。
- `approved.tsv`：既有签准记录为外部事实，金额单位分；支付、保留及改约指令分别标记。 列为 `approval`、`voucher`、`signed`、`operation`、`destination`、`amount`、`settlement_week`。
