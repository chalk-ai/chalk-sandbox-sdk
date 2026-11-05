"""Scoped read-only process/IO progress; never reads argv or environment."""

from __future__ import annotations

import argparse
import json
import shlex
import subprocess
from pathlib import Path

from training_smoke import ResourceGuard

PROBE = r"""
import json,os,time
from pathlib import Path

def sample():
    raw={}
    for path in Path('/proc').glob('[0-9]*'):
        try:
            stat=(path/'stat').read_text()
            values=stat[stat.rfind(')')+2:].split()
            status={line.split(':',1)[0]:line.split(':',1)[1].strip() for line in (path/'status').read_text().splitlines() if line.startswith(('VmRSS:','Threads:'))}
            io={line.split(':',1)[0]:int(line.split(':',1)[1]) for line in (path/'io').read_text().splitlines() if line.startswith(('rchar:','syscr:','read_bytes:'))}
            raw[int(path.name)]={'pid':int(path.name),'ppid':int(values[1]),'name':(path/'comm').read_text().strip(),'state':values[0],'utime':int(values[11]),'stime':int(values[12]),'start_ticks':int(values[19]),'rss_kib':int(status.get('VmRSS','0 kB').split()[0]),'threads':int(status.get('Threads','0')),**io}
        except (OSError,ValueError,IndexError):
            pass
    def owned(pid):
        seen=set()
        while pid not in seen and pid in raw:
            if pid==1:return True
            seen.add(pid);pid=raw[pid]['ppid']
        return False
    processes=[row for pid,row in raw.items() if owned(pid)]
    listeners={}
    for file in ('tcp','tcp6'):
        try:
            for line in Path('/proc/net/'+file).read_text().splitlines()[1:]:
                values=line.split()
                port=int(values[1].rsplit(':',1)[1],16)
                if values[3]=='0A' and port in (8000,8001):listeners[values[9]]=port
        except OSError:pass
    for row in processes:
        ports=set()
        try:
            for fd in Path('/proc/'+str(row['pid'])+'/fd').iterdir():
                try:
                    link=os.readlink(fd)
                    if link.startswith('socket:[') and link[8:-1] in listeners:ports.add(listeners[link[8:-1]])
                except OSError:pass
        except OSError:pass
        row['listening_ports']=sorted(ports)
    return {'time':time.time(),'processes':processes}

first=sample();time.sleep(2);second=sample()
previous={row['pid']:row for row in first['processes']}
for row in second['processes']:
    prior=previous.get(row['pid'])
    if prior and prior['start_ticks']==row['start_ticks']:
        row['delta']={key:row[key]-prior[key] for key in ('utime','stime','rchar','syscr','read_bytes')}
rows=second['processes']
second['sample_seconds']=second['time']-first['time']
second['native_api_listening']=any(8001 in row['listening_ports'] for row in rows)
second['engine_core_name_present']=any('engine' in row['name'].lower() for row in rows)
second['owned_child_count']=sum(row['pid']!=1 for row in rows)
second['owned_rchar_delta']=sum(row.get('delta',{}).get('rchar',0) for row in rows)
second['owned_rchar_bytes_per_second']=second['owned_rchar_delta']/second['sample_seconds']
print(json.dumps(second))
"""


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ledger", type=Path, required=True)
    parser.add_argument("--context", required=True)
    parser.add_argument("--ssh-host", required=True)
    parser.add_argument("--group-name", required=True)
    parser.add_argument("--replica", required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    record = json.loads(args.ledger.read_text())
    guard = ResourceGuard(
        record["name"], args.ledger.parent, record, args.context, args.context
    )
    group_id = record["group_ids"][args.group_name]
    groups = guard.kube_items(
        "chalkscalinggroups", name="sg-" + group_id, context=args.context
    )
    assert len(groups) == 1
    metadata = groups[0]["metadata"]
    assert metadata["namespace"] in record["expected_namespaces"]
    children = guard.kube_items(
        "chalkcontainers", owner=metadata["uid"], context=args.context
    )
    assert any(item["metadata"]["name"] == args.replica for item in children)
    command = [
        "sudo",
        "docker",
        "exec",
        "chalk-external-hypervisor",
        "runsc",
        "--root=/var/lib/chalk-hypervisor/runsc",
        "exec",
        args.replica,
        "python3",
        "-c",
        PROBE,
    ]
    result = subprocess.run(
        [
            "ssh",
            "-o",
            "BatchMode=yes",
            "-o",
            "ConnectTimeout=10",
            args.ssh_host,
            shlex.join(command),
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    proof = json.loads(result.stdout)
    proof["group_id"] = group_id
    proof["replica"] = args.replica
    proof["seconds_since_budget_start"] = (
        proof["time"] - record["gpu_budget_started_at"]
    )
    args.out.write_text(json.dumps(proof, indent=2))
    print(json.dumps(proof, indent=2))


if __name__ == "__main__":
    main()
