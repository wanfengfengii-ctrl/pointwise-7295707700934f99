# 衡川试验记录交换站

耐久与压力试验台将测读导出为具名定宽分段。资料员整理原卷，允许乱序录入、原据重放和显式勘误。发行值班员将冻结版的字面原块发往指定隔离站。夜班从本站报告读出欠位选单，补齐后另签完卷报告。旧报告和实收回执随站库一起留存。

使用 Python 3.11 和标准库。在项目根目录运行完整回归和工作日样例。`check_project.py` 由控制台物化提供。

```sh
python3 -B check_project.py
python3 -B demo.py
```

独立使用项目源码时，可用 `python3 -B -m unittest discover -s tests` 直接运行同一 unittest 套件。demo 使用临时目录，示范两个站库、先欠后齐的报告，以及移开发行库后的历史复领。试验台的一帧夹具只传递同一件字节并记录实际正文帧数。字面件仍按件中指定的站点收。

## 公开办理接口

先连接 SQLite 数据库并初始化相应存储对象。来源侧的 `DraftStore`、`RevisionCatalog` 与 `IssueHistory` 使用同一发行库连接。隔离侧的 `StationMaterial`、`ReceiptStore` 与 `StatementArchive` 使用本站库连接。连接归调用者关闭。使用默认 sqlite3 连接即可。调用办理函数前结束调用者自己的事务，由办理函数负责相应提交边界。存储对象的低层写入方法供相应办理事务使用。

| 办理 | 接口及结果 |
| --- | --- |
| 登记与收料 | `intake.open_and_load(drafts, DraftLayout(volume, slot_count, width), segments=())` 返回 `Draft`。每段为 `DeviceSegment(reference, position, data)`。用 `intake.missing_positions(draft.layout, draft.blocks)` 获取欠位。 |
| 开后继与勘误 | `edition.build_successor_seed(catalog, volume, parent_revision)` 领取确切父版副本，交给 `intake.open_successor(drafts, seed)` 安装后继。`intake.correct(drafts, draft_id, Correction(position, data, reference))` 更新已占原位并追加出处。 |
| 冻结及领取 | `edition.freeze(drafts, catalog, draft_id, revision)` 返回 `FrozenRevision`，其 `directory` 包含有序摘要和出处，`bodies` 按原位存放原字节。`catalog.get(volume, revision)` 按号查阅。 |
| 发行与领取 | `shipment.issue(catalog, issues, volume, revision, station, selection=ALL_POSITIONS)` 返回 `LiteralManifest`。具名选单以零起原位编号。`issues.get(packet_id)` 返回固定件。`shipment.encode_literal_packet` 与 `decode_literal_packet` 提供 V1 JSON 字节表示。 |
| 隔离点收 | `station.accept(material, receipts, station, packet)` 接受 `LiteralManifest` 或编码后的 bytes，返回 `Receipt`。每项保留局部顺序、原位、原字节、冻结出处及其首次实收号。 |
| 核账与复领 | `reconcile.reconcile(material, archive, station, directory)` 返回 `Statement`。使用 `.received`、`.missing` 和 `.received_bytes` 核账，`.lines` 给出原位、冻结出处、实际长度及首执。`reconcile.read_history(receipts, archive, station, record_id)` 领取已保存的 R- 实收或 S- 报告。 |

## 数据与失败边界

卷号、修订号、站号和原据为非空文本，块宽为正整数，原位数允许为零。位置从零起算，正文为等宽 `bytes`。同布局重开返回当前草稿，重复打开同一父版的后继保留已提交的勘误。设备分段同号同位同正文视作重放，改位或改字节拒绝整批。新原据可与已有原位重叠，当前正文仍由原先放置或显式勘误决定。冻结区分缺位与实测全零，禁止重用同卷修订号。

冻结目录按原位升序列出块摘要和出处。整卷字节数按原位累计，字节相同的异位各计一次。目录摘要对固定字段的规范 JSON 计算 SHA-256。隔离站以自洽的合法来件登记初见目录；同卷同修订的后续件须与该目录一致。发行及回执保留具名选单顺序。空选单依然可以登记目录和空回执。第一个合法实收记录为该站该卷版该位的首执。相同来件重投原样复领，同件号更换规范化内容会被拒绝。

报告按目录原序计算，已收和缺位互补，已收字节数为本站各已收原位的实际正文长度之和。历史快照各自保留签报时的实收覆盖和出处。以一字节的两位卷为例，先收位 1 得到已收 `[1]`、缺位 `[0]` 和 1 字节。补件 `[1,0]` 的回执保持这一顺序，首项引用原首执。再签报告得到 `[0,1]`、无缺位和 2 字节。

错误输入、目录冲突或件号绑定冲突抛出 `ValueError`。办理必需的草稿、冻结版或已见目录缺失抛出 `LookupError`。按号查询缺失记录返回 `None`。SQLite 写入失败保留 `sqlite3.Error`，失败的接纳或签报不留下部分业务状态。
