"""Declarative, byte-preserving tensor layouts; Torch is imported only on use.

Recipes describe coordinates, never executable callbacks. Compilation checks the
complete source/destination schemas without allocating element-sized tables.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Callable

_MAX = (1 << 63) - 1
_JSON_LIMIT = 4 * 1024 * 1024
_DTYPES = {
    "bfloat16",
    "float16",
    "float32",
    "float64",
    "int8",
    "uint8",
    "int16",
    "int32",
    "int64",
    "bool",
}


def _integer(value: Any, *, minimum: int = 0) -> int:
    if type(value) is not int or not minimum <= value <= _MAX:
        raise ValueError("Invalid bounded integer")
    return value


def _shape(value: Any) -> tuple[int, ...]:
    if not isinstance(value, (list, tuple)) or len(value) > 16:
        raise ValueError("Invalid tensor shape")
    result = tuple(_integer(x) for x in value)
    if math.prod(result) > _MAX:
        raise ValueError("Tensor element count exceeds int64")
    return result


def _name(value: Any, *, pattern: bool = False) -> str:
    if (
        not isinstance(value, str)
        or not 0 < len(value) <= 1024
        or any(ord(c) < 32 for c in value)
    ):
        raise ValueError("Invalid tensor name")
    if "*" in value and (not pattern or "**" in value):
        raise ValueError("Only single numeric capture wildcards are supported")
    return value


def _canonical(value: dict) -> bytes:
    raw = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    ).encode()
    if len(raw) > _JSON_LIMIT:
        raise ValueError("Layout/schema exceeds JSON size bound")
    return raw


def _load(raw: bytes | str) -> dict:
    if not isinstance(raw, (bytes, str)) or len(raw) > _JSON_LIMIT:
        raise ValueError("Invalid bounded JSON")

    def pairs(items):
        result = {}
        for k, v in items:
            if k in result:
                raise ValueError("Duplicate JSON field")
            result[k] = v
        return result

    value = json.loads(
        raw,
        object_pairs_hook=pairs,
        parse_constant=lambda _: (_ for _ in ()).throw(ValueError("Nonfinite JSON")),
    )
    if not isinstance(value, dict):
        raise ValueError("Expected JSON object")
    return value


def _fields(value: dict, expected: set[str]) -> None:
    if not isinstance(value, dict) or set(value) != expected:
        raise ValueError("Unknown or missing fields")


def _digest(value: Any) -> str:
    if not isinstance(value, str) or re.fullmatch("[0-9a-f]{64}", value) is None:
        raise ValueError("Expected SHA256")
    return value


@dataclass(frozen=True, init=False)
class TensorSpec:
    name: str
    shape: tuple[int, ...]
    dtype: str

    def __init__(self, name: str, shape: Sequence[int], dtype: str):
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "shape", tuple(shape))
        object.__setattr__(self, "dtype", dtype)
        self.__post_init__()

    def __post_init__(self):
        _name(self.name)
        object.__setattr__(self, "shape", _shape(self.shape))
        if not isinstance(self.dtype, str) or self.dtype not in _DTYPES:
            raise ValueError("Unsupported tensor dtype")

    @property
    def numel(self) -> int:
        return math.prod(self.shape)

    def _dict(self) -> dict:
        return {"name": self.name, "shape": list(self.shape), "dtype": self.dtype}


@dataclass(frozen=True, init=False)
class WeightSchema:
    tensors: tuple[TensorSpec, ...]

    def __init__(self, tensors: Sequence[TensorSpec]):
        object.__setattr__(self, "tensors", tuple(tensors))
        self.__post_init__()

    def __post_init__(self):
        values = tuple(self.tensors)
        if not 1 <= len(values) <= 100_000 or any(
            not isinstance(t, TensorSpec) for t in values
        ):
            raise ValueError("Invalid tensor schema")
        if len({t.name for t in values}) != len(values):
            raise ValueError("Duplicate tensor schema name")
        object.__setattr__(self, "tensors", tuple(sorted(values, key=lambda t: t.name)))

    @classmethod
    def from_manifest(cls, initial):
        if initial.kind != "keyframe":
            raise ValueError("Initial keyframe schema required")
        return cls(tuple(TensorSpec(t.name, t.shape, t.dtype) for t in initial.tensors))

    def to_json(self) -> bytes:
        return _canonical({"version": 1, "tensors": [t._dict() for t in self.tensors]})

    @classmethod
    def from_json(cls, raw: bytes | str):
        obj = _load(raw)
        _fields(obj, {"version", "tensors"})
        if (
            type(obj["version"]) is not int
            or obj["version"] != 1
            or not isinstance(obj["tensors"], list)
        ):
            raise ValueError("Unsupported schema version")
        values = []
        for value in obj["tensors"]:
            _fields(value, {"name", "shape", "dtype"})
            values.append(TensorSpec(**value))
        return cls(tuple(values))

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.to_json()).hexdigest()


@dataclass(frozen=True)
class Concatenate:
    dim: int = 0


@dataclass(frozen=True, init=False)
class Split:
    sizes: tuple[int, ...]
    dim: int = 0

    def __init__(self, sizes: Sequence[int], dim: int = 0):
        object.__setattr__(self, "sizes", tuple(sizes))
        object.__setattr__(self, "dim", dim)
        self.__post_init__()

    def __post_init__(self):
        object.__setattr__(self, "sizes", tuple(_integer(x) for x in self.sizes))
        if not 1 <= len(self.sizes) <= 100_000:
            raise ValueError("Invalid split count")


@dataclass(frozen=True)
class Transpose:
    dim0: int
    dim1: int


@dataclass(frozen=True, init=False)
class Reshape:
    shape: tuple[int, ...]

    def __init__(self, shape: Sequence[int]):
        object.__setattr__(self, "shape", tuple(shape))
        self.__post_init__()

    def __post_init__(self):
        object.__setattr__(self, "shape", _shape(self.shape))


@dataclass(frozen=True)
class Interleave:
    """Split each input into ``groups`` along dim, then concatenate groupwise.

    For Q/K/V with two query groups: [Q0,K0,V0,Q1,K1,V1]. Input
    group widths may differ. This operation preserves all input values exactly.
    """

    dim: int
    groups: int

    def __post_init__(self):
        _integer(self.groups, minimum=1)


_OPERATIONS = {
    "concatenate": Concatenate,
    "split": Split,
    "transpose": Transpose,
    "reshape": Reshape,
    "interleave": Interleave,
}


def _operation_dict(op) -> dict:
    names = {v: k for k, v in _OPERATIONS.items()}
    if type(op) not in names:
        raise ValueError("Unsupported layout operation")
    result: dict[str, Any] = {"op": names[type(op)]}
    if isinstance(op, (Concatenate, Split, Interleave)):
        result["dim"] = op.dim
    if isinstance(op, Split):
        result["sizes"] = list(op.sizes)
    if isinstance(op, Transpose):
        result.update(dim0=op.dim0, dim1=op.dim1)
    if isinstance(op, Reshape):
        result["shape"] = list(op.shape)
    if isinstance(op, Interleave):
        result["groups"] = op.groups
    # Validate integers before serialization, including bool/float rejection.
    for key in ("dim", "dim0", "dim1"):
        if key in result and (
            type(result[key]) is not int or not -16 <= result[key] < 16
        ):
            raise ValueError("Invalid tensor dimension")
    return result


def _operation_from_dict(obj):
    if not isinstance(obj, dict) or obj.get("op") not in _OPERATIONS:
        raise ValueError("Unsupported layout operation")
    kind = obj["op"]
    fields = {
        "concatenate": {"op", "dim"},
        "split": {"op", "dim", "sizes"},
        "transpose": {"op", "dim0", "dim1"},
        "reshape": {"op", "shape"},
        "interleave": {"op", "dim", "groups"},
    }[kind]
    _fields(obj, fields)
    result = _OPERATIONS[kind](**{k: v for k, v in obj.items() if k != "op"})
    _operation_dict(result)
    return result


def _names(value) -> tuple[str, ...]:
    if not isinstance(value, (str, list, tuple)):
        raise ValueError("Mapping names must be strings or lists")
    result = (value,) if isinstance(value, str) else tuple(value)
    if not 1 <= len(result) <= 100_000:
        raise ValueError("Invalid mapping names")
    for n in result:
        _name(n, pattern=True)
    if len(set(result)) != len(result):
        raise ValueError("Duplicate mapping names")
    return result


@dataclass(frozen=True, init=False)
class TensorMapping:
    """A whole-tensor rule; ``*`` captures numeric strings, reused in targets.

    Every pattern in a rule must have the same capture count. Non-numeric and
    recursive wildcards are unsupported; expanded rules must be unambiguous.
    """

    sources: tuple[str, ...]
    target: tuple[str, ...]
    operations: tuple[Any, ...] = ()

    def __init__(
        self,
        sources: str | Sequence[str],
        target: str | Sequence[str],
        operations: Sequence[Any] = (),
    ):
        object.__setattr__(self, "sources", sources)
        object.__setattr__(self, "target", target)
        object.__setattr__(self, "operations", tuple(operations))
        self.__post_init__()

    def __post_init__(self):
        object.__setattr__(self, "sources", _names(self.sources))
        object.__setattr__(self, "target", _names(self.target))
        operations = tuple(self.operations)
        if len(operations) > 64:
            raise ValueError("Too many layout operations")
        for op in operations:
            _operation_dict(op)
        object.__setattr__(self, "operations", operations)
        captures = self.sources[0].count("*")
        if any(n.count("*") != captures for n in self.sources + self.target):
            raise ValueError("Wildcard capture counts differ")

    def _dict(self):
        return {
            "sources": list(self.sources),
            "target": list(self.target),
            "operations": [_operation_dict(o) for o in self.operations],
        }


def _axis(dim: int, shape: tuple[int, ...]) -> int:
    if type(dim) is not int or not -len(shape) <= dim < len(shape):
        raise ValueError("Dimension outside tensor rank")
    return dim % len(shape)


def _stage(shapes, op):
    if isinstance(op, (Concatenate, Interleave)):
        axis = _axis(op.dim, shapes[0])
        if isinstance(op, Interleave) and len(shapes) * op.groups > 100_000:
            # The independent inverse materializes one view per input/group.
            # Empty tensor dimensions must not bypass this metadata-work bound.
            raise ValueError("Too many interleave pieces")
        if any(
            len(s) != len(shapes[0])
            or any(s[i] != shapes[0][i] for i in range(len(s)) if i != axis)
            for s in shapes
        ):
            raise ValueError("Incompatible concatenate/interleave shapes")
        if isinstance(op, Interleave) and any(s[axis] % op.groups for s in shapes):
            raise ValueError("Interleave groups must divide each input")
        shape = list(shapes[0])
        shape[axis] = sum(s[axis] for s in shapes)
        return (_shape(shape),)
    if len(shapes) != 1:
        raise ValueError("This operation requires exactly one input tensor")
    shape = shapes[0]
    if isinstance(op, Split):
        axis = _axis(op.dim, shape)
        if sum(op.sizes) != shape[axis]:
            raise ValueError("Split sizes do not cover dimension")
        return tuple(shape[:axis] + (size,) + shape[axis + 1 :] for size in op.sizes)
    if isinstance(op, Transpose):
        a, b = _axis(op.dim0, shape), _axis(op.dim1, shape)
        result = list(shape)
        result[a], result[b] = result[b], result[a]
        return (tuple(result),)
    if isinstance(op, Reshape):
        if math.prod(shape) != math.prod(op.shape):
            raise ValueError("Reshape changes tensor element count")
        return (op.shape,)
    raise ValueError("Unsupported operation")


@dataclass(frozen=True, init=False)
class WeightLayout:
    rules: tuple[TensorMapping, ...] = ()
    match_by_name: bool = True
    source_schema_sha256: str | None = None

    def __init__(
        self,
        rules: Sequence[TensorMapping] = (),
        match_by_name: bool = True,
        source_schema_sha256: str | None = None,
    ):
        object.__setattr__(self, "rules", tuple(rules))
        object.__setattr__(self, "match_by_name", match_by_name)
        object.__setattr__(self, "source_schema_sha256", source_schema_sha256)
        self.__post_init__()

    def __post_init__(self):
        rules = tuple(self.rules)
        if (
            len(rules) > 100_000
            or any(not isinstance(r, TensorMapping) for r in rules)
            or type(self.match_by_name) is not bool
        ):
            raise ValueError("Invalid layout rules")
        object.__setattr__(self, "rules", rules)
        if self.source_schema_sha256 is not None:
            _digest(self.source_schema_sha256)

    def bind(self, schema: WeightSchema):
        if self.source_schema_sha256 not in (None, schema.sha256):
            raise ValueError("Layout already bound to another schema")
        return WeightLayout(self.rules, self.match_by_name, schema.sha256)

    def to_json(self) -> bytes:
        return _canonical(
            {
                "version": 1,
                "rules": [r._dict() for r in self.rules],
                "match_by_name": self.match_by_name,
                "source_schema_sha256": self.source_schema_sha256,
            }
        )

    @classmethod
    def from_json(cls, raw: bytes | str):
        obj = _load(raw)
        _fields(obj, {"version", "rules", "match_by_name", "source_schema_sha256"})
        if (
            type(obj["version"]) is not int
            or obj["version"] != 1
            or not isinstance(obj["rules"], list)
        ):
            raise ValueError("Unsupported layout version")
        rules = []
        for rule in obj["rules"]:
            _fields(rule, {"sources", "target", "operations"})
            if not isinstance(rule["operations"], list):
                raise ValueError("Invalid operations")
            rules.append(
                TensorMapping(
                    rule["sources"],
                    rule["target"],
                    tuple(_operation_from_dict(o) for o in rule["operations"]),
                )
            )
        return cls(tuple(rules), obj["match_by_name"], obj["source_schema_sha256"])

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.to_json()).hexdigest()

    def compile(self, source_schema: WeightSchema, target_schema: WeightSchema):
        if self.source_schema_sha256 != source_schema.sha256:
            raise ValueError("Layout must bind the exact source schema")
        sources = {t.name: t for t in source_schema.tensors}
        targets = {t.name: t for t in target_schema.tensors}
        claimed_sources: set[str] = set()
        claimed_targets: set[str] = set()
        programs = []
        for rule in self.rules:
            pattern = re.compile(
                "^"
                + "([0-9]+)".join(re.escape(x) for x in rule.sources[0].split("*"))
                + "$"
            )
            matches = [(name, pattern.fullmatch(name)) for name in sources]
            matches = [(name, m) for name, m in matches if m is not None]
            if not matches:
                raise ValueError("Mapping rule matches no source tensors")
            for _, match in matches:
                # Expand each pattern with the same numeric captures.
                def expand(name):
                    parts = name.split("*")
                    result = parts[0]
                    for value, part in zip(match.groups(), parts[1:]):
                        result += value + part
                    return result

                names = tuple(expand(n) for n in rule.sources)
                dest = tuple(expand(n) for n in rule.target)
                if len(set(names)) != len(names) or len(set(dest)) != len(dest):
                    raise ValueError("Expanded mapping has duplicate names")
                if any(n not in sources for n in names) or any(
                    n not in targets for n in dest
                ):
                    raise ValueError("Mapping references missing tensors")
                if claimed_sources.intersection(names) or claimed_targets.intersection(
                    dest
                ):
                    raise ValueError("Ambiguous or overlapping tensor mappings")
                dtype = sources[names[0]].dtype
                if any(sources[n].dtype != dtype for n in names) or any(
                    targets[n].dtype != dtype for n in dest
                ):
                    raise ValueError("Mapping changes tensor dtype")
                shapes = tuple(sources[n].shape for n in names)
                stages = [shapes]
                for op in rule.operations:
                    shapes = _stage(shapes, op)
                    stages.append(shapes)
                if shapes != tuple(targets[n].shape for n in dest):
                    raise ValueError("Mapping destination shapes differ")
                claimed_sources.update(names)
                claimed_targets.update(dest)
                programs.append(_Program(names, dest, rule.operations, tuple(stages)))
        if self.match_by_name:
            for name in sorted(sources.keys() - claimed_sources):
                if (
                    name in targets
                    and name not in claimed_targets
                    and sources[name] == targets[name]
                ):
                    shape = sources[name].shape
                    programs.append(_Program((name,), (name,), (), ((shape,),)))
                    claimed_sources.add(name)
                    claimed_targets.add(name)
        if claimed_sources != set(sources) or claimed_targets != set(targets):
            raise ValueError(
                "Layout must cover every source and target tensor exactly once"
            )
        return CompiledLayout(source_schema, target_schema, tuple(programs))


@dataclass(frozen=True)
class _Program:
    sources: tuple[str, ...]
    targets: tuple[str, ...]
    operations: tuple[Any, ...]
    stages: tuple[tuple[tuple[int, ...], ...], ...]


@dataclass(frozen=True)
class IndexRoute:
    target: str
    indices: Any
    source_positions: Any


def _coordinates(indices, shape):
    result = []
    rest = indices
    for width in reversed(shape):
        result.append(rest % width)
        rest = rest // width
    return list(reversed(result))


def _flatten(coordinates, shape, template):
    result = template.new_zeros(template.shape)
    for coordinate, width in zip(coordinates, shape):
        result = result * width + coordinate
    return result


@dataclass(frozen=True)
class CompiledLayout:
    source_schema: WeightSchema
    target_schema: WeightSchema
    _programs: tuple[_Program, ...]
    _lookup: dict[str, tuple[_Program, TensorSpec]] = field(
        init=False, repr=False, compare=False
    )

    def __post_init__(self):
        specs = {t.name: t for t in self.source_schema.tensors}
        object.__setattr__(
            self,
            "_lookup",
            {
                name: (program, specs[name])
                for program in self._programs
                for name in program.sources
            },
        )

    def _program(self, name):
        try:
            return self._lookup[name][0]
        except KeyError:
            raise ValueError("Unknown logical tensor") from None

    def map_indices(self, source_name: str, indices) -> list[IndexRoute]:
        import torch

        program = self._program(source_name)
        spec = self._lookup[source_name][1]
        if (
            not isinstance(indices, torch.Tensor)
            or indices.dtype != torch.int64
            or indices.device.type != "cpu"
            or indices.ndim != 1
        ):
            raise ValueError("Expected CPU int64 vector of indices")
        if indices.numel() and (
            bool((indices < 0).any()) or bool((indices >= spec.numel).any())
        ):
            raise ValueError("Sparse index outside source tensor")
        routes = [
            (
                program.sources.index(source_name),
                indices,
                torch.arange(indices.numel(), dtype=torch.int64, device="cpu"),
            )
        ]
        for stage, op in enumerate(program.operations):
            before, after = program.stages[stage : stage + 2]
            following = []
            for slot, current, positions in routes:
                shape = before[slot]
                if isinstance(op, Reshape):
                    following.append((0, current, positions))
                    continue
                coordinates = _coordinates(current, shape)
                if isinstance(op, (Concatenate, Interleave)):
                    axis = _axis(op.dim, shape)
                    if isinstance(op, Concatenate):
                        coordinates[axis] = coordinates[axis] + sum(
                            s[axis] for s in before[:slot]
                        )
                    else:
                        widths = [s[axis] // op.groups for s in before]
                        width = widths[slot]
                        coordinates[axis] = (
                            (coordinates[axis] // width) * sum(widths)
                            + sum(widths[:slot])
                            + coordinates[axis] % width
                            if current.numel()
                            else current
                        )
                    following.append(
                        (0, _flatten(coordinates, after[0], current), positions)
                    )
                elif isinstance(op, Split):
                    axis = _axis(op.dim, shape)
                    offset = 0
                    for outslot, size in enumerate(op.sizes):
                        mask = (coordinates[axis] >= offset) & (
                            coordinates[axis] < offset + size
                        )
                        selected = [c[mask] for c in coordinates]
                        selected[axis] -= offset
                        following.append(
                            (
                                outslot,
                                _flatten(selected, after[outslot], current[mask]),
                                positions[mask],
                            )
                        )
                        offset += size
                elif isinstance(op, Transpose):
                    a, b = _axis(op.dim0, shape), _axis(op.dim1, shape)
                    coordinates[a], coordinates[b] = coordinates[b], coordinates[a]
                    following.append(
                        (0, _flatten(coordinates, after[0], current), positions)
                    )
                else:
                    raise ValueError("Unsupported operation")
            routes = following
        # Multiple intermediate routes may converge after concatenation.
        result = []
        for slot, target in enumerate(program.targets):
            selected = [(i, p) for s, i, p in routes if s == slot and i.numel()]
            if selected:
                result.append(
                    IndexRoute(
                        target,
                        torch.cat([i for i, _ in selected]),
                        torch.cat([p for _, p in selected]),
                    )
                )
        return result

    def read_source(self, source_name: str, get_target: Callable[[str], Any]):
        """Reconstruct one logical tensor for a full audit, independently of indices.

        The caller controls device transfer/lifetimes through get_target. Only
        this rule's destination tensors are read; no model-sized snapshot exists.
        """
        import torch

        program = self._program(source_name)
        specs = {t.name: t for t in self.target_schema.tensors}
        values = []
        for name in program.targets:
            value = get_target(name)
            if (
                not isinstance(value, torch.Tensor)
                or tuple(value.shape) != specs[name].shape
                or str(value.dtype).removeprefix("torch.") != specs[name].dtype
            ):
                raise ValueError("Actual destination tensor differs from schema")
            values.append(value)
        for stage in range(len(program.operations) - 1, -1, -1):
            op = program.operations[stage]
            shapes = program.stages[stage]
            if isinstance(op, Concatenate):
                axis = _axis(op.dim, shapes[0])
                values = list(
                    torch.split(values[0], [s[axis] for s in shapes], dim=axis)
                )
            elif isinstance(op, Interleave):
                axis = _axis(op.dim, shapes[0])
                widths = [s[axis] // op.groups for s in shapes]
                pieces = torch.split(values[0], widths * op.groups, dim=axis)
                values = [
                    torch.cat(
                        [
                            pieces[group * len(shapes) + slot]
                            for group in range(op.groups)
                        ],
                        dim=axis,
                    )
                    for slot in range(len(shapes))
                ]
            elif isinstance(op, Split):
                values = [torch.cat(values, dim=_axis(op.dim, shapes[0]))]
            elif isinstance(op, Transpose):
                values = [values[0].transpose(op.dim0, op.dim1)]
            elif isinstance(op, Reshape):
                values = [values[0].reshape(shapes[0])]
            else:
                raise ValueError("Unsupported operation")
        return values[program.sources.index(source_name)]
