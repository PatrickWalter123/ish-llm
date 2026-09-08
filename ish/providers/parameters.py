"""Copy request containers without copying live SDK clients or callbacks."""

from typing import Any


def copy_params(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: copy_params(item) for key, item in value.items()}
    if isinstance(value, list):
        return [copy_params(item) for item in value]
    if isinstance(value, tuple):
        return tuple(copy_params(item) for item in value)
    return value
