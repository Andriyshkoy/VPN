#!/usr/bin/env python3
"""Install only the NL node agent. Input, including credentials, is SSH stdin."""
import base64
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import pwd
import grp
import re
import shutil
import socket
import subprocess
import sys
import tarfile
import time
import urllib.request
import venv

PREFIX = Path('/opt/vpn-control')
ETC = Path('/etc/vpn-control')
STATE = Path('/var/lib/vpn-control')
BACKUPS = Path('/var/backups/vpn-control')
UNIT_DIR = Path('/etc/systemd/system')
UNITS = ('vpn-control-hys.service', 'vpn-control-api.service', 'vpn-control-backup.service', 'vpn-control-backup.timer')
USER = 'vpn-control'


def run(argv, *, timeout=30, check=True, env=None):
    return subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=check, env=env)


def private_json(path, value):
    temporary = path.with_name(path.name + '.tmp')
    with temporary.open('w', encoding='utf-8') as stream:
        json.dump(value, stream, indent=2)
        stream.write('\n')
        stream.flush()
        os.fsync(stream.fileno())
    temporary.chmod(0o600)
    os.replace(temporary, path)


def owned_directory(path, uid=0, gid=0, mode=0o700):
    if path.is_symlink():
        raise RuntimeError('symlink_directory_rejected')
    path.mkdir(mode=mode, parents=True, exist_ok=True)
    if path.stat().st_uid != uid and any(path.iterdir()):
        raise RuntimeError('directory_owner_conflict')
    os.chown(path, uid, gid)
    path.chmod(mode)


def service_account():
    try:
        group = grp.getgrnam(USER)
    except KeyError:
        run(['groupadd', '--system', USER])
        group = grp.getgrnam(USER)
    try:
        account = pwd.getpwnam(USER)
    except KeyError:
        run(['useradd', '--system', '--gid', USER, '--no-create-home', '--home-dir', str(STATE), '--shell', '/usr/sbin/nologin', USER])
        account = pwd.getpwnam(USER)
    if account.pw_uid >= 1000 or account.pw_gid != group.gr_gid or account.pw_dir != str(STATE):
        raise RuntimeError('unexpected_service_account')
    return account


def extract(bundle, destination):
    with tarfile.open(fileobj=io.BytesIO(bundle), mode='r:gz') as archive:
        members = archive.getmembers()
        if len(members) > 200 or sum(x.size for x in members) > 16 * 1024 * 1024:
            raise RuntimeError('archive_limit')
        for member in members:
            path = PurePosixPath(member.name)
            if (path.is_absolute() or '..' in path.parts or not path.parts
                    or path.parts[0] != 'app' or not member.isfile()):
                raise RuntimeError('unsafe_archive_member')
            target = destination.joinpath(*path.parts)
            target.parent.mkdir(parents=True, mode=0o755, exist_ok=True)
            for parent in target.parents:
                if parent == destination:
                    break
                parent.chmod(0o755)
            with archive.extractfile(member) as source, target.open('wb') as output:
                shutil.copyfileobj(source, output)
            target.chmod(0o644)


def replace_link(path, target):
    temporary = path.with_name(path.name + '.next')
    if temporary.exists() or temporary.is_symlink():
        temporary.unlink()
    temporary.symlink_to(target)
    os.replace(temporary, path)


def get_json(path, bearer=None, port=18010):
    headers = {'Authorization': 'Bearer ' + bearer} if bearer else {}
    request = urllib.request.Request('http://127.0.0.1:' + str(port) + path, headers=headers)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(request, timeout=3) as response:
        return response.status, json.loads(response.read(262145))


def install(payload):
    os.umask(0o077)
    if os.geteuid() != 0 or socket.gethostname() != 'vps-5042':
        raise RuntimeError('wrong_host_or_user')
    if '195.133.195.40' not in run(['ip', '-j', '-4', 'address']).stdout:
        raise RuntimeError('expected_address_missing')
    node = payload['node']
    if (set(node) != {'node_id', 'country', 'public_host', 'hysteria_port', 'vless_port', 'hysteria_sni', 'reality_sni', 'api_port'}
            or not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,31}', node['node_id'])
            or not re.fullmatch(r'[A-Z]{2}', node['country'])
            or any(not re.fullmatch(r'[A-Za-z0-9.-]{1,253}', node[k]) for k in ('public_host', 'hysteria_sni', 'reality_sni'))
            or any(type(node[k]) is not int or not 1024 <= node[k] <= 65535 for k in ('hysteria_port', 'vless_port', 'api_port'))):
        raise RuntimeError('invalid_node_configuration')
    bundle = base64.b64decode(payload['bundle'], validate=True)
    checksum = hashlib.sha256(bundle).hexdigest()
    if checksum != payload['bundle_sha256']:
        raise RuntimeError('bundle_checksum')
    token = payload['operator_token']
    catalog = payload['token_catalog']
    if catalog['tokens'][0]['sha256'] != hashlib.sha256(token.encode()).hexdigest():
        raise RuntimeError('token_hash_mismatch')
    marzban = Path('/opt/vpn-managed/secrets/operator.json')
    legacy_python = Path('/etc/hysteria/hysteria2_venv/bin/python')
    if not marzban.is_file() or not legacy_python.is_file():
        raise RuntimeError('existing_backend_missing')
    hys_config = json.loads(Path('/etc/hysteria/config.json').read_text())
    if int(str(hys_config['listen']).rsplit(':', 1)[1]) != node['hysteria_port']:
        raise RuntimeError('hysteria_port_does_not_match_existing_receiver')
    original_cert = Path(hys_config['tls']['cert'])
    if not original_cert.is_absolute() or not original_cert.is_file():
        raise RuntimeError('hysteria_public_certificate_missing')
    public_cert = original_cert.read_bytes()
    if len(public_cert) > 65536 or b'PRIVATE KEY' in public_cert:
        raise RuntimeError('hysteria_public_certificate_invalid')
    original_der = subprocess.run(['openssl', 'x509', '-in', str(original_cert), '-outform', 'DER'],
                                  capture_output=True, check=True, timeout=5).stdout
    certificate_pin = hashlib.sha256(original_der).hexdigest()
    credentials = json.loads(marzban.read_text())
    vless_config = json.loads(Path('/var/lib/vpn-managed/marzban/xray_config.json').read_text())
    matching = [x for x in vless_config['inbounds'] if x.get('tag') == credentials['inbound_tag']]
    if len(matching) != 1 or matching[0]['port'] != node['vless_port']:
        raise RuntimeError('vless_port_does_not_match_existing_receiver')
    existing = PREFIX.exists()
    ownership = {'schema': 'vpn-node-agent-ownership/v1', 'managed_by': 'control-api/deploy'}
    if existing:
        marker = PREFIX / 'ownership.json'
        if marker.is_symlink() or not marker.is_file() or json.loads(marker.read_text()) != ownership:
            raise RuntimeError('unowned_prefix')
    elif any((UNIT_DIR / name).exists() for name in UNITS):
        raise RuntimeError('unowned_unit_name_conflict')
    if not existing:
        with socket.socket() as probe:
            probe.bind(('127.0.0.1', node['api_port']))
    account = service_account()
    owned_directory(PREFIX, gid=account.pw_gid, mode=0o750)
    if not existing:
        # Retain an ownership marker even if venv creation or package download
        # fails before the atomic activation step. A retry can resume safely.
        private_json(PREFIX / 'ownership.json', ownership)
    owned_directory(PREFIX / 'releases', gid=account.pw_gid, mode=0o750)
    owned_directory(ETC)
    owned_directory(STATE, account.pw_uid, account.pw_gid)
    owned_directory(BACKUPS)
    token_file = ETC / 'tokens.json'
    if token_file.exists():
        if json.loads(token_file.read_text()) != catalog:
            raise RuntimeError('token_rotation_requires_explicit_operation')
    else:
        private_json(token_file, catalog)
    node_env = '\n'.join(k + '=' + str(v) for k, v in {
        'VPN_CONTROL_NODE_ID': node['node_id'], 'VPN_CONTROL_COUNTRY': node['country'],
        'VPN_CONTROL_PUBLIC_HOST': node['public_host'], 'VPN_CONTROL_HYSTERIA_PORT': node['hysteria_port'],
        'VPN_CONTROL_VLESS_PORT': node['vless_port'], 'VPN_CONTROL_HYS_SNI': node['hysteria_sni'],
        'VPN_CONTROL_REALITY_SNI': node['reality_sni'], 'VPN_CONTROL_API_PORT': node['api_port'],
    }.items()) + '\n'
    release = PREFIX / 'releases' / checksum[:20]
    if release.exists() and release.is_symlink():
        raise RuntimeError('release_symlink_rejected')
    release.mkdir(mode=0o755, exist_ok=True)
    release.chmod(0o755)
    extract(bundle, release)
    app = release / 'app'
    requirements = app / 'requirements.lock'
    if not requirements.is_file():
        raise RuntimeError('pinned_requirements_missing')
    for line in requirements.read_text().splitlines():
        if line and not line.startswith('#'):
            if not re.fullmatch(r'[A-Za-z0-9_.-]+==[A-Za-z0-9_.+!\-]+', line):
                raise RuntimeError('non_pinned_requirement')
    environment = {k: v for k, v in os.environ.items() if not k.lower().endswith('_proxy')}
    environment['PYTHONDONTWRITEBYTECODE'] = '1'
    environment['PIP_DISABLE_PIP_VERSION_CHECK'] = '1'
    environment['PIP_NO_INPUT'] = '1'
    venv_path = release / '.venv'
    previous_umask = os.umask(0o022)
    try:
        if not (venv_path / 'bin/python').is_file():
            venv.EnvBuilder(with_pip=True).create(venv_path)
        run([str(venv_path / 'bin/python'), '-m', 'pip', 'install', '--requirement', str(requirements)], timeout=240, env=environment)
    finally:
        os.umask(previous_umask)
    run([str(venv_path / 'bin/python'), '-m', 'compileall', '-q', str(app / 'vpn_control'), str(app / 'deploy')], env=environment)
    for unit in UNITS:
        if not (app / 'deploy' / unit).is_file():
            raise RuntimeError('unit_missing')
    previous = os.readlink(PREFIX / 'current') if (PREFIX / 'current').is_symlink() else None
    old_node_env = (ETC / 'node.env').read_bytes() if (ETC / 'node.env').is_file() else None
    old_peer_cert = (ETC / 'hysteria-peer.crt').read_bytes() if (ETC / 'hysteria-peer.crt').is_file() else None
    old_units = {name: (UNIT_DIR / name).read_bytes() if (UNIT_DIR / name).is_file() else None for name in UNITS}
    old_active = {name: old_units[name] is not None and run(['systemctl', 'is-active', name], check=False).stdout.strip() == 'active' for name in UNITS}
    old_enabled = {name: old_units[name] is not None and run(['systemctl', 'is-enabled', name], check=False).stdout.strip() == 'enabled' for name in UNITS}
    old_manifest = (PREFIX / 'deploy.json').read_bytes() if (PREFIX / 'deploy.json').is_file() else None
    manifest = {'schema': 'vpn-node-agent-deploy/v1', 'release': checksum[:20], 'bundle_sha256': checksum,
        'user': USER, 'uid': account.pw_uid, 'gid': account.pw_gid, 'api': '127.0.0.1:' + str(node['api_port']),
        'node': node, 'services': ['hysteria', 'vless-reality'], 'status': 'installing',
        'hysteria_public_certificate_sha256': certificate_pin}
    try:
        if previous:
            run([str(PREFIX / 'venv/bin/python'), str(PREFIX / 'app/deploy/backup.py'), 'create'], timeout=120)
            run(['systemctl', 'stop', 'vpn-control-api.service', 'vpn-control-hys.service'])
        private_json(PREFIX / 'deploy.json', manifest)
        temporary_env = ETC / 'node.env.next'
        temporary_env.write_text(node_env)
        temporary_env.chmod(0o600)
        os.replace(temporary_env, ETC / 'node.env')
        temporary_cert = ETC / 'hysteria-peer.crt.next'
        temporary_cert.write_bytes(public_cert)
        temporary_cert.chmod(0o600)
        os.replace(temporary_cert, ETC / 'hysteria-peer.crt')
        copied_der = subprocess.run(['openssl', 'x509', '-in', str(ETC / 'hysteria-peer.crt'), '-outform', 'DER'],
                                    capture_output=True, check=True, timeout=5).stdout
        if hashlib.sha256(copied_der).hexdigest() != certificate_pin:
            raise RuntimeError('hysteria_public_certificate_copy_mismatch')
        replace_link(PREFIX / 'current', release)
        for name, target in (('app', 'current/app'), ('venv', 'current/.venv')):
            link = PREFIX / name
            if link.exists() and not link.is_symlink():
                raise RuntimeError('stable_path_not_symlink')
            replace_link(link, target)
        for unit in UNITS:
            temporary = UNIT_DIR / (unit + '.next')
            temporary.write_bytes((app / 'deploy' / unit).read_bytes())
            temporary.chmod(0o644)
            os.replace(temporary, UNIT_DIR / unit)
        run(['systemd-analyze', 'verify', *[str(UNIT_DIR / x) for x in UNITS]])
        run(['systemctl', 'daemon-reload'])
        run(['systemctl', 'reset-failed', 'vpn-control-hys.service', 'vpn-control-api.service'], check=False)
        run(['systemctl', 'enable', '--now', 'vpn-control-hys.service', 'vpn-control-api.service', 'vpn-control-backup.timer'], timeout=45)
        ready = None
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            try:
                status, value = get_json('/readyz', token, node['api_port'])
                if status == 200:
                    ready = value
                    break
            except Exception:
                time.sleep(1)
        if ready is None:
            raise RuntimeError('node_agent_not_ready')
        manifest['status'] = 'ready'
        private_json(PREFIX / 'deploy.json', manifest)
        run([str(PREFIX / 'venv/bin/python'), str(PREFIX / 'app/deploy/backup.py'), 'create'], timeout=120)
        return {'installed': True, 'node_id': node['node_id'], 'release': checksum[:20], 'bundle_sha256': checksum,
            'api': '127.0.0.1:' + str(node['api_port']), 'uid': account.pw_uid, 'gid': account.pw_gid,
            'hysteria_public_certificate_sha256': certificate_pin,
            'services': ['hysteria', 'vless-reality'], 'ready': True,
            'units': {x: run(['systemctl', 'is-active', x], check=False).stdout.strip()
                      for x in ('vpn-control-api.service', 'vpn-control-hys.service', 'vpn-control-backup.timer')}}
    except BaseException:
        run(['systemctl', 'stop', 'vpn-control-api.service', 'vpn-control-hys.service', 'vpn-control-backup.timer'], check=False)
        for name in UNITS:
            if not old_enabled[name]:
                run(['systemctl', 'disable', name], check=False)
        for name, old in old_units.items():
            path = UNIT_DIR / name
            if old is None:
                path.unlink(missing_ok=True)
            else:
                path.write_bytes(old)
                path.chmod(0o644)
        if previous:
            replace_link(PREFIX / 'current', previous)
        else:
            (PREFIX / 'current').unlink(missing_ok=True)
        if old_node_env is None:
            (ETC / 'node.env').unlink(missing_ok=True)
        else:
            (ETC / 'node.env').write_bytes(old_node_env)
            (ETC / 'node.env').chmod(0o600)
        if old_peer_cert is None:
            (ETC / 'hysteria-peer.crt').unlink(missing_ok=True)
        else:
            (ETC / 'hysteria-peer.crt').write_bytes(old_peer_cert)
            (ETC / 'hysteria-peer.crt').chmod(0o600)
        run(['systemctl', 'daemon-reload'], check=False)
        if previous:
            for name in UNITS:
                if old_active[name]:
                    run(['systemctl', 'start', name], check=False)
        manifest['status'] = 'failed_rolled_back' if previous else 'failed_preserved_for_retry'
        private_json(PREFIX / 'failed-deploy.json', manifest)
        if previous and old_manifest is not None:
            (PREFIX / 'deploy.json').write_bytes(old_manifest)
            (PREFIX / 'deploy.json').chmod(0o600)
        else:
            private_json(PREFIX / 'deploy.json', manifest)
        raise


if __name__ == '__main__':
    try:
        print(json.dumps(install(json.load(sys.stdin))))
    except BaseException as exc:
        # Do not put request bodies, credentials, tokens or upstream output in logs.
        print(json.dumps({'installed': False, 'error_class': type(exc).__name__,
                          'error': str(exc) if isinstance(exc, RuntimeError) else 'deployment_failed'}))
        sys.exit(1)
