#!/usr/bin/env python3
"""Build/test a pinned Hysteria fork locally; never contacts a deployed server."""
import argparse
import datetime
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request

HERE = Path(__file__).resolve().parent
PIN = json.loads((HERE / 'source.json').read_text())


def sha256(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def fetch(url, path, expected):
    if not path.exists():
        partial = path.with_suffix(path.suffix + '.partial')
        try:
            with urllib.request.urlopen(url, timeout=120) as response, partial.open('wb') as output:
                shutil.copyfileobj(response, output)
            if sha256(partial) != expected:
                raise RuntimeError(f'Checksum mismatch: {url}')
            partial.replace(path)
        finally:
            partial.unlink(missing_ok=True)
    if sha256(path) != expected:
        raise RuntimeError(f'Cached archive checksum mismatch: {path}')
    return path


def extract(archive, destination):
    # Python's data filter rejects paths/links escaping the extraction directory.
    with tarfile.open(archive, 'r:gz') as stream:
        stream.extractall(destination, filter='data')


def run(args, cwd, env):
    print('+', ' '.join(map(str, args)), flush=True)
    subprocess.run(list(map(str, args)), cwd=cwd, env=env, check=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cache-dir', type=Path, default=HERE / '.cache')
    parser.add_argument('--output-dir', type=Path, default=HERE / 'dist')
    parser.add_argument('--skip-tests', action='store_true', help='Only for rebuilding an already tested exact patch')
    args = parser.parse_args()
    if platform.system() != 'Linux' or platform.machine() not in {'x86_64', 'AMD64'}:
        parser.error('Pinned toolchain and race runner currently target Linux amd64')
    cache = args.cache_dir.resolve(); cache.mkdir(parents=True, exist_ok=True)
    output = args.output_dir.resolve(); output.mkdir(parents=True, exist_ok=True)
    tool_archive = fetch(PIN['go_url'], cache / f"go{PIN['go_version']}.linux-amd64.tar.gz", PIN['go_sha256'])
    go_home = cache / 'go'
    if not (go_home / 'bin/go').exists():
        extract(tool_archive, cache)
    go = go_home / 'bin/go'
    actual_version = subprocess.check_output([go, 'version'], text=True).strip()
    if f"go{PIN['go_version']} " not in actual_version:
        raise RuntimeError(f'Unexpected cached Go version: {actual_version}')
    archive = fetch(PIN['source_url'], cache / f"hysteria-{PIN['commit']}.tar.gz", PIN['source_sha256'])
    patch = HERE / 'session-limit.patch'
    patch_hash = sha256(patch)
    expected_patch = (HERE / 'session-limit.patch.sha256').read_text().split()[0]
    if patch_hash != expected_patch:
        raise RuntimeError('Patch checksum does not match session-limit.patch.sha256')
    env = dict(os.environ, PATH=f"{go_home / 'bin'}:{os.environ['PATH']}",
               GOTOOLCHAIN='local', GOMODCACHE=str(cache / 'gomod'), GOCACHE=str(cache / 'gobuild'))
    with tempfile.TemporaryDirectory(prefix='hysteria-source-', dir=cache) as temporary:
        extract(archive, temporary)
        source = next(Path(temporary).iterdir())
        run(['git', 'apply', '--check', patch], source, env)
        run(['git', 'apply', patch], source, env)
        if not args.skip_tests:
            run([go, 'test', '-race', '-count=1', '-timeout=180s', './core/server', './extras/auth', './extras/trafficlogger'], source, env)
        for module in ('core', 'extras', 'app'):
            run([go, 'mod', 'verify'], source / module, env)
        date = datetime.datetime.fromtimestamp(PIN['source_date_epoch'], datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
        prefix = 'github.com/apernet/hysteria/app/v2/cmd.'
        values = dict(appVersion=PIN['version'], appDate=date, appType='vpn-session-limit',
                      appToolchain=f"go{PIN['go_version']}", appCommit=PIN['commit'], appPlatform='linux', appArch='amd64')
        ldflags = ' '.join(f'-X {prefix}{key}={value}' for key, value in values.items())
        binary = output / 'hysteria-linux-amd64'
        run([go, 'build', '-trimpath', '-buildvcs=false', '-ldflags', ldflags, '-o', binary, './app'], source, env)
        run([binary, 'version'], source, env)
    shutil.copyfile(HERE / 'UPSTREAM-LICENSE.md', output / 'LICENSE.md')
    report = {**PIN, 'patch_sha256': patch_hash, 'artifact_sha256': sha256(binary),
              'tests': 'skipped' if args.skip_tests else 'native-QUIC-and-policy-packages-race',
              'http_runtime_mongo_native': bool(env.get('HYS_NATIVE_AUTH_URL')) and not args.skip_tests,
              'prepared_only': True}
    (output / 'build-manifest.json').write_text(json.dumps(report, indent=2) + '\n')
    (output / 'SHA256SUMS').write_text(f"{report['artifact_sha256']}  {binary.name}\n")
    print(json.dumps(report, indent=2), flush=True)


if __name__ == '__main__':
    main()
