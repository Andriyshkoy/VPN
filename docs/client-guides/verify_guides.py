#!/usr/bin/env python3
"""Check public guide sources and optional separately stored screenshot bytes."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from urllib.parse import unquote, urlsplit
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent
SOURCE_HOSTS = {
    "hiddify.com",
    "github.com",
    "is1-ssl.mzstatic.com",
    "play.google.com",
    "apps.apple.com",
}
RASTER_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".gif"}


def _source_url(value: str) -> bool:
    parsed = urlsplit(value)
    return (
        parsed.scheme == "https"
        and parsed.hostname in SOURCE_HOSTS
        and not parsed.username
        and not parsed.password
    )


def verify(*, online: bool = False, private_bundle: Path | None = None) -> dict:
    catalog = json.loads((ROOT / "client-downloads.json").read_text())
    manifest = json.loads((ROOT / "assets/manifest.json").read_text())
    errors: list[str] = []
    if manifest.get("schema_version") != 2:
        errors.append("Unexpected screenshot-source manifest version")
    if private_bundle is not None:
        private_bundle = private_bundle.resolve()
        if private_bundle.is_relative_to(ROOT):
            errors.append(
                "Private screenshot bundle must be outside the public guide tree"
            )
            private_bundle = None
    raster_files = [
        path for path in ROOT.rglob("*") if path.suffix.lower() in RASTER_SUFFIXES
    ]
    if raster_files:
        errors.append("Raster files must stay outside the public guide tree")
    expected_platforms = {"android", "ios", "windows", "macos", "linux", "tv"}
    if set(catalog["platforms"]) != expected_platforms:
        errors.append("Unexpected platform catalog")
    assets: dict[str, dict] = {}
    public_images: set[str] = set()
    private_verified = 0
    for row in manifest["assets"]:
        name = row["file"]
        if Path(name).name != name:
            errors.append("Screenshot identifier must be a basename")
            continue
        if name in assets:
            errors.append(f"Duplicate manifest entry: {name}")
        assets[name] = row
        for required in (
            "platform",
            "source_url",
            "attribution",
            "alt",
            "publication_status",
        ):
            if not row.get(required):
                errors.append(f"Screenshot lacks {required}: {name}")
        if row.get("modified") is not False:
            errors.append(f"Screenshot must be an unchanged capture: {name}")
        if row.get("copied_to_public_repository") is not False:
            errors.append(f"Screenshot must not be copied to public Git: {name}")
        if not _source_url(row["source_url"]) or not _source_url(
            row.get("source_page", "")
        ):
            errors.append(f"Unexpected screenshot source: {name}")
        if row.get("storage") == "external_reference":
            if (
                row.get("capture_type") != "official_reference"
                or row.get("public_image_url") != row["source_url"]
            ):
                errors.append(f"Unexpected public screenshot reference: {name}")
            public_images.add(row["source_url"])
        elif row.get("storage") != "private_preview" or "public_image_url" in row:
            errors.append(f"Unexpected private screenshot policy: {name}")
        if private_bundle is not None:
            path = private_bundle / "assets" / name
            if not path.is_file() or path.is_symlink():
                errors.append(f"Missing private screenshot: {name}")
                continue
            data = path.read_bytes()
            if hashlib.sha256(data).hexdigest() != row["sha256"]:
                errors.append(f"Private screenshot hash mismatch: {name}")
            if len(data) != row["bytes"]:
                errors.append(f"Private screenshot byte count mismatch: {name}")
            private_verified += 1
    image_count = 0
    for path in sorted(ROOT.glob("*.md")):
        content = path.read_text()
        for match in re.finditer(r"(!?)\[([^\]]*)\]\(([^)]+)\)", content):
            is_image, alt, target = match.groups()
            parsed = urlsplit(target)
            if is_image:
                image_count += 1
                if not alt.strip():
                    errors.append(f"Missing image alt text in {path.name}")
                if target not in public_images or not _source_url(target):
                    errors.append(
                        f"Image must use its recorded official origin in {path.name}"
                    )
            if parsed.scheme or target.startswith("#"):
                continue
            linked = (path.parent / unquote(parsed.path)).resolve()
            if not linked.is_relative_to(ROOT) or not linked.is_file():
                errors.append(f"Broken local link in {path.name}: {target}")
    for platform in catalog["platforms"].values():
        if not (ROOT / platform["guide"]).is_file():
            errors.append(f"Missing platform guide: {platform['guide']}")
        for row in platform["downloads"]:
            if not row["url"].startswith(
                "https://github.com/hiddify/hiddify-app/releases/latest/download/"
            ):
                errors.append(f"Unexpected download origin: {row['url']}")
    online_release = None
    if online:
        request = Request(
            catalog["sources"]["release_api"],
            headers={"User-Agent": "VPN-guide-check/1"},
        )
        with urlopen(request, timeout=30) as response:
            release = json.load(response)
        online_release = release["tag_name"]
        names = {row["name"] for row in release["assets"]}
        if online_release != catalog["client"]["verified_release"]:
            errors.append(f"Upstream release changed: {online_release}; review catalog")
        for platform in catalog["platforms"].values():
            for row in platform["downloads"]:
                name = unquote(urlsplit(row["url"]).path.rsplit("/", 1)[-1])
                if name not in names:
                    errors.append(
                        f"Download no longer exists in stable release: {name}"
                    )
    return {
        "passed": not errors,
        "guides": len(list(ROOT.glob("*.md"))),
        "platforms": len(catalog["platforms"]),
        "manifest_assets": len(assets),
        "image_references": image_count,
        "public_raster_files": len(raster_files),
        "external_image_origins": len(public_images),
        "private_assets_verified": private_verified,
        "online_release": online_release,
        "errors": errors,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--online",
        action="store_true",
        help="Check official stable release asset names",
    )
    parser.add_argument(
        "--private-bundle",
        type=Path,
        help="Check image bytes in the separate local preview",
    )
    arguments = parser.parse_args()
    result = verify(online=arguments.online, private_bundle=arguments.private_bundle)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    raise SystemExit(0 if result["passed"] else 1)


if __name__ == "__main__":
    main()
