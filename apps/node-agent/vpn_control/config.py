"""Explicit local configuration and private credential-file loading."""
from dataclasses import dataclass
import hashlib
import json
import ipaddress
import os
from pathlib import Path
import re
import stat

SCOPES = frozenset({"access:read", "access:write", "profiles:read"})


def _systemd_credential(path: Path, fd: int, info) -> bool:
    """systemd may grant the service ACL access to root-owned read-only files.

    This exception is confined to the exact credential directory supplied by
    the service manager. An ordinary root-owned/group-readable file is rejected.
    """
    directory = os.environ.get("CREDENTIALS_DIRECTORY")
    if not directory:
        return False
    parent = Path(directory)
    if (not path.is_absolute() or not parent.is_absolute() or path.parent != parent
            or parent.resolve(strict=True) != parent
            or info.st_uid != 0 or stat.S_IMODE(info.st_mode) not in (0o400, 0o440)
            or not os.fstatvfs(fd).f_flag & os.ST_RDONLY):
        return False
    parent_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW)
    try:
        parent_info = os.fstat(parent_fd)
        if (parent_info.st_uid != 0 or not stat.S_ISDIR(parent_info.st_mode)
                or stat.S_IMODE(parent_info.st_mode) not in (0o500, 0o550)):
            return False
        # Check that the already-open file came from this exact directory;
        # neither the final file nor the credential directory may be a symlink.
        entry = os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
        return (stat.S_ISREG(entry.st_mode)
                and (entry.st_dev, entry.st_ino) == (info.st_dev, info.st_ino))
    finally:
        os.close(parent_fd)


def private_json(path: Path) -> dict:
    path = Path(path)
    fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        owned_private = info.st_uid == os.geteuid() and stat.S_IMODE(info.st_mode) in (0o400, 0o600)
        if not stat.S_ISREG(info.st_mode) or not (owned_private or _systemd_credential(path, stream.fileno(), info)):
            raise ValueError("credential_file_must_be_owned_and_private")
        data = stream.read(65537)
    if len(data) > 65536:
        raise ValueError("credential_file_too_large")
    result = json.loads(data)
    if not isinstance(result, dict):
        raise ValueError("invalid_credential_file")
    return result


@dataclass(frozen=True)
class Principal:
    id: str
    sha256: str
    scopes: frozenset[str]


@dataclass(frozen=True)
class Settings:
    database: Path
    tokens_file: Path | None = None
    marzban_credentials: Path | None = None
    hysteria_socket: Path = Path("/run/vpn-control-hys/bridge.sock")
    principals: tuple[Principal, ...] = ()
    node_id: str = "nl-1"
    public_host: str = "195.133.195.40"
    country_code: str = "NL"
    vless_port: int = 9443
    reality_sni: str = "hst.andriyshkoy.ru"
    hysteria_port: int = 1935

    def __post_init__(self):
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", self.node_id):
            raise ValueError("invalid_node_id")
        for host in (self.public_host, self.reality_sni):
            try:
                ipaddress.ip_address(host)
            except ValueError:
                if (len(host) > 253 or not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?", host)
                        or any(not label or len(label) > 63 or label.startswith('-') or label.endswith('-') for label in host.split('.'))):
                    raise ValueError("invalid_public_hostname")
        if not re.fullmatch(r"[A-Z]{2}", self.country_code):
            raise ValueError("invalid_country_code")
        if any(type(p) is not int or not 1 <= p <= 65535 for p in (self.vless_port, self.hysteria_port)):
            raise ValueError("invalid_public_port")

    @classmethod
    def from_env(cls):
        credentials = Path(os.environ.get("CREDENTIALS_DIRECTORY", "/etc/vpn-control"))
        return cls(database=Path(os.environ.get("VPN_CONTROL_DB", "/var/lib/vpn-control/control.sqlite3")),
                   tokens_file=Path(os.environ.get("VPN_CONTROL_TOKENS_FILE", str(credentials / "tokens.json"))),
                   marzban_credentials=Path(os.environ.get("VPN_CONTROL_MARZBAN_CREDENTIALS_FILE", str(credentials / "marzban.json"))),
                   hysteria_socket=Path(os.environ.get("VPN_CONTROL_HYS_SOCKET", "/run/vpn-control-hys/bridge.sock")),
                   node_id=os.environ.get("VPN_CONTROL_NODE_ID", "nl-1"),
                   public_host=os.environ.get("VPN_CONTROL_PUBLIC_HOST", "195.133.195.40"),
                   country_code=os.environ.get("VPN_CONTROL_COUNTRY", "NL"),
                   vless_port=int(os.environ.get("VPN_CONTROL_VLESS_PORT", "9443")),
                   reality_sni=os.environ.get("VPN_CONTROL_REALITY_SNI", "hst.andriyshkoy.ru"),
                   hysteria_port=int(os.environ.get("VPN_CONTROL_HYSTERIA_PORT", "1935")))

    def load_principals(self) -> tuple[Principal, ...]:
        if self.principals:
            return self.principals
        data = private_json(self.tokens_file)
        values = data.get("tokens")
        if not isinstance(values, list) or not 1 <= len(values) <= 32:
            raise ValueError("invalid_tokens_file")
        result = []
        for item in values:
            if (not isinstance(item, dict) or not re.fullmatch(r"[a-zA-Z0-9_.-]{1,64}", item.get("id", ""))
                    or not re.fullmatch(r"[0-9a-f]{64}", item.get("sha256", ""))
                    or not isinstance(item.get("scopes"), list)
                    or not set(item["scopes"]) <= SCOPES or not item["scopes"]):
                raise ValueError("invalid_tokens_file")
            result.append(Principal(item["id"], item["sha256"], frozenset(item["scopes"])))
        if len({x.id for x in result}) != len(result) or len({x.sha256 for x in result}) != len(result):
            raise ValueError("duplicate_token_identity")
        return tuple(result)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()
