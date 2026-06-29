# astrbot_plugin_gemini_tts

本插件是为 AstrBot 开发的一个第三方语音合成辅助插件。它通过在 LLM（大语言模型）的文本回复中注入与解析特定的 XML 标签（`<gemini_tts>...</gemini_tts>`），并调用 Google GenAI API 异步生成高拟真语音。

支持多 API Key 轮询机制，旨在为需要高性价比、按需且具表现力语音互动的场景提供轻量化解决方案。

---

## ![diff](https://img.icons8.com/material-outlined/24/idea.png) 与官方内置 TTS 的差异点

AstrBot 本身已提供系统级的内置 TTS 服务（支持全局 Gemini TTS），本插件与内置 TTS 的主要差异如下：

| 特性 | 官方内置 TTS 服务 | 本插件（gemini_tts） |
| :--- | :--- | :--- |
| **合成范围** | **全量合成**：只要开启语音，大模型输出的所有文本都会被转为语音。 | **按需合成**：仅合成被 `<gemini_tts>` 标签包裹的文本。 |
| **混合输出** | 不支持。回复只能纯文本或纯语音。 | 支持。可以实现“文字说明 + 局部语音”的混合输出结构。 |
| **Markdown 净化** | 可能会合成 Markdown 符号（如 `*`、`#`）、换行和网络链接，导致发音异常。 | 自动清洗标签内的标记符号与格式，仅保留适合朗读的干净文本。 |
| **参数动态控制** | 发音人与配置在后台固定，无法根据具体语境随时改变。 | 允许大模型通过标签属性（如 `voice`、`scene`、`sample_context`）在每次生成时动态调整语气与场景特征。 |
| **API 额度优化** | 只要产生回复就会发起合成，API 消耗速度较快。 | 仅在触发标签时调用合成，可节省 API 额度与开销。 |

---

## ![design](https://img.icons8.com/material-outlined/24/wrench.png) 设计考量：为何采用标签解析而非工具调用（Tool/Function Calling）？

在设计语音生成逻辑时，本插件没有选择让大模型直接调用工具（Tool/Function Calling），而是采用了“大模型输出 XML 标签，插件在后台正则提取”的方案。主要基于以下考虑：

1. **节约 API 调用频次与额度（对免费 Key 友好）**
   Gemini 的免费 API Key 具有较严格的每分钟请求次数限制（RPM）。如果采用 Tool Calling，通常需要经过：*大模型生成工具参数 -> 框架执行工具 -> 返回结果给大模型 -> 大模型汇总生成回复* 这样的多轮交互或并发调用，容易频繁触发 Rate Limit（429 报错）。标签解析方案只需一次单向 of LLM 请求即可完成文本和语音参数的下发。
2. **减少 Token 消耗**
   定义和描述 Function/Tool 的 JSON Schema 需要占用较多的系统提示词 Token。直接让模型在上下文输出特定的 XML 语法结构更为轻量。
3. **兼容 Agent 能力较弱的低参数模型**
   并非所有接入 AstrBot 的大模型都具备优秀的工具调用（Tool Use）能力。一些推理能力较弱的轻量模型在频繁调用工具时，容易出现参数格式错误、甚至直接忽略工具调用指令的情况。相比之下，按照格式输出文本标签（如 `<gemini_tts>...</gemini_tts>`）对模型的逻辑推理能力要求较低，即使是小参数模型也能高概率正确输出，泛用性与健壮性更好。

---

## ![config](https://img.icons8.com/material-outlined/24/settings.png) 配置说明

在插件的配置文件 `_conf_schema.json` 或 WebUI 后台中，您可以配置以下参数：

* **`api_keys`**: Gemini API Key 列表。支持配置多个，插件将通过轮询方式调用。留空时，将尝试读取系统环境变量 `GEMINI_API_KEY` 或 AstrBot 内置 of Gemini 密钥。
* **`tts_model`**: 合成模型名称，默认为 `gemini-3.1-flash-tts-preview`。
* **`voice_name`**: 默认发音人，可选：`Puck`, `Charon`, `Kore`, `Fenrir`, `Aoede`, `Zephyr` 等。
* **`temperature`**: 控制语音的随机度与节奏。
* **`enable_system_prompt`**: 是否在 LLM 发起请求时，自动在 System Prompt 后面追加语音指令引导语。
* **`system_prompt_addition`**: 具体的引导提示词内容。

---

## ![usage](https://img.icons8.com/material-outlined/24/book.png) 使用方法

### 1. 自动注入引导
当开启 `enable_system_prompt` 后，插件会在用户的输入发送至 LLM 前，在系统提示词末尾自动追加规则。
AI 会在合适的语境下自动选择使用标签。例如：

> AI 的回复：
> 给你唱首歌吧！
> <gemini_tts voice="Zephyr" sample_context="唱着歌，有些走音">床前明月光，疑是地上霜...</gemini_tts>

### 2. 手动引导
如果你关闭了自动提示词注入，或者想主动测试，可以在与 AI 对话时使用以下提示词进行引导：

> **用户**：请用傲娇的语气对我说：你今天真好看。请用 `<gemini_tts>` 格式输出，设置 sample_context 为“傲娇”
>
> **AI**：<gemini_tts sample_context="傲娇">哼，你今天...勉强算好看啦！</gemini_tts>

### 3. 支持的属性
在标签中，你可以动态传递以下可选参数：
* `voice`: 发音人名称（区分大小写，如 `Zephyr`, `Fenrir`）。
* `scene`: 环境场景（如：`夜晚`、`雨天`、`安静的房间`）。
* `sample_context`: 说话时的语气背景（如：`悄悄耳语`、`非常激动`、`叹气`）。

---

## ![warning](https://img.icons8.com/material-outlined/24/error.png) 注意事项

1. **依赖项**：本插件使用 Google 官方最新的 `google-genai` SDK。请确保在插件部署环境执行过依赖安装。
2. **网络环境**：调用 Gemini 语音服务需要您的宿主服务器能够正常访问 Google API 终端点（`https://generativelanguage.googleapis.com`）。
3. **多 Key 配额**：如果经常遇到 `429 RESOURCE_EXHAUSTED` 错误，建议在配置项中多填入几个 API Key，以激活插件自带 of 轮询容错机制。
