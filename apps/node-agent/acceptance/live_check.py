#!/usr/bin/env python3
"""Opt-in NL node API acceptance with disposable owned accesses and native RU clients.

Default invocation only describes the test. Bearers and client credentials are
sent on SSH stdin and retained only in ignored private files. No billing or
legacy access is mutated. --restart-api restarts only the new API process.
"""
import argparse
import base64
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import secrets
import shlex
import subprocess
import time
from urllib.parse import parse_qs, unquote, urlencode, urlsplit
from uuid import UUID

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
NL = "195.133.195.40"
COMMON = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=10", "-o", "ConnectionAttempts=1",
          "-o", "StrictHostKeyChecking=yes", "-o", "IdentitiesOnly=yes", "-o", "ForwardAgent=no",
          "-i", "/home/andriyshkoy/.ssh/id_ed25519_codex"]
NL_SSH = ["ssh", *COMMON, "root@" + NL]
JUMP = shlex.join(["ssh", *COMMON, "-W", "%h:%p", "codex@185.250.251.170"])
RU_SSH = ["ssh", *COMMON, "-o", "HostKeyAlias=clashmc.gg", "-o", "ProxyCommand=" + JUMP, "root@185.135.83.128"]

API_CODE = r'''
import json,sys,urllib.request,urllib.error
p=json.load(sys.stdin);h={'Accept':'application/json'}
if p.get('token'):h['Authorization']='Bearer '+p['token']
if p.get('key'):h['Idempotency-Key']=p['key']
body=json.dumps(p['body']).encode() if p.get('body') is not None else None
if body is not None:h['Content-Type']='application/json'
req=urllib.request.Request('http://127.0.0.1:18010'+p['path'],data=body,headers=h,method=p['method'])
op=urllib.request.build_opener(urllib.request.ProxyHandler({}))
try:r=op.open(req,timeout=35)
except urllib.error.HTTPError as e:r=e
raw=r.read(131073)
if len(raw)>131072:raise RuntimeError('api_response_oversized')
print(json.dumps({'status':r.status,'headers':{k.lower():v for k,v in r.headers.items()},'body':json.loads(raw)}))
'''

SNAPSHOT_CODE = r'''
import hashlib,json,pathlib,sqlite3,subprocess
def digest(x):return hashlib.sha256(json.dumps(x,sort_keys=True,default=str,separators=(',',':')).encode()).hexdigest()
c=sqlite3.connect('file:/var/lib/vpn-managed/marzban/db.sqlite3?mode=ro',uri=True);c.row_factory=sqlite3.Row
cols={x['name'] for x in c.execute('PRAGMA table_info(users)')}
fields=[x for x in ('id','username','status','expire','data_limit','note') if x in cols]
users=[dict(x) for x in c.execute('SELECT '+','.join(fields)+' FROM users ORDER BY id')]
proxies=[dict(x) for x in c.execute('SELECT * FROM proxies ORDER BY id')];c.close()
mongo="import hashlib,json,pymongo;c=pymongo.MongoClient('mongodb://127.0.0.1:27017/',serverSelectionTimeoutMS=3000);a=[{k:r.get(k) for k in ('_id','password','blocked','max_download_bytes','expiration_days','unlimited_user','note')} for r in c.blitz_panel.users.find({}).sort('_id')];print(json.dumps({'count':len(a),'sha256':hashlib.sha256(json.dumps(a,sort_keys=True,default=str,separators=(',',':')).encode()).hexdigest()}));c.close()"
m=subprocess.run(['/etc/hysteria/hysteria2_venv/bin/python','-c',mongo],capture_output=True,text=True,check=True,timeout=10)
units=['hysteria-server.service','hysteria-auth.service','hysteria-caddy.service','vpn-managed.service']
s=subprocess.run(['systemctl','show',*units,'-p','Id','-p','MainPID','-p','ActiveEnterTimestampMonotonic'],capture_output=True,text=True,check=True)
top=subprocess.run(['/opt/vpn-managed/runtime/docker/docker','--host','unix:///run/vpn-managed-docker/docker.sock','top','vpn-managed-marzban','-eo','pid,comm'],capture_output=True,text=True,check=True,timeout=10).stdout
files=['/etc/hysteria/config.json','/etc/hysteria/core/scripts/webpanel/Caddyfile','/var/lib/vpn-managed/marzban/xray_config.json']
print(json.dumps({'vless_count':len(users),'vless_users_sha256':digest(users),'vless_proxies_sha256':digest(proxies),'hysteria':json.loads(m.stdout),'services':s.stdout,'managed_processes':top,'files':{p:hashlib.sha256(pathlib.Path(p).read_bytes()).hexdigest() for p in files}}))
'''

RU_WORKER = r'''
import concurrent.futures,hashlib,json,os,pathlib,signal,socket,subprocess,sys,time
os.umask(0o077);d=pathlib.Path(sys.argv[1]);phase=sys.argv[2]
def snapshot():
 return {'routes4':json.loads(subprocess.check_output(['ip','-j','-4','route','show','table','main'])),'routes6':json.loads(subprocess.check_output(['ip','-j','-6','route','show','table','main'])),'dns_sha256':hashlib.sha256(pathlib.Path('/etc/resolv.conf').read_bytes()).hexdigest()}
before=snapshot();children=[];logs=[];result={'phase':phase,'services':{}}
def probe(name):
 port={'hysteria':19831,'vless-reality':19832}[name];binary='/opt/vpn-lab-ru/bin/'+('hysteria' if name=='hysteria' else 'xray')
 pin={'hysteria':'6493dfffd55b5883f64c76c63880ecc32988f0c568c9ca9014907877b4d55f94','vless-reality':'8255dd939c34cf966cc91517b6324dd3c8d0bcf49ffac8beca049a38c46845ed'}[name]
 assert hashlib.sha256(pathlib.Path(binary).read_bytes()).hexdigest()==pin
 with socket.socket() as s:s.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1);s.bind(('127.0.0.1',port))
 log=(d/(phase+'-'+name+'.log')).open('xb');logs.append(log)
 args=[binary,'--disable-update-check','-l','error','client','-c',str(d/(name+'.json'))] if name=='hysteria' else [binary,'run','-config',str(d/(name+'.json'))]
 child=subprocess.Popen(args,stdin=subprocess.DEVNULL,stdout=log,stderr=subprocess.STDOUT);children.append(child)
 start=time.monotonic();ready=False
 while time.monotonic()-start<12 and child.poll() is None:
  try:
   with socket.create_connection(('127.0.0.1',port),timeout=.1):ready=True;break
  except OSError:time.sleep(.05)
 row={'listener_ready':ready,'checks':[]}
 if ready:
  for kind,url in [('ipify','https://api.ipify.org'),('example','https://example.com')]:
   t=time.monotonic();v=subprocess.run(['curl','--disable','--silent','--show-error','--fail','--ipv4','--http1.1','--noproxy','','--proxy','socks5h://127.0.0.1:'+str(port),'--connect-timeout','4','--max-time','7','--proto','=https','--max-filesize','65536',url],capture_output=True,timeout=9)
   ok=v.returncode==0 and (v.stdout.strip()==b'195.133.195.40' if kind=='ipify' else b'Example Domain' in v.stdout)
   row['checks'].append({'kind':kind,'curl_exit':v.returncode,'seconds':round(time.monotonic()-t,3),'bytes':len(v.stdout),'verified_pass':ok})
   (d/(phase+'-'+name+'-'+kind+'.stderr')).write_bytes(v.stderr)
   if not ok:break
 row['path_pass']=ready and len(row['checks'])==2 and all(x['verified_pass'] for x in row['checks'])
 row['fresh_path_denied']=not row['path_pass'] and not any(x['verified_pass'] for x in row['checks'])
 row['seconds']=round(time.monotonic()-start,3)
 return name,row
try:
 with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:result['services']=dict(pool.map(probe,['hysteria','vless-reality']))
except BaseException as e:result['error_class']=type(e).__name__
finally:
 for p in children:
  if p.poll() is None:
   p.terminate()
   try:p.wait(timeout=4)
   except subprocess.TimeoutExpired:p.kill();p.wait(timeout=2)
 for log in logs:log.close()
 result['children_joined']=all(p.poll() is not None for p in children)
 result['host_preserved']=before==snapshot();result['ports_released']=True
 for port in (19831,19832):
  try:
   with socket.socket() as s:s.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1);s.bind(('127.0.0.1',port))
  except OSError:result['ports_released']=False
 print(json.dumps(result))
'''


def save(path, value):
    raw = value if isinstance(value, bytes) else (json.dumps(value, indent=2) + "\n").encode()
    with path.open("xb") as out:
        out.write(raw)
    path.chmod(0o600)


def remote(ssh, code, payload=None, timeout=45):
    return subprocess.run(ssh + [shlex.join(["python3", "-c", code])],
                          input=json.dumps(payload).encode() if payload is not None else None,
                          capture_output=True, timeout=timeout)


def profile_config(uri, service):
    u = urlsplit(uri); q = parse_qs(u.query)
    assert u.hostname == NL
    if service == "hysteria":
        assert u.scheme in ("hy2", "hysteria2") and u.port == 1935
        pin = q["pinSHA256"][0]
        assert len(pin.replace(":", "")) == 64 and q["obfs"] == ["salamander"]
        return {"server": NL + ":1935", "auth": unquote(u.netloc.rsplit("@", 1)[0]),
                "obfs": {"type": "salamander", "salamander": {"password": q["obfs-password"][0]}},
                "tls": {"sni": q["sni"][0], "insecure": q.get("insecure") == ["1"], "pinSHA256": pin},
                "lazy": False, "fastOpen": False, "socks5": {"listen": "127.0.0.1:19831"}}
    assert u.scheme == "vless" and u.port == 9443 and q["security"] == ["reality"] and q["flow"] == ["xtls-rprx-vision"]
    return {"log": {"loglevel": "warning"}, "inbounds": [{"listen": "127.0.0.1", "port": 19832, "protocol": "socks", "settings": {"auth": "noauth", "udp": False}}],
            "outbounds": [{"protocol": "vless", "mux": {"enabled": False}, "settings": {"vnext": [{"address": NL, "port": 9443, "users": [{"id": str(UUID(u.username)), "encryption": "none", "flow": "xtls-rprx-vision"}]}]},
                           "streamSettings": {"network": "tcp", "security": "reality", "realitySettings": {"serverName": q["sni"][0], "fingerprint": q["fp"][0], "publicKey": q["pbk"][0], "shortId": q["sid"][0]}}}]}


def execute(tokens_file, restart_api=False):
    os.umask(0o077)
    tokens = json.loads(tokens_file.read_text())
    operator, readonly = tokens["operator_token"], tokens["readonly_token"]
    nonce = secrets.token_hex(6); local = ROOT / "private" / ("acceptance-" + nonce)
    local.mkdir(mode=0o700)
    directory = "/opt/vpn-lab-ru/runtime/node-api-" + nonce
    result = {"schema": "dual-node-live-acceptance/v1", "started_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
              "checks": [], "native": {}, "accesses": {}, "cleanup": {}, "private_evidence": str(local)}
    created = {}; payloads = {}; count = 0; staged = False

    def api(method, path, body=None, key=None, token=operator):
        nonlocal count
        p = remote(NL_SSH, API_CODE, {"method": method, "path": path, "body": body, "key": key, "token": token})
        save(local / ("api-" + str(count).zfill(3) + ".json"), p.stdout);count += 1
        assert p.returncode == 0, "api_transport_failed"
        return json.loads(p.stdout)

    def check(name, value):
        result["checks"].append({"name": name, "pass": bool(value)})
        print(json.dumps({"check": name, "pass": bool(value)}), flush=True)
        assert value, name

    def await_ready():
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            try:
                if api("GET", "/readyz")["status"] == 200:
                    return
            except AssertionError:
                pass
            time.sleep(.25)
        raise AssertionError("api_not_ready_after_restart")

    def probe(phase, allowed):
        unit = "vpn-node-accept-" + nonce + "-" + phase
        args = ["systemd-run", "--quiet", "--wait", "--pipe", "--collect", "--unit=" + unit,
                "-p", "RuntimeMaxSec=45s", "-p", "TimeoutStopSec=8s", "-p", "CPUQuota=200%", "-p", "MemoryMax=256M", "-p", "TasksMax=96",
                "python3", directory + "/worker.py", directory, phase]
        p = subprocess.run(RU_SSH + [shlex.join(args)], capture_output=True, timeout=60)
        save(local / (phase + ".stdout"), p.stdout); save(local / (phase + ".stderr"), p.stderr)
        assert p.returncode == 0, "native_probe_failed"
        row = json.loads(p.stdout); result["native"][phase] = row
        check(phase + "_native_cleanup", all(row.get(k) for k in ("children_joined", "ports_released", "host_preserved")))
        check(phase + "_fresh_paths", len(row["services"]) == 2 and all(v["path_pass"] if allowed else v["fresh_path_denied"] for v in row["services"].values()))

    try:
        before = remote(NL_SSH, SNAPSHOT_CODE); assert before.returncode == 0
        before = json.loads(before.stdout); save(local / "nl-before.json", before)
        check("unauthenticated_denied", api("GET", "/v1/services", token=None)["status"] == 401)
        check("readonly_write_denied", api("POST", "/v1/accesses", {"service_id": "hysteria"}, nonce + "-readonly", readonly)["status"] == 403)
        check("services_ready", api("GET", "/readyz")["status"] == 200)
        for service in ("hysteria", "vless-reality"):
            payload = {"service_id": service, "node_id": "nl-1", "owner_ref": "acceptance:" + nonce + ":" + service, "traffic_limit_bytes": 1024**3}
            payload.update({"activation_days": 7} if service == "hysteria" else {"expires_at": (dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=1)).isoformat()})
            payloads[service] = payload
            key = nonce + "-create-" + service
            first = api("POST", "/v1/accesses", payload, key)
            check(service + "_created", first["status"] == 201)
            access_id = first["body"]["access"]["id"]; created[service] = access_id
            second = api("POST", "/v1/accesses", payload, key)
            check(service + "_create_replay", second["status"] == 201 and second["body"] == first["body"] and second["headers"].get("idempotency-replayed") == "true")
            conflict = api("POST", "/v1/accesses", {**payload, "traffic_limit_bytes": 2 * 1024**3}, key)
            check(service + "_key_conflict", conflict["status"] == 409)
            path = "/v1/accesses/" + access_id
            check(service + "_profile_scope", api("GET", path + "/profile", token=readonly)["status"] == 403)
            profile = api("GET", path + "/profile")
            check(service + "_secret_export", profile["status"] == 200 and "no-store" in profile["headers"].get("cache-control", ""))
            uri = profile["body"]["uri"]; cfg = profile_config(uri, service)
            save(local / (service + ".json"), cfg)
            result["accesses"][service] = {"id": access_id, "profile_sha256": hashlib.sha256(uri.encode()).hexdigest()}
            usage = api("GET", path + "/usage")
            get = api("GET", path)
            listing = api("GET", "/v1/accesses?" + urlencode({"owner_ref": payload["owner_ref"]}))
            check(service + "_metadata_no_secrets", usage["status"] == get["status"] == listing["status"] == 200 and uri not in json.dumps([usage, get, listing]) and listing["body"]["total"] == 1)
            if service == "hysteria":
                effective = get["body"]["access"]["effective"]
                check("hysteria_activation_deferred", effective["expires_at"] is None and effective["activation_days"] == 7)
        if restart_api:
            code = "import json,subprocess; b=subprocess.check_output(['systemctl','show','vpn-control-api.service','-p','MainPID','--value'],text=True).strip();subprocess.run(['systemctl','restart','vpn-control-api.service'],check=True);a=subprocess.check_output(['systemctl','show','vpn-control-api.service','-p','MainPID','--value'],text=True).strip();print(json.dumps({'before':b,'after':a}))"
            p = remote(NL_SSH, code, timeout=40); assert p.returncode == 0
            result["api_restart"] = json.loads(p.stdout)
            check("api_new_process", result["api_restart"]["before"] != result["api_restart"]["after"])
            await_ready()
            for service, payload in payloads.items():
                replay = api("POST", "/v1/accesses", payload, nonce + "-create-" + service)
                check(service + "_durable_replay", replay["status"] == 201 and replay["body"]["access"]["id"] == created[service] and replay["headers"].get("idempotency-replayed") == "true")
        for service, access_id in created.items():
            path = "/v1/accesses/" + access_id
            update = {"traffic_limit_bytes": 2 * 1024**3}
            key = nonce + "-update-" + service
            updated = api("PATCH", path, update, key)
            check(service + "_quota_updated", updated["status"] == 200 and updated["body"]["access"]["effective"]["traffic_limit_bytes"] == update["traffic_limit_bytes"])
            repeated = api("PATCH", path, update, key)
            check(service + "_update_replay", repeated["status"] == 200 and repeated["headers"].get("idempotency-replayed") == "true")
            exported = api("GET", path + "/profile")
            check(service + "_update_preserves_profile", exported["status"] == 200 and hashlib.sha256(exported["body"]["uri"].encode()).hexdigest() == result["accesses"][service]["profile_sha256"])
            if service == "hysteria":
                check("hysteria_update_keeps_activation_deferred", updated["body"]["access"]["effective"]["expires_at"] is None)
        files = {"worker.py": RU_WORKER.encode(), **{s + ".json": (local / (s + ".json")).read_bytes() for s in created}}
        stage = "import os,json,pathlib,base64,sys;os.umask(0o077);p=json.load(sys.stdin);d=pathlib.Path(p['directory']);assert d.parent==pathlib.Path('/opt/vpn-lab-ru/runtime') and d.name.startswith('node-api-');d.mkdir(mode=0o700)\nfor n,v in p['files'].items():\n assert n in ('worker.py','hysteria.json','vless-reality.json')\n (d/n).write_bytes(base64.b64decode(v,validate=True))\nprint(json.dumps({'staged':True}))"
        staged = True; p = remote(RU_SSH, stage, {"directory": directory, "files": {k: base64.b64encode(v).decode() for k, v in files.items()}}); assert p.returncode == 0
        probe("active", True)
        # Legacy collector records activation once per minute after online/bytes;
        # successful native transport alone does not synchronously write a date.
        activation_started = time.monotonic(); observations = []
        while True:
            active_hys = api("GET", "/v1/accesses/" + created["hysteria"])
            expiry = active_hys.get("body", {}).get("access", {}).get("effective", {}).get("expires_at")
            observations.append({"seconds": round(time.monotonic() - activation_started, 3), "status": active_hys["status"], "effective_expires_at": expiry})
            if expiry is not None or time.monotonic() - activation_started >= 90:
                break
            time.sleep(min(5, max(0, 90 - (time.monotonic() - activation_started))))
        result["activation_observations"] = observations
        check("hysteria_activated_by_native_connection", active_hys["status"] == 200 and expiry is not None)
        for service, access_id in created.items():
            check(service + "_suspended", api("POST", "/v1/accesses/" + access_id + "/suspend", {}, nonce + "-suspend-" + service)["status"] == 200)
        probe("suspended", False)
        for service, access_id in created.items():
            path = "/v1/accesses/" + access_id
            check(service + "_resumed", api("POST", path + "/resume", {}, nonce + "-resume-" + service)["status"] == 200)
            profile = api("GET", path + "/profile")
            check(service + "_identity_preserved", profile["status"] == 200 and hashlib.sha256(profile["body"]["uri"].encode()).hexdigest() == result["accesses"][service]["profile_sha256"])
        probe("resumed", True)
        for service, access_id in created.items():
            path = "/v1/accesses/" + access_id
            check(service + "_revoked", api("DELETE", path, key=nonce + "-revoke-" + service)["status"] == 200)
            check(service + "_repeat_revoke", api("DELETE", path, key=nonce + "-revoke-again-" + service)["status"] == 200)
            check(service + "_revoked_tombstone", api("GET", path)["body"]["access"]["status"] == "revoked" and api("GET", path + "/profile")["status"] == 409)
        probe("revoked", False)
    except BaseException as error:
        result["error_class"] = type(error).__name__
        result["error_code"] = str(error) if isinstance(error, AssertionError) else "acceptance_failed"
    finally:
        for service, payload in payloads.items():
            try:
                listing = api("GET", "/v1/accesses?" + urlencode({"owner_ref": payload["owner_ref"]}))
                assert listing["status"] == 200, "cleanup_inventory_unavailable"
                for row in listing.get("body", {}).get("accesses", []):
                    if row["owner_ref"] == payload["owner_ref"] and row["status"] != "revoked":
                        api("POST", "/v1/accesses", payload, nonce + "-create-" + service)
                        answer = api("DELETE", "/v1/accesses/" + row["id"], key=nonce + "-cleanup-" + service)
                        assert answer["status"] == 200
                result["cleanup"][service] = True
            except Exception:
                result["cleanup"][service] = False
        if staged:
            code = "import base64,json,pathlib;d=pathlib.Path(" + repr(directory) + ");print(json.dumps({p.name:base64.b64encode(p.read_bytes()).decode() for p in d.iterdir() if p.is_file() and p.suffix in ('.log','.stderr') and p.stat().st_size<262144}))"
            try:
                evidence = remote(RU_SSH, code)
                if evidence.returncode == 0:
                    for name, encoded in json.loads(evidence.stdout).items():
                        assert Path(name).name == name
                        save(local / name, base64.b64decode(encoded, validate=True))
            except Exception:
                pass
            units = ["vpn-node-accept-" + nonce + "-" + phase for phase in ("active", "suspended", "resumed", "revoked")]
            code = "import pathlib,shutil,json,socket,subprocess;d=pathlib.Path(" + repr(directory) + ");units=" + repr(units) + ";states=[]\nfor u in units:\n subprocess.run(['systemctl','stop',u],capture_output=True)\n states.append(subprocess.run(['systemctl','show',u,'-p','MainPID','-p','ActiveState'],capture_output=True,text=True).stdout)\nports=True\nfor port in (19831,19832):\n try:\n  with socket.socket() as s:s.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1);s.bind(('127.0.0.1',port))\n except OSError:ports=False\nshutil.rmtree(d,ignore_errors=True);print(json.dumps({'directory_removed':not d.exists(),'units_inactive':all('MainPID=0' in s and 'ActiveState=active' not in s for s in states),'ports_released':ports}))"
            p = remote(RU_SSH, code)
            cleanup = json.loads(p.stdout) if p.returncode == 0 else {}
            result["cleanup"]["ru_directory"] = cleanup.get("directory_removed", False)
            result["cleanup"]["ru_units"] = cleanup.get("units_inactive", False)
            result["cleanup"]["ru_ports"] = cleanup.get("ports_released", False)
        try:
            after = remote(NL_SSH, SNAPSHOT_CODE); assert after.returncode == 0
            after = json.loads(after.stdout); save(local / "nl-after.json", after)
            result["legacy_preserved"] = before == after
        except Exception:
            result["legacy_preserved"] = False
        result["pass"] = len(result["checks"]) >= 30 and all(x["pass"] for x in result["checks"]) and all(result["cleanup"].values()) and result["legacy_preserved"] and "error_class" not in result
        result["finished_utc"] = dt.datetime.now(dt.timezone.utc).isoformat()
        result["limitations"] = ["Fresh native SOCKS paths only; existing open streams and phone/TUN behavior are not tested.", "Usage is delayed and not authoritative financial accounting."]
        save(local / "result.json", result)
        (HERE / "live-result.json").write_text(json.dumps(result, indent=2) + "\n")
        print(json.dumps({"complete": True, "pass": result["pass"], "checks": len(result["checks"]), "error_code": result.get("error_code"), "private_evidence": str(local)}), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--tokens-file", type=Path, default=ROOT / "private/operator-tokens.json")
    parser.add_argument("--restart-api", action="store_true", help="Explicitly authorize restarting only vpn-control-api.service")
    args = parser.parse_args()
    if args.execute:
        execute(args.tokens_file, args.restart_api)
    else:
        print(json.dumps({"mutations": False, "services": ["hysteria", "vless-reality"], "api": "127.0.0.1:18010", "restart_api_requested": args.restart_api}))
