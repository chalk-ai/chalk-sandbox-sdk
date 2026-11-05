"""Real local process death/deadline/group tests; no SDK auth or remote calls."""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from profile_sharded_processes import (
    identity,
    members,
    prove_registered_absent,
    stop_group,
)


@pytest.mark.parametrize("fault", ["popen_failure", "exit_before_ready"])
def test_real_inert_spawn_failure_does_not_block_later_resource_release(
    tmp_path, monkeypatch, fault
):
    import profile_sharded_guard as guard
    import profile_sharded_plan as plan

    ledger = tmp_path / "launcher.json"
    record = {"cpu_phase_id": "test-phase", "cpu_deadline_epoch": time.time() + 10}
    ledger.write_text(json.dumps(record))
    monkeypatch.setattr(
        plan, "validate_ledger", lambda path: json.loads(path.read_bytes())
    )
    original_popen = subprocess.Popen

    def spawn(command, *args, **kwargs):
        if "--attempt" in command:
            if fault == "popen_failure":
                raise OSError("controlled spawn failure before execution")
            return original_popen(
                [sys.executable, "-c", "raise SystemExit(3)"], *args, **kwargs
            )
        return original_popen(command, *args, **kwargs)

    monkeypatch.setattr(guard.subprocess, "Popen", spawn)
    with pytest.raises((OSError, RuntimeError)):
        guard.run_attempt(ledger, record, 0)
    root = tmp_path / "cleanup-attempt-0"
    state = json.loads((root / "spawn-state.json").read_bytes())
    assert state["state"] == (
        "never_started" if fault == "popen_failure" else "stopped"
    )
    assert (
        not (root / "ready.json").exists()
        and not (root / "authorization.json").exists()
    )
    calls = []

    def release(path, record, number, **kwargs):
        calls.append(number)
        record.update(
            status="released", cpu_compute_released=True, volume_verified_absent=True
        )
        guard.write_json(path, record)

    monkeypatch.setattr(guard, "run_attempt", release)
    assert guard.cleanup(ledger)["released"] is True and calls == [1]
    prove_registered_absent(ledger)


def test_unknown_never_authorized_inert_attempt_permits_release_but_not_local_proof(
    tmp_path, monkeypatch
):
    import profile_sharded_guard as guard
    import profile_sharded_plan as plan

    ledger = tmp_path / "launcher.json"
    record = {"cpu_phase_id": "test-phase", "cpu_deadline_epoch": time.time() + 10}
    ledger.write_text(json.dumps(record))
    root = tmp_path / "cleanup-attempt-0"
    root.mkdir()
    (root / "request.json").write_text(
        json.dumps(
            {
                "phase_id": "test-phase",
                "ledger": str(ledger),
                "deadline": time.time() - 1,
            }
        )
    )
    monkeypatch.setattr(
        plan, "validate_ledger", lambda path: json.loads(path.read_bytes())
    )

    def release(path, record, number, **kwargs):
        assert number == 1
        record.update(
            status="released", cpu_compute_released=True, volume_verified_absent=True
        )
        guard.write_json(path, record)

    monkeypatch.setattr(guard, "run_attempt", release)
    assert guard.cleanup(ledger)["released"] is True
    resolution = json.loads((root / "inert-resolution.json").read_bytes())
    assert (
        resolution["authorization_absent"] is True
        and resolution["local_identity_proved"] is False
    )
    with pytest.raises(RuntimeError, match="local worker identity remains unproved"):
        prove_registered_absent(ledger)


WORKER = r"""
import json,subprocess,sys,time
from pathlib import Path
from profile_plan import write_json
import profile_sharded_guard as guard
root=Path(sys.argv[1]); request=json.loads((root/'request.json').read_bytes())
if request['inert']: time.sleep(.2)
def fake_blocked_api(ledger):
 (root/'work-authorized').write_text('no actual API')
 child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)'])
 write_json(root/'descendant.json',{'pid':child.pid})
 if request['fault']=='worker-completion': return
 if request['fault']=='worker-failure': raise RuntimeError('simulated API failure with descendant')
 while True: time.sleep(10)
guard.cleanup_attempt=fake_blocked_api
guard.attempt_process(root)
"""


def wait_for(predicate, seconds=5):
    until = time.monotonic() + seconds
    while time.monotonic() < until:
        value = predicate()
        if value:
            return value
        time.sleep(0.02)
    raise AssertionError("Owned local process condition did not finish")


def launch(root, *, inert=False, seconds=3, fault=""):
    root.mkdir()
    supervisor_code = f"""
import json,os,subprocess,sys,time
from pathlib import Path
from profile_plan import write_json
from profile_sharded_processes import identity
root=Path(sys.argv[1])
write_json(root/'request.json',{{'phase_id':'test-phase','ledger':str(root.parent/'launcher.json'),'parent_pid':os.getpid(),'parent_identity':identity(os.getpid()),'deadline':time.time()+{seconds},'inert':{inert!r},'fault':{fault!r}}})
child=subprocess.Popen([sys.executable,'-c',{WORKER!r},str(root)],start_new_session=True)
known=identity(child.pid)
write_json(root/'spawned.json',known)
if not {inert!r}:
 while not (root/'fuse-ready.json').exists(): time.sleep(.01)
 write_json(root/'authorization.json',json.loads((root/'ready.json').read_bytes()))
while True: time.sleep(10)
"""
    return subprocess.Popen(
        [sys.executable, "-c", supervisor_code, str(root)],
        env={**os.environ, "PYTHONPATH": str(Path(__file__).parent)},
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def finish(supervisor, root):
    if supervisor.poll() is None:
        supervisor.kill()
    supervisor.wait(timeout=2)
    if (root / "spawned.json").exists():
        known = json.loads((root / "spawned.json").read_bytes())
        if known is not None:
            stop_group(known)


@pytest.mark.parametrize(
    "fault",
    [
        "supervisor-death",
        "absolute-deadline",
        "inert-parent-death",
        "worker-completion",
        "worker-failure",
    ],
)
def test_independent_monitor_stops_entire_group_during_blocked_work(tmp_path, fault):
    root = tmp_path / "cleanup-attempt-0"
    (tmp_path / "launcher.json").write_text(json.dumps({"cpu_phase_id": "test-phase"}))
    supervisor = launch(
        root, inert=fault == "inert-parent-death", seconds=2, fault=fault
    )
    try:
        wait_for(lambda: (root / "spawned.json").exists())
        known = json.loads((root / "spawned.json").read_bytes())
        if fault == "inert-parent-death":
            supervisor.kill()
            supervisor.wait(timeout=2)
            wait_for(lambda: (root / "ready.json").exists())
        else:
            wait_for(lambda: (root / "descendant.json").exists())
            if fault not in ("worker-completion", "worker-failure"):
                assert len(members(known)) >= 3  # Worker, OS monitor, inherited child.
            if fault == "supervisor-death":
                supervisor.kill()
                supervisor.wait(timeout=2)
        wait_for(lambda: not members(known))
        if fault == "inert-parent-death":
            assert not (root / "work-authorized").exists()
        if fault in ("worker-completion", "worker-failure"):
            result = json.loads((root / "result.json").read_bytes())
            assert result["released"] is (fault == "worker-completion")
        prove_registered_absent(tmp_path / "launcher.json")
    finally:
        finish(supervisor, root)


def test_registry_and_stop_cover_descendant_after_group_leader_exit(tmp_path):
    root = tmp_path / "cleanup-attempt-0"
    root.mkdir()
    script = """
import json,subprocess,sys,time
from pathlib import Path
root=Path(sys.argv[1])
subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)'])
(root/'child-started').write_text('yes')
while not (root/'exit-leader').exists(): time.sleep(.01)
"""
    child = subprocess.Popen(
        [sys.executable, "-c", script, str(root)], start_new_session=True
    )
    known = identity(child.pid)
    assert known is not None
    ledger = tmp_path / "launcher.json"
    ledger.write_text(json.dumps({"cpu_phase_id": "test-phase"}))
    (root / "request.json").write_text(
        json.dumps({"phase_id": "test-phase", "ledger": str(ledger)})
    )
    (root / "ready.json").write_text(
        json.dumps({"phase_id": "test-phase", "identity": known})
    )
    try:
        wait_for(lambda: (root / "child-started").exists())
        (root / "exit-leader").touch()
        child.wait(timeout=2)
        assert identity(child.pid) is None and members(known)
        with pytest.raises(RuntimeError, match="group members remain"):
            prove_registered_absent(ledger)
        stop_group(known)
        prove_registered_absent(ledger)
    finally:
        if child.poll() is None:
            child.kill()
        child.wait(timeout=2)
        stop_group(known)
