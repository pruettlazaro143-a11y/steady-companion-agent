"""Host-only model access and runtime policies. No model facts enter core prompts.

Official behavior requires an explicit transport policy. Legacy ten-field
connections retain their generic behavior; a provider label alone changes nothing.
"""
from dataclasses import dataclass, replace
import getpass
import json
import math
import os
from pathlib import Path
import re
import sys

from .chat_transport import (ConnectionConfig, ChatCompletionsClient, ProviderError,
                             GENERIC_POLICY, DEEPSEEK_POLICY, validate_policy)
from .provider import DEFAULT_BASE_URL, DEFAULT_MODEL

PROFILE_NAMES = ('daily-deepseek', 'competition-nebius')
PROTOCOL = 'chat-completions'
DAILY_FIELDS = frozenset(('provider', 'protocol', 'base_url', 'model', 'api_key_env',
                         'timeout', 'max_tokens', 'response_format', 'temperature', 'top_p'))
OFFICIAL_FIELDS = DAILY_FIELDS | {'transport_policy', 'thinking'}
TEMPLATE = Path(__file__).parent / 'data/deepseek-connection.example.json'


@dataclass(frozen=True)
class RuntimePolicy:
    response_mode: str
    companion_runtime_version: str
    allow_recovery: bool

    def kwargs(self):
        return {'response_mode': self.response_mode, 'companion_runtime_version': self.companion_runtime_version,
                'allow_recovery': self.allow_recovery}


@dataclass(frozen=True)
class AccessPlan:
    profile: str
    provider: str | None
    protocol: str | None
    connection: ConnectionConfig | None
    credential_env: str
    policy: RuntimePolicy
    missing: tuple[str, ...] = ()
    temperature: float | None = None
    top_p: float | None = None
    max_tokens: int = 4096
    timeout: float = 90
    response_format: str = 'off'
    transport_policy: str = GENERIC_POLICY

    def require_ready(self):
        if self.connection is not None:
            validate_policy(self.connection, self.transport_policy)
        if self.missing:
            raise ProviderError('DeepSeek connection is not configured. Supply explicit ' + ', '.join(self.missing)
                + ' in a non-secret --connection JSON file. No credential read or request made.', code='configuration')

    def status(self):
        cfg = self.connection
        result = {'profile': self.profile, 'provider': self.provider, 'protocol': self.protocol,
                'configuration_status': 'pending_configuration' if self.missing else 'configured_not_live_verified',
                'missing_fields': list(self.missing), 'model': cfg.model if cfg else None,
                'base_url': cfg.base_url if cfg else None, 'credential_env': self.credential_env,
                'credential_loaded': False, 'runtime': self.policy.kwargs(),
                'parameters': {'max_tokens': cfg.max_tokens if cfg else self.max_tokens, 'timeout': cfg.timeout if cfg else self.timeout,
                    'temperature': self.temperature, 'top_p': self.top_p, 'store': False,
                    'response_format': cfg.response_format if cfg else self.response_format,
                    'unsent_sampling_and_thinking': 'provider_default_unknown'},
                'capability_status': 'unverified; no startup probes or parameter fallback',
                'memory_authorization': 'existing user settings preserved; new users manual',
                'quality_status': 'research configuration; psychological effect unverified'}
        result['transport_policy'] = self.transport_policy
        if self.transport_policy == DEEPSEEK_POLICY:
            params = result['parameters']
            params.pop('store'); params.pop('unsent_sampling_and_thinking')
            params.update(thinking={'type':'disabled'}, store_field='omitted',
                          sampling='service defaults; temperature/top_p not sent',
                          main_response_format='omitted', auxiliary_response_format='json_object')
            result['capability_status'] = 'official documentation checked; account connection and visible replies unverified; no probes/fallback'
        return result


def _config_error():
    return ProviderError('Invalid non-secret connection configuration; check documented fields and types. '
                         'Do not put credentials in this file.', code='configuration')


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result: raise ValueError
        result[key] = value
    return result


def read_connection(path):
    path = Path(path)
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 12000: raise ValueError
        data = json.loads(path.read_text(encoding='utf-8'), object_pairs_hook=_unique_object)
    except (OSError, UnicodeError, ValueError, TypeError, RecursionError):
        raise _config_error() from None
    if not isinstance(data, dict) or set(data) not in (DAILY_FIELDS, OFFICIAL_FIELDS): raise _config_error()
    return data


def resolve(profile, connection_file=None):
    """Resolve public settings only: never reads any credential environment value."""
    if profile not in PROFILE_NAMES: raise _config_error()
    from .companion_runtime import validate_baseline, validate_materials
    validate_baseline(); validate_materials()
    if profile == 'competition-nebius':
        if connection_file is not None:
            raise ProviderError('competition-nebius has a fixed NVIDIA route; --connection is for daily-deepseek.', code='configuration')
        configured_model = os.environ.get('NEBIUS_MODEL', DEFAULT_MODEL).strip()
        if configured_model != DEFAULT_MODEL:
            raise ProviderError('NEBIUS_MODEL conflicts with the fixed competition-nebius NVIDIA route.', code='configuration')
        cfg = ConnectionConfig(base_url=os.environ.get('NEBIUS_BASE_URL', DEFAULT_BASE_URL).strip(),
                               model=DEFAULT_MODEL, response_format='json_schema', max_tokens=4096, timeout=90)
        return AccessPlan(profile, 'nebius', PROTOCOL, cfg, 'NEBIUS_API_KEY', RuntimePolicy('checked', 'v2', True))
    data = read_connection(connection_file if connection_file is not None else TEMPLATE)
    for key in ('provider', 'protocol', 'base_url', 'model'):
        if data[key] is not None and (not isinstance(data[key], str) or not data[key].strip()): raise _config_error()
    if data['provider'] is not None and not re.fullmatch(r'[A-Za-z0-9_.-]{1,80}', data['provider']): raise _config_error()
    if data['model'] is not None and not re.fullmatch(r'[A-Za-z0-9_./:@+-]{1,256}', data['model']): raise _config_error()
    if data['protocol'] not in (None, PROTOCOL):
        raise ProviderError('Unsupported explicit protocol; only the Chat Completions adapter is implemented. '
                            'No provider protocol is inferred or automatically converted.', code='configuration')
    credential = data['api_key_env']
    if (not isinstance(credential, str) or not re.fullmatch(r'[A-Z][A-Z0-9_]{1,100}', credential)
            or credential.startswith('NEBIUS_')):
        raise ProviderError('Daily DeepSeek requires a separate credential environment name; NEBIUS_* reuse is refused.', code='configuration')
    for key, upper in (('temperature', 2), ('top_p', 1)):
        value = data[key]
        if value is not None and (type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= upper): raise _config_error()
    # Validate provided values even when the routing fields are still incomplete.
    if type(data['timeout']) not in (int, float) or not math.isfinite(data['timeout']) or not 0 < data['timeout'] <= 90: raise _config_error()
    if type(data['max_tokens']) is not int or not 1 <= data['max_tokens'] <= 4096: raise _config_error()
    if data['response_format'] not in ('off', 'json_object', 'json_schema'): raise _config_error()
    if data['base_url'] is not None:
        from .chat_transport import _endpoint
        _endpoint(data['base_url'])
    transport_policy = data.get('transport_policy', GENERIC_POLICY)
    if set(data) == OFFICIAL_FIELDS:
        if (transport_policy != DEEPSEEK_POLICY or data['thinking'] != {'type':'disabled'}
                or data['provider'] != 'deepseek-official' or data['protocol'] != PROTOCOL
                or credential != 'STEADY_DEEPSEEK_API_KEY'
                or data['temperature'] is not None or data['top_p'] is not None):
            raise _config_error()
    missing = tuple(key for key in ('provider', 'protocol', 'base_url', 'model') if data[key] is None)
    cfg = None if missing else ConnectionConfig(base_url=data['base_url'], model=data['model'],
        max_tokens=data['max_tokens'], timeout=data['timeout'], response_format=data['response_format'])
    if transport_policy == DEEPSEEK_POLICY:
        if cfg is None: raise _config_error()
        validate_policy(cfg, transport_policy)
    return AccessPlan(profile, data['provider'], data['protocol'], cfg, credential,
                      RuntimePolicy('companion', 'v3', False), missing, data['temperature'], data['top_p'],
                      data['max_tokens'], data['timeout'], data['response_format'], transport_policy)


class AccessClient:
    """Bind only routing/parameters. Messages, tools, evidence and state pass unchanged."""
    def __init__(self, plan, transport):
        plan.require_ready()
        if (replace(transport.config, api_key='') != plan.connection
                or transport.policy != plan.transport_policy):
            raise ProviderError('Transport configuration does not match selected profile.', code='configuration')
        self.plan, self.transport, self.config = plan, transport, transport.config

    @property
    def auxiliary_response_format(self):
        return {'type':'json_object'} if self.plan.transport_policy == DEEPSEEK_POLICY else None

    def complete(self, messages, **kwargs):
        for key, value in (('temperature', self.plan.temperature), ('top_p', self.plan.top_p)):
            if key in kwargs and kwargs[key] != value:
                raise ProviderError('Sampling override conflicts with selected access configuration.', code='configuration')
            if value is not None: kwargs[key] = value
        return self.transport.complete(messages, **kwargs)


def connect(plan):
    """Only an explicit chat launch reaches here; no GET, probe or hidden retry."""
    plan.require_ready()
    key = os.environ.get(plan.credential_env, '').strip()
    if not key:
        if not sys.stdin.isatty():
            raise ProviderError('Set the selected profile credential locally, or use interactive hidden input. '
                                'No fallback credential was read.', code='configuration')
        key = getpass.getpass('输入所选接入配置的 API Key（隐藏，仅本次运行）: ').strip()
    if not key: raise ProviderError('No credential supplied for this access profile.', code='configuration')
    cfg = replace(plan.connection, api_key=key)
    return AccessClient(plan, ChatCompletionsClient(cfg, policy=plan.transport_policy))
