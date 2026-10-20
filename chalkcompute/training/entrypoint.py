"""Deprecated training entrypoint kept for servers that invoke the old path.

Servers that honor ``CreateTrainingRunRequest.entrypoint`` run
``chalkcompute._training_entrypoint``, which the SDK sends. Older servers ignore
the field and run this module, as does any request that leaves it unset, such as
post-training's. Remove this package once nothing runs it.
"""

from __future__ import annotations

import chalkcompute
from chalkcompute._training import training as _training_decorator
from chalkcompute._training_entrypoint import main

if __name__ == "__main__":
    # ``python -m`` imported this subpackage first, binding it over the
    # decorator. Restore it before main() imports the user's training module.
    setattr(chalkcompute, "training", _training_decorator)
    main()
