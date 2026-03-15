import inspect
import json
import re
from collections.abc import AsyncIterator, Coroutine
from typing import Any

from pydantic import BaseModel

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star
from astrbot.core.message.components import Plain, Reply
from astrbot.core.star import StarMetadata, command_management
from astrbot.core.star.filter.command import CommandFilter


# 自定义异常
class PluginBaseException(Exception):
    pass


class NoProviderException(PluginBaseException):
    pass


class CommandInfo(BaseModel):
    handler_full_name: str
    handler_name: str
    plugin: str
    plugin_display_name: str | None
    module_path: str
    description: str
    type: str
    parent_signature: str
    parent_group_handler: str
    original_command: str
    current_fragment: str
    effective_command: str
    aliases: list[str]
    permission: str
    enabled: bool
    is_group: bool
    has_conflict: bool
    reserved: bool
    sub_commands: list["CommandInfo"]


class CommandBrief(BaseModel):
    full_description: str
    command_name: str
    aliases: list[str]
    args: dict[str, Any]
    id: int


class LLMResponse(BaseModel):
    matched: bool
    id: int | None = None
    parameters: dict[str, Any] | None = None
    confidence: float | None = None
    reason: str | None = None


class CommandParser:
    def __init__(self, context: Context):
        self.context = context
        self.id_dict: dict[int, str] = {}
        self.commands: dict[int, CommandInfo] = {}
        self.brief_map: dict[int, CommandBrief] = {}
        self.plugin_meta: dict[str, StarMetadata] = {}
        self.plugin_desc: dict[str, str] = {}
        self.plugin_contain: dict[str, list[int]] = {}

        self.max_id = 1

    async def initialize(self):
        await self.clear()
        cmd_list = await command_management.list_commands()
        descriptors = command_management._collect_descriptors(include_sub_commands=True)
        params = {
            desc.filter_ref.command_name: desc.filter_ref.handler_params
            for desc in descriptors
            if isinstance(desc.filter_ref, CommandFilter)
        }
        for cmd in cmd_list:
            info = CommandInfo(**cmd)
            self._build_dicts(info, params)

    async def clear(self):
        self.max_id = 1
        self.id_dict = {}
        self.commands = {}
        self.brief_map = {}
        self.plugin_meta = {}
        self.plugin_desc = {}

    def _build_dicts(
        self,
        info: CommandInfo,
        params: dict[str, Any],
    ):
        meta = self.context.get_registered_star(info.plugin)
        self.plugin_meta[info.plugin] = meta
        self.plugin_desc[info.plugin] = meta.desc

        if not info.enabled:
            return

        start = self.max_id
        if info.is_group:
            main_desc = info.description
            for sub_command in info.sub_commands:
                args = params.get(sub_command.current_fragment)
                arg = {
                    name: args[name].__name__
                    for name in args
                    if isinstance(args[name], type)
                }
                self.id_dict[self.max_id] = (
                    f"{sub_command.plugin}:{sub_command.original_command}"
                )
                self.commands[self.max_id] = sub_command
                self.brief_map[self.max_id] = CommandBrief(
                    full_description=f"插件描述：{meta.desc}\n"
                    f"指令组描述：{main_desc}\n"
                    f"本指令描述：{sub_command.description}",
                    command_name=sub_command.current_fragment,
                    aliases=sub_command.aliases,
                    args=arg,
                    id=self.max_id,
                )
                self.max_id += 1
        else:
            args = params.get(info.current_fragment)
            arg = {
                name: args[name].__name__
                for name in args
                if isinstance(args[name], type)
            }

            self.id_dict[self.max_id] = f"{info.plugin}:{info.original_command}"
            self.commands[self.max_id] = info
            self.brief_map[self.max_id] = CommandBrief(
                full_description=f"插件描述：{meta.desc}\n指令描述：{info.description}",
                command_name=info.current_fragment,
                aliases=info.aliases,
                args=arg,
                id=self.max_id,
            )
            self.max_id += 1
        end = self.max_id
        plu = self.plugin_contain.get(info.plugin, [])
        if plu:
            plu.extend(list(range(start, end)))
        else:
            self.plugin_contain[info.plugin] = list(range(start, end))


class LLM:
    def __init__(
        self,
        context: Context,
        config: AstrBotConfig,
        brief_map: dict[int, CommandBrief],
        plugin_desc: dict[str, str],
    ):
        self.context = context
        self.config = config
        self.brief_map = brief_map
        self.plugin_desc = plugin_desc
        self.response_pattern = re.compile(r"\{.*}", re.S)

    def build_prompt(self, user_message: str, **kwargs) -> str:
        plugins = kwargs.get("plugins", [])
        if not plugins:
            return f"""
                    你是一个智能指令解析器。请分析用户的消息，根据以下备选插件列表的插件描述，判断用户是否想要调用插件中的功能。
                    要求：最多选择3个。如果不符合任何插件的描述，返回空列表。
                    
                    备选插件列表（键为插件名，值为插件描述）：
                    {self.plugin_desc}

                    用户消息: "{user_message}"

                    请按以下格式返回：
                       {{
                         "plugins": [key1, key2, ...] // 插件名，list[str]
                       }}

                    请确保只返回JSON格式的字符串结果，不要有任何其他的文本信息。
                    """
        else:
            return f"""
                    你是一个智能指令解析器。请分析用户的消息，判断用户意图是否匹配以下指令的效果。
                    要求：
                    1. 只匹配一个指令
                    2. 无论是否有参数都返回字典类型（键值对）
                    3. 返回的参数符合指令要求的参数个数、类型
                    4. 如果一个参数类型是GreedyStr，说明接受一个以空格作为分隔符的含有多个内容的字符串

                    可用指令列表（args字典键是参数名，值是参数类型）：
                    {plugins}

                    用户消息: "{user_message}"

                    请按以下格式分析：
                    1. 如果匹配某个指令，请返回：
                       {{
                         "matched": true,
                         "id": xxx, // 匹配指令的唯一id, int
                         "parameters": {{'arg1': v1, 'arg2': v2, ...}}, // 匹配指令的参数, dict[str, Any]
                         "confidence": number  // 匹配置信度, float, 取值0 ~ 1
                       }}

                    2. 如果不匹配任何指令，请返回：
                       {{
                         "matched": false,
                         "reason": "xxx" // 不匹配的原因简要说明
                       }}

                    请确保参数提取准确，只返回JSON格式的字符串结果，不要有任何其他的文本信息。
                    """

    async def get_provider(self, umo: str):
        provider_id = self.config.get("text_provider_id", "")
        if not provider_id:
            try:
                provider_id = await self.context.get_current_chat_provider_id(umo=umo)
            except Exception:
                raise NoProviderException("LLM供应商获取错误！")
        return provider_id

    async def submit(self, event: AstrMessageEvent, **kwargs):
        llm_resp = await self.context.llm_generate(
            chat_provider_id=await self.get_provider(event.unified_msg_origin),
            prompt=self.build_prompt(event.message_str, **kwargs),
        )
        text_resp = llm_resp.completion_text
        return json.loads(self.response_pattern.findall(text_resp)[0])


class CommandRouterPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.context = context
        self.config = config
        self.wake_prefix = []

        self.parser = CommandParser(context)
        self.llm: LLM | None = None

    async def initialize(self):
        """可选择实现异步的插件初始化方法，当实例化该插件类之后会自动调用该方法。"""
        await self.parser.initialize()
        self.llm = LLM(
            self.context, self.config, self.parser.brief_map, self.parser.plugin_desc
        )

    async def terminate(self):
        """可选择实现异步的插件销毁方法，当插件被卸载/停用时会调用。"""
        pass

    def get_wake_prefix(self):
        cfg = self.context.get_config()
        self.wake_prefix = cfg.get("wake_prefix")

    def match_filter(self, event: AstrMessageEvent):
        if (
            event._has_send_oper  # 已经有指令向用户发送过消息，触发指令的识别标志
            or not self.config.get("enable_global_match", True)  # 未开启全局模式
            or not event.message_str  # 发送空文本
        ):
            return False

        # 唤醒触发
        if self.config.get("activate_by_wake", True):
            if not event.is_at_or_wake_command:
                return False

        return True

    def permission_filter(self, event: AstrMessageEvent, require: str):
        if require == "admin" and not event.is_admin():
            return False

        return True

    def describe_resp(self, resp: LLMResponse):
        return f"{self.parser.id_dict[resp.id].split(':', 1)[1]}，参数：{' '.join(resp.parameters.values()) or '无'}"

    def reply(self, event: AstrMessageEvent, message: str):
        return event.chain_result(
            [Reply(id=event.message_obj.message_id), Plain(message)]
        )

    async def core_handler(self, event: AstrMessageEvent, **kwargs):
        plugins: list[str] = (await self.llm.submit(event))["plugins"]
        validated = []
        for i in plugins:
            if i in self.parser.plugin_desc:
                for j in self.parser.plugin_contain[i]:
                    validated.append(self.parser.brief_map[j].model_dump())
        if not validated:
            return
        brief_body = LLMResponse.model_validate(
            await self.llm.submit(event, plugins=validated)
        )
        if brief_body.matched:
            whole_body = self.parser.commands[brief_body.id]
            meta = self.parser.plugin_meta[whole_body.plugin]

            plugin_class = meta.star_cls
            function_name = whole_body.handler_name
            args = brief_body.parameters
            is_sync = (
                False if kwargs.get("try_again", True) and not meta.activated else True
            )
            if hasattr(plugin_class, function_name) and is_sync:
                if not meta.activated:
                    return

                actual_command = getattr(plugin_class, function_name)
                sig = inspect.signature(actual_command)
                try:
                    param = sig.bind(event, **args)
                    if not self.permission_filter(event, whole_body.permission):
                        yield self.reply(
                            event,
                            f"成功匹配指令：{self.describe_resp(brief_body)}，但你无权调用。",
                        )
                        return
                    if self.config.get("matched_tips", False):
                        yield self.reply(
                            event, f"成功匹配指令：{self.describe_resp(brief_body)}。"
                        )

                    result = actual_command(**param.arguments)
                    if isinstance(result, Coroutine):
                        yield await result
                    elif isinstance(result, AsyncIterator):
                        async for res in result:
                            yield res
                except TypeError:
                    logger.error(f"指令匹配成功，参数不匹配...{args}")
                    return

            else:
                if kwargs.get("try_again", True):
                    logger.info("指令状态异常，正在尝试同步...")
                    await self.initialize()
                    async for res in self.core_handler(event, try_again=False):
                        yield res
                else:
                    raise Exception("自动同步无效！")

    async def handle_meta_change(self):
        old = set(self.parser.plugin_meta)
        await self.initialize()
        new = set(self.parser.plugin_meta)
        if old == new:
            return "同步成功！\n未发现增删插件。"
        else:
            return f"同步成功！\n新增插件：{'、'.join(new - old) or '无'}\n删除插件：{'、'.join(old - new) or '无'}"

    @filter.on_astrbot_loaded()
    async def lazy_initialize(self):
        """延迟初始化"""
        await self.initialize()

    @filter.event_message_type(filter.EventMessageType.ALL, priority=-100)
    async def global_parser(self, event: AstrMessageEvent):
        """全局响应模式"""
        if not self.match_filter(event):
            return

        try:
            sent = False
            async for res in self.core_handler(event):
                sent = True
                yield res

            # Only mark the event as handled (to block further handlers) if this plugin
            # actually sent a response. If `always_llm` is enabled we keep allowing
            # other handlers to run (don't mark as handled).
            event._has_send_oper = sent and not self.config.get("always_llm", False)
        except PluginBaseException as e1:
            # We do send a reply in this error case, so mark the event handled
            # (unless always_llm is set).
            yield self.reply(event, str(e1))
            event._has_send_oper = True and not self.config.get("always_llm", False)
            logger.error(e1, exc_info=True)
        except Exception as e2:
            logger.error(e2, exc_info=True)

    @filter.command("解析", alias={"parse"})
    async def command_parser(self, event: AstrMessageEvent):
        """指令响应模式"""
        try:
            sent = False
            async for res in self.core_handler(event):
                sent = True
                yield res

            # Same behavior as global_parser — only mark handled when something was sent.
            event._has_send_oper = sent and not self.config.get("always_llm", False)
        except PluginBaseException as e1:
            # We do send a reply in this error case, so mark the event handled
            # (unless always_llm is set).
            yield self.reply(event, str(e1))
            event._has_send_oper = True and not self.config.get("always_llm", False)
            logger.error(e1, exc_info=True)
        except Exception as e2:
            logger.error(e2, exc_info=True)

    @filter.command("同步", alias={"更新"})
    async def data_sync(self, event: AstrMessageEvent):
        """同步因新增插件导致的缓存列表不一致的情况，也可用作意外导致的不同步情况"""
        yield event.plain_result(await self.handle_meta_change())
