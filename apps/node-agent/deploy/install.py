#!/usr/bin/env python3
"""Package and deploy the private node agent. Never deploys protocol engines."""
import argparse
import base64
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import secrets
import shlex
import subprocess
import tarfile

ROOT = Path(__file__).resolve().parents[1]
SSH = ['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10', '-o', 'StrictHostKeyChecking=yes',
       '-o', 'IdentitiesOnly=yes', '-o', 'ForwardAgent=no', '-i', '/home/andriyshkoy/.ssh/id_ed25519_codex',
       'root@195.133.195.40']


def bundle():
    files = [ROOT / 'requirements.lock']
    files += sorted((ROOT / 'vpn_control').rglob('*.py'))
    files += sorted(p for p in (ROOT / 'deploy').iterdir()
                    if p.is_file() and p.suffix in ('.py', '.service', '.timer') and not p.name.startswith('test_'))
    data = io.BytesIO()
    with gzip.GzipFile(fileobj=data, mode='wb', mtime=0) as compressed:
        with tarfile.open(fileobj=compressed, mode='w') as archive:
            for path in files:
                if not path.is_file() or path.is_symlink():
                    raise RuntimeError('source_file_missing_or_symlink')
                raw = path.read_bytes()
                name = 'app/' + str(path.relative_to(ROOT))
                item = tarfile.TarInfo(name)
                item.size, item.mode, item.mtime = len(raw), 0o644, 0
                archive.addfile(item, io.BytesIO(raw))
    return data.getvalue()


def token(private, name):
    file = private / (name + '-token.txt')
    if file.exists():
        if file.is_symlink() or file.stat().st_mode & 0o777 != 0o600:
            raise RuntimeError('operator_token_permissions')
        value = file.read_text().strip()
    else:
        value = secrets.token_urlsafe(32)
        with file.open('x') as stream:
            stream.write(value + '\n')
        file.chmod(0o600)
    if len(value) < 40 or any(x.isspace() for x in value):
        raise RuntimeError('operator_token_invalid')
    return value


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--execute', action='store_true', help='Mutates only the new node agent after local validation')
    parser.add_argument('--node-id', default='nl-1')
    parser.add_argument('--country', default='NL')
    parser.add_argument('--public-host', default='195.133.195.40')
    parser.add_argument('--hysteria-port', type=int, default=1935)
    parser.add_argument('--vless-port', type=int, default=9443)
    parser.add_argument('--hysteria-sni', default='github.com')
    parser.add_argument('--reality-sni', default='hst.andriyshkoy.ru')
    parser.add_argument('--api-port', type=int, default=18010)
    args = parser.parse_args()
    os.umask(0o077)
    packed = bundle()
    checksum = hashlib.sha256(packed).hexdigest()
    node = {key: getattr(args, key) for key in ('node_id', 'country', 'public_host', 'hysteria_port', 'vless_port', 'hysteria_sni', 'reality_sni', 'api_port')}
    if not args.execute:
        print(json.dumps({'bundle_sha256': checksum, 'bytes': len(packed), 'node': node, 'remote_mutations': False}))
        return
    private = ROOT / 'private'
    private.mkdir(mode=0o700, exist_ok=True)
    if private.stat().st_mode & 0o777 != 0o700:
        raise RuntimeError('private_directory_permissions')
    operator = token(private, 'operator')
    readonly = token(private, 'readonly')
    catalog = {'tokens': [{'id': 'operator', 'sha256': hashlib.sha256(operator.encode()).hexdigest(),
                          'scopes': ['access:read', 'access:write', 'profiles:read']},
                         {'id': 'acceptance_read', 'sha256': hashlib.sha256(readonly.encode()).hexdigest(),
                          'scopes': ['access:read']}]}
    token_bundle = private / 'operator-tokens.json'
    token_bundle.write_text(json.dumps({'operator_token': operator, 'readonly_token': readonly}) + '\n')
    token_bundle.chmod(0o600)
    attempt = private / ('deploy-' + secrets.token_hex(5))
    attempt.mkdir(mode=0o700)
    for name in ('deploy.stdout', 'deploy.stderr'):
        previous = private / name
        if previous.is_file():
            (attempt / ('previous-' + name)).write_bytes(previous.read_bytes())
    source = (ROOT / 'deploy/bootstrap.py').read_text()
    remote = "import json,sys; p=json.load(sys.stdin); s={'__name__':'remote_bootstrap','__file__':'/opt/vpn-control/bootstrap.py'}; exec(compile(p.pop('_source'),'bootstrap.py','exec'),s); print(json.dumps(s['install'](p)))"
    payload = {'_source': source, 'bundle': base64.b64encode(packed).decode(),
               'bundle_sha256': checksum, 'operator_token': operator, 'token_catalog': catalog, 'node': node}
    try:
        result = subprocess.run(SSH + [shlex.join(['python3', '-c', remote])],
                                input=json.dumps(payload).encode(), capture_output=True, timeout=420)
    except subprocess.TimeoutExpired as exc:
        (attempt / 'timeout.stderr').write_bytes(exc.stderr or b'')
        raise RuntimeError('deployment_timeout_check_owned_units_before_retry') from None
    (attempt / 'stdout').write_bytes(result.stdout)
    (attempt / 'stderr').write_bytes(result.stderr)
    (private / 'deploy.stderr').write_bytes(result.stderr)
    (private / 'deploy.stdout').write_bytes(result.stdout)
    if result.returncode:
        raise RuntimeError('deployment_failed_private_output_retained')
    receipt = json.loads(result.stdout)
    receipt['private_evidence'] = str(attempt)
    (attempt / 'receipt.json').write_text(json.dumps(receipt, indent=2) + '\n')
    (ROOT / 'deployment-result.json').write_text(json.dumps(receipt, indent=2) + '\n')
    print(json.dumps(receipt))


if __name__ == '__main__':
    main()
