"""Validate desired software and keep machine-local permission policy separate."""

from pathlib import PurePosixPath
import re

from .core import ConfigurationError, read_json


NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.+-]*\Z")
TAG = re.compile(r"v?\d+\.\d+\.\d+(?:[-+][A-Za-z0-9.-]+)?\Z")
REPOSITORY = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z")
DNF_FLAGS = {
    "-y", "--assumeyes", "--refresh", "--best", "--nobest",
    "--security", "--bugfix", "--enhancement", "--newpackage",
}


def require(condition, message):
    if not condition:
        raise ConfigurationError(message)


def name(value):
    require(isinstance(value, str) and NAME.fullmatch(value), "Invalid software/package/entry name")
    return value


def entries(value):
    require(isinstance(value, list), "Expected a list of software records")
    return value


def profiles(record):
    selected = record.get("profiles")
    require(isinstance(selected, list) and selected and all(isinstance(v, str) for v in selected) and set(selected) <= {"local", "work"}, "Invalid record profiles")
    return selected


def user_command(value):
    require(isinstance(value, list) and value and all(isinstance(v, str) and v for v in value), "An explicit updater argument list is required")
    require(PurePosixPath(value[0]).name not in {"sudo", "su", "doas", "pkexec"}, "User-tool managers must not run elevated")
    return value


def destination(tool):
    relative = tool.get("destination", ".local/bin/" + tool["executable"])
    require(isinstance(relative, str), "Invalid tool destination")
    path = PurePosixPath(relative)
    require(not path.is_absolute() and ".." not in path.parts and path.name == tool["executable"], "Tool destinations must be relative to home and end in the executable name")
    require(len(path.parts) > 1, "Tool destination must include a user directory")
    return relative


class Manifest:
    def __init__(self, data):
        require(isinstance(data, dict) and type(data.get("schema_version")) is int and data["schema_version"] == 1, "Unsupported software manifest schema")
        self.data = data
        self.profile = data.get("profile")
        require(self.profile in ("local", "work"), "Initialize chezmoi with a local/work profile first")
        definitions = data.get("profiles")
        require(isinstance(definitions, dict) and set(definitions) == {"local", "work"}, "Both host profiles must be defined")
        for key, record in definitions.items():
            require(isinstance(record, dict), "Invalid profile record")
            require(record.get("os") == {"local": "fedora", "work": "rocky"}[key], "Profile OS does not match the supported host")
            require(record.get("variant") == "sway-atomic" if key == "local" else record.get("major_version") == "9", "Unsupported host variant/version")
            packages = entries(record.get("native_packages"))
            package_names = []
            for package in packages:
                require(isinstance(package, dict), "Invalid native package record")
                package_names.append(name(package.get("name")))
                require(package["name"] not in {"chezmoi", "opencode"}, "chezmoi is standalone; OpenCode is container-only")
                require(isinstance(package.get("source"), str) and package["source"], "Record each native package source")
                if package.get("required_repository"):
                    name(package["required_repository"])
            require(len(package_names) == len(set(package_names)), "Duplicate native package")
            installations = entries(record.get("flatpak_installations"))
            ids = []
            for installation in installations:
                require(isinstance(installation, dict), "Invalid Flatpak installation")
                ids.append(name(installation.get("id")))
                require(installation.get("scope") in ("user", "system"), "Invalid Flatpak scope")
                if installation.get("name"):
                    require(installation["scope"] == "system", "Only system Flatpak installations have names")
                    name(installation["name"])
                    require(installation["name"] not in {"user", "system"}, "Named Flatpak installations must not shadow the default scopes")
                require(installation["id"] == (installation.get("name") or installation["scope"]), "Flatpak installation IDs must identify their actual scope/name, not a remappable alias")
            require(len(ids) == len(set(ids)), "Duplicate Flatpak installation")
        self.settings = definitions[self.profile]
        self.packages = self.settings["native_packages"]
        self.installations = self.settings["flatpak_installations"]
        apps = entries(data.get("flatpaks"))
        app_keys = []
        for app in apps:
            require(isinstance(app, dict), "Invalid Flatpak app")
            name(app.get("id"))
            name(app.get("remote"))
            for key in profiles(app):
                ids = {i["id"] for i in definitions[key]["flatpak_installations"]}
                require(app.get("installation") in ids, "Flatpak app refers to an undefined installation")
                app_keys.append((key, app["installation"], app["id"]))
        require(len(app_keys) == len(set(app_keys)), "Duplicate Flatpak app")
        self.apps = [app for app in apps if self.profile in app["profiles"]]
        tools = entries(data.get("downloaded_tools"))
        owners = set()
        tool_ids = set()
        for tool in tools:
            require(isinstance(tool, dict), "Invalid downloaded-tool record")
            name(tool.get("id"))
            name(tool.get("executable"))
            require(tool["id"] not in tool_ids, "Duplicate downloaded tool")
            tool_ids.add(tool["id"])
            require(tool["executable"] not in {"fish", "opencode"}, "fish is package-owned and OpenCode is container-only")
            require(isinstance(tool.get("repository"), str) and REPOSITORY.fullmatch(tool["repository"]) and not set(tool["repository"].split("/")) & {".", ".."}, "Invalid upstream GitHub repository")
            profiles(tool)
            version = tool.get("version", {})
            require(isinstance(version, dict) and version.get("policy") in ("latest", "pinned"), "Version policy must be latest or pinned")
            if version["policy"] == "pinned":
                require(isinstance(version.get("tag"), str) and TAG.fullmatch(version["tag"]), "A pinned tool requires a valid release tag")
            assets = tool.get("assets")
            require(isinstance(assets, dict) and assets and set(assets) <= {"x86_64", "aarch64"}, "Declare supported tool architectures")
            for patterns in assets.values():
                require(isinstance(patterns, list) and patterns, "Asset choices must be an ordered list")
                for pattern in patterns:
                    require(isinstance(pattern, str) and pattern.endswith(".tar.gz") and "/" not in pattern, "Only named tar.gz release assets are supported")
                    try:
                        pattern.format(tag="v1.2.3", version="1.2.3")
                    except (KeyError, ValueError) as exc:
                        raise ConfigurationError("Unsupported release-asset template") from exc
            require(isinstance(tool.get("compatibility"), str) and tool["compatibility"], "Record the tool build compatibility policy")
            if tool.get("checksum_asset") is not None:
                checksum = tool["checksum_asset"]
                require(isinstance(checksum, str) and checksum and "/" not in checksum, "Invalid upstream checksum asset")
                try:
                    checksum.format(tag="v1.2.3", version="1.2.3", asset="program.tar.gz")
                except (KeyError, ValueError) as exc:
                    raise ConfigurationError("Unsupported checksum-asset template") from exc
            relative = PurePosixPath(destination(tool))
            names = [tool["executable"]] + entries(tool.get("aliases"))
            for entry in names:
                name(entry)
                owned = str(relative.parent / entry)
                require(owned not in owners, "More than one owner for a user executable")
                owners.add(owned)
        self.tools = [tool for tool in tools if self.profile in tool["profiles"]]
        managers = entries(data.get("tool_managers"))
        manager_ids = set()
        for manager in managers:
            require(isinstance(manager, dict), "Invalid tool-manager record")
            name(manager.get("id"))
            require(manager["id"] not in manager_ids, "Duplicate tool manager")
            manager_ids.add(manager["id"])
            profiles(manager)
            user_command(manager.get("install_argv"))
            user_command(manager.get("update_argv"))
            for entry in entries(manager.get("entries")):
                name(entry)
                owned = ".local/bin/" + entry
                require(owned not in owners, "More than one owner for a user executable")
                owners.add(owned)
        self.managers = [m for m in managers if self.profile in m["profiles"]]
        self.managed = entries(data.get("managed_bin_entries"))
        require(len(self.managed) == len(set(self.managed)), "Duplicate managed script")
        for entry in self.managed:
            name(entry)
            require(".local/bin/" + entry not in owners, "A script must not also be owned by an installer")

    @classmethod
    def load(cls, path):
        try:
            return cls(read_json(path))
        except (KeyError, TypeError, ValueError) as exc:
            raise ConfigurationError("Malformed software manifest record") from exc


def work_policy(path, manifest):
    policy = read_json(path)
    require(isinstance(policy, dict) and policy.get("approved") is True, "Work operations require recorded site approval in host-policy.json")
    dnf = policy.get("dnf", {})
    require(isinstance(dnf, dict), "Invalid site dnf policy")
    repos = entries(dnf.get("repositories"))
    require(repos, "List the site-approved dnf repositories")
    for repo in repos:
        name(repo)
    for key, verbs in (("install_args", {"install"}), ("update_args", {"upgrade", "update", "upgrade-minimal"})):
        args = dnf.get(key)
        require(isinstance(args, list) and args and all(isinstance(a, str) and a for a in args), "Explicit site dnf argument lists are required")
        require(args[0] in verbs, "Unsupported site dnf operation")
        require(set(args[1:]) <= DNF_FLAGS, "Unsupported site dnf flag; only approved update filters and ordinary confirmation/refresh/solver flags are accepted, not repository/config overrides or no-op modes")
    permitted = entries(policy.get("flatpak_installations", []))
    require(all(isinstance(v, str) for v in permitted), "Invalid approved Flatpak installations")
    require({i["id"] for i in manifest.installations} <= set(permitted), "Work Flatpak installations need explicit site approval")
    return policy
