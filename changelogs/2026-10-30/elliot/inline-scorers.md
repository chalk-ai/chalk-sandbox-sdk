Scorer factories, including `cc.scorers.llm_judge`, can now be called inside
setup functions and `if __name__ == "__main__"` blocks. Deployments capture the
generated scorer and its configuration instead of requiring the worker to
replay the factory call when importing the caller's module.

For inline judges, response models and custom prompt/parse functions must be
importable definitions in the shipped source. Existing import-time judges
using lambdas or locally defined models retain their previous loading behavior.
