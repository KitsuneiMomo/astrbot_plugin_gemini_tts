import os
import re
import json
import time
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


DEFAULT_SYSTEM_PROMPT_ADDITION = (
    "【语音能力】\n"
    "你可以把回复中的部分文字转为语音：将待朗读内容包在 <gemini_tts>...</gemini_tts> 中，标签外的文字照常以文字发送，整条回复也可以只有标签。不要用代码块包裹标签。\n\n"
    "【使用时机】仅当用户要求语音、朗读、唱歌、讲故事，或语境明显适合语音（如哄睡、语音问候、角色演绎）时才使用；其它情况一律纯文字回复。普通文字中禁止出现 [sighs]、[whispers] 等声音标记，它们只能写在标签内部。\n\n"
    "【属性（均可省略）】\n"
    'voice：发音人，仅当用户指定声音时使用，可选 Puck, Charon, Kore, Fenrir, Aoede, Zephyr（区分大小写）\n'
    'scene：语气/环境，如"悄悄耳语""非常激动"\n'
    "sample_context：上下文背景\n"
    'audio_profile：发音人的人设声线，如"傲娇美少女""专业男播音员"（中文描述更佳）\n\n'
    "【标签内规则】只放适合朗读的干净文字：不含 Markdown 符号、链接、换行。可用英文方括号标记控制演绎，如 [sighs] [whispers] [short pause] [excited]。\n\n"
    '示例：<gemini_tts scene="耳语">[whispers] 晚安，做个好梦。</gemini_tts>'
)


@register(
    "astrbot_plugin_gemini_tts",
    "KitsuneiMomo",
    "让AI可以调用Gemini TTS工具发送语音与拼接长音频文件",
    "1.2.1",
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
        self.system_prompt_addition = sys_addition if (sys_addition and sys_addition.strip()) else DEFAULT_SYSTEM_PROMPT_ADDITION

        # 长音频合成设置
        self.enable_long_tts = self.config.get("enable_long_tts", False)
        try:
            self.long_tts_silence_sec = float(self.config.get("long_tts_silence_sec", 0.4))
        except (ValueError, TypeError):
            self.long_tts_silence_sec = 0.4

        try:
            self.max_long_tts_segments = int(self.config.get("max_long_tts_segments", 15))
        except (ValueError, TypeError):
            self.max_long_tts_segments = 15

        try:
            self.max_concurrent_tasks = max(1, int(self.config.get("max_concurrent_tasks", 3)))
        except (ValueError, TypeError):
            self.max_concurrent_tasks = 3

        self.safety_threshold = self.config.get("safety_threshold", "BLOCK_ONLY_HIGH")

        # 用户设置的默认场景和背景
        self.default_scene = self.config.get("default_scene", "")
        self.default_sample_context = self.config.get("default_sample_context", "")
        self.audio_profile_mode = self.config.get("audio_profile_mode", "ai")
        self.default_audio_profile = self.config.get("default_audio_profile", "")

        # 并发控制信号量
        self.semaphore = asyncio.Semaphore(self.max_concurrent_tasks)

        # 缓存备用 API 密钥，避免重试时高频读取磁盘
        self.fallback_keys = []
        if not self.api_keys:
            self.fallback_keys = self.get_fallback_keys()

        # Rotation index
        self.key_index = 0

        # 正则表达式匹配语音指令标签
        self.tts_tag_pattern = re.compile(
            r'[<\[]gemini_tts(?:\s+([^>\]]*))?[>\]](.*?)(?:[<\[]\s*[/\\\\]\s*gemini_tts\s*[>\]]?|$)',
            re.DOTALL | re.IGNORECASE,
        )
        self.long_tts_tag_pattern = re.compile(
            r'[<\[]gemini_long_tts(?:\s+([^>\]]*))?[>\]](.*?)(?:[<\[]\s*[/\\\\]\s*gemini_long_tts\s*[>\]]?|$)',
            re.DOTALL | re.IGNORECASE,
        )

        # 异步启动旧临时文件清理
        asyncio.create_task(self._cleanup_old_temp_files())

        logger.info("[Gemini TTS] 插件 v1.2.1 初始化成功 (含长音频并发分段与安全限制调优)")

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
            logger.debug(f"[Gemini TTS] 临时文件清理防护: {e}")

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
                                    logger.info(
                                        f"[Gemini TTS] 从系统配置 google_gemini 中获取到 {len(valid_keys)} 个密钥"
                                    )
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
        """净化文本，移除 Markdown 结构标记与语法符号，保留正常标点、加减号、数学符号与日期"""
        if not text:
            return ""

        # 去除代码块包裹及内嵌
        text = re.sub(r"```[\s\S]*?```", "", text)
        text = re.sub(r"`([^`]+)`", r"\1", text)
        # 去除 Markdown 标题符号 (如 # 标题)
        text = re.sub(r"^\s*#+\s*", "", text, flags=re.MULTILINE)
        # 去除成对的星号或下划线强调 (**粗体** -> 粗体, *斜体* -> 斜体)
        text = re.sub(r"\*{1,3}([^\*]+)\*{1,3}", r"\1", text)
        text = re.sub(r"_{1,3}([^_]+)_{1,3}", r"\1", text)
        # 去除行首列表符号与引用块 (> 引用, - 列表, * 列表)
        text = re.sub(r"^\s*[-*\+>]+\s+", "", text, flags=re.MULTILINE)
        # 过滤 HTTP 链接
        text = re.sub(r"https?://\S+", "", text)
        # 连续换行转换为自然停顿符号
        text = re.sub(r"\n+", "，", text)

        return text.strip()

    def parse_tag_attributes(self, attr_str: str) -> dict:
        """从标签属性字符串中提取 voice, scene, sample_context, audio_profile"""
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
            "audio_profile": audio_profile,
        }

    async def create_file_component_async(self, file_path: str):
        """安全异步创建 File 消息组件，将文件转为 base64 传输解决跨 Docker 容器路径不匹配问题"""
        file_name = os.path.basename(file_path)

        if os.path.exists(file_path):
            try:
                def _read_b64():
                    with open(file_path, "rb") as f:
                        return base64.b64encode(f.read()).decode("utf-8")

                b64_str = await asyncio.to_thread(_read_b64)
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
                logger.debug(f"[Gemini TTS] 转换文件为 base64 提示: {e}")

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

        logger.warning("[Gemini TTS] 无法构造 File 组件，降级为 Record 语音组件发送")
        return Record.fromFileSystem(file_path)

    async def send_file_via_onebot(self, event: AstrMessageEvent, file_path: str) -> bool:
        """调用 OneBot (NapCat) 专属 API upload_group_file / upload_private_file 发送文件"""
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
                def _read_b64():
                    with open(file_path, "rb") as f:
                        return base64.b64encode(f.read()).decode("utf-8")

                b64_str = await asyncio.to_thread(_read_b64)
                b64_file = f"base64://{b64_str}"
            except Exception as e:
                logger.debug(f"[Gemini TTS] 读取音频转 base64 提示: {e}")

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
                if res.get("status") == "failed" or (
                    res.get("retcode") is not None and res.get("retcode") != 0
                ):
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
                    await _call_api(action_name, group_id=group_id, file=f_arg, name=file_name)
                    logger.info(f"[Gemini TTS] 🎉 群文件发送成功！(接口: {action_name})")
                    return True
                except Exception as e:
                    logger.debug(f"[Gemini TTS] 群文件接口 {action_name} 试错提示: {e}")

        # 2. 私聊上传
        elif user_id:
            logger.info(f"[Gemini TTS] 尝试为用户 {user_id} 发送私聊文件: {file_name}...")
            private_candidates = []
            if b64_file:
                private_candidates.append(("upload_private_file", b64_file))
            private_candidates.extend(
                [
                    ("upload_private_file", file_path),
                    ("upload_private_file", f"file://{file_path}"),
                    ("upload_private_file", rel_path),
                    ("upload_file_stream", file_path),
                ]
            )

            for action_name, f_arg in private_candidates:
                try:
                    await _call_api(action_name, user_id=user_id, file=f_arg, name=file_name)
                    logger.info(f"[Gemini TTS] 🎉 私聊文件发送成功！(接口: {action_name})")
                    return True
                except Exception as e:
                    logger.debug(f"[Gemini TTS] 私聊文件接口 {action_name} 试错提示: {e}")

        return False

    @filter.on_llm_request()
    async def inject_tts_instruction(self, event: AstrMessageEvent, req: ProviderRequest):
        """注入系统提示词以引导 LLM 知道可以使用特殊语法生成语音回复与长音频"""
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
            sys_prompt = DEFAULT_SYSTEM_PROMPT_ADDITION

            custom_addition = self.config.get("system_prompt_addition", "").strip()
            if custom_addition and custom_addition != DEFAULT_SYSTEM_PROMPT_ADDITION.strip():
                sys_prompt += f"\n\n【额外自定义要求】\n{custom_addition}"

            if self.enable_long_tts:
                sys_prompt += (
                    "\n\n【长音频/多角色】需要长文朗读、播客或多人对话时，按语义把内容分成若干段（每段约150~250字），每段用 <gemini_long_tts>...</gemini_long_tts> 包裹，属性与 <gemini_tts> 相同；各段可指定不同的 voice 和 audio_profile 扮演不同角色，插件会自动依次合成并拼接为一个音频文件发送。"
                )

            req.system_prompt = (req.system_prompt or "") + "\n\n" + sys_prompt

    def _process_short_tts_in_text(
        self, text: str, event: AstrMessageEvent
    ) -> Tuple[List[object], bool]:
        """解析文本切片中的短语音 <gemini_tts> 标签并构建消息组件列表"""
        chain_items = []
        called_success = False

        matches = list(self.tts_tag_pattern.finditer(text))
        if not matches:
            if text.strip():
                chain_items.append(Plain(text))
            return chain_items, False

        last_idx = 0
        for match in matches:
            start, end = match.span()
            if start > last_idx:
                prefix = text[last_idx:start]
                if prefix.strip():
                    chain_items.append(Plain(prefix))

            attr_str = match.group(1) or ""
            inner_text = match.group(2) or ""
            attrs = self.parse_tag_attributes(attr_str)

            # 同步调用异步生成器需在此处等待（在 process_text_and_tts 中统一 await）
            last_idx = end

        return matches, True

    @filter.on_decorating_result()
    async def process_text_and_tts(self, event: AstrMessageEvent):
        """拦截最终回复，精确切分并提取 <gemini_long_tts> 与 <gemini_tts> 标签内容（防止文本丢失）"""
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

            # 场景 1: 长音频开关被关闭，但 AI 仍然输出了 <gemini_long_tts>，做剥离恢复处理 (B9)
            if not self.enable_long_tts:
                text = re.sub(
                    r'[<\[]gemini_long_tts(?:\s+[^>\]]*))?[>\]](.*?)(?:[<\[]\s*[/\\\\]\s*gemini_long_tts\s*[>\]]?|$)',
                    r'\1',
                    text,
                    flags=re.DOTALL | re.IGNORECASE,
                )

            # 场景 2: 开启了长音频，精准切分长音频标签与中间非长音频文本 (B1)
            long_matches = list(self.long_tts_tag_pattern.finditer(text)) if self.enable_long_tts else []

            if long_matches:
                # 限制长音频最大段数防止无限爆额度
                if len(long_matches) > self.max_long_tts_segments:
                    logger.warning(
                        f"[Gemini Long TTS] 长音频段数 ({len(long_matches)}) 超过允许上限 ({self.max_long_tts_segments})，截断后续段落"
                    )
                    long_matches = long_matches[: self.max_long_tts_segments]

                last_idx = 0
                for match in long_matches:
                    start, end = match.span()

                    # 1.1 处理长音频标签前的普通文本或短语音标签 (B1)
                    if start > last_idx:
                        middle_text = text[last_idx:start]
                        await self._process_text_slice_for_short_tts(middle_text, event, new_chain)

                    # 1.2 处理长音频段落
                    attr_str = match.group(1) or ""
                    inner_text = match.group(2) or ""
                    attrs = self.parse_tag_attributes(attr_str)
                    attrs["text"] = inner_text

                    audio_path = await self.generate_long_tts_audio(event, [attrs])
                    if audio_path:
                        any_tts_called = True
                        sent = await self.send_file_via_onebot(event, audio_path)
                        if not sent:
                            file_comp = await self.create_file_component_async(audio_path)
                            new_chain.append(file_comp)
                    else:
                        new_chain.append(Plain("\n（长音频段落生成失败）\n"))

                    last_idx = end

                # 1.3 处理最后一个长音频标签后的剩余文本
                if last_idx < len(text):
                    trailing_text = text[last_idx:]
                    await self._process_text_slice_for_short_tts(trailing_text, event, new_chain)

            else:
                # 场景 3: 无长音频标签，仅处理标准短语音标签 <gemini_tts>
                short_called = await self._process_text_slice_for_short_tts(text, event, new_chain)
                if short_called:
                    any_tts_called = True

        result.chain = new_chain

        if any_tts_called:
            event.set_extra("gemini_tts_called", True)

    async def _process_text_slice_for_short_tts(
        self, text: str, event: AstrMessageEvent, chain: List[object]
    ) -> bool:
        """处理任意文本切片中的短语音 <gemini_tts> 标签，并把结果追加到 chain"""
        if not text:
            return False

        matches = list(self.tts_tag_pattern.finditer(text))
        if not matches:
            if text.strip():
                chain.append(Plain(text))
            return False

        any_success = False
        last_idx = 0

        for match in matches:
            start, end = match.span()
            if start > last_idx:
                prefix = text[last_idx:start]
                if prefix.strip():
                    chain.append(Plain(prefix))

            attr_str = match.group(1) or ""
            inner_text = match.group(2) or ""
            attrs = self.parse_tag_attributes(attr_str)

            audio_path = await self.generate_tts_audio(
                event=event,
                text=inner_text,
                voice_name=attrs["voice"],
                scene=attrs["scene"],
                sample_context=attrs["sample_context"],
                audio_profile=attrs["audio_profile"],
            )

            if audio_path:
                any_success = True
                chain.append(Record.fromFileSystem(audio_path))
            else:
                chain.append(Plain(f"\n（语音生成失败：{inner_text}）\n"))

            last_idx = end

        if last_idx < len(text):
            suffix = text[last_idx:]
            if suffix.strip():
                chain.append(Plain(suffix))

        return any_success

    def _get_safety_settings() -> List[types.SafetySetting]:
        """根据配置选择安全过滤等级 (BLOCK_NONE | BLOCK_ONLY_HIGH | BLOCK_MEDIUM_AND_ABOVE)"""
        level = getattr(self, "safety_threshold", "BLOCK_ONLY_HIGH")
        threshold_map = {
            "BLOCK_NONE": "BLOCK_NONE",
            "BLOCK_ONLY_HIGH": "BLOCK_ONLY_HIGH",
            "BLOCK_MEDIUM_AND_ABOVE": "BLOCK_MEDIUM_AND_ABOVE",
        }
        t_val = threshold_map.get(level, "BLOCK_ONLY_HIGH")

        return [
            types.SafetySetting(category="HARM_CATEGORY_HATE_SPEECH", threshold=t_val),
            types.SafetySetting(category="HARM_CATEGORY_HARASSMENT", threshold=t_val),
            types.SafetySetting(category="HARM_CATEGORY_SEXUALLY_EXPLICIT", threshold=t_val),
            types.SafetySetting(category="HARM_CATEGORY_DANGEROUS_CONTENT", threshold=t_val),
        ]

    async def fetch_tts_raw_audio(
        self,
        text: str,
        voice_name: Optional[str] = None,
        scene: Optional[str] = None,
        sample_context: Optional[str] = None,
        audio_profile: Optional[str] = None,
    ) -> Optional[Tuple[bytes, str]]:
        """调用 Gemini API 生成原始 PCM 音频数据及 mime_type（含信号量限流、超时保护与错误区分）"""
        cleaned_text = self.clean_text_for_tts(text)
        if not cleaned_text:
            return None

        selected_voice = voice_name if voice_name else self.voice_name
        if selected_voice and selected_voice.lower() == "custom":
            selected_voice = self.custom_voice.strip() if self.custom_voice else "Zephyr"

        final_scene = scene.strip() if (scene and scene.strip()) else self.default_scene.strip()
        final_context = (
            sample_context.strip()
            if (sample_context and sample_context.strip())
            else self.default_sample_context.strip()
        )

        final_audio_profile = None
        if self.audio_profile_mode == "ai":
            final_audio_profile = (
                audio_profile.strip()
                if (audio_profile and audio_profile.strip())
                else self.default_audio_profile.strip()
            )
        elif self.audio_profile_mode == "default":
            final_audio_profile = self.default_audio_profile.strip()

        # 文本长度超限保护 (B4): 限制文本长度 <= 6000 字节（预留 prompt 空间）
        text_bytes = cleaned_text.encode("utf-8")
        if len(text_bytes) > 6000:
            logger.warning(
                f"[Gemini TTS] 待合成文本长度 ({len(text_bytes)} 字节) 超过预设上限 (6000 字节)，自动截断"
            )
            cleaned_text = text_bytes[:6000].decode("utf-8", errors="ignore")

        logger.info(
            f"[Gemini TTS] 请求 API. 文本: '{cleaned_text[:30]}...' 发音人: {selected_voice} 场景: {final_scene} 背景: {final_context} 画像: {final_audio_profile}"
        )

        max_attempts = self.get_total_keys_count()
        if max_attempts == 0:
            logger.error("[Gemini TTS] 没有配置可用的 API Key")
            return None

        last_exception = None
        async with self.semaphore:
            for attempt in range(max_attempts):
                api_key = self.get_api_key()
                try:
                    client = genai.Client(api_key=api_key)

                    prompt_parts = []
                    if final_audio_profile:
                        prompt_parts.append("Read the following transcript based on the audio profile.")
                        prompt_parts.append(f"# Audio Profile\n{final_audio_profile}\n")

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
                        safety_settings=self._get_safety_settings(),
                    )

                    audio_data = bytearray()
                    mime_type = None

                    # 增加了 45 秒 API 调用超时限制 (B6)
                    async with asyncio.timeout(45.0):
                        response_stream = await client.aio.models.generate_content_stream(
                            model=self.tts_model,
                            contents=contents,
                            config=generate_content_config,
                        )

                        async for chunk in response_stream:
                            if not chunk.parts:  # 修复空 parts 导致的 IndexError (B3)
                                continue
                            part = chunk.parts[0]
                            if part.inline_data and part.inline_data.data:
                                audio_data.extend(part.inline_data.data)
                                if not mime_type and part.inline_data.mime_type:
                                    mime_type = part.inline_data.mime_type

                    if not audio_data:
                        raise ValueError("模型未返回任何音频数据。")

                    if not mime_type:
                        mime_type = "audio/L16;rate=24000"

                    return bytes(audio_data), mime_type

                except Exception as e:
                    last_exception = e
                    err_msg = str(e)

                    # 参数错误 / 不支持的发音人直接中断，不再盲目轮询 key (B5)
                    if "400" in err_msg or "INVALID_ARGUMENT" in err_msg:
                        logger.error(f"[Gemini TTS] ❌ 请求参数错误 (400 Invalid Argument)，直接中断: {err_msg}")
                        return None

                    if (
                        "429" in err_msg
                        or "RESOURCE_EXHAUSTED" in err_msg
                        or "Quota" in err_msg
                        or isinstance(e, TimeoutError)
                    ):
                        logger.warning(
                            f"[Gemini TTS] ⚠️ 当前 API Key 受限/超时，正在尝试下一个密钥... (剩余重试: {max_attempts - attempt - 1})"
                        )
                        continue
                    else:
                        logger.error(
                            f"[Gemini TTS] ❌ 音频生成异常，正在尝试下一个密钥... (错误: {err_msg})"
                        )
                        continue

        logger.error(
            f"[Gemini TTS] 所有配置的 API Key 均已尝试，仍未能成功生成音频。最后报错: {last_exception}",
            exc_info=True,
        )
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
        audio_profile: Optional[str] = None,
    ) -> Optional[str]:
        """核心单段语音合成业务逻辑"""
        res = await self.fetch_tts_raw_audio(
            text=text,
            voice_name=voice_name,
            scene=scene,
            sample_context=sample_context,
            audio_profile=audio_profile,
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

        def _write():
            with open(temp_file_path, "wb") as f:
                f.write(data_buffer)

        await asyncio.to_thread(_write)

        logger.info(f"[Gemini TTS] 音频文件生成成功: {temp_file_path}")

        if hasattr(event, "track_temporary_local_file"):
            event.track_temporary_local_file(temp_file_path)

        return temp_file_path

    async def generate_long_tts_audio(
        self, event: AstrMessageEvent, segments: List[dict]
    ) -> Optional[str]:
        """多段长音频并发合成并按顺序拼接为单个 WAV 音频文件（含信号量限制）"""
        if not segments:
            return None

        total_segments = min(len(segments), self.max_long_tts_segments)
        segments = segments[:total_segments]

        logger.info(
            f"[Gemini Long TTS] ⚡ 开始并发合成长音频，共 {total_segments} 段 (最大并发数: {self.max_concurrent_tasks})..."
        )

        tasks = [
            self.fetch_tts_raw_audio(
                text=seg.get("text", ""),
                voice_name=seg.get("voice"),
                scene=seg.get("scene"),
                sample_context=seg.get("sample_context"),
                audio_profile=seg.get("audio_profile"),
            )
            for seg in segments
        ]

        results = await asyncio.gather(*tasks, return_exceptions=True)

        combined_pcm = bytearray()
        final_mime_type = "audio/L16;rate=24000"

        silence_sec = max(0.0, self.long_tts_silence_sec)

        success_count = 0
        for i, res in enumerate(results, 1):
            if isinstance(res, Exception):
                logger.warning(f"[Gemini Long TTS] ⚠️ 第 {i}/{total_segments} 段并发合成抛出异常，跳过该段: {res}")
                continue
            if res:
                pcm_data, mime = res
                if mime:
                    final_mime_type = mime

                # 动态计算停顿静音字节数 (B7)
                params = self.parse_audio_mime_type(final_mime_type)
                sample_rate = params.get("rate", 24000)
                bytes_per_sample = params.get("bits_per_sample", 16) // 8
                silence_bytes = b"\x00" * int(sample_rate * bytes_per_sample * silence_sec)

                if combined_pcm and silence_bytes:
                    combined_pcm.extend(silence_bytes)
                combined_pcm.extend(pcm_data)
                success_count += 1
            else:
                logger.warning(f"[Gemini Long TTS] ⚠️ 第 {i}/{total_segments} 段未获取到音频数据，跳过该段。")

        if not combined_pcm or success_count == 0:
            logger.error("[Gemini Long TTS] 所有长音频段落均合成失败。")
            return None

        wav_buffer = self.convert_to_wav(bytes(combined_pcm), final_mime_type)
        temp_dir = self.get_temp_dir()
        temp_file_name = f"gemini_long_tts_{uuid.uuid4().hex}.wav"
        temp_file_path = os.path.join(temp_dir, temp_file_name)

        def _write():
            with open(temp_file_path, "wb") as f:
                f.write(wav_buffer)

        await asyncio.to_thread(_write)

        logger.info(
            f"[Gemini Long TTS] ✨ 长音频并发合成并拼接成功 (成功 {success_count}/{total_segments} 段): {temp_file_path}"
        )

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
        """从 mime 类型中解析出比特率和采样率（支持大小写无关匹配 B10）"""
        bits_per_sample = 16
        rate = 24000

        if not mime_type:
            return {"bits_per_sample": bits_per_sample, "rate": rate}

        parts = mime_type.split(";")
        for param in parts:
            param = param.strip()
            if param.lower().startswith("rate="):
                try:
                    rate_str = param.split("=", 1)[1]
                    rate = int(rate_str)
                except (ValueError, IndexError):
                    pass
            elif param.lower().startswith("audio/l"):
                try:
                    bits_per_sample = int(param.lower().split("l", 1)[1])
                except (ValueError, IndexError):
                    pass

        return {"bits_per_sample": bits_per_sample, "rate": rate}