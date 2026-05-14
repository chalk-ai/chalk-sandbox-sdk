"""
Polyglot code judge and sandbox safety demo built on Chalk containers.

Three acts that show what you can do when arbitrary untrusted code runs in
an isolated container: judge submissions, contain dangerous scripts, and
execute AI-generated code without reviewing it first.

## Run the demo

    python examples/polyglot/polyglot.py

  Act 1 — Code Judge (Python, Java, C++)
    Judges a "sum N integers" problem across three submissions.
    Python/Java pass all test cases. C++ has an infinite loop — TIMEOUT x3,
    killed cleanly after 5s each, no cleanup needed.

  Act 2 — Dangerous Code, Run Safely
    Scripts that would damage a real machine, run inside a container:
      rm -rf /       → exit=0,   container filesystem wiped, host untouched
      fork bomb      → killed,   PID namespace contained the explosion
      100 GB alloc   → exit=137, OOM-killed before touching host RAM
      read ~/.ssh    → exit=0,   host filesystem not visible inside container

  Act 3 — GPT-4o Writes Code, Sandbox Runs It
    GPT-4o generates solutions in Python, Java, and C++. Each is piped
    directly into judge() without human review. Requires OPENAI_API_KEY.

## How it works

Each submission gets one container (cold-start paid once, not per test case):

1. Source is base64-encoded and written into the container — avoids shell
   quoting limits that would break long Java/C++ sources.
2. Compiled once (javac / g++). Failure → COMPILE_ERROR on all test cases.
3. Each test case runs via container.exec() with a per-exec timeout. The
   container stays alive between cases; stopped in a finally block.

Verdicts:
    PASS          — stdout matches expected (whitespace-stripped)
    WRONG_ANSWER  — exited cleanly but output didn't match
    RUNTIME_ERROR — non-zero exit code
    TIMEOUT       — process killed after time_limit_secs; container survives

## Library usage

    from run_code import judge, run_once, Language, TestCase

    results = judge(
        Language.PYTHON,
        source_code,
        [TestCase(stdin="3\\n1\\n2\\n3", expected="6", label="small")],
        time_limit_secs=5,
    )
    for r in results:
        print(r.verdict, r.stdout)

    result = run_once(Language.CPP, cpp_source)
    print(result.stdout)
"""

from __future__ import annotations

import base64
import os
import sys
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from chalkcompute import Container


# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------

class Language(str, Enum):
    PYTHON = "python"
    NODEJS = "nodejs"
    JAVA = "java"
    CPP = "cpp"


class Verdict(str, Enum):
    PASS = "PASS"
    WRONG_ANSWER = "WRONG_ANSWER"
    RUNTIME_ERROR = "RUNTIME_ERROR"
    TIMEOUT = "TIMEOUT"
    COMPILE_ERROR = "COMPILE_ERROR"


@dataclass
class TestCase:
    stdin: str
    expected: str
    label: str = ""


@dataclass
class ExecutionResult:
    language: Language
    stdout: str
    stderr: str
    exit_code: int
    duration_secs: float

    @property
    def success(self) -> bool:
        return self.exit_code == 0


@dataclass
class JudgeResult:
    test_case: TestCase
    verdict: Verdict
    stdout: str
    stderr: str
    duration_secs: float

    def __str__(self) -> str:
        label = f" ({self.test_case.label})" if self.test_case.label else ""
        line = f"  {self.verdict.value:<14}{label}  [{self.duration_secs:.1f}s]"
        if self.verdict == Verdict.WRONG_ANSWER:
            line += f"\n    expected: {self.test_case.expected!r}"
            line += f"\n    got:      {self.stdout.strip()!r}"
        elif self.verdict in (Verdict.RUNTIME_ERROR, Verdict.COMPILE_ERROR):
            first_err = self.stderr.strip().splitlines()[0] if self.stderr.strip() else "(no stderr)"
            line += f"\n    {first_err}"
        return line


# ---------------------------------------------------------------------------
# Language configs
# ---------------------------------------------------------------------------
# Each language writes source to a temp file, compiles if needed, then runs
# with stdin piped in. This pattern works uniformly for all four languages
# and avoids heredoc-vs-pipe conflicts.

def _shell_quote(s: str) -> str:
    return "'" + s.replace("'", "'\\''") + "'"


def _b64write(src: str, dest: str) -> str:
    """Shell command that base64-decodes ``src`` into ``dest`` inside the container.

    Avoids shell quoting limits — base64 is plain ASCII with no special chars.
    """
    encoded = base64.b64encode(src.encode()).decode()
    return f"printf '%s' '{encoded}' | base64 -d > {dest}"


def _java_wrap(src: str) -> str:
    """Wrap a bare code snippet in a Main class if needed."""
    if "class " in src:
        return src
    return (
        "import java.util.*;\n"
        "import java.util.stream.*;\n"
        "public class Main {\n"
        "    public static void main(String[] args) throws Exception {\n"
        "        Scanner sc = new Scanner(System.in);\n"
        f"        {src}\n"
        "    }\n"
        "}"
    )


_CONFIGS: dict[Language, dict] = {
    Language.PYTHON: {
        "image": "python:3.12-slim",
        "compile_cmd": lambda src: _b64write(src, "/tmp/prog.py"),
        "compile_timeout": 10,
        "run_cmd": lambda stdin: (
            f"printf '%s' {_shell_quote(stdin)} | python3 /tmp/prog.py"
        ),
        "run_timeout": 10,
    },
    Language.NODEJS: {
        "image": "node:20-slim",
        "compile_cmd": lambda src: _b64write(src, "/tmp/prog.js"),
        "compile_timeout": 10,
        "run_cmd": lambda stdin: (
            f"printf '%s' {_shell_quote(stdin)} | node /tmp/prog.js"
        ),
        "run_timeout": 10,
    },
    Language.JAVA: {
        "image": "eclipse-temurin:21-jdk",
        # Compile once with a long timeout — javac cold-start on JDK can be slow.
        # Each test case then just runs the already-compiled class.
        "compile_cmd": lambda src: (
            f"{_b64write(_java_wrap(src), '/tmp/Main.java')} && "
            "javac /tmp/Main.java -d /tmp"
        ),
        "compile_timeout": 60,
        "run_cmd": lambda stdin: (
            f"printf '%s' {_shell_quote(stdin)} | java -cp /tmp Main"
        ),
        "run_timeout": 10,
    },
    Language.CPP: {
        "image": "gcc:13",
        # Compile once, run per test case.
        "compile_cmd": lambda src: (
            f"{_b64write(src, '/tmp/main.cpp')} && "
            "g++ -O2 -o /tmp/main /tmp/main.cpp"
        ),
        "compile_timeout": 30,
        "run_cmd": lambda stdin: (
            f"printf '%s' {_shell_quote(stdin)} | /tmp/main"
        ),
        "run_timeout": 10,
    },
}


# ---------------------------------------------------------------------------
# Core: run one piece of code with stdin
# ---------------------------------------------------------------------------

def compile_source(
    language: Language,
    source: str,
    *,
    container: Container,
) -> ExecutionResult:
    """Compile ``source`` inside the container (write + build step).

    For interpreted languages this just writes the source file.
    Returns an ``ExecutionResult`` — callers should check ``exit_code`` before running.
    """
    cfg = _CONFIGS[language]
    cmd = cfg["compile_cmd"](source)
    t0 = time.monotonic()
    result = container.exec("bash", "-c", cmd, timeout_secs=cfg["compile_timeout"])
    return ExecutionResult(
        language=language,
        stdout=result.stdout_text,
        stderr=result.stderr_text,
        exit_code=result.exit_code,
        duration_secs=time.monotonic() - t0,
    )


def run_code(
    language: Language,
    stdin: str = "",
    *,
    container: Container,
    timeout_secs: int | None = None,
) -> ExecutionResult:
    """Run the already-compiled program inside ``container``, feeding ``stdin``.

    Call ``compile_source`` first. The caller owns the container lifecycle.
    """
    cfg = _CONFIGS[language]
    timeout = timeout_secs or cfg["run_timeout"]
    cmd = cfg["run_cmd"](stdin)
    t0 = time.monotonic()
    result = container.exec("bash", "-c", cmd, timeout_secs=timeout)
    return ExecutionResult(
        language=language,
        stdout=result.stdout_text,
        stderr=result.stderr_text,
        exit_code=result.exit_code,
        duration_secs=time.monotonic() - t0,
    )


# ---------------------------------------------------------------------------
# Judge: run a submission against a list of test cases
# ---------------------------------------------------------------------------

def judge(
    language: Language,
    source: str,
    test_cases: list[TestCase],
    *,
    time_limit_secs: int = 5,
) -> list[JudgeResult]:
    """Judge ``source`` against each test case.

    Starts one container per submission and runs all test cases inside it via
    exec — paying the cold-start cost once, not per test case. The container
    is stopped after all test cases complete.

    The per-exec timeout ensures infinite loops are killed cleanly without
    affecting the container itself or adjacent test cases.

    Verdicts:
        ``PASS``          — output matches expected (stripped)
        ``WRONG_ANSWER``  — ran successfully but output didn't match
        ``RUNTIME_ERROR`` — non-zero exit code
        ``TIMEOUT``       — exec hit ``time_limit_secs``
        ``COMPILE_ERROR`` — compile step failed (Java/C++ only, detected via stderr)
    """
    cfg = _CONFIGS[language]
    container = Container(image=cfg["image"]).run()

    results: list[JudgeResult] = []
    try:
        # --- Compile once ---
        compile_result = compile_source(language, source, container=container)
        if compile_result.exit_code != 0:
            # Fail all test cases with COMPILE_ERROR immediately.
            for tc in test_cases:
                results.append(JudgeResult(
                    test_case=tc,
                    verdict=Verdict.COMPILE_ERROR,
                    stdout="",
                    stderr=compile_result.stderr,
                    duration_secs=compile_result.duration_secs,
                ))
            return results

        # --- Run each test case ---
        for tc in test_cases:
            try:
                exec_result = run_code(
                    language,
                    stdin=tc.stdin,
                    container=container,
                    timeout_secs=time_limit_secs,
                )
            except Exception as exc:
                results.append(JudgeResult(
                    test_case=tc,
                    verdict=Verdict.TIMEOUT,
                    stdout="",
                    stderr=str(exc),
                    duration_secs=time_limit_secs,
                ))
                continue

            stdout = exec_result.stdout
            stderr = exec_result.stderr
            duration = exec_result.duration_secs

            if exec_result.exit_code != 0:
                verdict = Verdict.RUNTIME_ERROR
            elif stdout.strip() == tc.expected.strip():
                verdict = Verdict.PASS
            else:
                verdict = Verdict.WRONG_ANSWER

            results.append(JudgeResult(
                test_case=tc,
                verdict=verdict,
                stdout=stdout,
                stderr=stderr,
                duration_secs=duration,
            ))
    finally:
        container.stop()

    return results


# ---------------------------------------------------------------------------
# One-shot convenience wrapper (used by the safety demo)
# ---------------------------------------------------------------------------

def run_once(
    language: Language,
    source: str,
    stdin: str = "",
    *,
    timeout_secs: int = 10,
) -> ExecutionResult:
    """Spin up a container, run source once, stop the container, return the result."""
    cfg = _CONFIGS[language]
    container = Container(image=cfg["image"]).run()
    try:
        compile_result = compile_source(language, source, container=container)
        if compile_result.exit_code != 0:
            return compile_result
        return run_code(language, stdin=stdin, container=container, timeout_secs=timeout_secs)
    finally:
        container.stop()


# ---------------------------------------------------------------------------
# Act 1 — Judge demo: sum-of-array problem across 3 languages
# ---------------------------------------------------------------------------
#
# Correct Python, correct Java, and a C++ submission with an infinite loop.
# Shows PASS, COMPILE_ERROR, and TIMEOUT verdicts.

TEST_CASES = [
    TestCase(stdin="3\n1\n2\n3",            expected="6",   label="small"),
    TestCase(stdin="5\n10\n20\n30\n40\n50", expected="150", label="medium"),
    TestCase(stdin="1\n42",                 expected="42",  label="single element"),
]

JUDGE_SUBMISSIONS: list[tuple[Language, str, str]] = [
    (
        Language.PYTHON,
        "correct solution",
        """\
import sys
lines = sys.stdin.read().split()
n = int(lines[0])
print(sum(int(x) for x in lines[1:n+1]))
""",
    ),
    (
        Language.JAVA,
        "correct solution",
        """\
public class Main {
    public static void main(String[] args) throws Exception {
        java.util.Scanner sc = new java.util.Scanner(System.in);
        int n = sc.nextInt();
        long sum = 0;
        for (int i = 0; i < n; i++) sum += sc.nextLong();
        System.out.println(sum);
    }
}
""",
    ),
    (
        Language.CPP,
        "infinite loop — never reaches return",
        """\
#include <iostream>
int main() {
    int n; std::cin >> n;
    long long sum = 0;
    for (int i = 0; i < n; i++) {
        int x; std::cin >> x;
        sum += x;
    }
    while (true) {}
    std::cout << sum << std::endl;
}
""",
    ),
]


# ---------------------------------------------------------------------------
# Act 2 — Safety demo: code that would destroy a laptop, run safely
# ---------------------------------------------------------------------------
#
# Each snippet below would cause real damage if run on your machine.
# Inside a Chalk container it runs in a fully isolated filesystem and process
# namespace. When the container stops, every effect disappears.

SAFETY_SUBMISSIONS: list[tuple[str, str]] = [
    (
        "rm -rf /  (wipe the entire filesystem)",
        """\
import subprocess, os
print("About to delete everything...")
subprocess.run(["rm", "-rf", "/"], check=False)
print("Done. Container filesystem is gone.")
""",
    ),
    (
        "fork bomb  (would freeze your machine)",
        """\
import os
print("Forking...")
while True:
    os.fork()
""",
    ),
    (
        "memory bomb  (would OOM your laptop)",
        """\
print("Allocating 100 GB...")
x = b"x" * (100 * 1024 ** 3)
print("Done.")
""",
    ),
    (
        "read ~/.ssh/id_rsa  (host filesystem is not visible)",
        """\
import os, pathlib
# These paths exist on the host Mac but not inside a Linux container.
# If isolation is working, all of them will fail.
targets = [
    os.path.expanduser("~/.ssh/id_rsa"),
    "/Users",
    "/System",
]
for path in targets:
    exists = pathlib.Path(path).exists()
    print(f"  {path}: {'VISIBLE (bad!)' if exists else 'not visible (good)'}")
""",
    ),
]


# ---------------------------------------------------------------------------
# Act 3 — AI-generated submissions: GPT writes code, sandbox runs it
# ---------------------------------------------------------------------------
#
# The whole point of a sandbox: you don't need to trust the code you run.
# Ask an LLM to generate solutions, pipe them straight into judge() without
# reading them. The sandbox handles whatever comes out.
#
# We ask GPT-4o for solutions in three languages and one deliberately broken
# version — then judge all of them against the same test cases.

PROBLEM_STATEMENT = """\
Write a solution to this competitive programming problem:

  Given N integers, one per line (first line is N, then N integers),
  print their sum.

  Example input:
    3
    1
    2
    3
  Example output:
    6

Return ONLY the raw source code with no explanation, no markdown, no code fences.
The code must read from stdin and print to stdout.
"""

AI_LANGUAGES: list[tuple[Language, str]] = [
    (Language.PYTHON, "Write it in Python 3."),
    (Language.JAVA,   "Write it in Java. The public class must be named Main."),
    (Language.CPP,    "Write it in C++. Include all necessary headers."),
]


def generate_solution(extra_instruction: str) -> str:
    """Ask GPT-4o to write a solution for ``PROBLEM_STATEMENT``.

    Uses httpx directly (already a chalkcompute dependency) so no extra
    packages are required beyond what the SDK already installs.
    """
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY environment variable not set")

    response = httpx.post(
        "https://api.openai.com/v1/chat/completions",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json={
            "model": "gpt-4o",
            "temperature": 0,
            "messages": [
                {
                    "role": "system",
                    "content": "You are an expert competitive programmer. Return only raw source code — no markdown, no explanation.",
                },
                {
                    "role": "user",
                    "content": f"{PROBLEM_STATEMENT}\n{extra_instruction}",
                },
            ],
        },
        timeout=30,
    )
    response.raise_for_status()
    return response.json()["choices"][0]["message"]["content"].strip()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    # --- Act 1: Judge ---
    print("=" * 50)
    print("  Act 1: Code Judge")
    print("  Problem: sum N integers")
    print("=" * 50 + "\n")

    for lang, description, source in JUDGE_SUBMISSIONS:
        print(f"[{lang.value}] {description}")
        results = judge(lang, source, TEST_CASES, time_limit_secs=5)
        for r in results:
            print(r)
        passed = sum(1 for r in results if r.verdict == Verdict.PASS)
        print(f"  → {passed}/{len(TEST_CASES)} test cases passed\n")

    # --- Act 2: Safety ---
    print("=" * 50)
    print("  Act 2: Dangerous Code, Run Safely")
    print("  Each snippet below would damage a real machine.")
    print("  Inside a Chalk container: fully contained.")
    print("=" * 50 + "\n")

    for description, source in SAFETY_SUBMISSIONS:
        print(f"Running: {description}")
        try:
            result = run_once(Language.PYTHON, source, timeout_secs=5)
            outcome = "contained" if result.exit_code != 0 else "ran safely"
            print(f"  exit={result.exit_code}  [{outcome}]")
            if result.stdout.strip():
                for line in result.stdout.strip().splitlines():
                    print(f"  stdout: {line}")
            if result.stderr.strip():
                first = result.stderr.strip().splitlines()[0]
                print(f"  stderr: {first}")
        except Exception as exc:
            print(f"  killed by sandbox: {exc}")
        print()

    # --- Act 3: AI-generated code ---
    print("=" * 50)
    print("  Act 3: GPT-4o Writes Code, Sandbox Runs It")
    print("  No human reviews the generated code before execution.")
    print("=" * 50 + "\n")

    for lang, instruction in AI_LANGUAGES:
        print(f"[{lang.value}] asking GPT-4o for a solution...")
        try:
            source = generate_solution(instruction)
        except Exception as exc:
            print(f"  generation failed: {exc}\n")
            continue

        # Print the first line of what GPT produced so the audience can see it's real code.
        first_line = source.splitlines()[0] if source else "(empty)"
        print(f"  generated ({len(source.splitlines())} lines): {first_line} ...")

        results = judge(lang, source, TEST_CASES, time_limit_secs=10)
        for r in results:
            print(r)
        passed = sum(1 for r in results if r.verdict == Verdict.PASS)
        print(f"  → {passed}/{len(TEST_CASES)} test cases passed\n")


if __name__ == "__main__":
    main()
