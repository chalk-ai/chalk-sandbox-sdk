Add an opt-in SGLang0.5.23 dense BF16/TP1 checkpoint reload adapter and CLI with
verified deterministic Triton runtime settings and native model/version acknowledgements.
Preparation completes before admission closes; updates drain, pause, reload, flush and
resume before advancing the applied stream version. SGLang remains an optional runtime;
unsupported model/parallel/cache settings fail closed. The backend catalog stays disabled.
