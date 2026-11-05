"""Read-only live host and hypervisor cgroup capacity gates before CPU creation."""

from __future__ import annotations

import json

import profile_host_guard as host

CPU = 16
MEMORY = 128 * 1024**3
DISK = 120 * 1024**3

# Executed on the fixed HOST only. No filesystem mutation, image or model reads.
CAPACITY_CODE = r"""
import json,os,sys
from pathlib import Path
pid=int(sys.argv[1]); assert pid>0
mem={v.split(':')[0]:int(v.split()[1])*1024 for v in Path('/proc/meminfo').read_text().splitlines() if v.startswith(('MemTotal:','MemAvailable:'))}
lines=Path(f'/proc/{pid}/cgroup').read_text().splitlines()
v2=[v.split(':',2)[2] for v in lines if v.startswith('0::')]
assert len(v2)==1, 'Require cgroupv2 capacity proof'
root=Path('/sys/fs/cgroup'); current=root/v2[0].lstrip('/')
assert current.is_relative_to(root)
limits=[]
while True:
 item={'path':str(current)}
 for field in ('memory.max','memory.current','cpu.max','cpuset.cpus.effective'):
  p=current/field
  if p.exists(): item[field]=p.read_text().strip()
 limits.append(item)
 if current==root: break
 current=current.parent
mounts=[]
for line in Path('/proc/self/mountinfo').read_text().splitlines():
 fields=line.split(); sep=fields.index('-')
 mounts.append({'mountpoint':fields[4],'filesystem':fields[sep+1],'source':fields[sep+2],'device':fields[2]})
filesystems={}
for p in ('/var/lib/docker','/tmp'):
 s=os.statvfs(p); m=max((m for m in mounts if p==m['mountpoint'] or p.startswith(m['mountpoint'].rstrip('/')+'/')),key=lambda m:len(m['mountpoint']))
 filesystems[p]={'free_bytes':s.f_bavail*s.f_frsize,'total_bytes':s.f_blocks*s.f_frsize,'mount':m}
# These map the hypervisor's known writable roots without reading guest/model data.
hypervisor_paths={}
for p in ('/tmp','/var/lib/chalk-hypervisor','/var/lib/chalk-hypervisor/containerd','/run/containerd'):
 target=Path(f'/proc/{pid}/root')/p.lstrip('/')
 if target.exists():
  s=os.statvfs(target);hypervisor_paths[p]={'free_bytes':s.f_bavail*s.f_frsize,'device':os.stat(target).st_dev,'total_bytes':s.f_blocks*s.f_frsize}
print(json.dumps({'memory':mem,'cpu_affinity_count':len(os.sched_getaffinity(pid)),'hypervisor_pid':pid,'cgroup_v2':limits,'filesystems':filesystems,'hypervisor_paths':hypervisor_paths,'guest_tmp_mapping_requires_guest_rootfs_proof':True}))
"""


def validate_capacity(value):
    if value["memory"]["MemAvailable"] < MEMORY:
        raise RuntimeError("Host available memory below CPU128GiB limit")
    if value["cpu_affinity_count"] < CPU:
        raise RuntimeError("Hypervisor CPU affinity below16CPU")
    if value["filesystems"]["/var/lib/docker"]["free_bytes"] < DISK:
        raise RuntimeError("Host free disk below preserved120GiB gate")
    limits = value.get("cgroup_v2")
    if not isinstance(limits, list) or not limits:
        raise RuntimeError("Missing hypervisor cgroup capacity proof")
    for item in limits:
        maximum, used = item.get("memory.max"), item.get("memory.current")
        if maximum is not None and maximum != "max":
            if used is None or int(maximum) - int(used) < MEMORY:
                raise RuntimeError(
                    "Hypervisor ancestor cgroup memory headroom below128GiB"
                )
        quota = item.get("cpu.max")
        if quota:
            amount, period = quota.split()
            if amount != "max" and int(amount) < CPU * int(period):
                raise RuntimeError("Hypervisor ancestor cgroup CPU quota below16CPU")
    return value


def collect_raw():
    pid = host.ssh(
        [
            "sudo",
            "-n",
            "docker",
            "inspect",
            "chalk-external-hypervisor",
            "--format",
            "{{.State.Pid}}",
        ]
    ).strip()
    if not pid.isdigit() or int(pid) <= 0:
        raise RuntimeError("Missing exact hypervisor PID")
    value = json.loads(host.ssh(["sudo", "-n", "python3", "-c", CAPACITY_CODE, pid]))
    return value


def collect():
    return validate_capacity(collect_raw())
