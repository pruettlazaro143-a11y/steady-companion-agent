"""Terminal UI. Local commands never go to the model."""
from __future__ import annotations

import argparse
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
import getpass
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
from tempfile import TemporaryDirectory

from . import __version__
from .engine import Conversation, visible_text, SKILL_DIR
from .provider import Client, Config, ProviderError
from .store import Store
from .terminal import WaitingStatus
from .architecture import Architecture
from .natural_memory import MODES
from .memory_language import PARSER_VERSION

HELP = """直接输入即可聊天。/learn 会额外调用一次模型，其余管理指令在本地执行：
  /memory-mode session|manual|auto  持久配置：仅会话 / 手动（默认）/ 普通自动记忆
  /memory-assist on-confirm-cost|off  可选语义候选；启用会增加调用
  /context-summary on-confirm-cost|off  可选有界摘要选择；启用会增加调用
  /auto-memory                查看自动记忆（A 编号，含来源、范围和时间）
  /auto-correct A编号 TEXT     更正一条自动记忆并移除相关旧上下文
  /auto-forget A编号|all       删除自动记忆及相关旧上下文
  /memory                     查看已保存的记忆
  /remember KIND TEXT         保存你指定的信息
                              KIND: fact | preference | boundary | goal
  /correct ID TEXT            替换一条记忆，清空本轮上下文
  /forget ID                  删除一条记忆，清空本轮上下文
  /new                        开始新对话，保留明确保存的记忆
  /mode casual|support        普通聊天 / 预加载支持知识
  /emoji off|light            关闭表情 / 允许少量（仅本次会话）
  /status                     查看实际版本、模式和规则指纹
  /learn                      按需整理近期交流，建议经确认后才保存
  /notes                      查看相处记录和待确认建议
  /keep L编号                 保存一条待确认建议；/drop L编号 放弃
  /note KIND TEXT             直接记下 moment | thread | style
  /revise N编号 TEXT          更正相处记录，清空旧上下文
  /unlearn N编号              删除相处记录，清空旧上下文
  /remind TIME TEXT           创建本地提醒，TIME 必须带时区
                              如 2026-10-01T20:00:00+08:00
  /reminders                  查看提醒；/cancel ID 撤销
  /check                      检查当前到期提醒
  /usage                      查看本次请求数与已报告 token
  /budget N                   修改本次请求上限，保留当前聊天（不会立即调用）
  /wipe                       清空本地记忆和提醒（需输入确认）
  /help                       显示命令；/quit 退出
提醒仅在输入轮次之间或 /check 时显示。不会向外发送消息。"""


def configured_client() -> Client:
    config = Config.from_env()
    if not config.api_key:
        if not sys.stdin.isatty():
            raise ProviderError("Set NEBIUS_API_KEY in your environment, or run interactively to enter it privately.")
        key = getpass.getpass("输入 Nebius API Key（输入不显示，仅用于本次运行）: ").strip()
        if not key:
            raise ProviderError("API Key is empty. Run again and enter your Nebius key locally.")
        config = replace(config, api_key=key)
    return Client(config)


def default_data_dir() -> Path:
    override = os.environ.get("STEADY_DATA_DIR")
    return Path(override).expanduser() if override else Path.home() / ".steady-companion"


def runtime_status(config=None, conversation=None) -> dict:
    core = (SKILL_DIR / "SKILL.md").read_bytes()
    version_line = next((line for line in core.decode("utf-8").splitlines() if line.startswith("Version:")), "Version: unknown")
    result = {"agent_version": __version__, "skill_version": version_line.split("·")[0].strip(),
              "package_path": str(Path(__file__).parent.resolve()),
              "skill_sha256": hashlib.sha256(core).hexdigest()[:16],
              "response_mode": conversation.response_mode if conversation else "checked",
              "emoji_mode": conversation.emoji_mode if conversation else "off"}
    result.update(conversation.architecture.status() if conversation else Architecture().status())
    result["memory_mode"] = conversation.natural_memory.mode if conversation else "manual"
    result["memory_parser_version"] = PARSER_VERSION
    result["memory_schema_version"] = 2
    result['semantic_validator_version']='p12-grounded-1'
    from .pipeline import SELECTION_PROTOCOL
    result['selection_protocol']=SELECTION_PROTOCOL
    result['memory_selection_protocol']='p124-unit-ids-1'
    result['auxiliary_limits']={'memory':4,'summary':1,'timeout_seconds':20,'max_output_tokens':1200}
    if conversation is not None:
        result['optional_features']={name:conversation.natural_memory.feature(name) for name in ('memory_assist','context_summary')}
    if config is not None:
        result["structured_output"] = {"mode": getattr(config,"response_format","off"),
            "stages": ["generation", "review", "repair_generation", "repair_review", "memory"],
            "endpoint_compatibility": "unverified", "format_recovery_limit": 1,
            "shared_with_content_repair": True, "response_request_limit": 4}
        result.update({"model": config.model, "base_url": config.base_url,
                       "key_configured": bool(config.api_key),"parameters":{"max_tokens":config.max_tokens,"timeout":config.timeout,"store":False}})
    if conversation is not None and conversation.response_mode == "companion":
        result["structured_output"]={"mode":"text","stages":["generation","repair_generation"],"response_request_limit":2,"format_recovery_limit":1,"semantic_review":False,"auxiliary_schema_mode":getattr(config,"response_format","off")}
        result["runtime_protocol"]="companion-runtime-"+conversation.companion_runtime_version
    if conversation is not None and hasattr(conversation.client, 'plan'):
        result['model_access'] = conversation.client.plan.status()
        result['parameters'] = dict(result['model_access']['parameters'])
        result['model_access']['credential_loaded'] = bool(config and config.api_key)
        if conversation.response_mode == 'companion':
            result['structured_output'].update(response_request_limit=1, format_recovery_limit=0,
                stages=['generation'])
    if conversation is not None:
        result.update({"request_limit": conversation.max_calls,
                       "requests_used": conversation.usage.calls})
    if conversation is not None and hasattr(conversation,'interaction'):
        result['interaction_control']={'state':conversation.interaction.snapshot(),'check_mode':conversation.interaction_check,'semantic_capability':'unverified'}
    return result


def inspect_runtime(args) -> int:
    # Config validates URL components; no request and no key value is displayed.
    from .model_access import resolve
    profile = getattr(args, 'profile', None)
    connection_file = getattr(args, 'connection', None)
    if connection_file is not None and profile is None:
        raise ValueError('--connection requires an explicit --profile.')
    if profile:
        plan = resolve(profile, connection_file)
        status = runtime_status(plan.connection)
        status.update(model_access=plan.status(), response_mode=plan.policy.response_mode,
                      parameters=plan.status()['parameters'])
        if plan.policy.response_mode == 'companion':
            status.update(runtime_protocol='companion-runtime-'+plan.policy.companion_runtime_version,
                structured_output={'mode':'text','response_request_limit':1,'format_recovery_limit':0,
                                   'semantic_review':False,'auxiliary_schema_mode':plan.connection.response_format if plan.connection else 'off'})
    else:
        status = runtime_status(Config.from_env())
    db = args.data_dir.expanduser() / 'companion.sqlite3'
    if db.is_symlink():
        raise ValueError('数据库路径不能是符号链接。')
    status['database_exists'] = db.is_file()
    if db.is_file():
        status['memory_schema_version'] = 1
        with sqlite3.connect(db.resolve().as_uri()+'?mode=ro', uri=True) as connection:
            if connection.execute("SELECT 1 FROM sqlite_master WHERE name='p1_settings'").fetchone():
                schema = connection.execute("SELECT value FROM p1_settings WHERE key='memory_schema'").fetchone()
                status['memory_schema_version'] = int(schema[0]) if schema else 1
                row = connection.execute("SELECT value FROM p1_settings WHERE key='memory_mode'").fetchone()
                status['memory_mode'] = row[0] if row else 'manual'
                if status['memory_mode'] not in MODES:
                    raise ValueError('记忆配置无效。')
    print(json.dumps(status, ensure_ascii=False, indent=2))
    return 0


def show_due(store: Store) -> None:
    for pending in store.due_reminders():
        item = store.deliver_reminder(pending["id"])
        if item:
            print(f"\n[本地提醒 #{item['id']}] {visible_text(item['text'])}")


def execute_command(line: str, conversation: Conversation, store: Store) -> bool:
    """Return false only for exit. All durable mutation is user-commanded."""
    command, _, argument = line.partition(" ")
    argument = argument.strip()
    if conversation.natural_memory.mode == 'session' and command in ('/remember','/note','/keep','/learn'):
        raise ValueError('当前为仅会话模式；如需持久保存，先 /memory-mode manual。')
    if command in ("/quit", "/exit"):
        return False
    if command == "/help":
        print(HELP)
    elif command in ('/memory-assist','/context-summary'):
        name=command[1:].replace('-','_')
        if not argument:
            print(name+': '+('on' if conversation.natural_memory.feature(name) else 'off'))
        else:
            if argument not in ('on-confirm-cost','off'):
                raise ValueError('启用会增加 API 开销；使用 '+command+' on-confirm-cost，或 off。')
            conversation.natural_memory.set_feature(name,argument=='on-confirm-cost')
            conversation.clear()
            print('设置已保存；语义提取每会话最多 4 次，摘要最多 1 次，与聊天共用总预算。')
    elif command == '/memory-mode':
        if not argument:
            print('当前记忆模式：'+conversation.natural_memory.mode)
        else:
            conversation._sync()
            before = conversation._revision
            conversation.natural_memory.set_mode(argument)
            conversation._pending_joke_label = None
            if argument == 'session':
                conversation.clear()
                print('已切换仅会话模式；为移除已载入的长期记忆，当前上下文已清空。')
            else:
                conversation._revision = before + 1
                conversation._clear_learning()
                print('记忆模式已保存：'+argument+'。普通自动模式只提取明确的普通自述；不保存诊断、人格推断或敏感健康史。')
    elif command == '/auto-memory':
        rows = conversation.natural_memory.list()
        print('暂无自动记忆。' if not rows else '自动记忆：')
        for item in rows:
            print(f"  A{item['id']} [{item['kind']}] {visible_text(item['text'])}")
            print('    '+visible_text(item['scope'])+' · '+item['source']+' · '+item['updated_at'])
            print('    原话：'+visible_text(item['evidence']))
            if item.get('merge_policy') == 'excluded_legacy':
                print('    旧版自动记录未通过当前范围检查，已排除检索；可显式更正或删除。')
    elif command == '/auto-correct':
        identifier, _, body = argument.partition(' ')
        if not identifier.upper().startswith('A') or not identifier[1:].isdigit():
            raise ValueError('用法：/auto-correct A1 更正内容')
        conversation._sync()
        before = conversation._revision
        old = conversation.natural_memory.correct(int(identifier[1:]), body)
        conversation.invalidate_natural([old['id']], [old['text'],old['evidence']], [old['semantic_key']])
        conversation._revision = before + 1
        print('已更正；相关旧对话及待确认项已移除，无关对话继续保留。')
    elif command == '/auto-forget':
        if argument != 'all' and not (argument.upper().startswith('A') and argument[1:].isdigit()):
            raise ValueError('用法：/auto-forget A1 或 /auto-forget all')
        conversation._sync()
        before = conversation._revision
        old = conversation.natural_memory.forget(None if argument == 'all' else int(argument[1:]))
        conversation.invalidate_natural([m['id'] for m in old], [v for m in old for v in (m['text'],m['evidence'])], [m['semantic_key'] for m in old])
        if argument == 'all':
            conversation.continuity.invalidate()
            conversation._memory_overrides.clear()
            conversation._temporary_overrides.clear()
            conversation.history.clear()
            conversation._history_dependencies.clear()
        conversation._revision = before + int(bool(old))
        print('已删除 '+str(len(old))+' 条自动记忆及相关旧对话；待确认项已移除。')
    elif command == "/learn":
        if argument:
            raise ValueError("用法：/learn（整理近期对话，额外一次请求）；也可 /note KIND TEXT 直接记录。")
        print("整理近期交流，额外调用一次模型；确认前只临时保留建议，不写入长期记忆。")
        with WaitingStatus():
            proposals = conversation.learn()
        if not proposals:
            print("这次没有新增建议，不必为了学习再找话说。")
        else:
            show_learning(proposals)
            print("只保存你认可的内容：/keep L编号；放弃用 /drop L编号。继续发新消息会清除待确认建议。")
    elif command == "/notes":
        conversation._sync()
        notes = store.list_notes()
        print("已确认的相处记录：" if notes else "暂无相处记录。")
        for note in notes:
            print(f"  N{note['id']} [{note['kind']}] {visible_text(note['text'])}")
            if note["scope"]:
                print("    适用范围：" + visible_text(note["scope"]))
            if note["evidence"]:
                print("    确认时的原话依据：" + visible_text(note["evidence"]))
        if conversation.pending_learning:
            print("待确认（尚未长期保存）：")
            show_learning(conversation.pending_learning)
    elif command == "/keep":
        item = conversation.keep_learning(argument.upper())
        print(f"已保存为 N{item['id']}。以后在相关时可使用；当前聊天继续保留。")
    elif command == "/drop":
        print("已放弃，未保存。" if conversation.drop_learning(argument.upper()) else "没有这条待确认建议。")
    elif command == "/note":
        kind, _, body = argument.partition(" ")
        conversation._sync()
        revision = conversation._revision
        note = store.remember_note(body, kind, expected_revision=revision)
        conversation._revision = revision + 1
        conversation._clear_learning()
        print(f"已记下 N{note['id']}。当前聊天继续保留，可用 /notes 查看。")
    elif command == "/revise":
        identifier, _, body = argument.partition(" ")
        note = store.correct_note(note_identifier(identifier), body)
        conversation.clear()
        print(f"已更正 N{note['id']}，清除旧依据和当前上下文。")
    elif command == "/unlearn":
        removed = store.forget_note(note_identifier(argument))
        conversation.clear()
        print("已删除相处记录并清空当前上下文。" if removed else "没有这条记录；当前上下文已清空。")
    elif command == "/memory":
        items = store.list_memories()
        print("暂无长期记忆。" if not items else "你明确保存的信息：")
        for item in items:
            print(f"  #{item['id']} [{item['kind']}] {visible_text(item['text'])}")
    elif command == "/remember":
        kind, _, text = argument.partition(" ")
        item = store.remember(text, kind)
        conversation.clear()
        print(f"已保存 #{item['id']}。当前对话上下文已清空，后续按相关性读取。")
    elif command == "/correct":
        memory_id, _, text = argument.partition(" ")
        item = store.correct(int(memory_id), text)
        conversation.clear()
        print(f"已更新 #{item['id']}，清空当前上下文；与旧记忆关联的提醒已失效。")
    elif command == "/forget":
        removed = store.forget(int(argument))
        conversation.clear()
        print("已删除并清空当前上下文。" if removed else "没有找到该记忆；当前上下文已清空。")
    elif command == "/new":
        if hasattr(conversation, "reset_interaction"): conversation.reset_interaction()
        else: conversation.clear()
        print("新对话已开始。")
    elif command == "/mode":
        if argument not in ("casual", "support"):
            raise ValueError("用法：/mode casual 或 /mode support")
        conversation.mode = argument
        print("已切换模式：" + argument)
    elif command == "/emoji":
        if argument not in ("off", "light"):
            raise ValueError("用法：/emoji off 或 /emoji light")
        conversation.emoji_mode = argument
        print("本次会话表情设置：" + argument)
    elif command == "/status":
        print(json.dumps(runtime_status(getattr(conversation.client, "config", None), conversation), ensure_ascii=False, indent=2))
    elif command == "/remind":
        when, _, text = argument.partition(" ")
        item = store.schedule(text, when)
        print(f"已安排本地提醒 #{item['id']}：{item['at']}。输入轮次之间检查，不能保证准时显示。")
    elif command == "/reminders":
        items = store.list_reminders()
        if not items:
            print("暂无提醒。")
        for item in items:
            print(f"  #{item['id']} [{item['status']}] {item['at']} {visible_text(item['text']) or '[内容已清除]'}")
    elif command == "/cancel":
        print("已取消。" if store.cancel_reminder(int(argument)) else "没有可取消的提醒。")
    elif command == "/check":
        show_due(store)
    elif command == "/usage":
        u = conversation.usage
        print(f"本次请求：{u.calls}/{conversation.max_calls}；已报告 token：{u.total_tokens} "
              f"(输入 {u.prompt_tokens}，输出 {u.completion_tokens})。未返回用量或失败的请求不含在 token 数内。")
    elif command == "/budget":
        if not argument.isascii() or not argument.isdigit() or not 1 <= int(argument) <= 10000:
            raise ValueError("用法：/budget 80；本次上限可设为 1—10000 次请求。")
        maximum = int(argument)
        if maximum < conversation.usage.calls:
            raise ValueError("新上限不能低于本次已经使用的请求数；用 /usage 查看。")
        conversation.max_calls = maximum
        print(f"本次上限已设为 {maximum} 次请求；聊天继续保留。继续对话会消耗 API 额度。")
    elif command == "/wipe":
        answer = input("删除本地全部记忆、相处记录和提醒？输入 DELETE 确认：")
        if answer == "DELETE":
            store.wipe_all()
            conversation.clear()
            print("本地数据已清空；不会撤回此前已发送给推理服务的内容。")
        else:
            print("未删除。")
    else:
        print("未知命令。输入 /help 查看用法。")
    return True


def note_identifier(value: str) -> int:
    value = value.upper()
    if value.startswith("N"):
        value = value[1:]
    if not value.isascii() or not value.isdigit() or int(value) < 1:
        raise ValueError("相处记录编号例如 N1；用 /notes 查看。")
    return int(value)


def show_learning(proposals: list[dict]) -> None:
    for item in proposals:
        print(f"  {item['id']} [{item['kind']}] {visible_text(item['text'])}")
        print("    适用范围：" + visible_text(item["scope"]))
        print("    你的原话：" + visible_text(item["evidence"]))


def chat(args) -> int:
    print("稳伴 · Steady Companion — AI 心理支持研究原型")
    print("聊天将发送到你配置的推理服务；本程序不保存完整聊天记录。默认手动记忆，可用 /memory-mode 配置。")
    print("提供日常交流与支持，不冒充医生，也不承诺治疗效果。输入 /help 查看命令。")
    scope_mode = getattr(args, 'interaction_control', 'off')
    scope_check = getattr(args, 'interaction_check', 'off')
    if scope_check != 'off' and scope_mode != 'session':
        raise ValueError('--interaction-check requires --interaction-control session; no credential read.')
    profile = getattr(args, 'profile', None)
    connection_file = getattr(args, 'connection', None)
    if connection_file is not None and profile is None:
        raise ValueError('--connection requires an explicit --profile.')
    if profile:
        from .model_access import resolve, connect
        plan = resolve(profile, connection_file)
        if getattr(args, '_response_mode_explicit', False) and args.response_mode != plan.policy.response_mode:
            raise ValueError('--response-mode conflicts with the selected profile; no credential read.')
        if args.max_calls < 1: raise ValueError('Request budget must be positive.')
        if scope_mode == 'session' and plan.policy.response_mode != 'companion':
            raise ValueError('Interaction experiment requires companion; competition profile unchanged.')
        plan.require_ready()
        client = connect(plan)
        policy = plan.policy.kwargs()
    else:
        if scope_mode == 'session' and args.response_mode != 'companion':
            raise ValueError('Interaction experiment requires explicit companion mode.')
        client = configured_client()
        policy = {'response_mode': args.response_mode}
    factory = Conversation
    if scope_mode == 'session':
        from .interaction_runtime import InteractionConversation
        factory = InteractionConversation
        policy['interaction_check'] = scope_check
    with Store(args.data_dir) as store:
        conversation = factory(client, store, mode=args.mode, max_calls=args.max_calls,
                                    emoji_mode=args.emoji, **policy)
        print("角色 "+conversation.architecture.role["role_id"]+" · 记忆模式 "+conversation.natural_memory.mode)
        print(f"版本 {__version__} · 回应模式 {conversation.response_mode} · emoji {args.emoji}")
        if profile:
            print('接入配置：'+profile+' · 服务商 '+plan.provider+' · 模型 '+client.config.model)
            print('研究配置，心理效果未验证；/status 查看实际参数、材料与记忆授权。')
        if conversation.response_mode == "checked":
            print("每轮通常 2 次请求，必要时最多 4 次；检查未完成时会明确报错。")
        if conversation.response_mode == "companion":
            if profile:
                print('日常开发基准：一次普通回复一次生成，协议失败不恢复，可继续输入；已有获授权记忆辅助或 /learn 另计请求。')
            else:
                print("companion 实验模式：通常一次纯文本生成，输出协议失败最多一次恢复；无通用模型审阅。")
        if scope_mode == 'session':
            print('会话范围控制已启用；语义检查 '+scope_check+'。检查能力未验证；启用检查最多额外一次，与辅助共享总预算，无自动恢复。')
        print("请求同步执行；若底层 I/O 未立即响应中断，需等待中断或超时；不会在后台补写新会话。")
        print("等待回复时按 Ctrl+C 取消本轮，仍可继续聊天；在输入处按 Ctrl+C 或输入 /quit 退出。")
        while True:
            # Reminder failures must not prevent the next prompt from appearing.
            try:
                show_due(store)
            except (OSError, sqlite3.Error) as exc:
                report_local_error(exc)
            except Exception as exc:
                report_unexpected_error(exc)
            try:
                line = input("\n你 › ").strip()
            except (EOFError, KeyboardInterrupt):
                print("\n已结束。")
                break
            except OSError as exc:
                report_local_error(exc)
                return 1
            if not line:
                continue
            try:
                if line.startswith("/"):
                    if not execute_command(line, conversation, store):
                        break
                    continue
                with WaitingStatus():
                    result = conversation.reply(line)
                try:
                    print("\n稳伴 › " + result.text)
                except (OSError,ValueError):
                    report_local_error(OSError(),stream=sys.stderr,recoverable=False)
                    return 1  # Validation/commit already finished; do not replay a disconnected display.
                except KeyboardInterrupt:
                    print("显示已中断；回复此前已生成，记忆状态以本地记录为准。",file=sys.stderr)
                    return 130
                update = result.memory_update
                if result.architecture.get('continuity_status') in ('extractive_capacity_limited','summary_failed_extractive_fallback','summary_cancelled_capacity_fallback'):
                    print('[上下文容量] 部分旧原文已省略；仅保留可验证的会话摘录。')
                if update.get('status') in ('saved','corrected','retracted','scope_changed'):
                    print('[记忆已更新] '+', '.join('A'+str(i) for i in update['ids'])+'；/auto-memory 查看，/auto-forget 删除。')
                elif update.get('status') in ('not_saved','storage_limit'):
                    print('[本轮记忆未持久保存] 当前更正仅在本次会话生效；重启后仍可能读到旧记录。未自动重试，可用明确命令更正。')
                    if update.get('read_degraded'):
                        print('[记忆读取降级] 本轮未加载长期记忆；无法核对来源的旧上下文已清理。')
                if args.trace:
                    print("[本地运行信息] " + json.dumps(asdict(result) | {"text": "[不记录回复正文]"}, ensure_ascii=False))
            except (ValueError, ProviderError) as exc:
                print("\n[未完成] " + visible_text(str(exc)))
                required = 2 if conversation.response_mode == "checked" else 1
                if conversation.max_calls - conversation.usage.calls < required:
                    print("本次请求额度不足；回复未完成，未自动扩额或重试。用 /usage 查看实际用量。")
            except KeyboardInterrupt:
                print("\n已取消本轮。可以继续输入；已发出的请求仍可能计费。")
            except EOFError:
                print("\n已结束。")
                break
            except (OSError, sqlite3.Error) as exc:
                report_local_error(exc)
            except Exception as exc:
                report_unexpected_error(exc)
    return 0


def error_code(exc: Exception) -> str:
    """Return a bounded exception type, never its message, path or payload."""
    name = type(exc).__name__
    return name if name.isascii() and name.isidentifier() and len(name) <= 64 else "UnexpectedError"


def report_local_error(exc: Exception, *, stream=None, recoverable=True) -> None:
    next_step = "可以继续输入，或用 /quit 退出。" if recoverable else "请检查项目文件和数据目录是否可读写，再重新运行。"
    try:
        print(f"\n[本地读写未完成：{error_code(exc)}] {next_step}", file=stream)
    except (OSError, ValueError):
        pass  # The terminal may already have disconnected.


def report_unexpected_error(exc: Exception, *, stream=None, recoverable=True) -> None:
    next_step = "可以继续输入；未自动重试。" if recoverable else "程序已停止，请保留此错误类型以便排查。"
    try:
        print(f"\n[本轮未完成：{error_code(exc)}] {next_step}", file=stream)
    except (OSError, ValueError):
        pass


def doctor(args) -> int:
    print("将发起一次真实 Nebius 请求，可能产生少量费用。")
    client = configured_client()
    response = client.complete([
        {"role": "system", "content": "Respond briefly. Do not use tools."},
        {"role": "user", "content": "Reply with a short greeting."},
    ])
    content = response["choices"][0]["message"].get("content")
    answer = visible_text(content) if isinstance(content, str) else ""
    if not answer:
        raise ProviderError("API responded but no visible text was returned. Check output-token limits and reasoning settings.")
    print("连接成功：" + answer)
    usage = response.get("usage") or {}
    print("用量：" + json.dumps({k: usage.get(k) for k in ("prompt_tokens", "completion_tokens", "total_tokens")}))
    return 0


def demo(args) -> int:
    print("OFFLINE DEMO / 离线程序演示：使用固定测试回复，不调用 Nebius，不验证心理支持效果。")

    class FixtureClient:
        def __init__(self):
            self.requests = []

        def complete(self, messages, **kwargs):
            self.requests.append(json.loads(json.dumps(messages)))
            return {"choices": [{"message": {"role": "assistant", "content": "[固定测试回复] 本轮上下文已接收。"}}]}

    with TemporaryDirectory(prefix="steady-demo-") as folder:
        with Store(Path(folder)) as store:
            record = store.remember("演示标记：OLD_MEMORY_紫色", "preference")
            fixture = FixtureClient()
            agent = Conversation(fixture, store, response_mode="direct")
            agent.reply("这是一段旧的演示对话 OLD_TURN_旧内容。")
            store.correct(record["id"], "演示标记：NEW_MEMORY_蓝色")
            agent.reply("现在读取记忆。")
            serialized = json.dumps(fixture.requests[-1], ensure_ascii=False)
            assert "OLD_MEMORY_紫色" not in serialized and "OLD_TURN_旧内容" not in serialized
            assert "NEW_MEMORY_蓝色" in serialized
            print("PASS：更正后，新请求包含新记忆，旧记忆与旧对话已移除。")
            store.forget(record["id"])
            agent.reply("再聊一句。")
            assert "NEW_MEMORY_蓝色" not in json.dumps(fixture.requests[-1], ensure_ascii=False)
            print("PASS：删除后，下一次请求不再包含该记忆。")
            at = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
            reminder = store.schedule("演示提醒", at)
            store.cancel_reminder(reminder["id"])
            future = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
            assert store.deliver_reminder(reminder["id"], future) is None
            print("PASS：取消的提醒不能被领取。临时演示数据会删除。")
    return 0


def evaluate(args) -> int:
    if not args.confirm_live:
        raise ValueError("Live evaluation sends synthetic cases to Nebius. Add --confirm-live to allow requests and costs.")
    if args.limit < 1 or args.limit > 100:
        raise ValueError("--limit must be between 1 and 100")
    cases = []
    for line in args.cases.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        case = json.loads(line)
        if not isinstance(case, dict) or not isinstance(case.get("messages"), list) or not case["messages"]:
            raise ValueError("Each case requires a non-empty messages list")
        messages = case["messages"]
        if len(messages) > 16 or not isinstance(messages[-1], dict) or messages[-1].get("role") != "user":
            raise ValueError("Cases must end with a user message and contain at most 16 messages")
        for item in messages:
            if not isinstance(item, dict) or item.get("role") not in ("user", "assistant") or not isinstance(item.get("content"), str) or not item["content"].strip() or len(item["content"]) > 6000:
                raise ValueError("Cases must contain bounded user/assistant text only")
        if sum(len(m["content"]) for m in messages[:-1]) > 18000:
            raise ValueError("Case history exceeds the 18000-character budget")
        if case.get("mode", "casual") not in ("casual", "support"):
            raise ValueError("Case mode must be casual or support")
        memories = case.get("memories", [])
        if not isinstance(memories, list) or any(
            not isinstance(m, dict) or not isinstance(m.get("text"), str)
            or not m["text"].strip() or m.get("kind", "fact") not in ("fact", "preference", "boundary", "goal")
            for m in memories
        ):
            raise ValueError("Case memories require non-empty text and a supported kind")
        cases.append(case)
        if len(cases) >= args.limit:
            break
    if not cases:
        raise ValueError("No evaluation cases found")
    client = configured_client()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    print(f"运行 {len(cases)} 个案例 × {len(args.conditions)} 个条件；结果需人工评阅，不自动判断疗效或胜出。")
    failures = 0
    # Refuse accidental overwrite of existing evaluation evidence.
    with args.output.open("x", encoding="utf-8") as output:
        try:
            os.chmod(args.output, 0o600)
        except OSError:
            pass
        for case in cases:
            with TemporaryDirectory(prefix="steady-eval-") as folder, Store(Path(folder)) as store:
                for memory in case.get("memories", []):
                    store.remember(memory["text"], memory.get("kind", "fact"))
                for condition in args.conditions:
                    agent = Conversation(client, store, use_skill=condition != "baseline",
                                         mode=case.get("mode", "casual"), max_calls=4 if condition == "checked" else 2,
                                         response_mode="checked" if condition == "checked" else "direct", emoji_mode="off",
                                         capture_stages=getattr(args,"capture_stages",False))
                    agent.history = [dict(m) for m in case["messages"][:-1]]
                    record = {"case_id": case.get("id"), "condition": condition,
                              "model": client.config.model, "version": __version__,
                              "timestamp": datetime.now(timezone.utc).isoformat(),
                              "checks": case.get("checks", []), "review": None,
                              "skill_sha256": runtime_status()["skill_sha256"],
                              "settings": {"response_mode": agent.response_mode, "emoji_mode": "off",
                                           "max_tokens": client.config.max_tokens}}
                    try:
                        turn = agent.reply(case["messages"][-1]["content"])
                        record.update(asdict(turn))
                    except ProviderError as exc:
                        record["error"] = str(exc)
                        failures += 1
                    record["usage"] = asdict(agent.usage)
                    output.write(json.dumps(record, ensure_ascii=False) + "\n")
                    output.flush()
                    print(f"完成 {case.get('id')} / {condition}")
    print(f"结果：{args.output}；请求失败记录 {failures} 条。")
    return int(failures > 0)


class ExplicitResponseMode(argparse.Action):
    def __call__(self, parser, namespace, values, option_string=None):
        setattr(namespace, self.dest, values)
        setattr(namespace, '_response_mode_explicit', True)


def archived_experiments_available():
    # Historical design approvals and raw-output replays are not public assets.
    return (Path(__file__).parent/'data/expression/dev3-approved.json').is_file()


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description="Steady Companion · core framework with explicit model access profiles")
    root.add_argument("--version", action="version", version=__version__)
    root.add_argument("--data-dir", type=Path, default=default_data_dir())
    sub = root.add_subparsers(dest="command")
    chat_parser = sub.add_parser("chat", help="Chat with legacy Nebius access or an explicit deployment profile")
    from .model_access import PROFILE_NAMES
    chat_parser.add_argument('--profile', choices=PROFILE_NAMES, help='daily-deepseek (requires explicit service/endpoint/model/protocol) or competition-nebius; legacy defaults unchanged')
    chat_parser.add_argument('--connection', type=Path, help='Non-secret daily connection JSON; never put an API key in this file')
    chat_parser.add_argument("--mode", choices=("casual", "support"), default="casual")
    chat_parser.add_argument("--max-calls", type=int, default=40)
    chat_parser.add_argument("--interaction-control",choices=("off","session"),default="off",help="Opt-in session scope experiment; default keeps dev12 compiler")
    chat_parser.add_argument("--interaction-check",choices=("off","selective","all"),default="off",help="Explicit additional paid narrow checks under shared budget; unverified")
    chat_parser.add_argument("--trace", action="store_true", help="Show transient metadata, never hidden reasoning")
    chat_parser.add_argument("--response-mode", choices=("checked", "direct", "companion"), default="checked", action=ExplicitResponseMode)
    chat_parser.add_argument("--emoji", choices=("off", "light"), default="off")
    experience = sub.add_parser('eval-experience', help='Six-turn synthetic continuity trial; offline by default; fixed 12-request cap')
    experience.add_argument('--connection', type=Path, default=Path('configs/deepseek-official.json'))
    experience.add_argument('--output', type=Path, help='New exclusive local report directory; default unique eval-results/continuous-dev16-*')
    experience.add_argument('--interaction-control', choices=('session',), default='session')
    experience.add_argument('--interaction-check', choices=('selective',), default='selective')
    experience.add_argument('--confirm-live', action='store_true', help='Authorize at most 12 paid requests and local synthetic visible reply/withheld-candidate capture; no retry')
    story = sub.add_parser('eval-story', help='Continuous synthetic P1 scenario; offline unless --confirm-live')
    story.add_argument('--scenario', type=Path, default=Path(__file__).parent/'data/p1_story.json')
    story.add_argument('--output', type=Path, default=Path('eval-results/p1-story.json'))
    story.add_argument('--max-calls', type=int, default=80)
    story.add_argument('--confirm-live', action='store_true',help='Authorize paid synthetic calls, including configured memory/summary, within the shared cap')
    story.add_argument('--capture-stages', action='store_true')
    pilot=sub.add_parser('eval-pilot',help='T1/T2/T6 seven-user-turn pilot; at most 32 total paid requests')
    pilot.add_argument('--scenario',type=Path,default=Path(__file__).parent/'data/p12_pilot.json')
    pilot.add_argument('--output',type=Path,default=Path('eval-results/p12-pilot.jsonl'))
    pilot.add_argument('--max-calls',type=int,default=32)
    pilot.add_argument('--confirm-live',action='store_true',help='Authorize paid synthetic generation, review, memory and summary requests under the shared cap')
    pilot.add_argument('--capture-stages',action='store_true')
    diagnose=sub.add_parser('eval-diagnose',help='Synthetic name retest (8 requests) or ordered pilot diagnostic (32); explicit cost confirmation')
    diagnose.add_argument('--context',choices=('name','pilot'),default='name')
    diagnose.add_argument('--output',type=Path,default=Path('eval-results/p121-diagnostic.jsonl'))
    diagnose.add_argument('--max-calls',type=int,default=None)
    diagnose.add_argument('--confirm-live',action='store_true')
    diagnose.add_argument('--capture-stages',action='store_true')
    if archived_experiments_available():
        proposal=sub.add_parser('eval-dev2-plan',help='Offline-only 8/12-request proposal for design review; no live option')
        proposal.add_argument('--output',type=Path,default=Path('eval-results/dev2-proposal-01.jsonl'))
        config_compare=sub.add_parser('eval-config-compare',help='dev10 fixed deployment comparison: offline by default, max 20 inference + one model-list GET')
        config_compare.add_argument('--output',type=Path,default=Path('eval-results/config-compare-dev10-preview-01.jsonl'))
        config_compare.add_argument('--confirm-live',action='store_true',help='Confirm costs and visible synthetic capture for at most 20 inference requests and one metadata GET; no retries')
        companionship=sub.add_parser('eval-companionship',help='One synthetic 28-call checked/companion batch; default offline preview plus readable report')
        companionship.add_argument('--profile',choices=('checked-companion-v1','companion-v2'),default='checked-companion-v1',help='Versioned fixed matrix: unchanged dev8 comparison 28 calls, or dev9 companion-only 12 calls')
        companionship.add_argument('--output',type=Path,default=None)
        companionship.add_argument('--confirm-live',action='store_true',help='Confirm this fixed synthetic batch and visible request/reply capture; fixed profile cap 28 or 12 calls, no recovery or per-turn confirmation')
        assumptions=sub.add_parser('eval-assumptions',help='Four fixed synthetic reply-assumption probes; default offline')
        assumptions.add_argument('--output',type=Path,default=Path('eval-results/assumptions-dev7-preview-01.jsonl'))
        assumptions.add_argument('--confirm-live',action='store_true',help='Accept at most four paid calls and bounded visible claims capture for these fixed synthetic inputs')
        grounding=sub.add_parser('eval-grounding',help='Four fixed synthetic user-self-report grounding probes; default offline')
        grounding.add_argument('--output',type=Path,default=Path('eval-results/grounding-dev6-preview-01.jsonl'))
        grounding.add_argument('--confirm-live',action='store_true',help='Accept at most four paid grounding calls; bounded status/evidence capture is intrinsic to this synthetic diagnostic')
        review_inputs=sub.add_parser('eval-review-inputs',help='Exploratory dev4 A/B review input matrix; exactly four maximum calls')
        review_inputs.add_argument('--output',type=Path,default=Path('eval-results/review-inputs-dev5-preview-01.jsonl'))
        review_inputs.add_argument('--confirm-live',action='store_true',help='Accept costs for this fixed four-cell diagnostic only')
        review_inputs.add_argument('--capture-stages',action='store_true',help='Bounded visible synthetic review return capture')
        small=sub.add_parser('eval-small',help='Design-approved synthetic 8/12 experiment; default offline preview')
        small.add_argument('--output',type=Path,default=Path('eval-results/expression-p13-dev4-preview-01.jsonl'))
        small.add_argument('--max-calls',type=int,default=12)
        small.add_argument('--confirm-live',action='store_true',help='Accept costs for the approved R1/R2, N1/N2 and two-turn trial, at most 12 total calls')
        small.add_argument('--capture-stages',action='store_true',help='Explicit synthetic-only bounded visible diagnostic capture')
        expression=sub.add_parser('eval-expression',help='Separated synthetic review/generation/continuous diagnostics; explicit paid-run cap 24')
        expression.add_argument('--phase',choices=('all','review','generation','continuous'),default='all')
        expression.add_argument('--output',type=Path,default=Path('eval-results/expression-p13-dev1-inputs.jsonl'))
        expression.add_argument('--max-calls',type=int,default=24)
        expression.add_argument('--confirm-live',action='store_true')
        expression.add_argument('--confirm-calibration',action='store_true',help='Operator has reviewed and accepts the proposed host-only calibration labels; required for live A/all')
        expression.add_argument('--capture-stages',action='store_true')
    comparison=sub.add_parser('eval-compare',help='Single-scene P1.1.1 vs P1.2; offline unless cost confirmed')
    comparison.add_argument('--scenario',type=Path,default=Path(__file__).parent/'data/p12_T1_v0.json')
    comparison.add_argument('--baseline',type=Path,help='Explicit external P1.1.1 ZIP; historical archives are not bundled')
    comparison.add_argument('--output',type=Path,default=Path('eval-results/p12-compare.json'))
    comparison.add_argument('--max-calls',type=int,default=32)
    comparison.add_argument('--confirm-live',action='store_true')
    inspection = sub.add_parser("inspect", help="Show loaded version, access configuration and memory mode; no network")
    inspection.add_argument('--profile', choices=PROFILE_NAMES)
    inspection.add_argument('--connection', type=Path, help='Inspect non-secret daily connection JSON without reading credentials')
    sub.add_parser("doctor", help="One live API connection check")
    sub.add_parser("demo", help="Offline infrastructure demo, no API key")
    e = sub.add_parser("eval", help="Live synthetic comparison, manual review required")
    e.add_argument("--cases", type=Path, default=Path(__file__).parent / "data" / "cases.jsonl")
    e.add_argument("--output", type=Path, default=Path("eval-results") / "comparison.jsonl")
    e.add_argument("--limit", type=int, default=3)
    e.add_argument("--confirm-live", action="store_true")
    e.add_argument("--capture-stages", action="store_true", help="Synthetic evaluation only: save visible candidate texts and review labels")
    e.add_argument("--conditions", nargs="+", choices=("baseline", "skill", "checked"), default=["baseline", "skill", "checked"])
    return root


def main(argv=None) -> int:
    p = parser()
    args = p.parse_args(argv)
    if args.command is None:
        args.command, args.mode, args.max_calls, args.trace = "chat", "casual", 40, False
        args.response_mode, args.emoji = "checked", "off"
    from .story_eval import evaluate_story, evaluate_diagnostic
    from .compare_eval import evaluate_comparison
    from .experience_eval import evaluate as evaluate_experience
    archived={}
    if archived_experiments_available():
        from .expression_eval import evaluate_expression
        from .dev2_plan import export_plan
        from .small_eval import evaluate_small
        from .review_inputs import evaluate_review_inputs
        from .grounding_eval import evaluate_grounding
        from .assumptions_eval import evaluate_assumptions
        from .companionship_eval import evaluate_companionship
        from .config_compare import evaluate as evaluate_config_compare
        archived={"eval-config-compare":evaluate_config_compare,"eval-companionship":evaluate_companionship,"eval-assumptions":evaluate_assumptions,"eval-grounding":evaluate_grounding,"eval-review-inputs":evaluate_review_inputs,"eval-small":evaluate_small,"eval-dev2-plan":export_plan,"eval-expression":evaluate_expression}
    try:
        return {**archived,"eval-experience":evaluate_experience,"eval-diagnose":evaluate_diagnostic,"eval-story":evaluate_story,"eval-pilot":evaluate_story,"eval-compare":evaluate_comparison, "chat": chat, "doctor": doctor, "demo": demo, "eval": evaluate, "inspect": inspect_runtime}[args.command](args)
    except (ValueError, ProviderError) as exc:
        # Expected provider/config validation messages are already redacted.
        print("Error: " + visible_text(str(exc)), file=sys.stderr)
        return 1
    except (OSError, sqlite3.Error) as exc:
        report_local_error(exc, stream=sys.stderr, recoverable=False)
        return 1
    except Exception as exc:
        report_unexpected_error(exc, stream=sys.stderr, recoverable=False)
        return 1
    except (KeyboardInterrupt, EOFError):
        print("\n已结束。")
        return 130
