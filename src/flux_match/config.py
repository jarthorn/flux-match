"""Typed access to scenario TOML files.

Every lookup is required: parameters have no defaults in code, so each value used by a
run is visible in its config.toml snapshot.
"""

import tomllib
from pathlib import Path
from typing import Any, Self

from flux_match.errors import ConfigError


class Config:
    """Read-only view of one table in a scenario TOML file."""

    def __init__(self, data: dict[str, Any], source: Path, prefix: str = "") -> None:
        self._data = data
        self.source = source
        self._prefix = prefix

    @classmethod
    def load(cls, path: Path) -> Self:
        if not path.is_file():
            raise ConfigError(f"Config file not found: {path}")
        try:
            data = tomllib.loads(path.read_text())
        except tomllib.TOMLDecodeError as e:
            raise ConfigError(f"{path}: invalid TOML: {e}") from e
        return cls(data, path)

    def section(self, key: str) -> Config:
        value = self._lookup(key)
        if not isinstance(value, dict):
            raise ConfigError(f"{self.source}: '{self._qualify(key)}' must be a table")
        return Config(value, self.source, self._qualify(key))

    def get[T](self, key: str, expected: type[T]) -> T:
        return self._check_type(self._lookup(key), expected, self._qualify(key))

    def get_list[T](self, key: str, element: type[T]) -> list[T]:
        """A non-empty array whose elements all have the given type."""
        value = self._lookup(key)
        name = self._qualify(key)
        if not isinstance(value, list) or not value:
            raise ConfigError(f"{self.source}: '{name}' must be a non-empty array, got {value!r}")
        return [self._check_type(v, element, f"{name}[{i}]") for i, v in enumerate(value)]

    def _check_type[T](self, value: Any, expected: type[T], name: str) -> T:
        if expected is float and type(value) is int:
            value = float(value)
        # bool is a subclass of int, so `true` would otherwise pass as an int.
        if not isinstance(value, expected) or (isinstance(value, bool) and expected is not bool):
            raise ConfigError(
                f"{self.source}: '{name}' must be {expected.__name__}, "
                f"got {type(value).__name__} {value!r}"
            )
        return value

    def _lookup(self, key: str) -> Any:
        if key not in self._data:
            raise ConfigError(f"{self.source}: missing required key '{self._qualify(key)}'")
        return self._data[key]

    def _qualify(self, key: str) -> str:
        return f"{self._prefix}.{key}" if self._prefix else key
