import os
import re
import json
import struct
import mimetypes
import tempfile
import uuid
from typing import Optional, List
from google import genai
from google.genai import types

from astrbot.api.star import Star, Context, StarTools, register
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api import logger, AstrBotConfig
from astrbot.api.provider import ProviderRequest
from astrbot.api.message_components import Plain, Record


@register(
    "astrbot_plugin_gemini_tts",
    "KitsuneiMomo",
    "让AI可以调用Gemini TTS工具发送语音",
    "1.1.1",
    "https://github.com/KitsuneiMomo/astrbot_plugin_gemini_tts",
)
class GeminiTTSPlugin(Star):
    def __init__(self, context: Context, config: Optional[AstrBotConfig] = None):
        super().__init__(context)
        self.config = config if config is not None else {}
        
        self.api_keys = self.config.get("api_keys", [])
        self.tts_model = self.config.get("tts_model", "gemini-3.1-flash-tts-preview")
        self.voice_name = self.config.get("voice_name", "Zephyr")
        self.temperature = self.config.get("temperature", 1.0)
        self.enable_system_prompt = self.config.get("enable_system_prompt", True)
        self.system_prompt_addition = self.config.get("system_prompt_addition", "")
        
        # 用户设置的默认场景和背景
        self.default_scene = self.config.get("default_scene", "")
        self.default_sample_context = self.config.get("default_sample_context", "")
        
        # 缓存备用 API 密钥，避免重试时高频读取磁盘
        self.fallback_keys = []
        if not self.api_keys:
            self.fallback_keys = self.get_fallback_keys()
        
        # Rotation index
        self.key_index = 0
        
        # 正则表达式匹配语音指令标签
        self.tts_tag_pattern = re.compile(r'<gemini_tts(?:\s+([^>]*))?>(.*?)</gemini_tts>', re.DOTALL)
        
        logger.info("[Gemini TTS] 插件初始化成功 (提示词解析与安全解锁版)")

    def get_fallback_keys(self) -> List[str]:
        """从环境变量或系统的 cmd_config.json 中获取备用 API 密钥"""
        env_key = os.environ.get("GEMINI_API_KEY")
        if env_key:
            logger.info("[Gemini TTS] 成功获取环境变量 GEMINI_API_KEY")
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
                                    logger.info(f"[Gemini TTS] 从系统配置 google_gemini 中获取到 {len(valid_keys)} 个密钥")
                                    return valid_keys
                            elif isinstance(keys, str) and keys:
                                logger.info("[Gemini TTS] 从系统配置 google_gemini 中获取到 1 个密钥")
                                return [keys]
        except Exception as e:
            logger.warning(f"[Gemini TTS] 尝试从 cmd_config.json 读取密钥失败: {e}")
            
        return []

    def get_api_key(self) -> str:
        """根据 Round-robin 算法获取轮询密钥"""
        keys = self.api_keys if self.api_keys else self.fallback_keys
        if not keys:
            # 容错：如果缓存为空，尝试重新动态加载一次
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
        """净化文本，去除 Markdown 符号、HTML 标签、链接等，防止语音合成出怪声"""
        text = re.sub(r'[\*_`#\-\+>]', '', text)
        text = re.sub(r'https?://\S+', '', text)
        text = re.sub(r'\n+', '，', text)
        return text.strip()

    @filter.on_llm_request()
    async def inject_tts_instruction(self, event: AstrMessageEvent, req: ProviderRequest):
        """注入系统提示词以引导 LLM 知道可以使用该特殊语法生成语音回复"""
        if self.enable_system_prompt and self.system_prompt_addition:
            req.system_prompt = (req.system_prompt or "") + self.system_prompt_addition

    @filter.on_decorating_result()
    async def process_text_and_tts(self, event: AstrMessageEvent):
        """拦截最终回复，提取 <gemini_tts> 标签内容并自动转为语音组件"""
        result = event.get_result()
        if not result or not result.chain:
            return

        new_chain = []
        for comp in result.chain:
            if isinstance(comp, Plain):
                text = comp.text
                matches = list(self.tts_tag_pattern.finditer(text))
                
                if not matches:
                    new_chain.append(comp)
                    continue

                last_idx = 0
                for match in matches:
                    start, end = match.span()
                    
                    if start > last_idx:
                        prefix_text = text[last_idx:start]
                        if prefix_text.strip():
                            new_chain.append(Plain(prefix_text))

                    attr_str = match.group(1) or ""
                    inner_text = match.group(2) or ""

                    voice = None
                    scene = None
                    sample_context = None

                    if attr_str:
                        voice_match = re.search(r'voice=["\']([^"\']*)["\']', attr_str)
                        if voice_match:
                            voice = voice_match.group(1)
                        
                        scene_match = re.search(r'scene=["\']([^"\']*)["\']', attr_str)
                        if scene_match:
                            scene = scene_match.group(1)
                        
                        context_match = re.search(r'sample_context=["\']([^"\']*)["\']', attr_str)
                        if context_match:
                            sample_context = context_match.group(1)

                    audio_path = await self.generate_tts_audio(
                        event=event,
                        text=inner_text,
                        voice_name=voice,
                        scene=scene,
                        sample_context=sample_context
                    )

                    if audio_path:
                        new_chain.append(Record.fromFileSystem(audio_path))
                    else:
                        new_chain.append(Plain(f"\n（语音生成失败：{inner_text}）\n"))

                    last_idx = end
                    event.set_extra("gemini_tts_called", True)

                if last_idx < len(text):
                    suffix_text = text[last_idx:]
                    if suffix_text.strip():
                        new_chain.append(Plain(suffix_text))
            else:
                new_chain.append(comp)

        result.chain = new_chain

    async def generate_tts_audio(
        self,
        event: AstrMessageEvent,
        text: str,
        voice_name: Optional[str] = None,
        scene: Optional[str] = None,
        sample_context: Optional[str] = None
    ) -> Optional[str]:
        """核心语音合成业务逻辑"""
        cleaned_text = self.clean_text_for_tts(text)
        if not cleaned_text:
            return None
            
        selected_voice = voice_name if voice_name else self.voice_name
        final_scene = scene.strip() if (scene and scene.strip()) else self.default_scene.strip()
        final_context = sample_context.strip() if (sample_context and sample_context.strip()) else self.default_sample_context.strip()
        
        logger.info(f"[Gemini TTS] 正在提取标签进行合成. 文本: '{cleaned_text[:30]}...' 发音人: {selected_voice} 场景: {final_scene} 背景: {final_context}")
        
        max_attempts = self.get_total_keys_count()
        if max_attempts == 0:
            logger.error("[Gemini TTS] 没有配置可用的 API Key")
            return None
            
        last_exception = None
        for attempt in range(max_attempts):
            api_key = self.get_api_key()
            try:
                client = genai.Client(api_key=api_key)
                
                prompt_parts = []
                if final_scene:
                    prompt_parts.append(f"## Scene\n{final_scene}\n")
                if final_context:
                    prompt_parts.append(f"## Sample Context\n{final_context}\n")
                prompt_parts.append(f"## Transcript\n{cleaned_text}")
                
                prompt_text = "\n".join(prompt_parts)
                contents = [
                    types.Content(
                        role="user",
                        parts=[
                            types.Part.from_text(text=prompt_text),
                        ],
                    ),
                ]
                
                # 配置解锁安全限制，避免敏感词/拟声词导致的合成拦截
                generate_content_config = types.GenerateContentConfig(
                    temperature=self.temperature,
                    response_modalities=["audio"],
                    speech_config=types.SpeechConfig(
                        voice_config=types.VoiceConfig(
                            prebuilt_voice_config=types.PrebuiltVoiceConfig(
                                voice_name=selected_voice
                            )
                        )
                    ),
                    safety_settings=[
                        types.SafetySetting(
                            category="HARM_CATEGORY_HATE_SPEECH",
                            threshold="BLOCK_NONE",
                        ),
                        types.SafetySetting(
                            category="HARM_CATEGORY_HARASSMENT",
                            threshold="BLOCK_NONE",
                        ),
                        types.SafetySetting(
                            category="HARM_CATEGORY_SEXUALLY_EXPLICIT",
                            threshold="BLOCK_NONE",
                        ),
                        types.SafetySetting(
                            category="HARM_CATEGORY_DANGEROUS_CONTENT",
                            threshold="BLOCK_NONE",
                        ),
                    ]
                )
                
                audio_data = b""
                mime_type = None
                
                for chunk in client.models.generate_content_stream(
                    model=self.tts_model,
                    contents=contents,
                    config=generate_content_config,
                ):
                    if chunk.parts is None:
                        continue
                    part = chunk.parts[0]
                    if part.inline_data and part.inline_data.data:
                        audio_data += part.inline_data.data
                        if not mime_type and part.inline_data.mime_type:
                            mime_type = part.inline_data.mime_type
                
                if not audio_data:
                    raise ValueError("模型未返回任何音频数据。")
                    
                if not mime_type:
                    mime_type = "audio/L16;rate=24000"
                    
                file_extension = mimetypes.guess_extension(mime_type)
                if file_extension is None:
                    file_extension = ".wav"
                    data_buffer = self.convert_to_wav(audio_data, mime_type)
                else:
                    data_buffer = audio_data
                    
                temp_dir = tempfile.gettempdir()
                temp_file_name = f"gemini_tts_{uuid.uuid4().hex}{file_extension}"
                temp_file_path = os.path.join(temp_dir, temp_file_name)
                
                with open(temp_file_path, "wb") as f:
                    f.write(data_buffer)
                    
                logger.info(f"[Gemini TTS] 音频文件生成成功: {temp_file_path}")
                
                if hasattr(event, "track_temporary_local_file"):
                    event.track_temporary_local_file(temp_file_path)
                
                return temp_file_path
                
            except Exception as e:
                last_exception = e
                err_msg = str(e)
                if "429" in err_msg or "RESOURCE_EXHAUSTED" in err_msg or "Quota" in err_msg:
                    logger.warning(f"[Gemini TTS] ⚠️ 当前 API Key 受限 (429/配额不足)，正在尝试下一个密钥... (剩余重试: {max_attempts - attempt - 1})")
                    continue
                else:
                    logger.error(f"[Gemini TTS] ❌ 其它音频生成异常，正在尝试下一个密钥... (错误: {err_msg})")
                    continue
                    
        logger.error(f"[Gemini TTS] 所有配置的 API Key 均已尝试，仍未能成功生成音频。最后一次报错: {last_exception}", exc_info=True)
        return None

    def convert_to_wav(self, audio_data: bytes, mime_type: str) -> bytes:
        """为原始的 PCM 语音数据加上 WAV 头部"""
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
            b"RIFF",          # ChunkID
            chunk_size,       # ChunkSize
            b"WAVE",          # Format
            b"fmt ",          # Subchunk1ID
            16,               # Subchunk1Size
            1,                # AudioFormat
            num_channels,     # NumChannels
            sample_rate,      # SampleRate
            byte_rate,        # ByteRate
            block_align,      # BlockAlign
            bits_per_sample,  # BitsPerSample
            b"data",          # Subchunk2ID
            data_size         # Subchunk2Size
        )
        return header + audio_data

    def parse_audio_mime_type(self, mime_type: str) -> dict:
        """从 mime 类型中解析出比特率和采样率"""
        bits_per_sample = 16
        rate = 24000

        parts = mime_type.split(";")
        for param in parts:
            param = param.strip()
            if param.lower().startswith("rate="):
                try:
                    rate_str = param.split("=", 1)[1]
                    rate = int(rate_str)
                except (ValueError, IndexError):
                    pass
            elif param.startswith("audio/L"):
                try:
                    bits_per_sample = int(param.split("L", 1)[1])
                except (ValueError, IndexError):
                    pass

        return {"bits_per_sample": bits_per_sample, "rate": rate}