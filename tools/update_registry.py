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
import copy

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
    "Gland0rf/Nicos-Super-Mods-Trust-Registry/1.0 "
    "(https://github.com/Gland0rf/"
    "Nicos-Super-Mods-Trust-Registry)"
)


def request_headers(
    accept: str,
    *,
    github_auth: bool = False,
) -> dict[str, str]:
    headers = {
        "Accept": accept,
        "User-Agent": USER_AGENT,
    }

    if github_auth:
        token = os.environ.get("GITHUB_TOKEN")

        if token:
            headers["Authorization"] = f"Bearer {token}"

    return headers


def download_json(
    url: str,
    *,
    github_auth: bool = False,
) -> Any:
    request = urllib.request.Request(
        url,
        headers=request_headers(
            "application/json",
            github_auth=github_auth,
        ),
    )

    try:
        with urllib.request.urlopen(
            request,
            timeout=30,
        ) as response:
            return json.load(response)

    except urllib.error.HTTPError as exception:
        body = exception.read().decode(
            "utf-8",
            errors="replace",
        )

        raise RuntimeError(
            f"Request failed with HTTP "
            f"{exception.code}: {url}\n{body}"
        ) from exception


def download_jar(url: str) -> bytes:
    request = urllib.request.Request(
        url,
        headers=request_headers(
            "application/octet-stream"
        ),
    )

    try:
        with urllib.request.urlopen(
            request,
            timeout=60,
        ) as response:
            content_length = response.headers.get(
                "Content-Length"
            )

            if content_length is not None:
                declared_size = int(content_length)

                if declared_size > MAX_JAR_BYTES:
                    raise ValueError(
                        f"JAR is too large: "
                        f"{declared_size} bytes"
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

        response = download_json(
            url,
            github_auth=True,
        )

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
    known_file_names = registered_file_names(project)
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

            if file_name.casefold() in known_file_names:
                print(
                    f"Skipping {repository}: {file_name} "
                    f"(already in registry)"
                )
                continue

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

def modrinth_versions(
    project_id: str,
    loaders: list[str],
    game_versions: list[str],
) -> list[dict[str, Any]]:
    query = {
        "include_changelog": "false",
    }

    if loaders:
        query["loaders"] = json.dumps(loaders)

    if game_versions:
        query["game_versions"] = json.dumps(game_versions)

    url = (
        f"https://api.modrinth.com/v2/project/"
        f"{urllib.parse.quote(project_id, safe='')}/version?"
        f"{urllib.parse.urlencode(query)}"
    )

    response = download_json(url)

    if not isinstance(response, list):
        raise ValueError(
            f"Unexpected Modrinth response for {project_id}"
        )

    return response

def update_modrinth_project(
    source: dict[str, Any],
    registry: dict[str, Any],
    known_hashes: set[str],
) -> bool:
    project_id = source.get("projectId")
    loaders = source.get("loaders", ["fabric"])
    game_versions = source.get("gameVersions", [])
    raw_release_types = source.get("releaseTypes")

    if raw_release_types is None:
        # Missing releaseTypes means accept every type:
        # release, beta, and alpha.
        release_types: set[str] | None = None
    else:
        if not isinstance(raw_release_types, list):
            raise ValueError(
                f"{project_id}: releaseTypes must be an array"
            )

        release_types = {
            str(value)
            for value in raw_release_types
        }

    raw_mod_ids = source.get("modIds", [])

    if not isinstance(raw_mod_ids, list):
        raise ValueError(
            f"{project_id}: modIds must be an array"
        )

    configured_mod_ids: set[str] = set()

    for value in raw_mod_ids:
        if (
            not isinstance(value, str)
            or not FABRIC_MOD_ID_PATTERN.fullmatch(value)
        ):
            raise ValueError(
                f"{project_id}: invalid configured mod ID {value!r}"
            )

        configured_mod_ids.add(value.lower())

    if not isinstance(project_id, str) or not project_id.strip():
        raise ValueError(
            "Modrinth source requires projectId"
        )

    if not isinstance(loaders, list):
        raise ValueError(
            f"{project_id}: loaders must be an array"
        )

    if not isinstance(game_versions, list):
        raise ValueError(
            f"{project_id}: gameVersions must be an array"
        )

    versions = modrinth_versions(
        project_id,
        [str(value) for value in loaders],
        [str(value) for value in game_versions],
    )

    project_url = f"https://modrinth.com/mod/{project_id}"
    project = get_existing_project(registry, project_url)

    changed = False

    if project is not None and configured_mod_ids:
        existing_mod_ids = {
            str(value).lower()
            for value in project.get("modIds", [])
        }

        merged_mod_ids = sorted(
            existing_mod_ids | configured_mod_ids
        )

        if merged_mod_ids != sorted(existing_mod_ids):
            project["modIds"] = merged_mod_ids
            changed = True

            print(
                f"Updated {project_id} mod IDs: "
                f"{merged_mod_ids}"
            )

    for version in versions:
        version_type = version.get("version_type")

        if (
            release_types is not None
            and version_type not in release_types
        ):
            continue

        files = version.get("files")

        if not isinstance(files, list) or not files:
            continue

        # Prefer Modrinth's primary file.
        primary_files = [
            file
            for file in files
            if file.get("primary") is True
        ]

        selected_file = (
            primary_files[0]
            if primary_files
            else files[0]
        )

        file_name = selected_file.get("filename")
        download_url = selected_file.get("url")
        hashes = selected_file.get("hashes", {})
        modrinth_sha512 = hashes.get("sha512")

        if not isinstance(file_name, str):
            raise ValueError(
                f"{project_id}: version file has no filename"
            )

        if not isinstance(download_url, str):
            raise ValueError(
                f"{project_id}: {file_name} has no URL"
            )

        if (
            not isinstance(modrinth_sha512, str)
            or not re.fullmatch(
                r"[0-9a-fA-F]{128}",
                modrinth_sha512,
            )
        ):
            raise ValueError(
                f"{project_id}: {file_name} has no valid SHA-512"
            )

        digest = modrinth_sha512.lower()

        if digest in known_hashes:
            continue

        print(
            f"Checking Modrinth project "
            f"{project_id}: {file_name}"
        )

        # Still download the file so we can verify metadata and
        # independently confirm Modrinth's hash.
        jar_bytes = download_jar(download_url)
        calculated_digest = hashlib.sha512(
            jar_bytes
        ).hexdigest()

        if calculated_digest != digest:
            raise ValueError(
                f"{project_id}: SHA-512 mismatch for {file_name}"
            )

        metadata = read_fabric_metadata(jar_bytes)

        if project is None:
            if (
                configured_mod_ids
                and metadata["id"] not in configured_mod_ids
            ):
                raise ValueError(
                    f"{project_id}: {file_name} claims "
                    f"unexpected mod ID {metadata['id']!r}; "
                    f"expected {sorted(configured_mod_ids)!r}"
                )

            project_mod_ids = sorted(
                configured_mod_ids or {metadata["id"]}
            )

            project = {
                "name": metadata["name"],
                "projectUrl": project_url,
                "modIds": project_mod_ids,
                "releases": [],
            }

            registry.setdefault(
                "projects",
                []
            ).append(project)

            changed = True

            print(
                f"Discovered {metadata['name']} "
                f"with mod IDs {project_mod_ids}"
            )
        else:
            registered_mod_ids = {
                str(value).lower()
                for value in project.get("modIds", [])
            }

            if metadata["id"] not in registered_mod_ids:
                raise ValueError(
                    f"{project_id}: {file_name} claims "
                    f"unexpected mod ID {metadata['id']!r}; "
                    f"expected {sorted(registered_mod_ids)!r}"
                )

        known_hashes.add(digest)
        changed = True

        print(
            f"Added {metadata['name']} "
            f"{metadata['version']} "
            f"from Modrinth"
        )

    return changed

def registered_file_names(
    project: dict[str, Any] | None,
) -> set[str]:
    if project is None:
        return set()

    names: set[str] = set()

    releases = project.get("releases", [])

    if not isinstance(releases, list):
        return names

    for release in releases:
        if not isinstance(release, dict):
            continue

        file_name = release.get("fileName")

        if isinstance(file_name, str):
            names.add(file_name.casefold())

    return names

def project_identity(
    project: dict[str, Any],
) -> str:
    project_url = project.get("projectUrl")

    if isinstance(project_url, str) and project_url.strip():
        return normalize_project_url(project_url)

    return str(project.get("name", "")).strip().casefold()


def release_identity(
    release: dict[str, Any],
) -> str:
    sha512 = release.get("sha512")

    if isinstance(sha512, str) and sha512.strip():
        return f"sha512:{sha512.lower()}"

    # Fallback for malformed or older registry entries.
    return json.dumps(
        release,
        sort_keys=True,
        ensure_ascii=False,
    )


def print_update_summary(
    before: dict[str, Any],
    after: dict[str, Any],
) -> None:
    before_projects: dict[str, dict[str, Any]] = {}

    for project in before.get("projects", []):
        if isinstance(project, dict):
            before_projects[project_identity(project)] = project

    new_projects: list[str] = []
    added_releases: list[str] = []
    mod_id_changes: list[str] = []

    for project in after.get("projects", []):
        if not isinstance(project, dict):
            continue

        key = project_identity(project)
        project_name = str(
            project.get("name", "<unnamed project>")
        )

        previous_project = before_projects.get(key)

        if previous_project is None:
            new_projects.append(project_name)
            previous_releases: set[str] = set()
            previous_mod_ids: set[str] = set()
        else:
            previous_releases = {
                release_identity(release)
                for release in previous_project.get("releases", [])
                if isinstance(release, dict)
            }

            previous_mod_ids = {
                str(mod_id).lower()
                for mod_id in previous_project.get("modIds", [])
            }

        current_mod_ids = {
            str(mod_id).lower()
            for mod_id in project.get("modIds", [])
        }

        if (
            previous_project is not None
            and current_mod_ids != previous_mod_ids
        ):
            mod_id_changes.append(
                f"{project_name}: "
                f"{sorted(previous_mod_ids)} -> "
                f"{sorted(current_mod_ids)}"
            )

        for release in project.get("releases", []):
            if not isinstance(release, dict):
                continue

            if release_identity(release) in previous_releases:
                continue

            version = str(
                release.get("version", "<unknown version>")
            )
            file_name = str(
                release.get("fileName", "<unknown file>")
            )

            added_releases.append(
                f"{project_name} {version} — {file_name}"
            )

    print()
    print("========== Registry update summary ==========")
    print(f"New projects:         {len(new_projects)}")
    print(f"New releases:         {len(added_releases)}")
    print(f"Changed mod ID lists: {len(mod_id_changes)}")

    if new_projects:
        print()
        print("New projects:")

        for project_name in sorted(
            new_projects,
            key=str.casefold,
        ):
            print(f"  + {project_name}")

    if added_releases:
        print()
        print("Added releases:")

        for release in sorted(
            added_releases,
            key=str.casefold,
        ):
            print(f"  + {release}")

    if mod_id_changes:
        print()
        print("Changed mod ID lists:")

        for change in sorted(
            mod_id_changes,
            key=str.casefold,
        ):
            print(f"  ~ {change}")

    if not (
        new_projects
        or added_releases
        or mod_id_changes
    ):
        print()
        print("No registry content was changed.")

    print("=============================================")

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

    original_registry = copy.deepcopy(registry)

    # Save the original state so partial changes are detected even
    # when an updater throws before returning its changed value.
    original_registry_state = json.dumps(
        original_registry,
        sort_keys=True,
        ensure_ascii=False,
    )

    hashes = all_known_hashes(registry)
    errors: list[str] = []

    for source in raw_sources:
        if not isinstance(source, dict):
            errors.append(
                "Skipped invalid source: source must be an object"
            )
            continue

        source_type = source.get("type", "github")

        if source_type == "github":
            source_name = str(
                source.get("repository", "<unknown GitHub source>")
            )

        elif source_type == "modrinth":
            source_name = str(
                source.get("projectId", "<unknown Modrinth source>")
            )

        else:
            source_name = str(source_type)

        try:
            if source_type == "github":
                update_github_project(
                    source,
                    registry,
                    hashes,
                )

            elif source_type == "modrinth":
                update_modrinth_project(
                    source,
                    registry,
                    hashes,
                )

            else:
                raise ValueError(
                    f"Unsupported source type: {source_type}"
                )

        except Exception as exception:
            message = (
                f"Skipped remaining releases for "
                f"{source_type} source {source_name}: "
                f"{exception}"
            )

            errors.append(message)

            # This prints one readable warning rather than a traceback.
            print(
                f"WARNING: {message}",
                file=sys.stderr,
            )

            # Continue with the next configured source.
            continue

    current_registry_state = json.dumps(
        registry,
        sort_keys=True,
        ensure_ascii=False,
    )

    registry_changed = (
        current_registry_state != original_registry_state
    )

    if registry_changed:
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
    else:
        print("No new releases found.")

    print_update_summary(
        original_registry,
        registry,
    )

    # Return success so GitHub Actions can still create the update PR.
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