#!/usr/bin/env python3

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import sys
import urllib.error
import urllib.request
import zipfile

from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent.parent
SOURCES_PATH = ROOT / "sources.json"
REGISTRY_PATH = ROOT / "registry.json"

MAX_JAR_BYTES = 128 * 1024 * 1024

FABRIC_MOD_ID_PATTERN = re.compile(
    r"^[A-Za-z][A-Za-z0-9_-]{1,63}$"
)

USER_AGENT = (
    "Gland0rf/NSM-Trust-Registry/1.0 "
    "(https://github.com/Gland0rf/"
    "Nicos-Super-Mods-Trust-Registry)"
)


def request_headers() -> dict[str, str]:
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": USER_AGENT,
    }

    token = os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"

    return headers


def download_json(url: str) -> Any:
    request = urllib.request.Request(
        url,
        headers=request_headers(),
    )

    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.load(response)
    except urllib.error.HTTPError as exception:
        body = exception.read().decode("utf-8", errors="replace")

        raise RuntimeError(
            f"GitHub request failed with HTTP "
            f"{exception.code}: {url}\n{body}"
        ) from exception


def download_jar(url: str) -> bytes:
    request = urllib.request.Request(
        url,
        headers={
            **request_headers(),
            "Accept": "application/octet-stream",
        },
    )

    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            content_length = response.headers.get("Content-Length")

            if content_length is not None:
                declared_size = int(content_length)

                if declared_size > MAX_JAR_BYTES:
                    raise ValueError(
                        f"JAR is too large: {declared_size} bytes"
                    )

            data = response.read(MAX_JAR_BYTES + 1)

    except urllib.error.HTTPError as exception:
        raise RuntimeError(
            f"Could not download release asset: {url} "
            f"(HTTP {exception.code})"
        ) from exception

    if len(data) > MAX_JAR_BYTES:
        raise ValueError(
            f"Downloaded JAR exceeds the "
            f"{MAX_JAR_BYTES}-byte limit"
        )

    return data


def read_json_file(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as file:
        value = json.load(file)

    if not isinstance(value, dict):
        raise ValueError(f"{path.name} must contain a JSON object")

    return value


def write_json_file(path: Path, value: dict[str, Any]) -> None:
    text = json.dumps(
        value,
        indent=2,
        ensure_ascii=False,
    ) + "\n"

    # Explicit LF output is important because registry signatures
    # are calculated from the exact registry.json bytes.
    with path.open(
        "w",
        encoding="utf-8",
        newline="\n",
    ) as file:
        file.write(text)


def read_fabric_metadata(jar_bytes: bytes) -> dict[str, str]:
    try:
        with zipfile.ZipFile(io.BytesIO(jar_bytes)) as jar:
            try:
                raw_metadata = jar.read("fabric.mod.json")
            except KeyError as exception:
                raise ValueError(
                    "JAR has no root-level fabric.mod.json"
                ) from exception

    except zipfile.BadZipFile as exception:
        raise ValueError(
            "Downloaded release asset is not a valid JAR"
        ) from exception

    try:
        metadata = json.loads(
            raw_metadata.decode("utf-8-sig")
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exception:
        raise ValueError(
            "fabric.mod.json is not valid UTF-8 JSON"
        ) from exception

    if not isinstance(metadata, dict):
        raise ValueError(
            "fabric.mod.json must contain a JSON object"
        )

    mod_id = metadata.get("id")
    version = metadata.get("version")
    name = metadata.get("name", mod_id)

    if (
        not isinstance(mod_id, str)
        or not FABRIC_MOD_ID_PATTERN.fullmatch(mod_id)
    ):
        raise ValueError(
            f"Invalid Fabric mod ID: {mod_id!r}"
        )

    if (
        not isinstance(version, str)
        or not version.strip()
        or version == "${version}"
    ):
        raise ValueError(
            f"Invalid Fabric mod version: {version!r}"
        )

    if not isinstance(name, str) or not name.strip():
        name = mod_id

    return {
        "id": mod_id.lower(),
        "name": name.strip(),
        "version": version.strip(),
    }


def normalize_project_url(url: str) -> str:
    return url.rstrip("/").lower()


def github_releases(repository: str) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    page = 1

    while True:
        url = (
            f"https://api.github.com/repos/{repository}/releases"
            f"?per_page=100&page={page}"
        )

        response = download_json(url)

        if not isinstance(response, list):
            raise ValueError(
                f"Unexpected releases response for {repository}"
            )

        results.extend(response)

        if len(response) < 100:
            break

        page += 1

    return results


def get_existing_project(
    registry: dict[str, Any],
    project_url: str,
) -> dict[str, Any] | None:
    normalized_target = normalize_project_url(project_url)

    for project in registry.get("projects", []):
        existing_url = project.get("projectUrl")

        if (
            isinstance(existing_url, str)
            and normalize_project_url(existing_url)
            == normalized_target
        ):
            return project

    return None


def all_known_hashes(
    registry: dict[str, Any],
) -> set[str]:
    hashes: set[str] = set()

    for project in registry.get("projects", []):
        for release in project.get("releases", []):
            sha512 = release.get("sha512")

            if isinstance(sha512, str):
                hashes.add(sha512.lower())

    return hashes


def update_github_project(
    source: dict[str, Any],
    registry: dict[str, Any],
    known_hashes: set[str],
) -> bool:
    repository = source.get("repository")
    asset_regex = source.get("assetRegex")
    include_prereleases = source.get(
        "includePrereleases",
        False,
    )

    if not isinstance(repository, str) or "/" not in repository:
        raise ValueError(
            "Each source requires repository: OWNER/REPOSITORY"
        )

    if not isinstance(asset_regex, str):
        raise ValueError(
            f"{repository} requires assetRegex"
        )

    pattern = re.compile(asset_regex)
    project_url = f"https://github.com/{repository}"

    project = get_existing_project(registry, project_url)
    changed = False

    for release in github_releases(repository):
        if release.get("draft", False):
            continue

        if (
            release.get("prerelease", False)
            and not include_prereleases
        ):
            continue

        assets = release.get("assets", [])

        if not isinstance(assets, list):
            continue

        for asset in assets:
            file_name = asset.get("name")
            download_url = asset.get("browser_download_url")

            if not isinstance(file_name, str):
                continue

            if not pattern.fullmatch(file_name):
                continue

            if not isinstance(download_url, str):
                raise ValueError(
                    f"Missing download URL for {file_name}"
                )

            print(f"Checking {repository}: {file_name}")

            jar_bytes = download_jar(download_url)
            metadata = read_fabric_metadata(jar_bytes)

            digest = hashlib.sha512(jar_bytes).hexdigest()

            if digest in known_hashes:
                continue

            if project is None:
                project = {
                    "name": metadata["name"],
                    "projectUrl": project_url,
                    "modIds": [metadata["id"]],
                    "releases": [],
                }

                registry.setdefault("projects", []).append(project)

                print(
                    f"Discovered project {metadata['name']} "
                    f"with mod ID {metadata['id']}"
                )

            else:
                registered_mod_ids = {
                    str(value).lower()
                    for value in project.get("modIds", [])
                }

                if metadata["id"] not in registered_mod_ids:
                    raise ValueError(
                        f"{repository} release {file_name} "
                        f"claims unexpected mod ID "
                        f"{metadata['id']!r}; expected one of "
                        f"{sorted(registered_mod_ids)!r}"
                    )

            project.setdefault("releases", []).append(
                {
                    "version": metadata["version"],
                    "fileName": file_name,
                    "sha512": digest,
                }
            )

            known_hashes.add(digest)
            changed = True

            print(
                f"Added {metadata['name']} "
                f"{metadata['version']} ({metadata['id']})"
            )

    return changed


def sort_registry(registry: dict[str, Any]) -> None:
    projects = registry.get("projects", [])

    projects.sort(
        key=lambda project: (
            str(project.get("name", "")).lower(),
            str(project.get("projectUrl", "")).lower(),
        )
    )

    for project in projects:
        releases = project.get("releases", [])

        releases.sort(
            key=lambda release: (
                str(release.get("version", "")).lower(),
                str(release.get("fileName", "")).lower(),
                str(release.get("sha512", "")).lower(),
            )
        )


def main() -> int:
    sources = read_json_file(SOURCES_PATH)
    registry = read_json_file(REGISTRY_PATH)

    if sources.get("schemaVersion") != 1:
        raise ValueError(
            "Unsupported sources.json schemaVersion"
        )

    if registry.get("schemaVersion") != 1:
        raise ValueError(
            "Unsupported registry.json schemaVersion"
        )

    raw_sources = sources.get("projects")

    if not isinstance(raw_sources, list):
        raise ValueError(
            "sources.json projects must be an array"
        )

    registry.setdefault("projects", [])

    hashes = all_known_hashes(registry)
    changed = False

    for source in raw_sources:
        if not isinstance(source, dict):
            raise ValueError(
                "Each sources.json project must be an object"
            )

        source_type = source.get("type", "github")

        if source_type != "github":
            raise ValueError(
                f"Unsupported source type: {source_type}"
            )

        changed |= update_github_project(
            source,
            registry,
            hashes,
        )

    if not changed:
        print("No new releases found.")
        return 0

    registry["generatedAt"] = (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )

    sort_registry(registry)
    write_json_file(REGISTRY_PATH, registry)

    print("registry.json was updated.")
    print("It must now be reviewed and signed manually.")

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exception:
        print(
            f"Registry update failed: {exception}",
            file=sys.stderr,
        )
        raise