"""Verified GitHub release downloads and recoverable per-file installation."""

import datetime
import hashlib
import http.client
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import struct
import tarfile
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zlib

from .core import CompatibilityError, Error, atomic_bytes, atomic_json, private_directory, sha256, sync_directory
from .manifest import TAG, destination


MAX_ARCHIVE = 200 * 1024 * 1024
MAX_EXECUTABLE = 128 * 1024 * 1024
VERSION = re.compile(r"\bv?(\d+\.\d+\.\d+(?:[-+][A-Za-z0-9.-]+)?)")
DIGEST = re.compile(r"sha256:([a-fA-F0-9]{64})\Z")


class SafeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        parsed = urllib.parse.urlsplit(newurl)
        if parsed.scheme != "https" or parsed.username or parsed.password:
            raise Error("Refusing an insecure or credential-bearing download redirect")
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        if redirected and urllib.parse.urlsplit(req.full_url).hostname != urllib.parse.urlsplit(newurl).hostname:
            redirected.remove_header("Authorization")
        return redirected


class GitHub:
    def __init__(self, token=None):
        self.token = token if token is not None else os.environ.get("GITHUB_TOKEN")
        self.opener = urllib.request.build_opener(SafeRedirect())

    def open(self, url):
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme != "https" or parsed.username or parsed.password:
            raise Error("Only credential-free HTTPS release URLs are accepted")
        headers = {"User-Agent": "dotfiles-host/1", "Accept": "application/vnd.github+json"}
        if self.token and parsed.hostname == "api.github.com":
            headers["Authorization"] = "Bearer " + self.token
        try:
            return self.opener.open(urllib.request.Request(url, headers=headers), timeout=30)
        except urllib.error.HTTPError as exc:
            raise Error("GitHub request failed (HTTP {}); check rate limits and upstream availability".format(exc.code)) from exc
        except (OSError, urllib.error.URLError) as exc:
            raise Error("Cannot reach the upstream release service") from exc

    def release(self, tool):
        version = tool["version"]
        endpoint = "latest" if version["policy"] == "latest" else "tags/" + urllib.parse.quote(version["tag"], safe="")
        url = "https://api.github.com/repos/{}/releases/{}".format(tool["repository"], endpoint)
        try:
            with self.open(url) as stream:
                payload = stream.read(4 * 1024 * 1024 + 1)
        except (OSError, http.client.HTTPException) as exc:
            raise Error("Release metadata download was interrupted") from exc
        if len(payload) > 4 * 1024 * 1024:
            raise Error("Release metadata exceeds the safety limit")
        try:
            release = json.loads(payload)
        except ValueError as exc:
            raise Error("Upstream returned invalid release metadata") from exc
        if not isinstance(release, dict) or not isinstance(release.get("assets"), list) or not all(isinstance(a, dict) for a in release["assets"]):
            raise Error("Incomplete upstream release metadata")
        tag = release.get("tag_name")
        if not isinstance(tag, str) or not TAG.fullmatch(tag) or release.get("draft"):
            raise Error("Unusable upstream release tag")
        if version["policy"] == "pinned" and tag != version["tag"]:
            raise Error("Upstream release does not match the requested pin")
        if release.get("prerelease") and not version.get("allow_prerelease", False):
            raise Error("Prerelease selection requires an explicit version policy")
        return release

    def download(self, asset, target):
        size = asset.get("size")
        if not isinstance(size, int) or not 0 < size <= MAX_ARCHIVE:
            raise Error("Unexpected release-asset size")
        for attempt in range(3):
            try:
                with self.open(asset["browser_download_url"]) as stream, Path(target).open("wb") as output:
                    count = 0
                    while True:
                        block = stream.read(1024 * 1024)
                        if not block:
                            break
                        count += len(block)
                        if count > size:
                            raise Error("Release download exceeds its declared size")
                        output.write(block)
                if count != size:
                    raise Error("Incomplete release download; installed binary was not touched")
                return
            except (OSError, http.client.HTTPException, Error) as exc:
                if attempt == 2:
                    raise Error("Release download failed after three attempts") from exc
                time.sleep(attempt + 1)

    def checksum_text(self, asset):
        if not isinstance(asset.get("size"), int) or not 0 < asset["size"] <= 2 * 1024 * 1024:
            raise Error("Unexpected upstream checksum-file size")
        try:
            with self.open(asset["browser_download_url"]) as stream:
                data = stream.read(asset["size"] + 1)
        except (OSError, http.client.HTTPException) as exc:
            raise Error("Upstream checksum download was interrupted") from exc
        if len(data) != asset["size"]:
            raise Error("Incomplete upstream checksum file")
        if asset.get("digest"):
            expected = github_digest(asset)
            if hashlib.sha256(data).hexdigest() != expected:
                raise Error("Upstream checksum file failed integrity verification")
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise Error("Upstream checksum file is not text") from exc


def github_digest(asset):
    value = asset.get("digest")
    match = DIGEST.fullmatch(value) if isinstance(value, str) else None
    if not match:
        raise Error("A trusted SHA256 digest is unavailable for this release asset")
    return match.group(1).lower()


def select_asset(tool, release, architecture):
    patterns = tool["assets"].get(architecture)
    if not patterns:
        raise Error("{} has no approved build for {}".format(tool["id"], architecture))
    tag = release["tag_name"]
    for pattern in patterns:
        wanted = pattern.format(tag=tag, version=tag.lstrip("v"))
        found = [a for a in release["assets"] if a.get("name") == wanted]
        if len(found) == 1:
            asset = found[0]
            validate_asset_url(asset, tool)
            return asset
        if len(found) > 1:
            raise Error("Ambiguous upstream release assets")
    raise Error("{} release has no matching approved archive".format(tool["id"]))


def validate_asset_url(asset, tool):
    url = asset.get("browser_download_url")
    if not isinstance(url, str):
        raise Error("Missing upstream release download URL")
    parsed = urllib.parse.urlsplit(url)
    prefix = "/" + tool["repository"] + "/releases/download/"
    if parsed.scheme != "https" or parsed.hostname != "github.com" or not parsed.path.startswith(prefix) or parsed.username or parsed.password:
        raise Error("Release asset is not hosted by its declared upstream repository")


def expected_digest(tool, release, asset, client):
    if asset.get("digest"):
        return github_digest(asset)
    pattern = tool.get("checksum_asset")
    if not pattern:
        raise Error("No upstream SHA256 digest/checksum available; refusing an unverified installation")
    wanted = pattern.format(tag=release["tag_name"], version=release["tag_name"].lstrip("v"), asset=asset["name"])
    matches = [a for a in release["assets"] if a.get("name") == wanted]
    if len(matches) != 1:
        raise Error("The declared upstream checksum file is unavailable")
    validate_asset_url(matches[0], tool)
    digests = []
    for line in client.checksum_text(matches[0]).splitlines():
        match = re.fullmatch(r"([a-fA-F0-9]{64})\s+\*?(.+)", line.strip())
        if match and match.group(2) == asset["name"]:
            digests.append(match.group(1).lower())
        match = re.fullmatch(r"SHA256\s*\((.+)\)\s*=\s*([a-fA-F0-9]{64})", line.strip())
        if match and match.group(1) == asset["name"]:
            digests.append(match.group(2).lower())
    if len(digests) != 1:
        raise Error("No unique SHA256 checksum for the selected archive")
    return digests[0]


def extract_executable(archive, executable, target):
    try:
        with tarfile.open(archive, "r:gz") as tar:
            selected = []
            total, count = 0, 0
            for member in tar:
                total += member.size
                count += 1
                if member.size > MAX_ARCHIVE or total > 512 * 1024 * 1024 or count > 10000:
                    raise Error("Upstream archive exceeds extraction safety limits")
                path = PurePosixPath(member.name)
                if path.is_absolute() or ".." in path.parts:
                    raise Error("Unsafe path in upstream archive")
                if path.name == executable:
                    if not member.isfile() or not 0 < member.size <= MAX_EXECUTABLE:
                        raise Error("Upstream executable is not a bounded regular file")
                    selected.append(member)
            if len(selected) != 1:
                raise Error("Archive does not contain exactly one expected executable")
            with tar.extractfile(selected[0]) as stream, Path(target).open("wb") as output:
                shutil.copyfileobj(stream, output, length=1024 * 1024)
                output.flush()
                os.fsync(output.fileno())
            if Path(target).stat().st_size != selected[0].size:
                raise Error("Truncated executable in upstream archive")
    except (tarfile.TarError, EOFError, OSError, zlib.error) as exc:
        raise Error("Cannot safely read the upstream archive") from exc
    Path(target).chmod(0o755)


def validate_elf(path, architecture):
    with Path(path).open("rb") as stream:
        header = stream.read(24)
    if len(header) < 20 or header[:4] != b"\x7fELF" or header[4] != 2 or header[5] not in (1, 2):
        raise CompatibilityError("Downloaded program is not a Linux 64-bit ELF executable")
    machine = struct.unpack_from("<H" if header[5] == 1 else ">H", header, 18)[0]
    if machine != {"x86_64": 62, "aarch64": 183}[architecture]:
        raise CompatibilityError("Downloaded program has the wrong CPU architecture")


def probe(runner, path):
    try:
        result = runner.capture([str(path), "--version"], check=False, timeout=15)
    except Error as exc:
        raise CompatibilityError("{} cannot execute on the running host".format(Path(path).name)) from exc
    match = VERSION.search(result.stdout)
    if result.returncode or not match:
        raise CompatibilityError("{} cannot report a usable version on the running host".format(Path(path).name))
    return match.group(1)


def snapshot(path):
    if not os.path.lexists(path):
        return None
    if path.is_symlink() or not path.is_file() or path.stat().st_uid != os.getuid():
        raise Error("Refusing to replace an unowned, nonregular, or symlink tool destination: {}".format(path))
    value = path.stat()
    return (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, sha256(path))


class ToolUpdater:
    def __init__(self, home, state, host, runner, client=None):
        self.home, self.state, self.host, self.runner = Path(home), Path(state), host, runner
        self.client = client or GitHub()

    def target(self, tool):
        target = self.home / destination(tool)
        try:
            target.parent.resolve().relative_to(self.home.resolve())
        except ValueError as exc:
            raise Error("Tool destination escapes the home directory") from exc
        return target

    def check_aliases(self, tool, target):
        for name in tool["aliases"]:
            alias = target.parent / name
            if os.path.lexists(alias):
                if not alias.is_symlink() or os.readlink(alias) != target.name:
                    raise Error("Refusing to overwrite a foreign alias: {}".format(alias))

    def aliases(self, tool, target):
        self.check_aliases(tool, target)
        for name in tool["aliases"]:
            alias = target.parent / name
            if os.path.lexists(alias):
                continue
            if self.runner.dry_run:
                self.runner.emit("Would create alias {} -> {}".format(alias, target.name))
            else:
                alias.symlink_to(target.name)

    def reconcile(self, tool, update=False):
        target = self.target(tool)
        before = snapshot(target)
        self.check_aliases(tool, target)
        installed = probe(self.runner, target) if before is not None else None
        policy = tool["version"]
        pin = policy["tag"].lstrip("v") if policy["policy"] == "pinned" else None
        if installed and not update and pin is not None and installed != pin:
            raise Error("{} differs from its pin; run update-host to reconcile after host updates".format(tool["id"]))
        if installed and (not update or installed == pin):
            self.runner.emit("{}: already installed ({})".format(tool["id"], installed))
            self.aliases(tool, target)
            return
        release = self.client.release(tool)
        version = release["tag_name"].lstrip("v")
        if installed == version:
            self.runner.emit("{}: current ({})".format(tool["id"], installed))
            self.aliases(tool, target)
            return
        asset = select_asset(tool, release, self.host.architecture)
        digest = expected_digest(tool, release, asset, self.client)
        if self.runner.dry_run:
            self.runner.emit("Would install {} {} at {} ({}; SHA256 available, archive not downloaded)".format(tool["id"], version, target, asset["name"]))
            self.aliases(tool, target)
            return
        self.install(tool, target, before, installed, version, asset, digest)
        self.aliases(tool, target)

    def install(self, tool, target, before, installed, version, asset, digest):
        target.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".dotfiles-" + tool["id"] + "-", dir=str(target.parent)) as directory:
            directory = Path(directory)
            archive, candidate = directory / "release.tar.gz", directory / "candidate"
            self.runner.emit("Downloading {} {} from {}".format(tool["id"], version, tool["repository"]))
            self.client.download(asset, archive)
            if archive.stat().st_size != asset["size"] or sha256(archive) != digest:
                raise Error("Release archive failed size/SHA256 verification; installed binary retained")
            extract_executable(archive, tool["executable"], candidate)
            validate_elf(candidate, self.host.architecture)
            if probe(self.runner, candidate) != version:
                raise Error("Downloaded executable version does not match the selected release")
            if snapshot(target) != before:
                raise Error("Tool destination changed concurrently; refusing replacement")
            record_dir = private_directory(self.state / "tools" / tool["id"])
            record_path = record_dir / "installed.json"
            record_before = snapshot(record_path)
            previous_record = record_path.read_bytes() if record_before is not None else None
            backup = None
            if before is not None:
                stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
                backup_dir = private_directory(record_dir / "backups" / (stamp + "-" + uuid.uuid4().hex[:8]))
                backup = backup_dir / target.name
                shutil.copy2(target, backup)
                if sha256(backup) != before[-1]:
                    raise Error("Backup verification failed; refusing replacement")
                with backup.open("rb") as stream:
                    os.fsync(stream.fileno())
                atomic_json(backup_dir / "record.json", {"version": installed, "sha256": before[-1], "destination": str(target)})
            promoted = False
            candidate_digest = sha256(candidate)
            record = {
                "version": version, "sha256": candidate_digest, "archive_sha256": digest,
                "upstream": tool["repository"], "asset": asset["name"],
                "destination": str(target), "previous_binary": str(backup) if backup else None,
            }
            record_payload = (json.dumps(record, indent=2) + "\n").encode("utf-8")
            try:
                if snapshot(target) != before or snapshot(record_path) != record_before:
                    raise Error("Tool or install record changed while creating its backup; refusing replacement")
                if before is None:
                    os.link(str(candidate), str(target))
                else:
                    os.replace(str(candidate), str(target))
                promoted = True
                sync_directory(target.parent)
                atomic_json(record_path, record)
            except (OSError, Error, KeyboardInterrupt):
                if not promoted:
                    raise
                if target.is_symlink() or not target.is_file() or sha256(target) != candidate_digest:
                    raise Error("Installed path changed externally during recovery; retained backup requires manual review")
                if backup is not None:
                    recovery = directory / "restore"
                    shutil.copy2(backup, recovery)
                    os.replace(str(recovery), str(target))
                else:
                    target.unlink()
                sync_directory(target.parent)
                # atomic_json may have replaced the record before its directory
                # fsync failed. Restore that state too, not just the executable.
                current_record = record_path.read_bytes() if snapshot(record_path) is not None else None
                if current_record != previous_record:
                    if current_record != record_payload:
                        raise Error("Install record changed externally during recovery; review the retained backup")
                    if previous_record is None:
                        record_path.unlink()
                        sync_directory(record_dir)
                    else:
                        atomic_bytes(record_path, previous_record)
                raise
            self.runner.emit("{}: installed {}{}".format(tool["id"], version, " (previous binary retained)" if backup else ""))
