import datetime

from google.protobuf import timestamp_pb2 as _timestamp_pb2
from google.protobuf.internal import enum_type_wrapper as _enum_type_wrapper
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class HealthState(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    HEALTH_STATE_UNSPECIFIED: _ClassVar[HealthState]
    HEALTH_STATE_GREEN: _ClassVar[HealthState]
    HEALTH_STATE_YELLOW: _ClassVar[HealthState]
    HEALTH_STATE_RED: _ClassVar[HealthState]
    HEALTH_STATE_EXHAUSTED: _ClassVar[HealthState]
HEALTH_STATE_UNSPECIFIED: HealthState
HEALTH_STATE_GREEN: HealthState
HEALTH_STATE_YELLOW: HealthState
HEALTH_STATE_RED: HealthState
HEALTH_STATE_EXHAUSTED: HealthState

class ServiceHealthHeartbeat(_message.Message):
    __slots__ = ("service_name", "current_state", "uptime_seconds", "internal_load_metric", "timestamp", "idempotency_key")
    SERVICE_NAME_FIELD_NUMBER: _ClassVar[int]
    CURRENT_STATE_FIELD_NUMBER: _ClassVar[int]
    UPTIME_SECONDS_FIELD_NUMBER: _ClassVar[int]
    INTERNAL_LOAD_METRIC_FIELD_NUMBER: _ClassVar[int]
    TIMESTAMP_FIELD_NUMBER: _ClassVar[int]
    IDEMPOTENCY_KEY_FIELD_NUMBER: _ClassVar[int]
    service_name: str
    current_state: HealthState
    uptime_seconds: int
    internal_load_metric: int
    timestamp: _timestamp_pb2.Timestamp
    idempotency_key: str
    def __init__(self, service_name: _Optional[str] = ..., current_state: _Optional[_Union[HealthState, str]] = ..., uptime_seconds: _Optional[int] = ..., internal_load_metric: _Optional[int] = ..., timestamp: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ..., idempotency_key: _Optional[str] = ...) -> None: ...

class TokenBurnEvent(_message.Message):
    __slots__ = ("agent_id", "action_context", "tokens_consumed", "cost_estimated_micro_usd", "timestamp", "idempotency_key")
    AGENT_ID_FIELD_NUMBER: _ClassVar[int]
    ACTION_CONTEXT_FIELD_NUMBER: _ClassVar[int]
    TOKENS_CONSUMED_FIELD_NUMBER: _ClassVar[int]
    COST_ESTIMATED_MICRO_USD_FIELD_NUMBER: _ClassVar[int]
    TIMESTAMP_FIELD_NUMBER: _ClassVar[int]
    IDEMPOTENCY_KEY_FIELD_NUMBER: _ClassVar[int]
    agent_id: str
    action_context: str
    tokens_consumed: int
    cost_estimated_micro_usd: int
    timestamp: _timestamp_pb2.Timestamp
    idempotency_key: str
    def __init__(self, agent_id: _Optional[str] = ..., action_context: _Optional[str] = ..., tokens_consumed: _Optional[int] = ..., cost_estimated_micro_usd: _Optional[int] = ..., timestamp: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ..., idempotency_key: _Optional[str] = ...) -> None: ...

class WidgetStatePayload(_message.Message):
    __slots__ = ("calculated_at", "fleet", "quota")
    CALCULATED_AT_FIELD_NUMBER: _ClassVar[int]
    FLEET_FIELD_NUMBER: _ClassVar[int]
    QUOTA_FIELD_NUMBER: _ClassVar[int]
    calculated_at: _timestamp_pb2.Timestamp
    fleet: FleetMetrics
    quota: QuotaMetrics
    def __init__(self, calculated_at: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ..., fleet: _Optional[_Union[FleetMetrics, _Mapping]] = ..., quota: _Optional[_Union[QuotaMetrics, _Mapping]] = ...) -> None: ...

class FleetMetrics(_message.Message):
    __slots__ = ("overall_health", "active_nodes", "degraded_nodes", "active_discovery_endpoint")
    OVERALL_HEALTH_FIELD_NUMBER: _ClassVar[int]
    ACTIVE_NODES_FIELD_NUMBER: _ClassVar[int]
    DEGRADED_NODES_FIELD_NUMBER: _ClassVar[int]
    ACTIVE_DISCOVERY_ENDPOINT_FIELD_NUMBER: _ClassVar[int]
    overall_health: HealthState
    active_nodes: int
    degraded_nodes: int
    active_discovery_endpoint: str
    def __init__(self, overall_health: _Optional[_Union[HealthState, str]] = ..., active_nodes: _Optional[int] = ..., degraded_nodes: _Optional[int] = ..., active_discovery_endpoint: _Optional[str] = ...) -> None: ...

class QuotaMetrics(_message.Message):
    __slots__ = ("runway_state", "runway_minutes_remaining", "burn_rate_tokens_per_minute", "absolute_quota_remaining_cents")
    RUNWAY_STATE_FIELD_NUMBER: _ClassVar[int]
    RUNWAY_MINUTES_REMAINING_FIELD_NUMBER: _ClassVar[int]
    BURN_RATE_TOKENS_PER_MINUTE_FIELD_NUMBER: _ClassVar[int]
    ABSOLUTE_QUOTA_REMAINING_CENTS_FIELD_NUMBER: _ClassVar[int]
    runway_state: HealthState
    runway_minutes_remaining: int
    burn_rate_tokens_per_minute: int
    absolute_quota_remaining_cents: int
    def __init__(self, runway_state: _Optional[_Union[HealthState, str]] = ..., runway_minutes_remaining: _Optional[int] = ..., burn_rate_tokens_per_minute: _Optional[int] = ..., absolute_quota_remaining_cents: _Optional[int] = ...) -> None: ...
