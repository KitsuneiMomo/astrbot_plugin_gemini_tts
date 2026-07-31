import os
import re
import json
import struct
import base64
import asyncio
import mimetypes
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


@register(
    "astrbot_plugin_gemini_tts",
    "KitsuneiMomo",
    "让AI可以调用Gemini TTS工具发送语音与拼接长音频文件",
    "1.2.0",
    "https://github.com/KitsuneiMomo/astrbot_plugin_gemini_tts",
)
class GeminiTTSPlugin(Star):
    def __init__(self, context: Context, config: Optional[AstrBotConfig] = None):
        super().__init__(context)
        self.config = config if config is not None else {}
        
        self.api_keys = self.config.get("api_keys", [])
        self.tts_model = self.config.get("tts_model", "gemini-3.1-flash-tts-preview")
        self.voice_name = self.config.get("voice_name", "Zephyr")
        self.custom_voice = self.config.get("custom_voice", "")
        self.temperature = self.config.get("temperature", 1.0)
        self.enable_system_prompt = self.config.get("enable_system_prompt", True)
        self.always_inject_prompt = self.config.get("always_inject_prompt", True)
        self.trigger_keywords = self.config.get("trigger_keywords", "语音,说,唱,读,听,声音,tts,voice,speak,read,listen,audio")
        self.system_prompt_addition = self.config.get("system_prompt_addition", "")
        
        # 长音频合成设置
        self.enable_long_tts = self.config.get("enable_long_tts", False)
        self.long_tts_silence_sec = float(self.config.get("long_tts_silence_sec", 0.4))
        
        # 用户设置的默认场景和背景
        self.default_scene = self.config.get("default_scene", "")
        self.default_sample_context = self.config.get("default_sample_context", "")
        self.audio_profile_mode = self.config.get("audio_profile_mode", "ai")
        self.default_audio_profile = self.config.get("default_audio_profile", "")
        
        # 缓存备用 API 密钥，避免重试时高频读取磁盘
        self.fallback_keys = []
        if not self.api_keys:
            self.fallback_keys = self.get_fallback_keys()
        
        # Rotation index
        self.key_index = 0
        
        # 正则表达式匹配语音指令标签 (兼容 XML <gemini_tts>、BBCode [/gemini_tts] 闭合标签及缺失闭合标签的情况)
        self.tts_tag_pattern = re.compile(
            r'[<\[]gemini_tts(?:\s+([^>\]]*))?[>\]](.*?)(?:[<\[]\s*[/\\\\]\s*gemini_tts\s*[>\]]?|$)',
            re.DOTALL | re.IGNORECASE
        )
        self.long_tts_tag_pattern = re.compile(
            r'[<\[]gemini_long_tts(?:\s+([^>\]]*))?[>\]](.*?)(?:[<\[]\s*[/\\\\]\s*gemini_long_tts\s*[>\]]?|$)',
            re.DOTALL | re.IGNORECASE
        )
        
        logger.info("[Gemini TTS] 插件初始化成功 (含长音频分段拼接支持)")

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

    def parse_tag_attributes(self, attr_str: str) -> dict:
        """从标签属性字符串中提取 voice, scene, sample_context, audio_profile (支持带引号与不带引号的属性)"""
        voice, scene, sample_context, audio_profile = None, None, None, None
        if attr_str:
            def extract(key_pattern):
                m = re.search(key_pattern, attr_str, re.IGNORECASE)
                if m:
                    return m.group(1) or m.group(2)
                return None

            voice = extract(r'voice=(?:["\']([^"\']*)["\']|([^\s>\]]+))')
            scene = extract(r'scene=(?:["\']([^"\']*)["\']|([^\s>\]]+))')
            sample_context = extract(r'sample_context=(?:["\']([^"\']*)["\']|([^\s>\]]+))')
            audio_profile = extract(r'(?:audio_profile|profile)=(?:["\']([^"\']*)["\']|([^\s>\]]+))')

        return {
            "voice": voice,
            "scene": scene,
            "sample_context": sample_context,
            "audio_profile": audio_profile
        }

    def create_file_component(self, file_path: str):
        """安全创建 File 消息组件，将文件转为 base64 协议串传输，彻底解决跨 Docker 容器路径挂载不匹配导致的 ENOENT 报错"""
        file_name = os.path.basename(file_path)

        # 1. 转换为 base64:// 串，通过网络 Payload 直接传给 NapCat/OneBot，不依赖容器本地文件系统
        if os.path.exists(file_path):
            try:
                with open(file_path, "rb") as f:
                    b64_str = base64.b64encode(f.read()).decode("utf-8")
                base64_url = f"base64://{b64_str}"

                try:
                    return File(file_=base64_url, name=file_name)
                except Exception:
                    pass

                try:
                    return File(file=base64_url, name=file_name)
                except Exception:
                    pass
            except Exception as e:
                logger.warning(f"[Gemini TTS] 转换文件为 base64 失败: {e}")

        # 2. 备用逻辑：直接传递本地路径
        try:
            return File(file_=file_path, name=file_name)
        except Exception:
            pass

        try:
            return File(file=file_path, name=file_name)
        except Exception:
            pass

        if hasattr(File, "fromFileSystem"):
            try:
                return File.fromFileSystem(file_path)
            except Exception:
                pass

        logger.warning("[Gemini TTS] 无法正确构造 File 组件，降级为 Record 语音组件发送")
        return Record.fromFileSystem(file_path)

    async def send_file_via_onebot(self, event: AstrMessageEvent, file_path: str) -> bool:
        """调用 OneBot (NapCat) 专属扩展 API upload_group_file / upload_private_file 发送文件"""
        bot = getattr(event, "bot", None)
        if not bot:
            return False

        client = bot.api if hasattr(bot, "api") else bot
        if not client or not hasattr(client, "call_action"):
            return False

        file_name = os.path.basename(file_path)

        b64_file = None
        if os.path.exists(file_path):
            try:
                with open(file_path, "rb") as f:
                    b64_str = base64.b64encode(f.read()).decode("utf-8")
                b64_file = f"base64://{b64_str}"
            except Exception as e:
                logger.warning(f"[Gemini TTS] 读取音频转 base64 失败: {e}")

        rel_path = os.path.join("data", "plugin_data", "astrbot_plugin_gemini_tts", "temp", file_name)

        group_id = None
        user_id = None

        if hasattr(event, "get_group_id"):
            try:
                gid = event.get_group_id()
                if gid:
                    group_id = int(gid)
            except Exception:
                pass
        if not group_id and hasattr(event, "message_obj") and getattr(event.message_obj, "group_id", None):
            try:
                group_id = int(event.message_obj.group_id)
            except Exception:
                pass

        if hasattr(event, "get_sender_id"):
            try:
                uid = event.get_sender_id()
                if uid:
                    user_id = int(uid)
            except Exception:
                pass
        if not user_id and hasattr(event, "message_obj") and getattr(event.message_obj, "sender", None):
            try:
                user_id = int(event.message_obj.sender.user_id)
            except Exception:
                pass

        async def _call_api(action: str, **kwargs):
            res = await client.call_action(action, **kwargs)
            if isinstance(res, dict):
                if res.get("status") == "failed" or (res.get("retcode") is not None and res.get("retcode") != 0):
                    msg = res.get("wording") or res.get("message") or str(res)
                    raise ValueError(f"{action} 接口返回错误: {msg} (retcode={res.get('retcode')})")
            return res

        # 1. 群聊上传
        if group_id:
            logger.info(f"[Gemini TTS] 尝试为群 {group_id} 上传群文件: {file_name}...")
            group_candidates = [
                ("upload_group_file", file_path),
                ("upload_group_file", f"file://{file_path}"),
                ("upload_group_file", rel_path),
                ("upload_group_file", f"./{rel_path}"),
                ("upload_file_stream", file_path),
            ]
            if b64_file:
                group_candidates.append(("upload_group_file", b64_file))

            for action_name, f_arg in group_candidates:
                try:
                    logger.info(f"[Gemini TTS] 尝试群文件 API: {action_name}(group_id={group_id}, file={str(f_arg)[:40]}...)")
                    await _call_api(action_name, group_id=group_id, file=f_arg, name=file_name)
                    logger.info(f"[Gemini TTS] 🎉 群文件发送成功！(接口: {action_name})")
                    return True
                except Exception as e:
                    logger.warning(f"[Gemini TTS] 群文件接口 {action_name} ({str(f_arg)[:30]}...) 尝试失败: {e}")

        # 2. 私聊上传
        elif user_id:
            logger.info(f"[Gemini TTS] 尝试为用户 {user_id} 发送私聊文件: {file_name}...")
            private_candidates = []
            if b64_file:
                private_candidates.append(("upload_private_file", b64_file))
            private_candidates.extend([
                ("upload_private_file", file_path),
                ("upload_private_file", f"file://{file_path}"),
                ("upload_private_file", rel_path),
                ("upload_file_stream", file_path),
            ])

            for action_name, f_arg in private_candidates:
                try:
                    logger.info(f"[Gemini TTS] 尝试私聊文件 API: {action_name}(user_id={user_id}, file={str(f_arg)[:40]}...)")
                    await _call_api(action_name, user_id=user_id, file=f_arg, name=file_name)
                    logger.info(f"[Gemini TTS] 🎉 私聊文件发送成功！(接口: {action_name})")
                    return True
                except Exception as e:
                    logger.warning(f"[Gemini TTS] 私聊文件接口 {action_name} ({str(f_arg)[:30]}...) 尝试失败: {e}")

        return False

    @filter.on_llm_request()
    async def inject_tts_instruction(self, event: AstrMessageEvent, req: ProviderRequest):
        """注入系统提示词以引导 LLM 知道可以使用该特殊语法生成语音回复与长音频"""
        if not self.enable_system_prompt or not self.system_prompt_addition:
            return

        should_inject = self.config.get("always_inject_prompt", True)
        if not should_inject:
            kw_str = self.config.get("trigger_keywords", "语音,说,唱,读,听,声音,tts,voice,speak,read,listen,audio")
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
            if self.audio_profile_mode == "ai" and "audio_profile" not in sys_prompt:
                sys_prompt += "\n\n【音频画像补充说明】\n你可以在 `<gemini_tts>` 或 `<gemini_long_tts>` 标签中添加 `audio_profile` 属性来自行定义音频的角色画像（例如：`audio_profile=\"傲娇美少女\"` 或 `audio_profile=\"冷酷大叔\"` 等），以此让语音更加符合人设。"
            
            if self.enable_long_tts:
                sys_prompt += (
                    "\n\n【长音频/多角色播客/对白指南】\n"
                    "你拥有长音频分段生成与多角色拼接能力。当用户需要长文朗读、播客讨论、故事讲述或多人对话时，请按照约 45 秒（约 150~250 字，根据语义自然分句弹性调整）为一段，将内容分为若干段，每一段使用一个 `<gemini_long_tts>` 标签包裹：\n"
                    "<gemini_long_tts voice=\"发音人\" scene=\"语气/环境\" sample_context=\"上下文\" audio_profile=\"音频画像\">第1段45秒左右内容</gemini_long_tts>\n"
                    "<gemini_long_tts voice=\"发音人\" scene=\"语气/环境\" sample_context=\"上下文\" audio_profile=\"音频画像\">第2段45秒左右内容</gemini_long_tts>\n"
                    "\n说明：\n"
                    "1. 每段标签均可独立指定 `voice`（如 Zephyr, Puck, Charon, Kore, Fenrir, Aoede）和 `audio_profile`（如“主持人”、“嘉宾”、“傲娇女高”、“冷酷大叔”等），模拟不同角色发音！\n"
                    "2. 所有 `<gemini_long_tts>` 标签将在后台自动依次合成并拼接为同一个完整音频文件发送给用户。"
                )

            req.system_prompt = (req.system_prompt or "") + sys_prompt

    @filter.on_decorating_result()
    async def process_text_and_tts(self, event: AstrMessageEvent):
        """拦截最终回复，提取 <gemini_long_tts> 与 <gemini_tts> 标签内容并自动转为语音/音频文件组件"""
        result = event.get_result()
        if not result or not result.chain:
            return

        new_chain = []
        for comp in result.chain:
            if isinstance(comp, Plain):
                text = comp.text
                
                # 1. 优先处理长音频标签 <gemini_long_tts>
                if self.enable_long_tts:
                    long_matches = list(self.long_tts_tag_pattern.finditer(text))
                    if long_matches:
                        segments = []
                        for match in long_matches:
                            attr_str = match.group(1) or ""
                            inner_text = match.group(2) or ""
                            attrs = self.parse_tag_attributes(attr_str)
                            attrs["text"] = inner_text
                            segments.append(attrs)

                        first_start = long_matches[0].start()
                        if first_start > 0:
                            prefix_text = text[:first_start]
                            if prefix_text.strip():
                                new_chain.append(Plain(prefix_text))

                        audio_path = await self.generate_long_tts_audio(event, segments)
                        if audio_path:
                            sent = await self.send_file_via_onebot(event, audio_path)
                            if not sent:
                                new_chain.append(self.create_file_component(audio_path))
                        else:
                            new_chain.append(Plain("\n（长音频生成失败）\n"))

                        last_end = long_matches[-1].end()
                        text = text[last_end:]
                        event.set_extra("gemini_tts_called", True)

                if not text:
                    continue

                # 2. 处理标准短语音标签 <gemini_tts>
                matches = list(self.tts_tag_pattern.finditer(text))
                if not matches:
                    if text.strip():
                        new_chain.append(Plain(text))
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
                    attrs = self.parse_tag_attributes(attr_str)

                    audio_path = await self.generate_tts_audio(
                        event=event,
                        text=inner_text,
                        voice_name=attrs["voice"],
                        scene=attrs["scene"],
                        sample_context=attrs["sample_context"],
                        audio_profile=attrs["audio_profile"]
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

    async def fetch_tts_raw_audio(
        self,
        text: str,
        voice_name: Optional[str] = None,
        scene: Optional[str] = None,
        sample_context: Optional[str] = None,
        audio_profile: Optional[str] = None
    ) -> Optional[Tuple[bytes, str]]:
        """调用 Gemini API 生成原始 PCM 音频数据及 mime_type"""
        cleaned_text = self.clean_text_for_tts(text)
        if not cleaned_text:
            return None
            
        selected_voice = voice_name if voice_name else self.voice_name
        if selected_voice and selected_voice.lower() == "custom":
            selected_voice = self.custom_voice.strip() if self.custom_voice else "Zephyr"
            
        final_scene = scene.strip() if (scene and scene.strip()) else self.default_scene.strip()
        final_context = sample_context.strip() if (sample_context and sample_context.strip()) else self.default_sample_context.strip()
        
        final_audio_profile = None
        if self.audio_profile_mode == "ai":
            final_audio_profile = audio_profile.strip() if (audio_profile and audio_profile.strip()) else self.default_audio_profile.strip()
        elif self.audio_profile_mode == "default":
            final_audio_profile = self.default_audio_profile.strip()
        
        logger.info(f"[Gemini TTS] 正在请求 API. 文本: '{cleaned_text[:30]}...' 发音人: {selected_voice} 场景: {final_scene} 背景: {final_context} 画像: {final_audio_profile}")
        
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
                if final_audio_profile:
                    prompt_parts.append("Read the following transcript based on the audio profile.")
                    prompt_parts.append(f"# Audio Profile\n{final_audio_profile}\n")
                
                if final_scene:
                    if final_audio_profile:
                        prompt_parts.append(f"## Scene:\n{final_scene}\n")
                    else:
                        prompt_parts.append(f"## Scene\n{final_scene}\n")
                if final_context:
                    if final_audio_profile:
                        prompt_parts.append(f"## Sample Context:\n{final_context}\n")
                    else:
                        prompt_parts.append(f"## Sample Context\n{final_context}\n")
                
                if final_audio_profile:
                    prompt_parts.append(f"## Transcript:\n{cleaned_text}")
                else:
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
                
                response_stream = await client.aio.models.generate_content_stream(
                    model=self.tts_model,
                    contents=contents,
                    config=generate_content_config,
                )
                
                async for chunk in response_stream:
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

                return audio_data, mime_type
                
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

    def get_temp_dir(self) -> str:
        """获取共享临时存储目录（放在插件数据目录下，确保 Docker/NapCat 容器共享存储可访问）"""
        try:
            data_dir = StarTools.get_data_dir("astrbot_plugin_gemini_tts")
            temp_dir = os.path.join(str(data_dir), "temp")
            os.makedirs(temp_dir, exist_ok=True)
            return temp_dir
        except Exception:
            return tempfile.gettempdir()

    async def generate_tts_audio(
        self,
        event: AstrMessageEvent,
        text: str,
        voice_name: Optional[str] = None,
        scene: Optional[str] = None,
        sample_context: Optional[str] = None,
        audio_profile: Optional[str] = None
    ) -> Optional[str]:
        """核心单段语音合成业务逻辑"""
        res = await self.fetch_tts_raw_audio(
            text=text,
            voice_name=voice_name,
            scene=scene,
            sample_context=sample_context,
            audio_profile=audio_profile
        )
        if not res:
            return None

        audio_data, mime_type = res
        file_extension = mimetypes.guess_extension(mime_type)
        if file_extension is None:
            file_extension = ".wav"
            data_buffer = self.convert_to_wav(audio_data, mime_type)
        else:
            data_buffer = audio_data

        temp_dir = self.get_temp_dir()
        temp_file_name = f"gemini_tts_{uuid.uuid4().hex}{file_extension}"
        temp_file_path = os.path.join(temp_dir, temp_file_name)

        with open(temp_file_path, "wb") as f:
            f.write(data_buffer)

        logger.info(f"[Gemini TTS] 音频文件生成成功: {temp_file_path}")

        if hasattr(event, "track_temporary_local_file"):
            event.track_temporary_local_file(temp_file_path)

        return temp_file_path

    async def generate_long_tts_audio(
        self,
        event: AstrMessageEvent,
        segments: List[dict]
    ) -> Optional[str]:
        """多段长音频并发合成并按顺序拼接为单个 WAV 音频文件"""
        if not segments:
            return None

        total_segments = len(segments)
        logger.info(f"[Gemini Long TTS] ⚡ 开始并发合成长音频，共 {total_segments} 段...")

        # 构建所有段落的并发 TTS 请求任务
        tasks = [
            self.fetch_tts_raw_audio(
                text=seg.get("text", ""),
                voice_name=seg.get("voice"),
                scene=seg.get("scene"),
                sample_context=seg.get("sample_context"),
                audio_profile=seg.get("audio_profile")
            )
            for seg in segments
        ]

        # 并发并行执行所有请求，极大提升合成效率
        results = await asyncio.gather(*tasks, return_exceptions=True)

        combined_pcm = b""
        final_mime_type = "audio/L16;rate=24000"

        silence_sec = max(0.0, self.long_tts_silence_sec)
        # 16-bit mono PCM @ 24000Hz = 24000 samples/sec * 2 bytes/sample
        silence_bytes = b'\x00' * int(24000 * 2 * silence_sec)

        success_count = 0
        for i, res in enumerate(results, 1):
            if isinstance(res, Exception):
                logger.warning(f"[Gemini Long TTS] ⚠️ 第 {i}/{total_segments} 段并发合成抛出异常，跳过该段: {res}")
                continue
            if res:
                pcm_data, mime = res
                if mime:
                    final_mime_type = mime
                if combined_pcm and silence_bytes:
                    combined_pcm += silence_bytes
                combined_pcm += pcm_data
                success_count += 1
            else:
                logger.warning(f"[Gemini Long TTS] ⚠️ 第 {i}/{total_segments} 段未获取到音频数据，跳过该段。")

        if not combined_pcm or success_count == 0:
            logger.error("[Gemini Long TTS] 所有长音频段落均合成失败。")
            return None

        wav_buffer = self.convert_to_wav(combined_pcm, final_mime_type)
        temp_dir = self.get_temp_dir()
        temp_file_name = f"gemini_long_tts_{uuid.uuid4().hex}.wav"
        temp_file_path = os.path.join(temp_dir, temp_file_name)

        with open(temp_file_path, "wb") as f:
            f.write(wav_buffer)

        logger.info(f"[Gemini Long TTS] ✨ 长音频并发合成并拼接成功 (成功 {success_count}/{total_segments} 段): {temp_file_path}")

        if hasattr(event, "track_temporary_local_file"):
            event.track_temporary_local_file(temp_file_path)

        return temp_file_path

    def convert_to_wav(self, audio_data: bytes, mime_type: str) -> bytes:
        """为原始的 PCM 语音 data 加上 WAV 头部"""
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