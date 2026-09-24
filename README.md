<div align="center">
  <img src="./logo.png" width="128" height="128" alt="logo"/>
  <h1>Gemini 3.8 TTS 插件 (astrbot_plugin_gemini_tts)</h1>
  <p>专为聊天机器人定制的极简、极速、专属音色语音合成插件</p>
</div>

本插件基于 Google 最新发布的 **Gemini 3.8 纯真 TTS 引擎**（`gemini-3.8-flash-lite-tts` 与 `gemini-3.8-flash-tts`）开发。

针对 IM 聊天场景全面重构，抛弃了臃肿冗余的长音频播客/对白拼接机制，采用**专属声线绑定（Voice Design ID）**与**逐字稿朗读（Verbatim）**架构，彻底杜绝了老版本“上一句御姐、下一句萝莉”的声线漂移和把提示词当台词念出的问题。

---

## 🚀 核心亮点

| 特性 | 说明 |
| :--- | :--- |
| **纯真 3.8 引擎支持** | 仅支持 `gemini-3.8-flash-lite-tts`（默认推荐，毫秒级响应、超低成本、高并发）和 `gemini-3.8-flash-tts`（旗舰级高拟真创作版）。 |
| **专属音色绑定 (Zero Drift)** | 支持直接填入在 Google AI Studio 捏好的专属声音 ID（如 `voice_xxxx`），0 Prompt 开销，声线 100% 稳定统一，告别声线漂移。 |
| **官方原生内联微动作** | 支持在台词中内嵌 `<laugh>`（笑）、`<sigh>`（叹气）、`<whisper>`（耳语）、`<pause>`（停顿）等动作表情，音色与情绪浑然天成。 |
| **逐字稿严格对齐** | 严格遵循 3.8 的 Verbatim 规则，内置自动 Markdown 清洗层，坚决防止大模型把提示词或 Markdown 格式字符念出。 |
| **默认免审 (BLOCK_NONE)** | 内容安全过滤策略默认设为 `BLOCK_NONE`，避免日常交流、二次元角色扮演被误拦截。 |
| **多 Key 轮询与容灾** | 支持多 API Key 自动轮询 (Round-robin)；遇到 429 速率限制或网络超时自动平滑切换下一个密钥。 |

---

## ⚙️ 配置说明

在 **AstrBot WebUI 控制台 -> 插件管理 -> Gemini 3.8 TTS** 中即可完成配置：

* **`api_keys`**: Gemini API Key 列表。支持配置多个自动轮询。留空时尝试自动读取环境变量 `GEMINI_API_KEY` 或 AstrBot 系统内置 Gemini 密钥。
* **`tts_model`**:
  * `gemini-3.8-flash-lite-tts` (**默认推荐**，极速响应，群聊对话首选)
  * `gemini-3.8-flash-tts` (旗舰级高拟真)
* **`voice_name`**:
  * 可选预置音色：`Puck`, `Charon`, `Kore`, `Fenrir`, `Aoede`, `Zephyr`, `Leda`, `Orus`, `Enceladus`。
  * 选择 **`Custom`**：激活下方的自定义音色输入框。
* **`custom_voice`**: 自定义音色名称或专属 Voice ID。
  * **输入其他内置音色**：Google 拥有 30+ 官方 Studio 音色及扩展库（如 `Sulafat`, `Despina`, `Achernar`, `Rasalgethi`, `Mimosa` 等，注意区分大小写）。
  * **输入专属 Voice ID**：在 Google AI Studio 捏好的专属声线 ID（格式如 `voice_xxxx`）。
* **`temperature`**: 合成韵律温度（0.0 ~ 2.0，默认 1.0）。
* **`enable_system_prompt`**: 是否在 LLM 发起请求时自动追加语音指令引导语。
* **`always_inject_prompt`**: 是否全局注入语音提示词（关闭后仅当用户发言命中 `trigger_keywords` 触发词时注入，节省 token）。
* **`trigger_keywords`**: 语音触发关键词列表。
* **`system_prompt_addition`**: 自定义系统提示词引导语。

---

## 💡 如何在 Google AI Studio 设计专属声音？

1. 登录 [Google AI Studio](https://aistudio.google.com/)；
2. 进入 Voice Design / TTS 模块；
3. 用一句自然语言描述你想要的 Bot 声线（例如：*“A cute, playful anime girl voice speaking fluent Mandarin with a sweet and gentle tone”*）或上传一段 30 秒无杂音的人声音频进行克隆；
4. 试听满意后，点击生成/保存声音，复制生成的 **`voice_...`** 字符串；
5. 将该字符串填入本插件的 **`custom_voice_id`**，音色模式选择 **`custom_voice_id`** 即可！

---

## 💬 使用方式

大语言模型（LLM）只需在回答中将需要转化为语音的内容包裹在 `<gemini_tts>` 标签中即可：

> **用户**：能给我说一句晚安吗？
> **AI 回复**：好呀！<gemini_tts><whisper> 晚安，做个好梦吧。<sigh></gemini_tts>
> 
> *（标签内的内容会被转换为拟真语音，标签外的文字照常以文本发送）*
