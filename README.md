# 评审参照资料

`data/` 为离线合成业务记录。文件均为 UTF-8 的 Tab 分隔表，首行是列名，逗号分隔集合，`-` 表示不适用，`?` 表示未取得，不把它们当作零。标识符只在各表声明的归属范围内有效。

## 运行与资料核对

使用 Python 3.11。只需 Python 3.11 标准库。

```sh
python3 inspect_packet.py
python3 -m unittest discover -s tests
```

前一条命令只检查表头和记录形状并显示行数。可传入另一目录读取同格式记录。当前没有分析、决策或业务处理结果。

## 输入表

- `works.tsv`：P 为专业组，U 为高校组；anchor 是标尺作品，不参与获奖，both 表示两组共用。 列顺序为 `work`、`group`、`kind`、`stage`。
- `scores.tsv`：每条为十分制（整数 0 到 10）的一维评分。NAS 为缺席。judge/work/stage/dim 一致的较高 rev 替代低版本，received 是当届顺序号。 列顺序为 `record`、`judge`、`work`、`stage`、`dim`、`points`、`rev`、`received`。
- `conflicts.tsv`：自收到起此前的相同 judge/work/stage/dim 评分不可使用；* 表示该范围内所有维度。 列顺序为 `case`、`judge`、`work`、`stage`、`dim`、`received`。
- `slots.tsv`：award 的适用组别、席位数、同分处理及不能由同一作品兼得的奖项。hold 表示遇边界并列暂缓。 列顺序为 `award`、`group`、`seats`、`tie`、`excludes`。
- `notices.tsv`：已经发出的通知及顺序号，不能当草稿。 列顺序为 `notice`、`award`、`work`、`received`。
- `capacity.tsv`：可补评的维度、可服务组别、具体场次、可承担的评分条目数与截止场次。 列顺序为 `judge`、`fit`、`groups`、`slot`、`max`、`deadline`。
