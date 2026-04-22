#!/usr/bin/env -S uv run

"""
Run sandbox probes and report whether sandboxing held.

`PASS` means the probe observed the sandbox blocking or hiding the risky action.
`INFO` probes are descriptive only. All other probes are treated as tests.
"""

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from dotenv import load_dotenv

load_dotenv()

from chalkcompute import Container, Image


THIS_DIR = Path(__file__).resolve().parent
DOCKERFILE = THIS_DIR / "Dockerfile"
USE_COLOR = sys.stdout.isatty()

RESET = "\033[0m"
BOLD = "\033[1m"
RED = "\033[31m"
GREEN = "\033[32m"
BLUE = "\033[34m"


@dataclass(frozen=True)
class Probe:
    name: str
    category: str
    is_info: bool
    attacker_goal: str
    command: tuple[str, ...]
    evaluator: str


@dataclass
class ProbeResult:
    probe: Probe
    status: str
    detail: str
    exit_code: int
    output: str


def build_image() -> Image:
    return Image.from_dockerfile(str(DOCKERFILE))


def colorize(text: str, *styles: str) -> str:
    if not USE_COLOR or not styles:
        return text
    return "".join(styles) + text + RESET


def format_status(status: str) -> str:
    if status == "PASS":
        return colorize(status, BOLD, GREEN)
    if status == "FAIL":
        return colorize(status, BOLD, RED)
    if status == "INFO":
        return colorize(status, BOLD, BLUE)
    return colorize(status, BOLD)


def combined_output(stdout: str, stderr: str) -> str:
    text = "\n".join(part for part in (stdout.strip(), stderr.strip()) if part)
    return text.strip()


def compact(text: str, limit: int = 120) -> str:
    single = " ".join(text.split())
    if len(single) <= limit:
        return single
    return single[: limit - 3] + "..."


def has_any(text: str, *needles: str) -> bool:
    lowered = text.lower()
    return any(needle.lower() in lowered for needle in needles)


def first_match(text: str, prefixes: tuple[str, ...]) -> str:
    for line in text.splitlines():
        for prefix in prefixes:
            if line.startswith(prefix):
                return line.strip()
    return ""


def evaluate(probe: Probe, output: str, exit_code: int) -> tuple[str, str]:
    if probe.evaluator == "deny_helper":
        blocked = exit_code != 0 or has_any(
            output,
            "operation not permitted",
            "permission denied",
            "function not implemented",
            "no such device",
            "failed",
            "denied",
        )
        succeeded = has_any(
            output,
            "SUCCESS",
            "ALLOWED",
            "Watching ",
            "kernel loaded successfully",
        )
        if blocked and not succeeded:
            return "PASS", compact(output)
        return "FAIL", compact(output or f"command exited {exit_code}")

    if probe.evaluator == "deny_simple":
        if exit_code != 0:
            return "PASS", compact(output or f"command exited {exit_code}")
        return "FAIL", compact(output or "command succeeded")

    if probe.evaluator == "quota_hidden":
        if has_any(output, "Kernel quota version"):
            return "FAIL", compact(output)
        if has_any(
            output,
            "Operation not permitted",
            "Error while getting old quota statistics",
        ):
            return "PASS", compact(output)
        return "FAIL", compact(output or f"command exited {exit_code}")

    if probe.evaluator == "caps_zero":
        if (
            "CapEff:\t0000000000000000" in output
            and "CapBnd:\t0000000000000000" in output
        ):
            return "PASS", compact(
                " | ".join(
                    filter(
                        None,
                        (
                            first_match(output, ("CapEff:",)),
                            first_match(output, ("CapBnd:",)),
                            first_match(output, ("Seccomp:",)),
                        ),
                    )
                )
            )
        return "FAIL", compact(
            " | ".join(
                filter(
                    None,
                    (
                        first_match(output, ("CapEff:",)),
                        first_match(output, ("CapBnd:",)),
                        first_match(output, ("Seccomp:",)),
                    ),
                )
            )
        )

    if probe.evaluator == "raw_socket_blocked":
        if has_any(output, "SUCCESS"):
            return "FAIL", compact(output)
        if has_any(output, "Operation not permitted", "Permission denied"):
            return "PASS", compact(output)
        return "FAIL", compact(output or f"command exited {exit_code}")

    if probe.evaluator == "no_host_devices":
        if has_any(output, "/dev/kcore", "/dev/mem", "/dev/port"):
            return "FAIL", compact(output)
        return "PASS", compact(output or "/dev/kcore,/dev/mem,/dev/port all absent")

    if probe.evaluator == "sys_kernel_hidden":
        if output.strip() == "0":
            return "PASS", "/sys/kernel is empty"
        return "FAIL", compact(f"/sys/kernel entries visible: {output.strip()}")

    if probe.evaluator == "no_host_block_mounts":
        if output.strip() == "0":
            return "PASS", "no host block-device mounts exposed"
        return "FAIL", compact(f"host block-device mounts visible: {output.strip()}")

    if probe.evaluator == "info_processes":
        lines = [line for line in output.splitlines() if line.strip()]
        if len(lines) > 1:
            return "INFO", compact(f"visible processes: {max(len(lines) - 1, 0)}")
        return "INFO", compact(output or "no process output")

    if probe.evaluator == "info_capsh":
        return "INFO", compact(
            " | ".join(
                filter(
                    None,
                    (
                        first_match(output, ("Current:",)),
                        first_match(output, ("Bounding set",)),
                    ),
                )
            )
        )

    if probe.evaluator == "capsh_locked":
        required = (
            "no-new-privs",
            "secure-noroot",
            "secure-no-suid-fixup",
            "secure-keep-caps",
        )
        missing = []
        for item in required:
            if not re.search(
                rf"{re.escape(item)}\s*[:=]\s*.*\b(?:1|yes|locked)\b",
                output,
                re.IGNORECASE,
            ):
                missing.append(item)
        if not missing:
            return "PASS", compact(
                " | ".join(
                    (
                        "no-new-privs=1",
                        "secure-noroot locked",
                        "secure-no-suid-fixup locked",
                        "secure-keep-caps locked",
                    )
                )
            )
        return "FAIL", compact("missing: " + ", ".join(missing))

    if probe.evaluator == "info_df":
        fs_types = []
        for line in output.splitlines()[1:]:
            fields = line.split()
            if len(fields) >= 2:
                fs_types.append(fields[1])
        detail = ", ".join(dict.fromkeys(fs_types[:6]))
        return "INFO", compact(detail or output)

    if probe.evaluator == "info_run_tree":
        if has_any(output, "serviceaccount", "token"):
            return "FAIL", "service account token present"
        return "PASS", "service account token absent"

    if probe.evaluator == "info_kubectl":
        if exit_code == 0:
            return "FAIL", "API credentials available"
        return "PASS", compact(output or f"kubectl exited {exit_code}")

    if probe.evaluator == "info_dmesg":
        if has_any(output, "starting gvisor"):
            return "INFO", "synthetic gVisor dmesg visible"
        if has_any(output, "operation not permitted"):
            return "INFO", "kernel dmesg blocked"
        return "INFO", compact(output or f"dmesg exited {exit_code}")

    raise ValueError(f"unknown evaluator: {probe.evaluator}")


INFO_PROBES: list[Probe] = [
    # Shows what other processes are visible from the current workload.
    Probe(
        name="visible-processes",
        category="workload visibility",
        is_info=True,
        attacker_goal="Inspect what else is running nearby.",
        command=("ps", "-faux"),
        evaluator="info_processes",
    ),
    # Records filesystem types and mount backing visible from the workload.
    Probe(
        name="filesystem-types",
        category="host visibility",
        is_info=True,
        attacker_goal="Infer what backing filesystems and host mounts are present.",
        command=("df", "-Th"),
        evaluator="info_df",
    ),
    # Records what dmesg visibility exists from inside the workload.
    Probe(
        name="dmesg",
        category="host visibility",
        is_info=True,
        attacker_goal="Read kernel logs.",
        command=("dmesg",),
        evaluator="info_dmesg",
    ),
]


DECISIVE_PROBES: list[Probe] = [
    # Fails if service account credentials are mounted into the workload.
    Probe(
        name="runtime-layout",
        category="kubernetes context",
        is_info=False,
        attacker_goal="Use pod-mounted Kubernetes credentials from the runtime filesystem.",
        command=("eza", "-al", "--tree", "/var/run", "/run"),
        evaluator="info_run_tree",
    ),
    # Fails if the workload can successfully authenticate to the Kubernetes API.
    Probe(
        name="cluster-api-credentials",
        category="kubernetes context",
        is_info=False,
        attacker_goal="Use the pod's Kubernetes API credentials.",
        command=("kubectl", "auth", "can-i", "--list"),
        evaluator="info_kubectl",
    ),
    # Tests access to pidfd-based process handles.
    Probe(
        name="pidfd-open",
        category="process inspection",
        is_info=False,
        attacker_goal="Get kernel-backed handles for processes that can support deeper process introspection/control patterns.",
        command=("./pidfd_open",),
        evaluator="deny_helper",
    ),
    # Tests access to the io_uring helper binary directly.
    Probe(
        name="io-uring-helper",
        category="kernel attack surface",
        is_info=False,
        attacker_goal="Reach complex async I/O kernel interfaces that have a large historical bug surface.",
        command=("./io_uring",),
        evaluator="deny_helper",
    ),
    # Tests io_uring through the multiplexed syscall probe.
    Probe(
        name="io-uring-syscall",
        category="kernel attack surface",
        is_info=False,
        attacker_goal="Reach io_uring directly through the raw syscall path.",
        command=("./syscalls", "io_uring"),
        evaluator="deny_helper",
    ),
    # Tests whether host quota/kernel accounting state is observable.
    Probe(
        name="quota-stats",
        category="host visibility",
        is_info=False,
        attacker_goal="Read host filesystem quota/kernel accounting state.",
        command=("quotastats",),
        evaluator="quota_hidden",
    ),
    # Tests for sensitive device nodes that would expose host/kernel internals.
    Probe(
        name="sensitive-devices-hidden",
        category="host visibility",
        is_info=False,
        attacker_goal="Reach sensitive kernel pseudo-devices that expose host memory or kernel state.",
        command=(
            "sh",
            "-lc",
            "for p in /dev/kcore /dev/mem /dev/port; do [ -e \"$p\" ] && echo \"$p\"; done",
        ),
        evaluator="no_host_devices",
    ),
    # Tests whether the workload can browse host kernel sysfs state.
    Probe(
        name="sys-kernel-hidden",
        category="host visibility",
        is_info=False,
        attacker_goal="Browse host kernel configuration and runtime internals through sysfs.",
        command=("sh", "-lc", "find /sys/kernel -mindepth 1 -maxdepth 1 | wc -l"),
        evaluator="sys_kernel_hidden",
    ),
    # Tests whether host-backed block device mounts are visible in /proc/mounts.
    Probe(
        name="host-block-devices-hidden",
        category="host visibility",
        is_info=False,
        attacker_goal="Identify host-backed block devices and real host mount sources.",
        command=("sh", "-lc", "grep -Ec '^/dev/(sd|vd|xvd|nvme)' /proc/mounts"),
        evaluator="no_host_block_mounts",
    ),
]


HARDENING_PROBES: list[Probe] = [
    # Confirms whether root inside the container still has effective capabilities.
    Probe(
        name="proc-status-capabilities",
        category="privilege baseline",
        is_info=False,
        attacker_goal="Use ambient root privileges inside the container.",
        command=("cat", "/proc/self/status"),
        evaluator="caps_zero",
    ),
    # Tests whether no-new-privs and the securebits locks are fully enabled.
    Probe(
        name="capsh-lockdown",
        category="privilege baseline",
        is_info=False,
        attacker_goal="Retain privilege-escalation paths through suid, ambient caps, or relaxed securebits.",
        command=("capsh", "--print"),
        evaluator="capsh_locked",
    ),
    # Tests whether the workload can open a raw packet socket.
    Probe(
        name="af-packet-raw-socket",
        category="packet capture",
        is_info=False,
        attacker_goal="Sniff or inject packets with a raw link-layer socket.",
        command=("./rawsock",),
        evaluator="raw_socket_blocked",
    ),
    # Tests BPF map creation via the raw syscall path.
    Probe(
        name="bpf",
        category="kernel attack surface",
        is_info=False,
        attacker_goal="Load or manipulate BPF objects inside the kernel.",
        command=("./syscalls", "bpf"),
        evaluator="deny_helper",
    ),
    # Tests perf_event_open via the raw syscall path.
    Probe(
        name="perf-event-open",
        category="kernel attack surface",
        is_info=False,
        attacker_goal="Use kernel perf interfaces for observation or attack research.",
        command=("./syscalls", "perf"),
        evaluator="deny_helper",
    ),
    # Tests userfaultfd via the raw syscall path.
    Probe(
        name="userfaultfd",
        category="kernel attack surface",
        is_info=False,
        attacker_goal="Use userfaultfd-driven memory tricks against the kernel or neighboring workloads.",
        command=("./syscalls", "uffd"),
        evaluator="deny_helper",
    ),
    # Tests whether filesystem event monitoring can be initialized.
    Probe(
        name="fanotify",
        category="filesystem observation",
        is_info=False,
        attacker_goal="Watch filesystem activity from the kernel side.",
        command=(
            "sh",
            "-lc",
            "./fanotify /tmp >/tmp/fanotify.out 2>&1 & pid=$!; sleep 1; kill $pid 2>/dev/null; wait $pid; rc=$?; cat /tmp/fanotify.out; exit $rc",
        ),
        evaluator="deny_helper",
    ),
    # Tests whether the workload can request a kexec kernel load.
    Probe(
        name="kexec-file-load",
        category="host takeover",
        is_info=False,
        attacker_goal="Load a new kernel image.",
        command=(
            "sh",
            "-lc",
            "dd if=/dev/zero of=/tmp/fake-kernel bs=1M count=1 >/dev/null 2>&1 && ./kexec_load /tmp/fake-kernel; rc=$?; rm -f /tmp/fake-kernel; exit $rc",
        ),
        evaluator="deny_helper",
    ),
    # Tests whether a loop-backed mount can be created from inside the workload.
    Probe(
        name="loop-mount",
        category="filesystem control",
        is_info=False,
        attacker_goal="Create a new mount backed by a loop device.",
        command=(
            "sh",
            "-lc",
            "mkdir -p /mnt/data && dd if=/dev/zero of=/tmp/demo.img bs=1M count=64 >/dev/null 2>&1 && mkfs.ext4 -F /tmp/demo.img >/dev/null 2>&1 && mount -o loop /tmp/demo.img /mnt/data; rc=$?; umount /mnt/data >/dev/null 2>&1; rm -f /tmp/demo.img; rmdir /mnt/data; exit $rc",
        ),
        evaluator="deny_simple",
    ),
    # Tests whether a tmpfs mount can be created directly.
    Probe(
        name="tmpfs-mount",
        category="filesystem control",
        is_info=False,
        attacker_goal="Create arbitrary mounts inside the container.",
        command=(
            "sh",
            "-lc",
            "mkdir -p /tmp/mnt && mount -t tmpfs tmpfs /tmp/mnt; rc=$?; umount /tmp/mnt >/dev/null 2>&1; rmdir /tmp/mnt; exit $rc",
        ),
        evaluator="deny_simple",
    ),
    # Tests whether swap can be enabled from inside the workload.
    Probe(
        name="swapon",
        category="host resource control",
        is_info=False,
        attacker_goal="Enable swap and perturb host memory management.",
        command=(
            "sh",
            "-lc",
            "dd if=/dev/zero of=/tmp/swapfile bs=1M count=1 >/dev/null 2>&1 && chmod 0600 /tmp/swapfile >/dev/null 2>&1 && mkswap /tmp/swapfile >/dev/null 2>&1 && swapon /tmp/swapfile; rc=$?; rm -f /tmp/swapfile; exit $rc",
        ),
        evaluator="deny_simple",
    ),
    # Tests whether link-level network administration is possible.
    Probe(
        name="link-admin",
        category="network control",
        is_info=False,
        attacker_goal="Bring interfaces up or down.",
        command=("ip", "link", "set", "lo", "down"),
        evaluator="deny_simple",
    ),
    # Tests whether the workload can trigger a forced reboot.
    Probe(
        name="reboot",
        category="host takeover",
        is_info=False,
        attacker_goal="Force a reboot.",
        command=("reboot", "-f"),
        evaluator="deny_simple",
    ),
]


PROBES: list[Probe] = INFO_PROBES + DECISIVE_PROBES + HARDENING_PROBES


def run_probe(container: Container, probe: Probe) -> ProbeResult:
    result = container.exec(*probe.command, timeout_secs=30)
    output = combined_output(result.stdout_text, result.stderr_text)
    status, detail = evaluate(probe, output, result.exit_code)
    return ProbeResult(
        probe=probe,
        status=status,
        detail=detail,
        exit_code=result.exit_code,
        output=output,
    )


def print_probe(result: ProbeResult, verbose: bool = False) -> None:
    probe = result.probe
    print(f"[{format_status(result.status):<13}] {probe.name} ({probe.category})")
    print(f"  attacker capability: {probe.attacker_goal}")
    print(f"  command: {' '.join(probe.command)}")
    print(f"  result: {result.detail}")
    if verbose:
        print("  output:")
        if result.output:
            for line in result.output.splitlines():
                print(f"    {line}")
        else:
            print("    (no output)")


def print_summary(results: list[ProbeResult]) -> None:
    info = [r for r in results if r.probe.is_info]
    tests = [r for r in results if not r.probe.is_info]
    tests_pass = sum(r.status == "PASS" for r in tests)

    print("\nSummary")
    print(f"  tests:  {tests_pass}/{len(tests)} PASS")
    print(f"  info:   {len(info)} observed")

    failed_tests = [r.probe.name for r in tests if r.status != "PASS"]
    if failed_tests:
        print(f"  verdict: {format_status('FAIL')}")
        print(f"  failing tests: {', '.join(failed_tests)}")
    else:
        print(f"  verdict: {format_status('PASS')}")
        print("  all sandbox tests passed")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--image",
        help="Use an existing image URI instead of building from examples/sandboxed/Dockerfile.",
    )
    parser.add_argument(
        "--name",
        default=f"sandbox-test-{uuid4().hex[:8]}",
        help="Container name prefix.",
    )
    parser.add_argument(
        "--container-id",
        help="Attach to an existing running container by ID instead of creating one.",
    )
    parser.add_argument(
        "--container-name",
        help="Attach to an existing running container by name instead of creating one.",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Print full stdout/stderr for every probe.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    if args.container_id and args.container_name:
        raise SystemExit("pass only one of --container-id or --container-name")

    created_container = False
    if args.container_id:
        container = Container.from_id(args.container_id)
    elif args.container_name:
        container = Container.from_name(args.container_name)
    else:
        image: Image | str = args.image if args.image else build_image()
        container = Container(image=image, name=args.name)
        container.run()
        created_container = True

    try:
        results = [run_probe(container, probe) for probe in PROBES]
    finally:
        if created_container:
            container.stop()

    for result in results:
        print_probe(result, verbose=args.verbose)

    print_summary(results)
    return 1 if any(not r.probe.is_info and r.status != "PASS" for r in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
