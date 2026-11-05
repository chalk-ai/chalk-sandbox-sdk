Add an opt-in experimental SGLang 0.5.23 full-checkpoint reload baseline for
dense BF16 Llama with TP/PP/DP/EP fixed to one. Replay the complete mounted chain
into one local checkpoint before draining; pause, reload, flush and resume before
advancing the applied version. Uncertain native updates fail closed and restart.
SGLang/ROCm remains absent from the backend runtime catalog until device/image
validation; no GPU correctness or performance claim is made by this change.
