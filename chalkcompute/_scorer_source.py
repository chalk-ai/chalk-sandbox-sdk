"""Ship generated scorers without replaying their caller's factory invocation."""

from __future__ import annotations

import importlib
import inspect
import io
import pickle
import textwrap
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ScorerSource:
    module: str
    name: str
    source: str
    body: Callable[..., Any]
    extra_globals: Mapping[str, Any]
    attributes: Mapping[str, Any]

    def render(self, user_module: str) -> str:
        # Only the SDK-owned body is shipped as source. Configuration uses
        # ordinary pickle, which imports user models/hooks by name and does not
        # serialize Python bytecode (the worker may use a different Python).
        closure = inspect.getclosurevars(self.body).nonlocals
        try:
            payload = pickle.dumps(
                (closure, dict(self.extra_globals), dict(self.attributes)), protocol=4
            )
        except (pickle.PickleError, AttributeError, TypeError):
            # Import-time declarations could already recreate local models and
            # lambdas. Preserve that path rather than rejecting working judges.
            return (
                "from chalkcompute._scorer_source import import_scorer as __import_scorer\n"
                f"{self.name} = __import_scorer({user_module!r}, {self.name!r})\n"
            )
        body_source = textwrap.dedent(inspect.getsource(self.body))
        return (
            "from chalkcompute._scorer_source import restore_scorer as __restore\n"
            "globals().update(__restore(\n"
            f"    {self.module!r}, {user_module!r}, {payload!r},\n"
            f"    {body_source!r}, {getattr(self.body, '__name__')!r},\n"
            f"    {self.source!r}, {self.name!r}, __name__,\n"
            "))\n"
        )


def restore_scorer(
    original_module: str,
    user_module: str,
    payload: bytes,
    body_source: str,
    body_name: str,
    source: str,
    name: str,
    module_name: str,
) -> dict[str, Any]:
    """Restore configuration captured by our own deployment builder.

    A loose script is shipped under a generated module name. Redirect its
    model/hook references to that name; package references keep their names.
    Never load a payload from an untrusted source.
    """
    from chalkcompute import scorers
    from chalkcompute._evaluation import EvaluationScorerResult
    from chalkcompute._trace import Trace

    class Unpickler(pickle.Unpickler):
        def find_class(self, module: str, name: str) -> Any:
            if module == original_module:
                module = user_module
            return super().find_class(module, name)

    closure, extra_globals, attributes = Unpickler(io.BytesIO(payload)).load()
    # Built-in bodies resolve SDK helpers from their original module; each
    # scorer gets its own namespace so equally named scorers cannot mix specs.
    body_globals = {**vars(scorers), **closure}
    exec(compile(body_source, "<chalk scorer body>", "exec"), body_globals)
    namespace = {
        "__name__": module_name,
        "__chalk_result__": EvaluationScorerResult,
        "__chalk_trace__": Trace | None,
        "__chalk_body__": body_globals[body_name],
        **extra_globals,
    }
    exec(compile(source, "<chalk scorer>", "exec"), namespace)
    for attribute, value in attributes.items():
        setattr(namespace[name], attribute, value)
    return namespace


def import_scorer(module: str, name: str) -> Any:
    """Keep the legacy import path for declarations with unimportable hooks."""
    from chalkcompute._function import FunctionBuildError

    imported = importlib.import_module(module)
    try:
        return getattr(imported, name)
    except AttributeError as error:
        raise FunctionBuildError(
            "Cannot load scorer configuration. For a scorer created inside a "
            "function or conditional, define response_model, prompt_fn, and "
            "parse_fn in an importable module (not as local classes, nested "
            "functions, or lambdas). The scorer factory itself can be called anywhere."
        ) from error
