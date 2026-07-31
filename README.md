<div align="center">
  <img src="./logo.png" width="128" height="128" alt="logo"/>
  <h1>Gemini TTS 插件 (astrbot_plugin_gemini_tts)</h1>
</div>

本插件是为 AstrBot 开发的一个第三方语音合成辅助插件。它通过在 LLM（大语言模型）的文本回复中注入与解析特定的 XML 标签（`<gemini_tts>...</gemini_tts>` 与长音频 `<gemini_long_tts>...</gemini_long_tts>`），调用 Google GenAI API 异步生成高拟真语音及拼接长音频文件。

支持多 API Key 轮询机制、音频人设画像 (Audio Profile)、长文本多角色播客/对白自动并发合成与 WAV 无缝拼接。

---

## ![diff](https://img.icons8.com/material-outlined/24/idea.png) 核心功能亮点

| 特性 | 说明 |
| :--- | :--- |
| **按需合成与混合输出** | 仅合成被 `<gemini_tts>` 或 `<gemini_long_tts>` 标签包裹的文本，实现“文字说明 + 局部语音”的混合输出结构。 |
| **长音频/播客拼接能力** | 支持启用 `<gemini_long_tts>` 标签，AI 可将长文、多角色对白或播客拆分为多段并行合成，后台自动加上指定停顿静音并无缝拼接为完整 WAV 音频文件发送。 |
| **音频人设画像 (Audio Profile)** | 支持自定义发音人的声线和人设风格（如：傲娇美少女、冷酷大叔、温柔大姐姐、专业男播音员等），让语音回复更贴合角色设定。 |
| **Markdown 与格式净化** | 自动清洗标签内的格式标记符号，去除链接与控制符，仅保留适合朗读的干净文本。 |
| **动态参数与轮询容灾** | 支持大模型通过标签属性（`voice`、`scene`、`sample_context`、`audio_profile`）随时改变发音人与语气；支持多 API Key 轮询与 429 自动重试。 |

---

## ![design](https://img.icons8.com/material-outlined/24/wrench.png) 设计考量：为何采用标签解析而非工具调用（Tool/Function Calling）？

在设计语音生成逻辑时，本插件采用“大模型输出 XML 标签，插件在后台正则提取”的方案：

1. **节约 API 调用频次与额度（对免费 Key 友好）**
   Gemini 的免费 API Key 具有较严格的每分钟请求次数限制（RPM）。标签解析方案只需一次单向 LLM 请求即可完成文本和语音参数的下发，避免多轮 Tool Calling 触发 Rate Limit（429 报错）。
2. **减少 Token 消耗**
   直接让模型在上下文输出特定的 XML 语法结构，相比加载复杂的 Tool Schema 更加省 Token。
3. **支持长音频多角色并发合成**
   模型可以在单次输出中带上多个 `<gemini_long_tts>` 标签（每段指定不同的发音人与角色画像），插件在后台使用 `asyncio.gather` 并发请求 Gemini API，极大缩短长音频生成等待时间。

---

## ![config](https://img.icons8.com/material-outlined/24/settings.png) 配置说明

在插件的配置文件 `_conf_schema.json` 或 WebUI 后台中，您可以配置以下参数：

* **`api_keys`**: Gemini API Key 列表。支持配置多个，插件将通过轮询方式调用。留空时，将尝试读取系统环境变量 `GEMINI_API_KEY` 或 AstrBot 内置 Gemini 密钥。
* **`tts_model`**: 合成模型名称，默认为 `gemini-3.1-flash-tts-preview`。
* **`voice_name`**: 默认发音人，可选：`Zephyr`, `Aoede`, `Kore`, `Leda`, `Puck`, `Charon`, `Fenrir`, `Orus`, `Custom`。
* **`enable_long_tts`**: 是否启用长音频合成能力。开启后 AI 可使用 `<gemini_long_tts>` 输出长文和多角色对白。
* **`long_tts_silence_sec`**: 长音频每段拼接时的停顿静音长度（秒），默认 `0.4` 秒。
* **`audio_profile_mode`**: 音频画像模式（`ai`: AI自主编写人设；`default`: 使用默认值；`disable`: 关闭）。
* **`default_audio_profile`**: 默认音频人设画像（例如：傲娇美少女、冷酷大叔等）。
* **`temperature`**: 控制语音合成的随机度与节奏。
* **`enable_system_prompt`**: 是否在 LLM 发起请求时自动追加语音指令引导语。
* **`always_inject_prompt`**: 是否全局注入语音提示词（关闭后仅命中关键词时注入，节省 token）。

---

## ![usage](https://img.icons8.com/material-outlined/24/book.png) 使用方法

### 1. 短语音标签 (`<gemini_tts>`)
> **AI 回复**：
> 给你唱首歌吧！<gemini_tts voice="Zephyr" sample_context="唱着歌，有些走音">床前明月光，疑是地上霜...</gemini_tts>

### 2. 长音频/多角色对白标签 (`<gemini_long_tts>`)
当开启 `enable_long_tts` 后，AI 可以分段输出多角色对白，后台会自动并发合成并拼接为一个音频文件：

> **AI 回复**：
> <gemini_long_tts voice="Charon" audio_profile="专业男主持">欢迎收听今日科技播客，我是主持人小王。</gemini_long_tts>
> <gemini_long_tts voice="Aoede" audio_profile="活泼女嘉宾">大家好！今天我们要聊聊 AI 语音合成的最新突破！</gemini_long_tts>

### 3. 支持的标签属性
* `voice`: 发音人名称（如 `Zephyr`, `Puck`, `Charon`, `Aoede` 等）。
* `scene`: 语气/环境描述（如：`悄悄耳语`、`非常激动`、`叹气`、`带南方口音`）。
* `sample_context`: 对话上下文语气背景。
* `audio_profile`: 音频角色画像（如：`傲娇美少女`、`冷酷大叔`）。

---

## ![warning](https://img.icons8.com/material-outlined/24/error.png) 注意事项

1. **依赖项**：本插件使用 Google 官方最新的 `google-genai` SDK。
2. **网络环境**：调用 Gemini 语音服务需要宿主服务器能够正常访问 Google API 终端点。
3. **多 Key 配额**：如果经常遇到 `429 RESOURCE_EXHAUSTED` 错误，建议配置多个 API Key 激活轮询机制。
