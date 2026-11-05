from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path


spec = spec_from_file_location(
    "diagnose_weight_workload",
    Path(__file__).parents[1] / "scripts" / "diagnose_weight_workload.py",
)
diagnostics = module_from_spec(spec)
spec.loader.exec_module(diagnostics)


def test_visible_ancestor_quota_is_minimum(tmp_path):
    proc, cgroup = tmp_path / "proc", tmp_path / "cgroup"
    (proc / "1").mkdir(parents=True)
    (proc / "1" / "cgroup").write_text("0::/parent/leaf\n")
    leaf = cgroup / "parent" / "leaf"
    leaf.mkdir(parents=True)
    (cgroup / "cpu.max").write_text("max 100000")
    (cgroup / "parent" / "cpu.max").write_text("1600000 100000")
    (leaf / "cpu.max").write_text("3200000 100000")
    (leaf / "memory.max").write_text("4096")
    (leaf / "cpu.stat").write_text("nr_throttled 7\nthrottled_usec 9000")
    result = diagnostics.cpu_context(1, proc, cgroup)
    assert result["minimum_visible_quota_cpus"] == 16
    assert result["cgroup_v2"][-1]["memory_max"] == 4096
    assert result["cgroup_v2"][-1]["cpu_stat"]["nr_throttled"] == 7
    assert "cgroup_membership" not in result


def test_thread_names_and_ticks_ignore_command_and_environment(tmp_path):
    proc = tmp_path / "proc"
    task = proc / "1" / "task" / "7"
    task.mkdir(parents=True)
    (task / "comm").write_text("tokio-runtime-w")
    fields = ["S"] + ["0"] * 20
    fields[11], fields[12] = "11", "13"
    (task / "stat").write_text("7 (name with spaces) " + " ".join(fields))
    (proc / "1" / "cmdline").write_text("credential-never-read")
    (proc / "1" / "environ").write_text("SECRET=credential-never-read")
    result = diagnostics.diagnose(1, proc, tmp_path / "cgroup", 0)
    assert result["thread_count"] == 1
    assert result["thread_groups"]["tokio-runtime-w"]["count"] == 1
    assert "credential-never-read" not in str(result)
    assert diagnostics.threads(1, proc)[7]["cpu_ticks"] == 24


def test_unlimited_and_invalid_quota_is_not_reported_as_finite():
    for value in [None, "max 100000", "-1 100000", "10 0", "invalid"]:
        assert diagnostics.parse_cpu_max(value) is None
    assert diagnostics.parse_cpu_max("50000 100000") == 0.5
