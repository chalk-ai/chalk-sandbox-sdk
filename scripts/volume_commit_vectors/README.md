# Offline format1 cross-language checks

`tests/fixtures/volume_commit_format1.json` freezes protobuf wire inputs, resolved
environment/volume identities, canonical bytes/digests and expected rejections.
Python tests exercise the SDK's manual projector and bounded CBOR encoder.
`main.go` independently projects messages through Go reflection, rejects unknown
fields/enums and unsupported semantics, and uses fxamacker's RFC8949 Core
Deterministic encoder. It does not call the Python implementation.

Run the Python vectors from the SDK root:

```sh
python -m pytest -q tests/test_volume_fingerprint.py
```

Run the independent checker with an already installed compatible Go toolchain
and generated Go module from the standalone protocol PR:

```sh
python scripts/volume_commit_vectors/check_go.py \
  --go-binary /path/to/cached/go1.27.3/bin/go \
  --go-protos-root /path/to/chalk-private/go-protos \
  --vectors tests/fixtures/volume_commit_format1.json
```

The runner creates and removes its own temporary Go module, uses one build
worker, bounds execution to120 seconds, disables module downloads/toolchain
selection, and never writes the supplied generated checkout. Missing cached
dependencies fail instead of installing them. Its report binds checker/vector
hashes. No GPU, sandbox, volume, credentials or RPC is involved.

Canonicalization is request identity only. The fixture hashes/references are
declarations rather than uploaded objects; an accepted vector does not prove
file content integrity or a request's publishability. New verified backend
validation/persistence and actual Catalog race/recovery tests are still required.
There is no public SDK submit API. Never relax a rejection or regenerate expected
bytes merely to make a failing implementation match.

Both projectors pin field tags/names, wire kinds, cardinalities, presence, oneof
membership and referenced value types, and reject retained unknown wire fields.
This guard requires a format review for generated field-schema changes. It does
not replace backend validation. In particular paths remain verbatim request text:
callers must validate the complete batch using the backend's grammar (including
duplicate normalized paths and root-remove policy) before applying any writes.
A canonicalized vector can still be rejected by the backend for those reasons.
