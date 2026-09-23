# 本地化翻译工作台

APP 本地化文案的**双模型交叉质检翻译**工具：A 模型初译 → B 模型逐条对照（key / 中文原意 / 译文）质检并修正 → 循环至通过 → 输出定稿表格。

提供两种使用方式：**网页工作台**（实时进度、结果对比、配音试听）与**命令行脚本**。

## 目录结构

```
app-localization-translator/
  SKILL.md                  技能说明
  scripts/translate.py      翻译引擎（提示词、模型调用、表格解析）—— Web 端与 CLI 共用
translator-web/
  server.py                 本地服务（Flask，端口 8770）
  index.html                工作台页面（单文件，无构建步骤）
```

## 环境要求

Python 3.9+

```bash
pip install flask pandas requests openpyxl
```

可选：`pip install edge-tts`（TTSMaker 不支持的语种走它兜底配音）

## 配置

密钥一律从环境变量读取，**代码内没有任何兜底值**：

```bash
cp .env.example .env
```

然后编辑 `.env`：

| 变量 | 说明 |
|---|---|
| `COMMANDCODE_API_KEY` | 翻译通道密钥，必填 |
| `COMMANDCODE_BASE_URL` | 接口地址，默认 `https://api.commandcode.ai/provider/v1` |
| `TTSMAKER_TOKEN` | 配音通道，留空则配音不可用（不影响翻译） |

`.env` 已被 `.gitignore` 排除，**不要提交**。也可以用环境变量直接注入，优先级高于 `.env`。

## 启动网页工作台

```bash
cd translator-web
python server.py
```

浏览器打开 **http://127.0.0.1:8770**

> ⚠️ 直接双击 `index.html` 打开是**不行的** —— 没有后端，模型与音色下拉会是空的。必须通过上面的服务地址访问。

功能：

- 导入 xlsx / csv（需含 `key`、`zh` 两列）或手动输入（每行一条，支持 `key|中文`）
- 16 种目标语言多选，串行翻译
- SSE 实时回传：节点流转、执行日志、整体进度
- 结果表两级视图：定稿总表 / 初译↔定稿对比（改动高亮 + "已优化"标记）
- 译文可试听与下载 mp3（按内容哈希缓存，不重复扣字符额度）
- 翻译历史：右上角「历史」按钮打开右侧抽屉，可回看、载入到工作台、重新下载表格

## 命令行用法

```bash
python app-localization-translator/scripts/translate.py \
  -i 输入.xlsx -t 英语 -o 输出.xlsx -m 3
```

| 参数 | 说明 |
|---|---|
| `-i, --input` | 输入文件（xlsx / csv，含 `key`、`zh` 两列） |
| `-t, --target-lang` | 目标语言（单语言，如 `英语`） |
| `-o, --output` | 输出路径（默认 xlsx） |
| `-m, --max-iterations` | 最大质检轮次，默认 3 |

输出表列名为 `key`、`zh` 与**目标语言代码**（英语→`en`、日语→`ja` …），便于按语言取列。

## 模型

| 角色 | 模型 | 说明 |
|---|---|---|
| A · 初译 | `inclusionai/ling-3.0-flash-sante:free` | 免费档 |
| B · 质检 | `meituan/LongCat-2.0` | 付费（$0.30/$1.20 per 1M token，单次成本可忽略） |

> `meituan/LongCat-2.0:free` 免费档已退役，调用返回 403，必须用不带后缀的付费版。

## 安全

- `.env` 不入库；`.env.example` 只含占位符
- `translator-web/outputs/`（翻译结果、历史记录 json、TTS 缓存）与 `translator-web/uploads/`（上传的源表格）均已排除，含业务文案，请勿手动提交
- 提交前可用以下命令自查：

```bash
git grep -nIE "user_[A-Za-z0-9]{20,}|sk-[A-Za-z0-9]{15,}|ghp_|ttsmaker_demo_token" HEAD
```
