"""Typed access to scenario TOML files.

Scenario files inherit undefined keys from scenarios/defaults.toml, and command-line
overrides (`--config dotted.key=value`) take precedence over both. Lookups are still
required with no defaults in code, so each value used by a run is visible in its
config.toml snapshot (the resolved merge of defaults, the scenario and the overrides).
"""

import tomllib
from datetime import date, time
from pathlib import Path
from typing import Any, Self

from flux_match import paths
from flux_match.errors import ConfigError


class Config:
    """Read-only view of one table in a scenario TOML file."""

    def __init__(self, data: dict[str, Any], source: Path, prefix: str = "") -> None:
        self._data = data
        self.source = source
        self._prefix = prefix

    @classmethod
    def load(
        cls,
        path: Path,
        *,
        inherit_defaults: bool = True,
        overrides: dict[str, Any] | None = None,
    ) -> Self:
        """`overrides` maps dotted keys (from `parse_override`) to values; each key must
        already exist in the merged file(s), so that a typo cannot be silently ignored."""
        data = _read_toml(path)
        if inherit_defaults and path.resolve() != paths.defaults_path().resolve():
            data = _deep_merge(_read_toml(paths.defaults_path()), data)
        for key, value in (overrides or {}).items():
            _set_existing(data, key, value, path)
        return cls(data, path)

    def to_toml(self) -> str:
        """Serialize this config's top-level table as TOML (for run-folder snapshots)."""
        return _dump_toml(self._data)

    def section(self, key: str) -> Config:
        value = self._lookup(key)
        if not isinstance(value, dict):
            raise ConfigError(f"{self.source}: '{self._qualify(key)}' must be a table")
        return Config(value, self.source, self._qualify(key))

    def get[T](self, key: str, expected: type[T]) -> T:
        return self._check_type(self._lookup(key), expected, self._qualify(key))

    def get_list[T](self, key: str, element: type[T], unique: bool = False) -> list[T]:
        """A non-empty array whose elements all have the given type, optionally distinct."""
        value = self._lookup(key)
        name = self._qualify(key)
        if not isinstance(value, list) or not value:
            raise ConfigError(f"{self.source}: '{name}' must be a non-empty array, got {value!r}")
        values = [self._check_type(v, element, f"{name}[{i}]") for i, v in enumerate(value)]
        if unique:
            duplicates = sorted({v for v in values if values.count(v) > 1}, key=str)
            if duplicates:
                raise ConfigError(f"{self.source}: '{name}' has duplicates: {duplicates}")
        return values

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


def _read_toml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise ConfigError(f"Config file not found: {path}")
    try:
        data = tomllib.loads(path.read_text())
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{path}: invalid TOML: {e}") from e
    if not isinstance(data, dict):
        raise ConfigError(f"{path}: root must be a table")
    return data


def parse_override(text: str) -> tuple[str, Any]:
    """Parse `dotted.key=value`. The value is a TOML literal (`0.9`, `true`, `[2022, 2023]`,
    `"text"`); anything that is not valid TOML is taken as a bare string (`cluster`)."""
    key, sep, raw = text.partition("=")
    key, raw = key.strip(), raw.strip()
    if not sep or not raw or not all(part.strip() for part in key.split(".")):
        raise ConfigError(f"Config override must be 'dotted.key=value', got {text!r}")
    try:
        value = tomllib.loads(f"v = {raw}")["v"]
    except tomllib.TOMLDecodeError:
        value = raw
    if isinstance(value, dict):
        raise ConfigError(f"Config override '{key}' must set a single value, not a table")
    if isinstance(value, (date, time)):
        raise ConfigError(f"Config override '{key}': dates and times are not supported")
    return key, value


def _set_existing(data: dict[str, Any], dotted_key: str, value: Any, source: Path) -> None:
    *parents, leaf = dotted_key.split(".")
    table = data
    for depth, part in enumerate(parents):
        table = table.get(part)
        if not isinstance(table, dict):
            prefix = ".".join(parents[: depth + 1])
            raise ConfigError(
                f"Config override '{dotted_key}': no table '{prefix}' in the config for {source}"
            )
    if leaf not in table:
        raise ConfigError(f"Config override '{dotted_key}': no such key in the config for {source}")
    if isinstance(table[leaf], dict):
        raise ConfigError(f"Config override '{dotted_key}' is a table, not a single value")
    table[leaf] = value


def _deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    """Return a new dict: overlay keys win; nested tables are merged recursively."""
    result = dict(base)
    for key, value in overlay.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def _dump_toml(data: dict[str, Any]) -> str:
    """Minimal TOML writer for the scalar, array, and nested-table shapes we use."""
    top_scalars = "".join(
        f"{key} = {_dump_value(value)}\n"
        for key, value in data.items()
        if not isinstance(value, dict)
    )
    tables = [_dump_section(key, value) for key, value in data.items() if isinstance(value, dict)]
    if top_scalars and tables:
        return top_scalars + "\n" + "\n".join(tables)
    return top_scalars + "\n".join(tables)


def _dump_section(path: str, data: dict[str, Any]) -> str:
    """One `[path]` table, with any nested tables emitted after its keys."""
    lines = [f"[{path}]\n"]
    nested: list[str] = []
    for key, value in data.items():
        if isinstance(value, dict):
            nested.append(_dump_section(f"{path}.{key}", value))
        else:
            lines.append(f"{key} = {_dump_value(value)}\n")
    return "".join(lines) + "".join(nested)


def _dump_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if type(value) is int:
        return str(value)
    if isinstance(value, float):
        return repr(value)
    if isinstance(value, str):
        return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'
    if isinstance(value, list):
        return "[ " + ", ".join(_dump_value(v) for v in value) + " ]"
    raise TypeError(f"unsupported TOML value type: {type(value).__name__}")
