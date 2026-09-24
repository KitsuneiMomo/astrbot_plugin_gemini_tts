import os
import re
import json
import time
import struct
import base64
import asyncio
import tempfile
import uuid
from typing import Optional, List, Tuple
from google import genai
from google.genai import types

from astrbot.api.star import Star, Context, StarTools, register
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api import logger, AstrBotConfig
from astrbot.api.provider import ProviderRequest

try:
    from astrbot.api.message_components import Plain, Record, File
except ImportError:
    from astrbot.api.message_components import Plain, Record
    File = Record


DEFAULT_SYSTEM_PROMPT_ADDITION = (
    "【语音回复能力】\n"
    "你拥有直接发送真实语音的能力。当你需要用语音回复或朗读时，请将需要朗读的文本用 <gemini_tts>...</gemini_tts> 标签包裹。\n"
    "标签外的文字会正常作为文字发送，标签内的文字会被转换为拟真语音。整条回复也可以全为语音标签。\n\n"
    "【使用时机】\n"
    "仅当用户明确要求语音、朗读、唱歌、讲故事，或语境极度适合发语音（如哄睡、道晚安、亲昵互动）时才使用；平时一律以纯文字回复。\n\n"
    "【标签内规则（严格遵循）】\n"
    "1. 标签内只放纯台词，严禁包含任何 Markdown 格式（如标题、加粗、列表、代码块、链接）。\n"
    "2. 严禁在标签内写诸如 [叹气]、(温柔地说) 等文字说明；若需表达语气动作，仅可使用官方内联动作标签：<laugh>（轻笑）、<sigh>（叹气）、<whisper>（耳语）、<pause>（短暂停顿）。\n"
    "示例：<gemini_tts><sigh> 唉，今天确实太辛苦了。<whisper> 晚安，做个好梦吧。</gemini_tts>"
)


@register(
    "astrbot_plugin_gemini_tts",
    "KitsuneiMomo",
    "调用谷歌 Gemini 3.8 TTS 接口的语音合成插件，支持自定义专属 Voice ID",
    "2.0.0",
    "https://github.com/KitsuneiMomo/astrbot_plugin_gemini_tts",
)
class GeminiTTSPlugin(Star):
    def __init__(self, context: Context, config: Optional[AstrBotConfig] = None):
        super().__init__(context)
        self.config = config if config is not None else {}

        self.api_keys = self.config.get("api_keys", [])
        self.tts_model = self.config.get("tts_model", "gemini-3.8-flash-lite-tts")
        self.voice_name = self.config.get("voice_name", "Puck")
        self.custom_voice = str(self.config.get("custom_voice", "")).strip()

        try:
            self.temperature = min(2.0, max(0.0, float(self.config.get("temperature", 1.0))))
        except (ValueError, TypeError):
            self.temperature = 1.0

        self.enable_system_prompt = self.config.get("enable_system_prompt", True)
        self.always_inject_prompt = self.config.get("always_inject_prompt", True)
        self.trigger_keywords = self.config.get(
            "trigger_keywords", "语音,说,唱,读,听,声音,tts,voice,speak,read,listen,audio"
        )

        sys_addition = self.config.get("system_prompt_addition")
        self.system_prompt_addition = (
            sys_addition
            if (sys_addition and sys_addition.strip())
            else DEFAULT_SYSTEM_PROMPT_ADDITION
        )

        # 备用密钥与轮询游标
        self.fallback_keys = []
        if not self.api_keys:
            self.fallback_keys = self.get_fallback_keys()
        self.key_index = 0

        # 正则表达式匹配短语音指令标签 <gemini_tts (voice="...")?>...</gemini_tts>
        self.tts_tag_pattern = re.compile(
            r'[<\[]gemini_tts(?:\s+([^>\]]*))?[>\]](.*?)(?:[<\[]\s*[/\\\\]\s*gemini_tts\s*[>\]]?|$)',
            re.DOTALL | re.IGNORECASE,
        )

        # 异步启动旧临时文件清理 (防御无运行中的事件循环场景)
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self._cleanup_old_temp_files())
        except RuntimeError:
            pass

        voice_display = self.get_active_voice()
        logger.info(
            f"[Gemini 3.8 TTS] 插件 v2.0.0 初始化成功 (模型: {self.tts_model}, 音色: {voice_display}, 审核: BLOCK_NONE)"
        )

    def extract_voice_override(self, attr_str: str) -> Optional[str]:
        """从标签属性中提取可选的 voice 临时覆盖参数 (如 voice='Sulafat' 或 voice=voice_xxxx)"""
        if not attr_str:
            return None
        m = re.search(r'voice=(?:["\']([^"\']*)["\']|([^\s>\]]+))', attr_str, re.IGNORECASE)
        if m:
            val = (m.group(1) or m.group(2) or "").strip()
            return val if val else None
        return None

    def get_active_voice(self, override_voice: Optional[str] = None) -> str:
        """获取当前配置的发音人 (支持临时标签覆盖、Custom 自定义音色名/Voice ID、或预置发音人)"""
        if override_voice and override_voice.strip():
            return override_voice.strip()

        if self.voice_name.lower() == "custom":
            if self.custom_voice:
                return self.custom_voice
            return "Puck"

        # 若未选 Custom 但配置了 custom_voice 且 voice_name 为空时的兼容
        if not self.voice_name and self.custom_voice:
            return self.custom_voice

        return self.voice_name if self.voice_name else "Puck"

    async def _cleanup_old_temp_files(self):
        """清理 24 小时前创建的旧临时音频文件，防止磁盘堆积"""
        try:
            def _clean():
                temp_dir = self.get_temp_dir()
                now = time.time()
                if not os.path.exists(temp_dir):
                    return
                for fname in os.listdir(temp_dir):
                    if fname.startswith("gemini_tts_") or fname.startswith("gemini_long_tts_"):
                        fpath = os.path.join(temp_dir, fname)
                        try:
                            if os.path.isfile(fpath) and (now - os.path.getmtime(fpath) > 86400):
                                os.remove(fpath)
                        except Exception:
                            pass

            await asyncio.to_thread(_clean)
        except Exception as e:
            logger.debug(f"[Gemini 3.8 TTS] 临时文件清理防护: {e}")

    def get_fallback_keys(self) -> List[str]:
        """从环境变量或系统的 cmd_config.json 中获取备用 API 密钥"""
        env_key = os.environ.get("GEMINI_API_KEY")
        if env_key:
            logger.info("[Gemini 3.8 TTS] 成功获取环境变量 GEMINI_API_KEY")
            return [env_key]

        try:
            data_dir = StarTools.get_data_dir("astrbot_plugin_gemini_tts")
            config_path = data_dir.parent.parent / "cmd_config.json"
            if config_path.exists():
                with open(config_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    providers = data.get("provider_sources", [])
                    for p in providers:
                        if p.get("id") == "google_gemini" or p.get("provider") == "google":
                            keys = p.get("key")
                            if isinstance(keys, list):
                                valid_keys = [k for k in keys if k]
                                if valid_keys:
                                    logger.info(
                                        f"[Gemini 3.8 TTS] 从系统配置获取到 {len(valid_keys)} 个密钥"
                                    )
                                    return valid_keys
                            elif isinstance(keys, str) and keys:
                                logger.info("[Gemini 3.8 TTS] 从系统配置获取到 1 个密钥")
                                return [keys]
        except Exception as e:
            logger.warning(f"[Gemini 3.8 TTS] 尝试从 cmd_config.json 读取密钥失败: {e}")

        return []

    def get_api_key(self) -> str:
        """根据 Round-robin 轮询算法获取 API 密钥"""
        keys = self.api_keys if self.api_keys else self.fallback_keys
        if not keys:
            self.fallback_keys = self.get_fallback_keys()
            keys = self.api_keys if self.api_keys else self.fallback_keys
            if not keys:
                raise ValueError("没有配置 Gemini API Key！请在插件设置中填写，或配置系统 Gemini 密钥。")

        key = keys[self.key_index % len(keys)]
        self.key_index = (self.key_index + 1) % len(keys)
        return key

    def get_total_keys_count(self) -> int:
        """获取当前配置或获取到的 API Key 总数"""
        keys = self.api_keys if self.api_keys else self.fallback_keys
        return len(keys)

    def clean_text_for_tts(self, text: str) -> str:
        """净化文本，移除 Markdown 标记，保留 3.8 原生内联动作标签与正常标点"""
        if not text:
            return ""

        # 去除代码块包裹及内嵌
        text = re.sub(r"```[\s\S]*?```", "", text)
        text = re.sub(r"`([^`]+)`", r"\1", text)
        # 去除 Markdown 标题符号 (# 标题)
        text = re.sub(r"^\s*#+\s*", "", text, flags=re.MULTILINE)
        # 去除粗体和斜体 (**粗体** -> 粗体, *斜体* -> 斜体)
        text = re.sub(r"\*{1,3}([^\*]+)\*{1,3}", r"\1", text)
        text = re.sub(r"(?<!\w)_{1,3}([^_]+)_{1,3}(?!\w)", r"\1", text)
        # 去除行首列表符号与引用块 (> 引用, - 列表, * 列表)
        text = re.sub(r"^\s*[-*\+>]+\s+", "", text, flags=re.MULTILINE)
        # 过滤 HTTP 链接与残留 gemini_tts 标签
        text = re.sub(r"https?://\S+", "", text)
        text = re.sub(r"[<\[]\s*[/\\\\]?\s*gemini_tts.*?[>\]]", "", text, flags=re.IGNORECASE)

        # 规范化动作标注 (将旧式方括号或复数标注统一为官方规范标签: <sigh>, <whisper>, <pause>, <laugh>)
        def _normalize_cue(match):
            key = match.group(1).lower().strip()
            key = re.sub(r"\s+", " ", key)
            mapping = {
                "sigh": "sigh",
                "sighs": "sigh",
                "whisper": "whisper",
                "whispers": "whisper",
                "pause": "pause",
                "short pause": "pause",
                "laugh": "laugh",
                "laughs": "laugh",
                "chuckle": "laugh",
                "chuckles": "laugh",
            }
            canonical = mapping.get(key, key)
            return f"<{canonical}>"

        text = re.sub(
            r"[<\[](sighs?|whispers?|pause|short\s+pause|laughs?|chuckles?)[>\]]",
            _normalize_cue,
            text,
            flags=re.IGNORECASE,
        )

        # 连续换行转换为自然停顿符号
        text = re.sub(r"\n+", "，", text)

        return text.strip()

    def _get_safety_settings(self) -> List[types.SafetySetting]:
        """默认完全关闭审查等级 (BLOCK_NONE)"""
        return [
            types.SafetySetting(category="HARM_CATEGORY_HATE_SPEECH", threshold="BLOCK_NONE"),
            types.SafetySetting(category="HARM_CATEGORY_HARASSMENT", threshold="BLOCK_NONE"),
            types.SafetySetting(category="HARM_CATEGORY_SEXUALLY_EXPLICIT", threshold="BLOCK_NONE"),
            types.SafetySetting(category="HARM_CATEGORY_DANGEROUS_CONTENT", threshold="BLOCK_NONE"),
        ]

    def get_temp_dir(self) -> str:
        """获取共享临时存储目录"""
        try:
            data_dir = StarTools.get_data_dir("astrbot_plugin_gemini_tts")
            temp_dir = os.path.join(str(data_dir), "temp")
            os.makedirs(temp_dir, exist_ok=True)
            return temp_dir
        except Exception:
            return tempfile.gettempdir()

    @filter.on_llm_request()
    async def inject_tts_instruction(self, event: AstrMessageEvent, req: ProviderRequest):
        """注入系统提示词以引导 LLM 在需要时生成语音回复"""
        if not self.enable_system_prompt or not self.system_prompt_addition:
            return

        should_inject = self.config.get("always_inject_prompt", True)
        if not should_inject:
            kw_str = self.config.get(
                "trigger_keywords", "语音,说,唱,读,听,声音,tts,voice,speak,read,listen,audio"
            )
            keywords = [k.strip().lower() for k in re.split(r"[,，\n]", kw_str) if k.strip()]
            user_msg = event.get_message_str().strip().lower()
            for kw in keywords:
                try:
                    if re.search(kw, user_msg):
                        should_inject = True
                        break
                except re.error:
                    if kw in user_msg:
                        should_inject = True
                        break

        if should_inject:
            sys_prompt = self.system_prompt_addition
            custom_addition = self.config.get("system_prompt_addition", "").strip()
            if custom_addition and custom_addition != DEFAULT_SYSTEM_PROMPT_ADDITION.strip():
                sys_prompt = f"{DEFAULT_SYSTEM_PROMPT_ADDITION}\n\n【额外自定义要求】\n{custom_addition}"

            req.system_prompt = (req.system_prompt or "") + "\n\n" + sys_prompt

    @filter.on_decorating_result()
    async def process_text_and_tts(self, event: AstrMessageEvent):
        """拦截最终回复，提取 <gemini_tts> 标签并转换为语音发送"""
        result = event.get_result()
        if not result or not result.chain:
            return

        new_chain = []
        any_tts_called = False

        for comp in result.chain:
            if not isinstance(comp, Plain):
                new_chain.append(comp)
                continue

            text = comp.text
            if not text:
                continue

            matches = list(self.tts_tag_pattern.finditer(text))
            if not matches:
                new_chain.append(comp)
                continue

            last_idx = 0
            for match in matches:
                start, end = match.span()
                if start > last_idx:
                    prefix = text[last_idx:start]
                    if prefix.strip():
                        new_chain.append(Plain(prefix))

                attr_str = match.group(1) or ""
                inner_text = match.group(2) or ""
                voice_override = self.extract_voice_override(attr_str)
                audio_path = await self.generate_tts_audio(
                    event=event, text=inner_text, voice_override=voice_override
                )

                if audio_path:
                    any_tts_called = True
                    new_chain.append(Record.fromFileSystem(audio_path))
                else:
                    new_chain.append(Plain(f"\n（语音生成失败：{inner_text}）\n"))

                last_idx = end

            if last_idx < len(text):
                suffix = text[last_idx:]
                if suffix.strip():
                    new_chain.append(Plain(suffix))

        result.chain = new_chain
        if any_tts_called:
            event.set_extra("gemini_tts_called", True)

    async def fetch_tts_raw_audio(
        self, text: str, voice_override: Optional[str] = None
    ) -> Optional[Tuple[bytes, str]]:
        """调用 Gemini 3.8 TTS API 获取合成音频及 MIME 类型"""
        cleaned_text = self.clean_text_for_tts(text)
        if not cleaned_text:
            return None

        # 文本截断保护：单句朗读限制 4000 字符内
        if len(cleaned_text) > 4000:
            logger.warning("[Gemini 3.8 TTS] 待合成文本超过 4000 字符，自动截断")
            cleaned_text = cleaned_text[:4000]

        active_voice = self.get_active_voice(override_voice=voice_override)
        logger.info(
            f"[Gemini 3.8 TTS] 请求语音合成. 模型: {self.tts_model}, 音色: {active_voice}, 文本: '{cleaned_text[:30]}...'"
        )

        max_attempts = self.get_total_keys_count()
        if max_attempts == 0:
            logger.error("[Gemini 3.8 TTS] 没有配置可用的 API Key")
            return None

        last_exception = None
        for attempt in range(max_attempts):
            api_key = self.get_api_key()
            try:
                client = genai.Client(api_key=api_key)

                # 配置 Gemini 3.8 TTS 请求体
                speech_config = types.SpeechConfig(
                    voice_config=types.VoiceConfig(
                        prebuilt_voice_config=types.PrebuiltVoiceConfig(
                            voice_name=active_voice
                        )
                    )
                )

                generate_content_config = types.GenerateContentConfig(
                    temperature=self.temperature,
                    response_modalities=["AUDIO"],
                    speech_config=speech_config,
                    safety_settings=self._get_safety_settings(),
                )

                # 3.8 TTS 单次调用（设置 30 秒超时）
                async with asyncio.timeout(30.0):
                    response = await client.aio.models.generate_content(
                        model=self.tts_model,
                        contents=cleaned_text,
                        config=generate_content_config,
                    )

                audio_data = None
                mime_type = "audio/wav"

                if response.candidates:
                    for cand in response.candidates:
                        if cand.content and cand.content.parts:
                            for part in cand.content.parts:
                                if part.inline_data and part.inline_data.data:
                                    audio_data = part.inline_data.data
                                    if part.inline_data.mime_type:
                                        mime_type = part.inline_data.mime_type
                                    break
                        if audio_data:
                            break

                if not audio_data:
                    raise ValueError("Gemini 3.8 TTS 模型未返回任何音频数据。")

                return bytes(audio_data), mime_type

            except Exception as e:
                last_exception = e
                err_msg = str(e)

                # 400 参数错误直接退出，不盲目消耗其他 Key
                if "400" in err_msg or "INVALID_ARGUMENT" in err_msg:
                    logger.error(f"[Gemini 3.8 TTS] ❌ 请求参数错误 (400 Invalid Argument): {err_msg}")
                    return None

                if (
                    "429" in err_msg
                    or "RESOURCE_EXHAUSTED" in err_msg
                    or "Quota" in err_msg
                    or isinstance(e, TimeoutError)
                ):
                    logger.warning(
                        f"[Gemini 3.8 TTS] ⚠️ 当前 API Key 限流/超时，切换下一个重试... (剩余: {max_attempts - attempt - 1})"
                    )
                    continue
                else:
                    logger.error(
                        f"[Gemini 3.8 TTS] ❌ 音频生成异常，尝试下一个密钥... (错误: {err_msg})"
                    )
                    continue

        logger.error(f"[Gemini 3.8 TTS] 所有 API Key 均尝试失败。最后错误: {last_exception}")
        return None

    def convert_to_wav(self, audio_data: bytes, mime_type: str) -> bytes:
        """为裸 PCM 数据流封装 44 字节标准 WAV 头部"""
        parameters = self.parse_audio_mime_type(mime_type)
        bits_per_sample = parameters["bits_per_sample"]
        sample_rate = parameters["rate"]
        num_channels = 1
        data_size = len(audio_data)
        bytes_per_sample = bits_per_sample // 8
        block_align = num_channels * bytes_per_sample
        byte_rate = sample_rate * block_align
        chunk_size = 36 + data_size

        header = struct.pack(
            "<4sI4s4sIHHIIHH4sI",
            b"RIFF",
            chunk_size,
            b"WAVE",
            b"fmt ",
            16,
            1,
            num_channels,
            sample_rate,
            byte_rate,
            block_align,
            bits_per_sample,
            b"data",
            data_size,
        )
        return header + audio_data

    def parse_audio_mime_type(self, mime_type: str) -> dict:
        """从 mime 类型中解析采样率与位深"""
        bits_per_sample = 16
        rate = 24000
        if not mime_type:
            return {"bits_per_sample": bits_per_sample, "rate": rate}

        parts = mime_type.split(";")
        for param in parts:
            param = param.strip()
            if param.lower().startswith("rate="):
                try:
                    rate = int(param.split("=", 1)[1])
                except (ValueError, IndexError):
                    pass
            elif param.lower().startswith("audio/l"):
                try:
                    bits_per_sample = int(param.lower().split("l", 1)[1])
                except (ValueError, IndexError):
                    pass

        return {"bits_per_sample": bits_per_sample, "rate": rate}

    async def generate_tts_audio(
        self, event: AstrMessageEvent, text: str, voice_override: Optional[str] = None
    ) -> Optional[str]:
        """合成单段音频并落盘到临时文件"""
        res = await self.fetch_tts_raw_audio(text=text, voice_override=voice_override)
        if not res:
            return None

        audio_data, mime_type = res

        # 智能检测：若模型返回已有标准 RIFF WAV 格式，则直接写入；否则若为裸 PCM 则包装 WAV 头
        if audio_data.startswith(b"RIFF"):
            data_buffer = audio_data
        else:
            data_buffer = self.convert_to_wav(audio_data, mime_type)

        temp_dir = self.get_temp_dir()
        temp_file_name = f"gemini_tts_{uuid.uuid4().hex}.wav"
        temp_file_path = os.path.join(temp_dir, temp_file_name)

        def _write():
            with open(temp_file_path, "wb") as f:
                f.write(data_buffer)

        await asyncio.to_thread(_write)
        logger.info(f"[Gemini 3.8 TTS] 音频生成成功: {temp_file_path}")

        if hasattr(event, "track_temporary_local_file"):
            event.track_temporary_local_file(temp_file_path)

        return temp_file_path