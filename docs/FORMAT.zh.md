# CRIWARE ACB / AWB 格式笔记

关于本工具所读取的那部分格式的工作笔记。依据 CRIWARE 1.37–1.46 的提示表，
以及 [vgmstream](https://github.com/vgmstream/vgmstream) 的参考实现
（`src/util/cri_utf.c`、`src/meta/acb.c`、`src/meta/awb.c`、`src/meta/hca.c`）。

以下内容均对照真实文件验证过，而非从其他游戏的文档推断，因此未必描述所有
CRIWARE 版本。

*[English →](FORMAT.md)*

---

## 1. `@UTF` 表

CRI 的通用序列化格式：一张带四个分区的行/列数据库。`ACB`、`ACF`、`CPK`
等格式都使用它。

```
偏移    长度  字段
0x00    4     "@UTF"
0x04    u32be table_size - 8        表结束于 base + 8 + 该值
0x08    u16be version               0 或 1
0x0a    u16be rows_offset    - 8
0x0c    u32be strings_offset - 8
0x10    u32be data_offset    - 8
0x14    u32be name_offset           表名，位于字符串区内
0x18    u16be column_count
0x1a    u16be row_width             每行字节数
0x1c    u32be row_count
```

有两个细节曾耗费大量调试时间：

- **分区偏移量存放时都偏置了 8**，因为它们是从「magic + size」之后开始计量的。
  每一个都需要 `+8` 才是真实偏移。`name_offset` 是例外：它本身就是相对于
  字符串区的偏移。

- **`row_width` 不能当作列预算来信任。** 自 HCA v3 起，某些表——尤其是 ACB 的
  `Header`——声明的 `row_width` 小于其行内各列宽度之和，并让末尾几列越过它读入
  空白区域。用 `row_width` 做校验会拒绝掉本可读取的文件。vgmstream 记录了同样的
  怪癖。应当改为校验行**分区**的大小。

各分区按顺序如下：

| 分区 | 内容 |
| --- | --- |
| schema | 每列一条记录：`u8 info`、`u32be name_offset`，当设置了 DEFAULT 标志时再跟常量值 |
| rows | `row_count` 条等宽记录 |
| strings | 以 NUL 结尾的字符串，由表名、列名与字符串值共用 |
| data | 变长载荷（VLDATA），通常是嵌套的 `@UTF` 表 |

### 列记录

```
info = (flags << 4) | type
```

标志位：

| 标志 | 含义 |
| --- | --- |
| `0x10` | NAME —— 该列在字符串区中有名字 |
| `0x20` | DEFAULT —— 单个常量，内联存放于 schema 区 |
| `0x40` | ROW —— 每行一个值，存放于行区 |
| `0x80` | UNDEFINED —— 不应出现 |

实际中一列要么是 DEFAULT 要么是 ROW，不会两者兼具（若都设置则 DEFAULT 优先）。
**DEFAULT 列的值位于 schema 区，因此其地址是 `table_base + offset`。**
对根表而言 `table_base == 0`，这与单纯的偏移恰好重合，从而掩盖了这个错误——
只有嵌套表才会暴露它。

类型及其宽度：

| 编码 | 类型 | 字节数 |
| --- | --- | --- |
| `0x00` | u8 | 1 |
| `0x01` | s8 | 1 |
| `0x02` | u16be | 2 |
| `0x03` | s16be | 2 |
| `0x04` | u32be | 4 |
| `0x05` | s32be | 4 |
| `0x06` | u64be | 8 |
| `0x07` | s64be | 8 |
| `0x08` | f32be | 4 |
| `0x09` | f64be | 8 |
| `0x0a` | string | 4（字符串区内的偏移） |
| `0x0b` | VLDATA | 8（u32 数据区偏移 + u32 长度） |
| `0x0c` | u128 | 16 |

辅助提示：文件中常把纯整数数组（`TrackIndex`、`BlockIndex`、`ReferenceItems`）
存为 VLDATA，内容是大端 u16 序列。见 `UtfTable.u16_array`。

---

## 2. ACB：提示表

`.acb` 含一张 `Header` 表，指向其他所有表。它内部通常没有音频——何时含有见 §3。

### 引用链

```
CueName ──► Cue ──► （取决于 ReferenceType）
```

实际中见到的 `Cue.ReferenceType` 取值：

| 值 | 链路 |
| --- | --- |
| `0x01` | Cue → Waveform |
| `0x02` | Cue → Synth → Waveform |
| `0x03` | Cue → Sequence → Track → TrackEvent → Command → Synth → Waveform |
| `0x08` | Cue → **BlockSequence** → Block → Track → TrackEvent → Command → Synth → Waveform |

`0x03` 是线性序列（一条或多条轨），`0x08` 是分块音乐。还存在其他取值
（`0x05` outsideLink、`0x09` insideLinkBlockSequence 等），但很少见；
本工具会忽略它们。

### 抵达音频

轨**不**直接引用波形。链路要经过一段命令块：

```
Track.EventIndex ──► TrackEvent[EventIndex].Command   (VLDATA)
```

`Command` 是一串 TLV 记录：

```
u16be code
u8    size
bytes payload（size 字节）
```

会发声的编码：

| 编码 | 含义 |
| --- | --- |
| 2000 | noteOn |
| 2003 | noteOnWithNo —— 载荷相同 |
| 2004 | noteOnWithDuration —— 不带引用 |

noteOn 的载荷以一个引用项开头：

```
u16be item_type
u16be item_index
```

| item_type | 目标 |
| --- | --- |
| `0x01` | Waveform[item_index] |
| `0x02` | Synth[item_index] |
| `0x03` | Sequence[item_index] |

`Synth.ReferenceItems` 使用相同的 `(type, index)` 对，因此一个 Synth 可以指向
Waveform、另一个 Synth，或 Sequence。嵌套深度很小（≤3）。

其他编码（音量、声像、静音、停止、回调……）不影响提取，会被跳过。

### Waveform

| 列 | 含义 |
| --- | --- |
| `StreamAwbId` | 外部 `.awb` 中该流的 0 基索引 |
| `MemoryAwbId` | `.acb` 自身内嵌库中的 0 基索引 |
| `Streaming` | 0 = 内嵌，1 = 流式，2 = 预取 + 流式 |
| `SamplingRate`、`NumSamples` | 精确时长 |
| `LoopFlag` | 1 = 不循环，2 = 循环 |

`StreamAwbId` 与 `MemoryAwbId` 中只有一个有意义，未使用的那个读出为 `0xFFFF`。
**单个 cue 可以混用两种类型**——一个真实例子有 9 个块，其 9 条波形中 8 条来自
`.awb`，1 条内嵌于 `.acb`。

循环点（若存在）位于单独的 `WaveformExtensionData` 表（`LoopStart`、`LoopEnd`），
通过 `Waveform.ExtensionData` 引用。

### 分块音乐

`BlockSequence` 是播放列表：

| 列 | 含义 |
| --- | --- |
| `NumBlocks` | 播放的段数 |
| `BlockIndex` | VLDATA，`NumBlocks` × u16 —— **权威顺序** |
| `NumTracks`、`TrackIndex` | 整个序列期间活跃的轨 |

`Block` 是一个段：

| 列 | 含义 |
| --- | --- |
| `NumTracks`、`TrackIndex` | 该段中**同时**发声的轨 |
| `Length` | 段长，毫秒 |
| `StartPosition` | 距 cue 起点的偏移，毫秒 |
| `PlaybackType` | 0 = 播放一次，非 0 = 可重复 |
| `NumLoops` | 循环次数 |
| `DestinationBlocks` | u16 的 VLDATA —— 该段可跳往何处 |
| `JumpDestination`、`LastTransitionBlockNo` | 另一套跳转记账字段 |
| `Name` | 面向人的段编号 |

cue 时长 = `max(StartPosition + Length)`。各段首尾相接排布，`StartPosition` 是
累计值——这正是线性重建得以成立的原因。

#### 决定重建效果是否正确的两个要点

**波形可以比它所在的块更长。** 已验证的例子：`Block.Length` = 3720 ms，但其波形为
7442 ms，而 `14883 + 7442 = 22325`——恰好等于**下下个**块的 `StartPosition`。
该音符是延续到后续各段之下的长音层。因此素材必须放置在绝对时间轴上；
把它裁剪到所属块会切断这个音。

**没有任何 noteOn 命令的块是休止。** 它照样消耗 `Length`。真实文件中存在这类块
（一个 3.8 秒、含三条轨却都不发 noteOn 的块，以及一个 1.9 秒的同类块）；
它们是写定的静音间隔。丢弃它们会让其后的内容整体前移，使曲目变短。

#### 分支

`DestinationBlocks` 编码运行时改道——战斗阶段、层次切换。一条真实的战斗 cue：

```
blk0   9.0s   前奏                      （无跳转）
blk1   6.0s   循环点                     （自跳）
blk2  72.0s   主循环                     （自跳）
blk3   3.0s
blk4  24.0s
blk5  22.5s   → blk7
blk6   1.5s   → blk2    转场音效
blk7   1.5s   → blk4    转场
blk8  12.0s   收尾
```

写定的 `BlockSequence` 顺序是静态的；游戏在那些跳转之间做选择。因此把序列完整
渲染一遍即可得到完整作品，包含各转场。

---

## 3. AWB：波形库

`.awb` 是一个 AFS2 容器，其后可选地跟随裸 HCA 码流。

```
0x00  4  "AFS2"
0x04  u8 version        (2)
0x05  u8 offset_size    (4 = 32 位偏移)
0x06  u8 id_size        (4 = 32 位 id)
0x07  u8 flags          (0，或 1 = 小端 id)
0x08  u32be num_files
0x0c  u16be alignment   (32)
0x0e  u16be subkey      HCA 解密密钥，0 = 无
0x10  entries[num_files]: { u32be offset, u32be size }（宽度由 offset_size 决定）
```

随后是按对齐排布的码流。每个解码后的载荷都以 HCA 头开始，因此偏移/长度对也可以
通过扫描 `HCA\0` 得出。

`subkey` 是该库的 HCA 密钥。当它为 `0x0000` 时音频是明文——在假定某游戏使用了
加密之前先检查这一点。

### 内嵌库

`.acb` 可以在 `Header` 的 `AwbFile` VLDATA 列中携带自己的 AFS2 库。当该列存在
但**长度为零**时，提示表自身没有音频，而是引用外部 `.awb`。

一个 ACB 可以既内嵌音频**又**引用外部库。这两个集合不重叠——通过对 18 个此类
文件逐流比较内容哈希验证（若只读取 ACB 会丢失的外部流总数：63+；其中一个库
单独持有 234 条内嵌流，而外部流仅 1 条，二者无一重合）。必须两者都提取，
否则素材会被静默丢弃。

---

## 4. HCA：编码

```
0x00  4  "HCA\0"
0x04  u16be version      0x0200 / 0x0300
0x06  u16be header_size
0x08  chunks...
```

需要留意的 chunk：

| chunk | 含义 |
| --- | --- |
| `fmt\0` | 声道数、采样率、块大小 |
| `comp` | 压缩参数 |
| `dec\0` | 解码器相关 |
| `loop` | 循环起止点 |
| `cipher` | 加密类型 —— **不存在即为明文** |
| `vbr` | 可变比特率 |

明文 HCA 没有 `cipher` chunk，且起始处即为可读的 `HCA\0` 签名。加密码流同样会
显示 `HCA\0`，但带有 `cipher`，其载荷需要来自 AWB/AHX 头或外部来源的密钥。

在寻找密钥之前先做验证：许多作品——包括一些打包了 CRIWARE 运行时却从未设置
密钥的 Unity 游戏——发行的就是明文音频。

---

## 5. 实践要点

- **让 `.acb` 与 `.awb` 保持同目录、同文件名。** 名称解析会用提示表对照库中的流
  id；单独一个 `.awb` 只能得到光秃秃的编号，因为映射关系位于提示表中。
- **流 id 是 0 基的；`vgmstream -s` 是 1 基的。** 这里差一位会得到听起来合理
  但实际错误的音频。
- **不要用 `?n` 命名 vgmstream 的输出。** 对于没有 cue 名称的流，它会用整个输入
  路径替代，而产生的 `/` 字符会让它拒绝写入文件。请使用 `?s`（子流编号），
  并从 stdout 的 `stream name:` 行读取名称——它们按子流顺序排列。
- 有些库声明的 `Waveform` 行数与实际流数不一致；始终通过 `StreamAwbId` 解析，
  而不要假定索引相等。
