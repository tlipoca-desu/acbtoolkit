# acbtoolkit

基于 vgmstream 的通用 CRIWARE `ACB` / `AWB` 音频容器解析与数据块重组库。

**[English →](README.md)**

---

## 1. 简介

`ACB`（cue sheet，提示表）与 `AWB`（wave bank，波形库）是构建于 CRI Atom
中间件之上的游戏所采用的容器格式。通常情况下 `ACB` 本身不含音频：它描述
「播放什么」与「何时播放」，而码流存放在同目录的 `.awb` 中。

在内部，提示表是一份由带索引的数据块组成的数据库。本库读取该结构，并据此把
音频数据块重组回完整的片段。

## 2. 功能

- 解析 `ACB` 的 `@UTF` 表结构，读取 cue / block / 波形索引。
- 调用 vgmstream 解码 `AWB` 容器内的音频流。
- 按索引将音频数据块重组为完整音频片段。
- 输出通用音频格式，依赖 ffmpeg。

## 3. 能力边界

- 本工具**不包含解密功能**；遇到加密的 HCA 码流会直接报错退出。
- **不附带任何游戏音频、密钥、游戏专用配置或二进制依赖**。
- **仅执行容器解析、数据块重组与格式转换**。
- **仅读取**输入文件，不向输入目录写入任何内容。

## 4. 依赖

| 组件 | 许可证 | 用途 |
| --- | --- | --- |
| [vgmstream](https://github.com/vgmstream/vgmstream) | ISC | 解码 `AWB` 容器与 `HCA` 码流 |
| [ffmpeg](https://ffmpeg.org/) | LGPL / GPL | 编码、混音、拼接 |
| Python 3.9+ | — | 运行时 |

**依赖由用户自行安装，本仓库不附带任何二进制文件。**
许可证文本集中存放于 [`THIRD_PARTY_LICENSES/`](THIRD_PARTY_LICENSES/)。

## 5. 安装

需要 Python 3.9 或更高版本，无需任何第三方 Python 包。

```bash
git clone https://github.com/tlipoca-desu/acbtoolkit.git
cd acbtoolkit
```

随后可以直接在源码目录运行：

```bash
python -m acbtoolkit --help
```

或安装 `acbtoolkit` 命令：

```bash
pip install .
```

## 6. 使用

```bash
acbtoolkit build <输入目录> -o <输出目录>
```

`<输入目录>` 须包含 `.acb` / `.awb` 文件。该路径始终由使用者显式给出——
可以是目录，也可以是单个 `.acb`；工具**不会**在你的文件系统中搜索输入。

**工具只读取该目录**——不会向输入目录写入任何内容；全部输出写入你指定的
输出目录。

## 7. 许可证

本仓库自身代码以 **MIT 许可证**发布，见 [LICENSE](LICENSE)。

第三方组件的许可证文本集中存放于 [`THIRD_PARTY_LICENSES/`](THIRD_PARTY_LICENSES/)。

## 8. 免责声明

本工具为通用 CRIWARE `ACB`/`AWB` 容器解析与数据重组库，**不针对任何特定游戏**。

工具本身不包含、不附带、不提供任何游戏音频文件、密钥或解密能力。

用户使用本工具处理任何游戏的音频数据时，**须自行确保其行为符合该游戏权利人
公布的许可条款**。

因用户将处理结果用于公开传播、再分发或其他超出个人私下使用范围的行为，
所产生的全部后果由用户自行承担。

本工具按「现状」提供，开发者不对准确性、可靠性或适用于特定目的作出任何
明示或默示的保证。

因使用或无法使用本工具产生的直接、间接、附带、特殊或后果性损害，
开发者概不承担责任。

完整文本见 [DISCLAIMER.md](DISCLAIMER.md)。

## 9. 下架响应

开发者承诺在收到权利人有效通知后，及时下架或调整本工具。

联系方式：在本仓库提交 issue。

## 10. 格式说明

本工具所读取的 `@UTF` 表布局、ACB 引用链以及 AWB / HCA 结构，
记录于 [`docs/FORMAT.zh.md`](docs/FORMAT.zh.md)（[English](docs/FORMAT.md)）。

## 11. 第三方组件

**vgmstream** —— ISC 许可证。版权声明与许可证全文保留于
[`THIRD_PARTY_LICENSES/vgmstream.txt`](THIRD_PARTY_LICENSES/vgmstream.txt)。

**ffmpeg** —— 依构建方式适用 LGPL / GPL。许可证条款、源码位置及再分发义务见
[`THIRD_PARTY_LICENSES/ffmpeg.txt`](THIRD_PARTY_LICENSES/ffmpeg.txt)。
