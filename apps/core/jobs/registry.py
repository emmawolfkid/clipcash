"""
Strict registry of background job types.

Only functions registered here can ever run; stored jobs name a registered
type and carry a payload validated against that type's schema (identifiers,
short strings, integers and booleans only). Nothing in the database can cause
arbitrary code to execute.
"""

import uuid
from dataclasses import dataclass
from typing import Callable

PAYLOAD_TYPES = ("uuid", "str", "int", "bool")
MAX_PAYLOAD_STRING = 200


class UnknownJobTypeError(LookupError):
    pass


class InvalidPayloadError(ValueError):
    pass


@dataclass(frozen=True)
class JobType:
    name: str
    handler: Callable
    payload_schema: dict
    max_attempts: int = 5
    backoff_base_seconds: int = 30
    backoff_max_seconds: int = 3600
    priority: int = 100
    admin_retryable: bool = True
    # Called inside the transaction that marks the job FAILED (terminal).
    on_failure: Callable | None = None
    description: str = ""


JOB_TYPES = {}


def register(name, *, payload_schema=None, **options):
    """Decorator registering ``handler(payload, job)`` as job type ``name``."""
    schema = dict(payload_schema or {})
    for key, kind in schema.items():
        if kind not in PAYLOAD_TYPES:
            raise ValueError(f"{name}: unsupported payload type {kind!r} for {key!r}")

    def decorator(handler):
        if name in JOB_TYPES and JOB_TYPES[name].handler is not handler:
            raise ValueError(f"Job type {name!r} is already registered.")
        JOB_TYPES[name] = JobType(name=name, handler=handler, payload_schema=schema, **options)
        return handler

    return decorator


def get_job_type(name):
    try:
        return JOB_TYPES[name]
    except KeyError:
        raise UnknownJobTypeError(f"Unregistered job type: {name!r}") from None


def validate_payload(job_type, payload):
    payload = payload or {}
    if not isinstance(payload, dict):
        raise InvalidPayloadError("Job payload must be a dict.")
    schema = job_type.payload_schema
    unknown = set(payload) - set(schema)
    missing = set(schema) - set(payload)
    if unknown or missing:
        raise InvalidPayloadError(
            f"{job_type.name} payload keys invalid (unknown: {sorted(unknown)}, missing: {sorted(missing)})."
        )
    cleaned = {}
    for key, kind in schema.items():
        value = payload[key]
        if kind == "uuid":
            try:
                cleaned[key] = str(uuid.UUID(str(value)))
            except ValueError as error:
                raise InvalidPayloadError(f"{key} must be a UUID.") from error
        elif kind == "str":
            if not isinstance(value, str) or len(value) > MAX_PAYLOAD_STRING:
                raise InvalidPayloadError(f"{key} must be a string of at most {MAX_PAYLOAD_STRING} characters.")
            cleaned[key] = value
        elif kind == "int":
            if isinstance(value, bool) or not isinstance(value, int):
                raise InvalidPayloadError(f"{key} must be an integer.")
            cleaned[key] = value
        elif kind == "bool":
            if not isinstance(value, bool):
                raise InvalidPayloadError(f"{key} must be a boolean.")
            cleaned[key] = value
    return cleaned
