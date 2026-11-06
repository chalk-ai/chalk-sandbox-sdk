Add opt-in two-phase direct resident indexed scatter RPCs for the audited Kimi-K3
BF16 norm and TP8 vocabulary head on gfx950. Preparation validates all entries and
stages only sparse values/indices; commit verifies worker/storage identity, writes
only indexed resident elements and fails closed on uncertain mutation. Existing
serving selection and legacy checkpoint-loader behavior are unchanged; quantized
expert updates and native AMD validation remain outside this change.
