# AstrBot Gemini TTS 插件

这是一个 AstrBot 的语音合成插件，通过调用 Google Gemini 3.8 TTS 接口，将大模型回复中的特定标签文字转换为语音发送。

本版本专门适配 Gemini 3.8 TTS 的文本朗读接口，去除了上一版中复杂的长音频播客分段和拼接逻辑，仅保留针对聊天场景的单条语音生成。

---

## 功能说明

* **支持模型**：支持调用 `gemini-3.8-flash-lite-tts`（延迟更低，适合聊天）与 `gemini-3.8-flash-tts`。
* **音色设置**：支持直接使用官方预置音色名称，或在 AI Studio 中设计并填入专属 Voice ID，保证音色固定。
* **微动作标签**：支持识别 Gemini 3.8 的内联语气标签（如 `<laugh>`、`<sigh>`、`<whisper>`、`<pause>`），大模型可直接在台词中加入叹气或轻笑。
* **多 Key 轮询**：支持配置多个 Gemini API Key，遇到 429 限流或超时时自动尝试下一个 Key。

---

## 配置说明

在 AstrBot WebUI 插件管理中配置以下字段：

* **api_keys**：Gemini API Key 列表。支持填入多个自动轮询。留空时尝试读取环境变量 `GEMINI_API_KEY` 或系统密钥。
* **tts_model**：
  * `gemini-3.8-flash-lite-tts`（默认）：生成速度较快，适合群聊对话。
  * `gemini-3.8-flash-tts`：旗舰版，音质和语气更细腻。
* **voice_name**：默认发音人音色。可选列表中提供的常见音色（如 `Puck`, `Charon`, `Kore`, `Zephyr` 等）；若需使用其他内置声音或专属声音，请选择 `Custom`。
* **custom_voice**：当 `voice_name` 选择 `Custom` 时生效。
  * 可填入任意 Google 官方内置音色名称（如 `Sulafat`, `Despina`, `Achernar`, `Enceladus` 等，区分大小写）。
  * 可填入在 Google AI Studio 中生成的专属 Voice ID（如 `voice_xxxx`）。
* **temperature**：合成韵律温度，范围 0.0 ~ 2.0，默认 1.0。
* **enable_system_prompt**：是否自动向大模型注入语音使用说明提示词。
* **always_inject_prompt**：开启时每轮对话都注入提示词；关闭后仅当用户发言命中 `trigger_keywords` 时才注入，以节省 Token。
* **trigger_keywords**：语音触发词列表，以逗号分隔。
* **system_prompt_addition**：附加在默认语音提示词后面的自定义要求，留空则使用默认。

---

## 如何获取专属 Voice ID

如果你想让机器人拥有独特且固定的声线：

1. 登录 Google AI Studio (https://aistudio.google.com/)。
2. 进入 Voice 相关模块，用一段文字描述你想要的声线（或上传一段 30 秒的参考音频）。
3. 试听满意后保存，复制生成的 `voice_...` 格式的 ID。
4. 在本插件配置中将 `voice_name` 选为 `Custom`，并将该 ID 粘贴至 `custom_voice` 即可。

---

## 使用方式

大模型在回复时，将需要转为语音的文本放在 `<gemini_tts>` 标签中即可。标签外的文本仍以普通文字发出。

示例：

> 好的，今天辛苦了。<gemini_tts><sigh> 确实挺累的，<whisper> 晚安，做个好梦。</gemini_tts>

如果某条回复希望临时换用其他声音，可以在标签内指定 `voice` 属性：

> <gemini_tts voice="Sulafat">这是一条使用指定声音朗读的内容。</gemini_tts>

---

## 依赖与环境

* 需要 `google-genai` SDK 支持。
* 运行环境需可正常访问 Google API 域名。
