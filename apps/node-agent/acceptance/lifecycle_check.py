#!/usr/bin/env python3
"""Targeted continuation after immutable scope/idempotency/update acceptance.

No API or engine restart. The legacy Hysteria collector runs once per minute,
so activation is observed with a bounded poll after a real native connection.
"""
import argparse
import base64
import datetime as dt
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import secrets
import shlex
import subprocess
import time
from urllib.parse import urlencode

from live_check import API_CODE, NL_SSH, ROOT, RU_SSH, RU_WORKER, SNAPSHOT_CODE, profile_config, remote, save

HERE = Path(__file__).resolve().parent


EGRESS_ATTEST_CODE = r'''
import ipaddress,json,subprocess
addresses=json.loads(subprocess.check_output(['ip','-j','address','show','scope','global']))
global_ips=sorted({a['local'] for dev in addresses for a in dev.get('addr_info',[]) if 'local' in a and ipaddress.ip_address(a['local']).is_global})
result={'global_addresses':global_ips,'direct':{}}
for family in ('--ipv4','--ipv6'):
 p=subprocess.run(['curl','--disable','--silent','--show-error','--fail',family,'--noproxy','*','--connect-timeout','4','--max-time','7','--proto','=https','https://www.cloudflare.com/cdn-cgi/trace'],capture_output=True,timeout=9)
 fields=dict(line.split('=',1) for line in p.stdout.decode(errors='replace').splitlines() if '=' in line)
 result['direct'][family]={'curl_exit':p.returncode,'ip':fields.get('ip'),'hostname':fields.get('h'),'stderr':p.stderr.decode(errors='replace')[:512]}
print(json.dumps(result))
'''


def diagnostic_worker(exit_addresses=("195.133.195.40",)):
    worker = RU_WORKER
    changes = {
        "import concurrent.futures,hashlib,json,os": "import concurrent.futures,hashlib,ipaddress,json,os",
        "'-l','error'": "'-l','info'",
        "[('ipify','https://api.ipify.org'),('example','https://example.com')]": "[('example','https://example.com'),('cloudflare','https://www.cloudflare.com/cdn-cgi/trace'),('ipify','https://api.ipify.org')]",
        "else b'Example Domain' in v.stdout": "else ((b'h=www.cloudflare.com' in v.stdout.splitlines() and any(ipaddress.ip_address(line.split(b'=',1)[1].decode()) in {ipaddress.ip_address(a) for a in " + repr(tuple(exit_addresses)) + "} for line in v.stdout.splitlines() if line.startswith(b'ip='))) if kind=='cloudflare' else b'Example Domain' in v.stdout)",
        "   if not ok:break": "   # Retain every destination outcome even when an earlier endpoint fails.",
        "len(row['checks'])==2": "len(row['checks'])==3",
        "not any(x['verified_pass'] for x in row['checks'])": "not any(x['curl_exit']==0 for x in row['checks'])",
        "   (d/(phase+'-'+name+'-'+kind+'.stderr')).write_bytes(v.stderr)": "   (d/(phase+'-'+name+'-'+kind+'.stderr')).write_bytes(v.stderr)\n   (d/(phase+'-'+name+'-'+kind+'.body')).write_bytes(v.stdout)",
    }
    for old, new in changes.items():
        assert old in worker
        worker = worker.replace(old, new)
    compile(worker, '<diagnostic-worker>', 'exec')
    return worker


def execute(token_file, activation_wait, diagnostic=False):
    os.umask(0o077)
    token = json.loads(token_file.read_text())["operator_token"]
    nonce = secrets.token_hex(6)
    local = ROOT / "private" / ("lifecycle-" + nonce); local.mkdir(mode=0o700)
    directory = "/opt/vpn-lab-ru/runtime/node-life-" + nonce
    units = ["vpn-node-life-" + nonce + "-" + p for p in ("active", "active-reconnect", "suspended", "resumed", "resumed-reconnect", "revoked")]
    result = {"schema": "dual-node-lifecycle-continuation/v1", "started_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
              "checks": [], "native": {}, "accesses": {}, "cleanup": {}, "private_evidence": str(local),
              "api_restarted": False, "prior_proof": "live-activation-observation.json", "diagnostic": diagnostic}
    payloads, ids, hashes = {}, {}, {}; staged = False; count = 0

    def api(method, path, body=None, key=None):
        nonlocal count
        p = remote(NL_SSH, API_CODE, {"method": method, "path": path, "body": body, "key": key, "token": token})
        save(local / ("api-" + str(count).zfill(3) + ".json"), p.stdout); count += 1
        assert p.returncode == 0, "api_transport_failed"
        return json.loads(p.stdout)

    def check(name, value, fatal=True):
        result["checks"].append({"name": name, "pass": bool(value)})
        print(json.dumps({"check": name, "pass": bool(value)}), flush=True)
        if fatal:
            assert value, name

    def native_attempt(phase):
        unit = "vpn-node-life-" + nonce + "-" + phase
        args = ["systemd-run", "--quiet", "--wait", "--pipe", "--collect", "--unit=" + unit,
                "-p", "RuntimeMaxSec=45s", "-p", "TimeoutStopSec=8s", "-p", "CPUQuota=200%", "-p", "MemoryMax=256M", "-p", "TasksMax=96",
                "python3", directory + "/worker.py", directory, phase]
        p = subprocess.run(RU_SSH + [shlex.join(args)], capture_output=True, timeout=60)
        save(local / (phase + ".stdout"), p.stdout); save(local / (phase + ".stderr"), p.stderr)
        assert p.returncode == 0, "native_probe_failed"
        row = json.loads(p.stdout); result["native"][phase] = row
        check(phase + "_cleanup", all(row.get(k) for k in ("children_joined", "ports_released", "host_preserved")))
        return row

    def probe(phase, allowed):
        row = native_attempt(phase)
        passed = len(row["services"]) == 2 and all(x["path_pass"] if allowed else x["fresh_path_denied"] for x in row["services"].values())
        if diagnostic and allowed and not passed:
            result.setdefault("reconnects", []).append(phase)
            row = native_attempt(phase + "-reconnect")
            passed = len(row["services"]) == 2 and all(x["path_pass"] for x in row["services"].values())
        check(phase + "_fresh_paths", passed, fatal=not diagnostic)

    try:
        p = remote(NL_SSH, SNAPSHOT_CODE); assert p.returncode == 0
        before = json.loads(p.stdout); save(local / "nl-before.json", before)
        for service in ("hysteria", "vless-reality"):
            body = {"service_id": service, "node_id": "nl-1", "owner_ref": "lifecycle:" + nonce + ":" + service, "traffic_limit_bytes": 1024**3}
            body.update({"activation_days": 7} if service == "hysteria" else {"expires_at": (dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=1)).isoformat()})
            payloads[service] = body
            created = api("POST", "/v1/accesses", body, nonce + "-create-" + service)
            check(service + "_created", created["status"] == 201)
            access_id = created["body"]["access"]["id"]; ids[service] = access_id
            exported = api("GET", "/v1/accesses/" + access_id + "/profile")
            check(service + "_profile_exported", exported["status"] == 200 and "no-store" in exported["headers"].get("cache-control", ""))
            uri = exported["body"]["uri"]; hashes[service] = hashlib.sha256(uri.encode()).hexdigest()
            save(local / (service + ".json"), profile_config(uri, service))
            result["accesses"][service] = {"id": access_id, "profile_sha256": hashes[service]}
        exit_addresses = ["195.133.195.40"]
        if diagnostic:
            p = remote(NL_SSH, EGRESS_ATTEST_CODE); assert p.returncode == 0
            attested = json.loads(p.stdout); save(local / "nl-egress-attestation.json", attested)
            exit_addresses += attested["global_addresses"]
            for row in attested["direct"].values():
                if row["curl_exit"] == 0 and row["hostname"] == "www.cloudflare.com" and ipaddress.ip_address(row["ip"]).is_global:
                    exit_addresses.append(row["ip"])
            exit_addresses = sorted({str(ipaddress.ip_address(a)) for a in exit_addresses})
            result["attested_nl_exit_addresses"] = exit_addresses
        files = {"worker.py": (diagnostic_worker(exit_addresses) if diagnostic else RU_WORKER).encode(), **{s + ".json": (local / (s + ".json")).read_bytes() for s in ids}}
        stage = "import os,json,pathlib,base64,sys;os.umask(0o077);p=json.load(sys.stdin);d=pathlib.Path(p['directory']);assert d.parent==pathlib.Path('/opt/vpn-lab-ru/runtime') and d.name.startswith('node-life-');d.mkdir(mode=0o700)\nfor n,v in p['files'].items():\n assert n in ('worker.py','hysteria.json','vless-reality.json')\n (d/n).write_bytes(base64.b64decode(v,validate=True))\nprint(json.dumps({'staged':True}))"
        staged = True
        p = remote(RU_SSH, stage, {"directory": directory, "files": {k: base64.b64encode(v).decode() for k, v in files.items()}}); assert p.returncode == 0
        probe("active", True)
        if not diagnostic:
            started = time.monotonic(); observations = []
            while True:
                active = api("GET", "/v1/accesses/" + ids["hysteria"])
                expiry = active.get("body", {}).get("access", {}).get("effective", {}).get("expires_at")
                observations.append({"seconds": round(time.monotonic() - started, 3), "status": active["status"], "effective_expires_at": expiry})
                if expiry is not None or time.monotonic() - started >= activation_wait:
                    break
                time.sleep(min(5, max(0, activation_wait - (time.monotonic() - started))))
            result["activation_observations"] = observations
            check("hysteria_collector_activation_observed", active["status"] == 200 and expiry is not None)
        for service, access_id in ids.items():
            check(service + "_suspended", api("POST", "/v1/accesses/" + access_id + "/suspend", {}, nonce + "-suspend-" + service)["status"] == 200)
        probe("suspended", False)
        for service, access_id in ids.items():
            path = "/v1/accesses/" + access_id
            check(service + "_resumed", api("POST", path + "/resume", {}, nonce + "-resume-" + service)["status"] == 200)
            profile = api("GET", path + "/profile")
            check(service + "_profile_preserved", profile["status"] == 200 and hashlib.sha256(profile["body"]["uri"].encode()).hexdigest() == hashes[service])
        probe("resumed", True)
        for service, access_id in ids.items():
            path = "/v1/accesses/" + access_id
            check(service + "_revoked", api("DELETE", path, key=nonce + "-revoke-" + service)["status"] == 200)
            check(service + "_repeat_revoke", api("DELETE", path, key=nonce + "-revoke-again-" + service)["status"] == 200)
            check(service + "_tombstone", api("GET", path)["body"]["access"]["status"] == "revoked" and api("GET", path + "/profile")["status"] == 409)
        probe("revoked", False)
        names = {s: "vc_" + a.replace("-", "")[:24] for s, a in ids.items()}
        absent_code = "import json,sqlite3,subprocess;names=" + repr(names) + ";c=sqlite3.connect('file:/var/lib/vpn-managed/marzban/db.sqlite3?mode=ro',uri=True);v=c.execute('SELECT count(*) FROM users WHERE username=?',(names['vless-reality'],)).fetchone()[0]==0;c.close();m=\"import pymongo; c=pymongo.MongoClient('mongodb://127.0.0.1:27017/',serverSelectionTimeoutMS=3000);print(c.blitz_panel.users.count_documents({'_id':\"+repr(names['hysteria'])+\"})==0);c.close()\";h=subprocess.check_output(['/etc/hysteria/hysteria2_venv/bin/python','-c',m],timeout=10).strip()==b'True';print(json.dumps({'hysteria':h,'vless-reality':v}))"
        p = remote(NL_SSH, absent_code); assert p.returncode == 0
        result["backend_absent"] = json.loads(p.stdout)
        check("revoked_backends_absent", all(result["backend_absent"].values()))
    except BaseException as error:
        result["error_class"] = type(error).__name__
        result["error_code"] = str(error) if isinstance(error, AssertionError) else "lifecycle_failed"
    finally:
        for service, body in payloads.items():
            try:
                listed = api("GET", "/v1/accesses?" + urlencode({"owner_ref": body["owner_ref"]})); assert listed["status"] == 200
                for row in listed["body"]["accesses"]:
                    if row["owner_ref"] == body["owner_ref"] and row["status"] != "revoked":
                        api("POST", "/v1/accesses", body, nonce + "-create-" + service)
                        assert api("DELETE", "/v1/accesses/" + row["id"], key=nonce + "-cleanup-" + service)["status"] == 200
                result["cleanup"][service] = True
            except Exception:result["cleanup"][service] = False
        if staged:
            collect = "import pathlib,json,base64;d=pathlib.Path(" + repr(directory) + ");print(json.dumps({p.name:base64.b64encode(p.read_bytes()).decode() for p in d.iterdir() if p.is_file() and p.suffix in ('.log','.stderr','.body') and p.stat().st_size<262144}))"
            try:
                p = remote(RU_SSH, collect)
                if p.returncode == 0:
                    for name, raw in json.loads(p.stdout).items():
                        assert Path(name).name == name
                        save(local / name, base64.b64decode(raw, validate=True))
            except Exception:pass
            clean = "import pathlib,shutil,json,subprocess,socket;d=pathlib.Path(" + repr(directory) + ");states=[]\nfor u in " + repr(units) + ":\n subprocess.run(['systemctl','stop',u],capture_output=True)\n states.append(subprocess.run(['systemctl','show',u,'-p','MainPID','-p','ActiveState'],capture_output=True,text=True).stdout)\nports=True\nfor port in (19831,19832):\n try:\n  with socket.socket() as s:s.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1);s.bind(('127.0.0.1',port))\n except OSError:ports=False\nshutil.rmtree(d,ignore_errors=True);print(json.dumps({'directory_removed':not d.exists(),'units_inactive':all('MainPID=0' in s and 'ActiveState=active' not in s for s in states),'ports_released':ports}))"
            p = remote(RU_SSH, clean)
            cleanup = json.loads(p.stdout) if p.returncode == 0 else {}
            result["cleanup"].update({"ru_directory": cleanup.get("directory_removed", False), "ru_units": cleanup.get("units_inactive", False), "ru_ports": cleanup.get("ports_released", False)})
        try:
            p = remote(NL_SSH, SNAPSHOT_CODE); assert p.returncode == 0
            after = json.loads(p.stdout); save(local / "nl-after.json", after)
            result["legacy_preserved"] = before == after
        except Exception:result["legacy_preserved"] = False
        result["first_attempt_paths_pass"] = not result.get("reconnects")
        result["pass"] = len(result["checks"]) >= 24 and all(x["pass"] for x in result["checks"]) and all(result["cleanup"].values()) and result["legacy_preserved"] and "error_class" not in result and result["first_attempt_paths_pass"]
        result["finished_utc"] = dt.datetime.now(dt.timezone.utc).isoformat()
        save(local / "result.json", result)
        (HERE / ("live-lifecycle-diagnostic.json" if diagnostic else "lifecycle-result.json")).write_text(json.dumps(result, indent=2) + "\n")
        print(json.dumps({"complete": True, "pass": result["pass"], "checks": len(result["checks"]), "first_attempt_paths_pass": result["first_attempt_paths_pass"], "error_code": result.get("error_code"), "private_evidence": str(local)}), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--tokens-file", type=Path, default=ROOT / "private/operator-tokens.json")
    parser.add_argument("--activation-wait-seconds", type=int, choices=range(0, 91), default=90)
    parser.add_argument("--diagnostic", action="store_true", help="Separate remaining-lifecycle proof: all destinations, one reconnect, no activation replay")
    args = parser.parse_args()
    if args.execute:execute(args.tokens_file, args.activation_wait_seconds, args.diagnostic)
    else:print(json.dumps({"mutations": False, "restart_api": False, "scope": "remaining lifecycle and collector activation"}))
