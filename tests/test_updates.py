"""Composable update tests: no native package actions or upstream network access."""

from contextlib import ExitStack, redirect_stderr, redirect_stdout
import ast
from dataclasses import FrozenInstanceError
from functools import partial
from itertools import combinations
import io
import json
import os
from pathlib import Path
import signal
import sys
from threading import Barrier, Event, Lock, get_ident
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from test_host_helpers import FakeGitHub, FakeRunner, demo_tool, elf, manifest_data, scratch

from dotfiles_host.cli import interrupt_updates, main
from dotfiles_host.core import CompatibilityError, ConfigurationError, Error, Host, Runner, host_lock
from dotfiles_host.downloads import ToolUpdater
from dotfiles_host.manifest import Manifest
from dotfiles_host.updates import Phase, UpdateTarget, run_updates
from dotfiles_host.updates.operations import update_flatpaks, update_rpm_ostree, update_user_tools
from dotfiles_host.updates.policy import (
    FLATPAK, REGISTRIES, RPM_OSTREE, USER_TOOLS, PhaseRegistry, Profile,
    build_updates, resolve_phases,
)


class MetadataTests(unittest.TestCase):
    def test_all_host_sources_parse_with_python_312_syntax(self):
        source = Path(__file__).resolve().parents[1] / "private_dot_local/lib/dotfiles_host"
        for path in source.rglob("*.py"):
            with self.subTest(path=path.relative_to(source)):
                ast.parse(path.read_text(), feature_version=(3, 12))

    def test_enum_values_are_cli_friendly(self):
        self.assertEqual([profile.value for profile in Profile], ["local", "work"])
        self.assertEqual([target.value for target in UpdateTarget], ["all", "flatpak", "user-tools", "rpm-ostree"])

    def test_phase_metadata_is_immutable_and_validated(self):
        with self.assertRaises(FrozenInstanceError):
            FLATPAK.stage = 3
        for name, stage in ((UpdateTarget.ALL, 0), ("flatpak", 0), (UpdateTarget.FLATPAK, -1), (UpdateTarget.FLATPAK, True)):
            with self.subTest(name=name, stage=stage), self.assertRaises(ConfigurationError):
                Phase(name, stage)

    def test_registry_is_immutable_and_rejects_duplicates(self):
        registry = PhaseRegistry([USER_TOOLS])
        self.assertEqual(registry.phases, (USER_TOOLS,))
        with self.assertRaises(FrozenInstanceError):
            registry.phases = ()
        for phases in ([], [USER_TOOLS, USER_TOOLS], [USER_TOOLS, Phase(UpdateTarget.USER_TOOLS, 2)], ["user-tools"]):
            with self.subTest(phases=phases), self.assertRaises(ConfigurationError):
                PhaseRegistry(phases)

    def test_all_expands_only_the_active_registry(self):
        self.assertEqual(resolve_phases(REGISTRIES[Profile.LOCAL], requested=[UpdateTarget.ALL]), (FLATPAK, USER_TOOLS, RPM_OSTREE))
        self.assertEqual(resolve_phases(REGISTRIES[Profile.WORK], requested=[UpdateTarget.ALL]), (USER_TOOLS,))

    def test_selection_is_deduplicated_and_uses_registry_order(self):
        self.assertEqual(
            resolve_phases(REGISTRIES[Profile.LOCAL], requested=[UpdateTarget.RPM_OSTREE, UpdateTarget.USER_TOOLS, UpdateTarget.USER_TOOLS]),
            (USER_TOOLS, RPM_OSTREE),
        )

    def test_invalid_or_unavailable_selections_are_rejected(self):
        for registry, requested in (
            (REGISTRIES[Profile.LOCAL], []),
            (REGISTRIES[Profile.LOCAL], ["flatpak"]),
            (REGISTRIES[Profile.LOCAL], [UpdateTarget.ALL, UpdateTarget.FLATPAK]),
            (REGISTRIES[Profile.WORK], [UpdateTarget.FLATPAK]),
            (REGISTRIES[Profile.WORK], [UpdateTarget.USER_TOOLS, UpdateTarget.RPM_OSTREE]),
        ):
            with self.subTest(requested=requested), self.assertRaises(ConfigurationError):
                resolve_phases(registry, requested=requested)


class ExecutorTests(unittest.TestCase):
    def test_single_operation_runs_directly_and_result_references_its_phase(self):
        caller = get_ident()
        report = run_updates({USER_TOOLS: lambda: self.assertEqual(get_ident(), caller)})
        self.assertIs(report.results[0].phase, USER_TOOLS)
        self.assertTrue(report.results[0].succeeded)
        self.assertEqual(report.exit_code, 0)

    def test_every_nonempty_local_combination_runs_exactly_selected_operations(self):
        for size in range(1, 4):
            for selected in combinations((FLATPAK, USER_TOOLS, RPM_OSTREE), size):
                with self.subTest(selected=selected):
                    calls = []
                    lock = Lock()

                    def update(phase):
                        with lock:
                            calls.append(phase)

                    report = run_updates({phase: partial(update, phase) for phase in selected})
                    self.assertCountEqual(calls, selected)
                    self.assertEqual(tuple(result.phase for result in report.results), selected)
                    self.assertEqual(report.exit_code, 0)

    def test_stage_zero_overlaps_and_os_waits_for_both_even_after_failure(self):
        barrier = Barrier(2)
        finished = set()
        lock = Lock()

        def update(phase):
            barrier.wait(timeout=3)
            with lock:
                finished.add(phase)
            if phase is FLATPAK:
                raise Error("offline")

        def os_update():
            self.assertEqual(finished, {FLATPAK, USER_TOOLS})
            finished.add(RPM_OSTREE)

        # Deliberately put the OS first: stages, not mapping position, control it.
        report = run_updates({RPM_OSTREE: os_update, FLATPAK: partial(update, FLATPAK), USER_TOOLS: partial(update, USER_TOOLS)})
        self.assertEqual([result.phase for result in report.results], [FLATPAK, USER_TOOLS, RPM_OSTREE])
        self.assertEqual([result.succeeded for result in report.results], [False, True, True])
        self.assertEqual(report.results[0].detail, "offline")
        self.assertEqual(report.exit_code, 1)

    def test_report_order_does_not_follow_completion_order(self):
        first_started, second_finished = Event(), Event()

        def first():
            first_started.set()
            self.assertTrue(second_finished.wait(3))

        def second():
            self.assertTrue(first_started.wait(3))
            second_finished.set()

        report = run_updates({FLATPAK: first, USER_TOOLS: second})
        self.assertEqual([result.phase for result in report.results], [FLATPAK, USER_TOOLS])

    def test_expected_oserror_is_reported_without_blocking_later_stages(self):
        calls = []

        def fail():
            raise OSError("missing updater")

        report = run_updates({USER_TOOLS: fail, RPM_OSTREE: lambda: calls.append("os")})
        self.assertEqual(calls, ["os"])
        self.assertFalse(report.results[0].succeeded)

    def test_interrupt_and_unexpected_errors_prevent_later_stages(self):
        for exception in (KeyboardInterrupt(), RuntimeError("bug")):
            calls = []

            def fail():
                raise exception

            with self.subTest(exception=exception), self.assertRaises(type(exception)):
                run_updates({USER_TOOLS: fail, RPM_OSTREE: lambda: calls.append("os")})
            self.assertEqual(calls, [])

    def test_invalid_plans_are_rejected_before_any_callback(self):
        calls = []
        update = lambda: calls.append("called")
        for updates in (
            {}, {UpdateTarget.USER_TOOLS: update}, {USER_TOOLS: None},
            {USER_TOOLS: update, Phase(UpdateTarget.USER_TOOLS, 2): update},
        ):
            with self.subTest(updates=updates), self.assertRaises(ConfigurationError):
                run_updates(updates)
        self.assertEqual(calls, [])


class PolicyTests(unittest.TestCase):
    def build(self, home, profile="local", phases=None, dry_run=True, output=lambda message: None):
        manifest = Manifest(manifest_data(profile))
        host = Host(profile, "fedora" if profile == "local" else "rocky", "x86_64")
        return build_updates(
            phases if phases is not None else REGISTRIES[Profile(profile)].phases,
            manifest=manifest, host=host, home=home, state=home / ".local/state/dotfiles",
            policy_path=home / "host-policy.json", dry_run=dry_run,
            cancelled=Event(), output=output,
        )

    def test_local_binding_is_lazy_and_creates_no_state(self):
        with scratch() as tmp, patch.object(Runner, "run", side_effect=AssertionError("query during binding")):
            home = Path(tmp)
            before = set(home.rglob("*"))
            updates = self.build(home)
            self.assertEqual(tuple(updates), (FLATPAK, USER_TOOLS, RPM_OSTREE))
            self.assertEqual(set(home.rglob("*")), before)
            for update in updates.values():
                self.assertIsInstance(update, partial)
                self.assertTrue(update.keywords["runner"].dry_run)

    def test_work_needs_only_approval_not_dnf_or_flatpak_policy(self):
        with scratch() as tmp, patch.object(Runner, "run", side_effect=AssertionError("native query")):
            home = Path(tmp)
            (home / "host-policy.json").write_text(json.dumps({"approved": True, "dnf": "unused", "flatpak_installations": "unused"}))
            updates = self.build(home, "work", dry_run=False)
            self.assertEqual(tuple(updates), (USER_TOOLS,))
            self.assertFalse((home / ".local/state/dotfiles").exists())

    def test_work_approval_missing_false_or_writable_is_rejected(self):
        with scratch() as tmp:
            home = Path(tmp)
            policy = home / "host-policy.json"
            with self.assertRaises(ConfigurationError):
                self.build(home, "work")
            policy.write_text('{"approved": false}')
            with self.assertRaises(ConfigurationError):
                self.build(home, "work")
            policy.write_text('{"approved": true}')
            policy.chmod(0o666)
            with self.assertRaisesRegex(ConfigurationError, "writable"):
                self.build(home, "work")

    def test_work_rejects_unavailable_or_modified_phases_before_binding(self):
        with scratch() as tmp:
            for phase in (FLATPAK, RPM_OSTREE, Phase(UpdateTarget.USER_TOOLS, 1)):
                with self.subTest(phase=phase), self.assertRaises(ConfigurationError):
                    self.build(Path(tmp), "work", phases=[phase])

    def test_output_is_prefixed_per_phase_and_per_line(self):
        with scratch() as tmp:
            output = []
            updates = self.build(Path(tmp), phases=[FLATPAK], output=output.append)
            updates[FLATPAK].keywords["runner"].emit("one\ntwo")
            self.assertEqual(output, ["[flatpak] one", "[flatpak] two"])

    def test_bound_all_dry_run_downloads_no_archives_and_creates_no_state(self):
        with scratch() as tmp:
            home = Path(tmp)
            manifest = Manifest(manifest_data())
            manifest.tools = [demo_tool()]
            client = FakeGitHub()
            output = []
            before = set(home.rglob("*"))

            def capture(runner, argv, **kwargs):
                self.assertEqual(argv, ["rpm-ostree", "status", "--json"])
                return SimpleNamespace(stdout='{"deployments": [{"booted": true}]}')

            updates = build_updates(
                REGISTRIES[Profile.LOCAL].phases,
                manifest=manifest, host=Host("local", "fedora", "x86_64"),
                home=home, state=home / "state", policy_path=home / "missing-policy.json",
                dry_run=True, cancelled=Event(), output=output.append,
            )
            with patch.object(Runner, "capture", capture), patch("dotfiles_host.downloads.GitHub", return_value=client), patch("dotfiles_host.core.subprocess.Popen") as command:
                report = run_updates(updates)
            self.assertEqual(report.exit_code, 0)
            self.assertEqual(client.calls, 1)
            self.assertEqual(client.downloads, 0)
            command.assert_not_called()
            self.assertEqual(set(home.rglob("*")), before)
            self.assertTrue(any("[flatpak] Would run: flatpak update --user" in line for line in output))
            self.assertTrue(any("[rpm-ostree] Would run: rpm-ostree upgrade" in line for line in output))


class OperationTests(unittest.TestCase):
    def test_flatpak_failures_do_not_block_other_installations(self):
        runner = FakeRunner()
        runner.fail_action = lambda argv: "--user" in argv
        installations = [
            {"id": "user", "scope": "user"},
            {"id": "system", "scope": "system"},
            {"id": "extra", "scope": "system", "name": "extra"},
        ]
        with self.assertRaisesRegex(Error, "incomplete: user"):
            update_flatpaks(flatpak_installations=installations, runner=runner)
        self.assertEqual([event[1][2] for event in runner.events], ["--user", "--system", "--installation=extra"])
        self.assertTrue(all(event[0] == "action" for event in runner.events))

    def test_rpm_ostree_is_local_only_and_never_reboots(self):
        runner = FakeRunner()
        with self.assertRaises(ConfigurationError):
            update_rpm_ostree(host=Host("work", "rocky", "x86_64"), runner=runner)
        self.assertEqual(runner.events, [])
        update_rpm_ostree(host=Host("local", "fedora", "x86_64"), runner=runner)
        self.assertEqual(runner.events[0], ("action", ["rpm-ostree", "upgrade"]))
        self.assertEqual(len(runner.events), 2)
        self.assertTrue(any("manual reboot" in message for message in runner.messages))

    def test_work_user_tools_never_query_native_or_flatpak_commands(self):
        with scratch() as tmp:
            runner = FakeRunner()
            manifest = Manifest(manifest_data("work"))
            calls = []
            updater = SimpleNamespace(reconcile=lambda tool, update: calls.append((tool["id"], update)))
            with patch("dotfiles_host.updates.operations.ToolUpdater", return_value=updater):
                update_user_tools(manifest=manifest, host=Host("work", "rocky", "x86_64"), home=Path(tmp), state=Path(tmp) / "state", runner=runner)
            self.assertEqual(len(calls), len(manifest.tools))
            self.assertTrue(all(update for _, update in calls))
            self.assertEqual(runner.events, [])

    def test_user_tools_and_managers_continue_after_failure_and_uv_is_untouched(self):
        with scratch() as tmp:
            home = Path(tmp)
            local_bin = home / ".local/bin"
            local_bin.mkdir(parents=True)
            (local_bin / "uv").write_text("chezmoi owns this")
            manifest = Manifest(manifest_data("work"))
            manifest.tools = [dict(demo_tool(), id="bad"), dict(demo_tool(), id="good")]
            manifest.managers = [
                {"id": name, "entries": [], "update_argv": [name, "update"]}
                for name in ("bad-manager", "good-manager")
            ]
            runner = FakeRunner()
            runner.fail_action = lambda argv: argv[0] == "bad-manager"
            calls = []

            def reconcile(tool, update):
                calls.append(tool["id"])
                if tool["id"] == "bad":
                    raise Error("unavailable")

            with patch("dotfiles_host.updates.operations.ToolUpdater", return_value=SimpleNamespace(reconcile=reconcile)):
                with self.assertRaisesRegex(Error, "bad, bad-manager"):
                    update_user_tools(manifest=manifest, host=Host("work", "rocky", "x86_64"), home=home, state=home / "state", runner=runner)
            self.assertEqual(calls, ["bad", "good"])
            self.assertEqual(runner.events, [("action", ["bad-manager", "update"]), ("action", ["good-manager", "update"])])
            self.assertEqual((local_bin / "uv").read_text(), "chezmoi owns this")

    def test_tools_only_reads_pending_deployment_for_compatibility_guidance(self):
        with scratch() as tmp:
            manifest = Manifest(manifest_data())
            manifest.tools = [demo_tool()]
            runner = FakeRunner()
            runner.pending = [{"staged": True}]

            def reconcile(tool, update):
                raise CompatibilityError("requires a newer booted host")

            with patch("dotfiles_host.updates.operations.ToolUpdater", return_value=SimpleNamespace(reconcile=reconcile)):
                with self.assertRaises(Error):
                    update_user_tools(manifest=manifest, host=Host("local", "fedora", "x86_64"), home=Path(tmp), state=Path(tmp) / "state", runner=runner)
            self.assertEqual(runner.events[0], ("query", ["rpm-ostree", "status", "--json"]))
            self.assertFalse(any(event[0] == "action" for event in runner.events))
            self.assertTrue(any("rerun update-host --user-tools" in message for message in runner.messages))


class UpdateCLITests(unittest.TestCase):
    def test_update_as_root_is_rejected_before_configuration_or_state(self):
        with patch("dotfiles_host.cli.os.geteuid", return_value=0), patch("dotfiles_host.cli.Manifest.load") as load, redirect_stderr(io.StringIO()):
            self.assertEqual(main("update", ["--user-tools"]), 2)
        load.assert_not_called()

    def invoke(self, arguments, profile="local", approved=True, failures=None):
        with scratch() as tmp, ExitStack() as stack:
            home = Path(tmp)
            policy = home / "host-policy.json"
            if approved:
                policy.write_text('{"approved": true}')
            output, errors = io.StringIO(), io.StringIO()
            stack.enter_context(redirect_stdout(output))
            stack.enter_context(redirect_stderr(errors))
            stack.enter_context(patch("dotfiles_host.cli.os.geteuid", return_value=1000))
            stack.enter_context(patch("dotfiles_host.cli.Path.home", return_value=home))
            stack.enter_context(patch("dotfiles_host.cli.Manifest.load", return_value=Manifest(manifest_data(profile))))
            stack.enter_context(patch("dotfiles_host.cli.detect_host", return_value=Host(profile, "fedora" if profile == "local" else "rocky", "x86_64")))
            operations = {}
            for target, name in (
                (UpdateTarget.FLATPAK, "update_flatpaks"),
                (UpdateTarget.USER_TOOLS, "update_user_tools"),
                (UpdateTarget.RPM_OSTREE, "update_rpm_ostree"),
            ):
                operations[target] = stack.enter_context(patch("dotfiles_host.updates.policy." + name, side_effect=(failures or {}).get(target)))
            code = main("update", ["--policy", str(policy), *arguments])
            return (
                code, {target: operation.call_count for target, operation in operations.items()},
                output.getvalue(), errors.getvalue(), (home / ".local/state/dotfiles").exists(),
            )

    def test_default_all_and_explicit_all_run_all_local_phases(self):
        for arguments in ([], ["--all"]):
            with self.subTest(arguments=arguments):
                code, counts, output, errors, state = self.invoke(arguments)
                self.assertEqual(code, 0, errors)
                self.assertEqual(list(counts.values()), [1, 1, 1])
                self.assertTrue(state)
                self.assertIn("rpm-ostree: OK", output)

    def test_composable_and_duplicate_flags_select_exactly_requested_operations(self):
        code, counts, _, errors, _ = self.invoke(["--user-tools", "--flatpak", "--flatpak"])
        self.assertEqual(code, 0, errors)
        self.assertEqual(counts, {UpdateTarget.FLATPAK: 1, UpdateTarget.USER_TOOLS: 1, UpdateTarget.RPM_OSTREE: 0})

    def test_invalid_mixed_all_and_work_native_flags_do_nothing(self):
        for arguments, profile in (
            (["--all", "--user-tools"], "local"),
            (["--flatpak", "--user-tools"], "work"),
            (["--rpm-ostree"], "work"),
        ):
            with self.subTest(arguments=arguments, profile=profile):
                code, counts, _, errors, state = self.invoke(arguments, profile)
                self.assertEqual(code, 2)
                self.assertFalse(any(counts.values()))
                self.assertFalse(state)
                self.assertIn("Configuration/policy error", errors)

    def test_work_all_runs_only_user_tools_and_requires_approval_even_in_dry_run(self):
        code, counts, _, errors, state = self.invoke(["--all", "--dry-run"], "work")
        self.assertEqual(code, 0, errors)
        self.assertEqual(counts, {UpdateTarget.FLATPAK: 0, UpdateTarget.USER_TOOLS: 1, UpdateTarget.RPM_OSTREE: 0})
        self.assertFalse(state)
        code, counts, _, _, state = self.invoke(["--dry-run"], "work", approved=False)
        self.assertEqual(code, 2)
        self.assertFalse(any(counts.values()))
        self.assertFalse(state)

    def test_phase_failure_returns_one_and_still_attempts_os(self):
        code, counts, output, _, _ = self.invoke([], failures={UpdateTarget.FLATPAK: Error("offline")})
        self.assertEqual(code, 1)
        self.assertEqual(list(counts.values()), [1, 1, 1])
        self.assertIn("flatpak: FAILED: offline", output)
        self.assertIn("rpm-ostree: OK", output)

    def test_interrupt_returns_130_without_starting_os(self):
        code, counts, _, errors, _ = self.invoke([], failures={UpdateTarget.USER_TOOLS: KeyboardInterrupt()})
        self.assertEqual(code, 130)
        self.assertEqual(counts[UpdateTarget.RPM_OSTREE], 0)
        self.assertIn("Interrupted", errors)

    def test_dry_run_does_not_create_state(self):
        code, _, output, errors, state = self.invoke(["--dry-run"])
        self.assertEqual(code, 0, errors)
        self.assertFalse(state)
        self.assertIn("no operational changes", output)

    def test_cli_holds_the_shared_host_lock_while_an_update_runs(self):
        def user_tools_update(*, state, **kwargs):
            with self.assertRaisesRegex(Error, "Another setup-host/update-host"):
                with host_lock(state):
                    self.fail("Update ran without the coordinator lock")

        code, _, _, errors, state = self.invoke(["--user-tools"], failures={UpdateTarget.USER_TOOLS: user_tools_update})
        self.assertEqual(code, 0, errors)
        self.assertTrue(state)


class CancellationAndRunnerTests(unittest.TestCase):
    def test_parallel_interrupt_stops_active_commands_and_never_starts_os(self):
        with scratch() as tmp:
            home = Path(tmp)
            cancelled = Event()
            lock = Lock()
            ready = set()
            os_calls = []

            def output(phase, message):
                if message != "ready":
                    return
                with lock:
                    ready.add(phase)
                    if ready == {FLATPAK, USER_TOOLS}:
                        os.kill(os.getpid(), signal.SIGINT)

            updates = {}
            for phase in (FLATPAK, USER_TOOLS):
                runner = Runner(cancelled=cancelled, emit=partial(output, phase))
                command = [
                    sys.executable, "-c",
                    'import os, pathlib, sys, time; pathlib.Path(sys.argv[1]).write_text(str(os.getpid())); print("ready", flush=True); time.sleep(30)',
                    str(home / phase.name.value),
                ]
                updates[phase] = partial(runner.action, command)
            updates[RPM_OSTREE] = lambda: os_calls.append("os")
            with self.assertRaises(KeyboardInterrupt):
                with interrupt_updates(cancelled):
                    run_updates(updates)
            self.assertTrue(cancelled.is_set())
            self.assertEqual(os_calls, [])
            for phase in (FLATPAK, USER_TOOLS):
                with self.assertRaises(ProcessLookupError):
                    os.kill(int((home / phase.name.value).read_text()), 0)

    def test_signal_guard_sets_event_and_restores_original_handler(self):
        cancelled = Event()
        previous = signal.getsignal(signal.SIGINT)
        with self.assertRaises(KeyboardInterrupt):
            with interrupt_updates(cancelled):
                signal.raise_signal(signal.SIGINT)
        self.assertTrue(cancelled.is_set())
        self.assertIs(signal.getsignal(signal.SIGINT), previous)

    def test_update_capture_preserves_stdout_stderr_and_exit_status(self):
        messages = []
        runner = Runner(cancelled=Event(), emit=messages.append)
        result = runner.capture([sys.executable, "-c", 'import sys; print("{\\"ok\\": true}"); print("warning", file=sys.stderr); sys.exit(3)'], check=False)
        self.assertEqual(json.loads(result.stdout), {"ok": True})
        self.assertEqual(result.stderr, "warning\n")
        self.assertEqual(result.returncode, 3)
        self.assertEqual(messages, [])

    def test_update_actions_stream_stdout_stderr_and_trailing_partial_line(self):
        messages = []
        runner = Runner(cancelled=Event(), emit=messages.append)
        runner.action([sys.executable, "-c", 'import sys; print("out"); print("err", file=sys.stderr); print("tail", end="")'])
        self.assertTrue(messages[0].startswith("Running: "))
        self.assertCountEqual(messages[1:], ["out", "err", "tail"])

    def test_update_timeouts_and_failures_are_normalized(self):
        runner = Runner(cancelled=Event(), emit=lambda message: None)
        with self.assertRaisesRegex(Error, "unavailable or timed out"):
            runner.capture([sys.executable, "-c", "import time; time.sleep(30)"], timeout=0.1)
        with self.assertRaisesRegex(Error, "specific failure"):
            runner.capture([sys.executable, "-c", 'import sys; print("specific failure", file=sys.stderr); sys.exit(1)'])

    def test_cancelling_active_command_reaps_it(self):
        with scratch() as tmp:
            pidfile = Path(tmp) / "pid"
            cancelled = Event()

            def output(message):
                if message == "ready":
                    cancelled.set()

            runner = Runner(cancelled=cancelled, emit=output)
            command = [sys.executable, "-c", 'import os, pathlib, sys, time; pathlib.Path(sys.argv[1]).write_text(str(os.getpid())); print("ready", flush=True); time.sleep(30)', str(pidfile)]
            with self.assertRaises(KeyboardInterrupt):
                runner.action(command)
            pid = int(pidfile.read_text())
            with self.assertRaises(ProcessLookupError):
                os.kill(pid, 0)

    def test_cancelled_dry_run_never_starts_an_action(self):
        cancelled = Event()
        cancelled.set()
        runner = Runner(dry_run=True, cancelled=cancelled, emit=lambda message: None)
        with patch("dotfiles_host.core.subprocess.Popen") as command, self.assertRaises(KeyboardInterrupt):
            runner.action(["not-an-updater"])
        command.assert_not_called()

    def test_cancel_after_download_retains_binary_and_cleans_temporary_archive(self):
        with scratch() as tmp:
            home = Path(tmp)
            target = home / ".local/bin/demo"
            target.parent.mkdir(parents=True)
            original = elf("1.0.0")
            target.write_bytes(original)
            target.chmod(0o755)
            cancelled = Event()
            runner = FakeRunner()

            def check_cancelled():
                if cancelled.is_set():
                    raise KeyboardInterrupt

            runner.check_cancelled = check_cancelled
            client = FakeGitHub()
            download = client.download

            def cancel_after_download(asset, path):
                download(asset, path)
                cancelled.set()

            client.download = cancel_after_download
            updater = ToolUpdater(home, home / "state", Host("local", "fedora", "x86_64"), runner, client)
            with self.assertRaises(KeyboardInterrupt):
                updater.reconcile(demo_tool(), update=True)
            self.assertEqual(target.read_bytes(), original)
            self.assertEqual(list(target.parent.glob(".dotfiles-*")), [])
            self.assertFalse((home / "state").exists())


if __name__ == "__main__":
    unittest.main()
