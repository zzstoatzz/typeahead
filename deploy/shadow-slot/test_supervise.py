"""The slot supervisor, run for real against stand-in processes.

Each test starts `supervise` under bash 5.2 or newer, the image's being 5.2,
with `SHADOW_APP` pointing at a directory where both binaries are one shell
script. The script records its starts and signals in a file and follows a
plan, one line per start, that says when to exit. Nothing here uses the
network or the real binaries.
"""

import os
import shutil
import signal
import stat
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

SUPERVISE = Path(__file__).with_name("supervise")

STUB = r"""#!/bin/sh
case ${0##*/} in typeahead-ingester*) role=store ;; *) role=index ;; esac
note() { echo "$1 $role $$" >> "$STUB_DIR/events"; }
plan=$STUB_DIR/$role.plan
step=
if [ -s "$plan" ]; then
  step=$(head -n 1 "$plan")
  tail -n +2 "$plan" > "$plan.next"
  mv "$plan.next" "$plan"
fi
echo "up MODE=${MODE:-} STORE_DIR=${STORE_DIR:-} PORT=${PORT:-} args=$*"
echo "binary ${0##*/}"
note start
set -- $step
verb=${1:-run}
arg=${2:-0}
case $verb in
  exit) exit "$arg" ;;
  exit-after) sleep "$arg"; exit 1 ;;
  ignore-term) trap 'note term' TERM ;;
  term-exit) trap 'note term; exit "$arg"' TERM ;;
  term-slow) trap 'note term; sleep "$arg"; note stopped; exit 0' TERM ;;
  run) trap 'note term; exit 0' TERM ;;
esac
while :; do
  sleep 0.1
done
"""


def find_bash() -> str:
    candidates = [
        os.environ.get("SUPERVISE_BASH"),
        shutil.which("bash"),
        "/opt/homebrew/opt/bash/bin/bash",
        "/usr/local/opt/bash/bin/bash",
        "/opt/homebrew/bin/bash",
        "/usr/local/bin/bash",
    ]
    seen = []
    for candidate in candidates:
        if not candidate or not os.access(candidate, os.X_OK):
            continue
        version = subprocess.run(
            [candidate, "-c", 'echo "${BASH_VERSINFO[0]} ${BASH_VERSINFO[1]}"'],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.split()
        if (int(version[0]), int(version[1])) >= (5, 2):
            return candidate
        seen.append(f"{candidate} is {'.'.join(version)}")
    raise RuntimeError(
        "supervise needs bash 5.2 or newer and none was found"
        f" ({'; '.join(seen) or 'no bash at all'})."
        " On macOS run `brew install bash`, or set SUPERVISE_BASH to one."
    )


BASH = ""


def setUpModule() -> None:
    global BASH
    BASH = find_bash()


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


class Slot:
    def __init__(self, case: unittest.TestCase, *, plans: dict[str, list[str]] | None = None, cpu_flags: str | None = None, **settings: int | str):
        self.case = case
        self.root = Path(tempfile.mkdtemp(prefix="supervise-test."))
        case.addCleanup(shutil.rmtree, self.root, True)
        (self.root / "data").mkdir()
        app = self.root / "app"
        app.mkdir()
        for binary in ("typeahead-ingester", "typeahead-tantivy"):
            (app / binary).write_text(STUB)
            (app / binary).chmod(stat.S_IRWXU)
        if cpu_flags is not None:
            (app / "typeahead-ingester.aes").write_text(STUB)
            (app / "typeahead-ingester.aes").chmod(stat.S_IRWXU)
            (self.root / "cpuinfo").write_text(f"processor\t: 0\nflags\t\t: {cpu_flags}\n\nprocessor\t: 1\nflags\t\t: {cpu_flags}\n")
        self.events_path = self.root / "events"
        self.events_path.touch()
        for role, plan in (plans or {}).items():
            (self.root / f"{role}.plan").write_text("".join(f"{line}\n" for line in plan))
        self.output_path = self.root / "output"
        environment = {
            "PATH": os.environ["PATH"],
            "SHADOW_DATA": str(self.root / "data"),
            "SHADOW_APP": str(app),
            "SHADOW_CPUINFO": str(self.root / "cpuinfo"),
            "STUB_DIR": str(self.root),
            "SHADOW_RESTART_MIN_S": "1",
            "SHADOW_RESTART_MAX_S": "4",
            "SHADOW_RESTART_STABLE_S": "300",
            "SHADOW_STOP_GRACE_S": "20",
            "SHADOW_STORE_STOP_S": "10",
            "SHADOW_TICK_S": "0.1",
        }
        environment.update({name: str(value) for name, value in settings.items()})
        with self.output_path.open("w") as output:
            self.process = subprocess.Popen(
                [BASH, str(SUPERVISE)],
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=output,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        self.started_at = time.monotonic()
        case.addCleanup(self.destroy)

    def destroy(self) -> None:
        try:
            os.killpg(self.process.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass  # gone already; macOS answers EPERM for a group of zombies
        self.process.wait()

    def events(self) -> list[tuple[str, str, int]]:
        rows = (line.split() for line in self.events_path.read_text().splitlines())
        return [(what, role, int(pid)) for what, role, pid in rows]

    def pids(self, what: str, role: str) -> list[int]:
        return [pid for event, name, pid in self.events() if event == what and name == role]

    def output(self) -> str:
        return self.output_path.read_text()

    def wait_for(self, what: str, role: str, count: int = 1, timeout: float = 20) -> list[int]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            found = self.pids(what, role)
            if len(found) >= count:
                return found
            if self.process.poll() is not None:
                break
            time.sleep(0.02)
        self.case.fail(f"no {count} x '{what} {role}' within {timeout}s\nevents: {self.events()}\n{self.output()}")

    def expect(self, text: str, count: int = 1, timeout: float = 5) -> None:
        """Lines from the processes pass through a pipe, so they can trail the events file."""
        deadline = time.monotonic() + timeout
        while self.output().count(text) < count and time.monotonic() < deadline:
            time.sleep(0.02)
        self.case.assertEqual(count, self.output().count(text), f"{text!r} in:\n{self.output()}")

    def signal(self, signum: int) -> None:
        os.kill(self.process.pid, signum)

    def exit_status(self, timeout: float = 20) -> int:
        try:
            return self.process.wait(timeout)
        except subprocess.TimeoutExpired:
            self.case.fail(f"the supervisor did not exit within {timeout}s\nevents: {self.events()}\n{self.output()}")

    def restart_delays(self, role: str) -> list[int]:
        marker = "starting it again in "
        return [
            int(line.split(marker)[1].split("s")[0])
            for line in self.output().splitlines()
            if line.startswith(f"[supervisor] {role} stopped on its own") and marker in line
        ]


class SuperviseTest(unittest.TestCase):
    def stop(self, slot: Slot) -> None:
        slot.signal(signal.SIGTERM)
        self.assertEqual(0, slot.exit_status(), slot.output())

    def test_a_store_exit_restarts_the_store_and_leaves_the_index_alone(self) -> None:
        slot = Slot(self, plans={"store": ["exit 3"]}, STORE_PORT="18082")
        first, second = slot.wait_for("start", "store", 2)
        (index,) = slot.wait_for("start", "index")
        self.assertNotEqual(first, second)
        self.assertTrue(alive(index))
        self.assertTrue(alive(second))
        self.assertEqual([index], slot.pids("start", "index"))
        self.assertEqual([], slot.pids("term", "index"))
        slot.expect(f"[store] up MODE=store STORE_DIR={slot.root}/data/store PORT=18082 args=\n", 2)
        slot.expect("[index] up MODE= STORE_DIR= PORT= args=serve --data")
        slot.expect("[supervisor] store exited with status 3 after ")
        slot.expect("store stopped on its own, 1 time(s) without staying up 300s: starting it again in 1s, index keeps running")
        self.assertIsNone(slot.process.poll())
        self.stop(slot)
        self.assertFalse(alive(index))
        self.assertFalse(alive(second))

    def test_a_store_that_cannot_stay_up_is_retried_with_a_wait_that_doubles_to_the_cap(self) -> None:
        slot = Slot(self, plans={"store": ["exit 1"] * 4})
        stores = slot.wait_for("start", "store", 5, timeout=40)
        elapsed = time.monotonic() - slot.started_at
        self.assertEqual(5, len(set(stores)))
        self.assertEqual([1, 2, 4, 4], slot.restart_delays("store"))
        self.assertGreaterEqual(elapsed, 1 + 2 + 4 + 4)
        self.assertIn("store stopped on its own, 4 time(s) without staying up 300s", slot.output())
        (index,) = slot.pids("start", "index")
        self.assertTrue(alive(index))
        self.assertEqual([], slot.pids("term", "index"))
        self.assertIsNone(slot.process.poll())
        self.stop(slot)

    def test_the_wait_returns_to_the_minimum_once_a_process_has_stayed_up(self) -> None:
        # the supervisor counts whole seconds and a loaded machine starts a
        # process slowly, so "stayed up" is set well above an immediate exit
        slot = Slot(self, plans={"store": ["exit 1", "exit 1", "exit-after 6"]}, SHADOW_RESTART_STABLE_S=5)
        slot.wait_for("start", "store", 4, timeout=40)
        self.assertEqual([1, 2, 1], slot.restart_delays("store"))
        self.assertIn("store stopped on its own, 2 time(s) without staying up 5s", slot.output())
        self.assertEqual(2, slot.output().count("store stopped on its own, 1 time(s)"))
        self.stop(slot)

    def test_an_index_exit_restarts_the_index_and_leaves_the_store_alone(self) -> None:
        slot = Slot(self, plans={"index": ["exit 9", "exit 9"]})
        indexes = slot.wait_for("start", "index", 3)
        (store,) = slot.pids("start", "store")
        self.assertEqual(3, len(set(indexes)))
        self.assertTrue(alive(store))
        self.assertTrue(alive(indexes[-1]))
        self.assertEqual([], slot.pids("term", "store"))
        self.assertEqual([1, 2], slot.restart_delays("index"))
        self.assertEqual([], slot.restart_delays("store"))
        self.assertIn("[supervisor] index exited with status 9 after ", slot.output())
        self.assertIn("starting it again in 1s, store keeps running", slot.output())
        self.stop(slot)

    def test_both_processes_killed_at_once_are_both_restarted(self) -> None:
        slot = Slot(self)
        (store,) = slot.wait_for("start", "store")
        (index,) = slot.wait_for("start", "index")
        os.kill(store, signal.SIGKILL)
        os.kill(index, signal.SIGKILL)
        slot.wait_for("start", "store", 2)
        slot.wait_for("start", "index", 2)
        self.assertIn("[supervisor] store exited with status 137 after ", slot.output())
        self.assertIn("[supervisor] index exited with status 137 after ", slot.output())
        self.assertIsNone(slot.process.poll())
        self.stop(slot)

    def test_sigterm_stops_the_store_and_then_the_index(self) -> None:
        slot = Slot(self, plans={"store": ["term-slow 1"]})
        (store,) = slot.wait_for("start", "store")
        (index,) = slot.wait_for("start", "index")
        slot.signal(signal.SIGTERM)
        self.assertEqual(0, slot.exit_status())
        self.assertEqual(
            [("term", "store", store), ("stopped", "store", store), ("term", "index", index)],
            slot.events()[2:],
        )
        self.assertFalse(alive(store))
        self.assertFalse(alive(index))
        self.assertTrue(slot.output().endswith("[supervisor] exiting 0 after SIGTERM\n"), slot.output())

    def test_sigint_is_passed_on_as_sigterm(self) -> None:
        slot = Slot(self)
        slot.wait_for("start", "store")
        slot.wait_for("start", "index")
        slot.signal(signal.SIGINT)
        self.assertEqual(0, slot.exit_status())
        self.assertEqual(["term", "term"], [what for what, _, _ in slot.events()[2:]])
        self.assertIn("[supervisor] exiting 0 after SIGINT", slot.output())

    def test_a_failed_stop_is_the_exit_status(self) -> None:
        slot = Slot(self, plans={"store": ["term-exit 5"]})
        slot.wait_for("start", "store")
        slot.wait_for("start", "index")
        slot.signal(signal.SIGTERM)
        self.assertEqual(5, slot.exit_status())

    def test_a_store_that_ignores_sigterm_does_not_hold_the_index_and_is_killed_at_the_deadline(self) -> None:
        slot = Slot(self, plans={"store": ["ignore-term"]}, SHADOW_STORE_STOP_S=1, SHADOW_STOP_GRACE_S=3)
        (store,) = slot.wait_for("start", "store")
        (index,) = slot.wait_for("start", "index")
        slot.signal(signal.SIGTERM)
        asked_at = time.monotonic()
        slot.wait_for("term", "index")
        self.assertGreaterEqual(time.monotonic() - asked_at, 0.9)
        self.assertTrue(alive(store))
        self.assertEqual(137, slot.exit_status())
        self.assertGreaterEqual(time.monotonic() - asked_at, 2.9)
        self.assertFalse(alive(store))
        self.assertFalse(alive(index))
        self.assertIn("[supervisor] store still running 3s after the stop was requested, sending SIGKILL", slot.output())

    def test_a_stop_while_the_store_waits_to_restart_does_not_start_it(self) -> None:
        slot = Slot(self, plans={"store": ["exit 1"]}, SHADOW_RESTART_MIN_S=30, SHADOW_RESTART_MAX_S=30)
        (index,) = slot.wait_for("start", "index")
        deadline = time.monotonic() + 10
        while not slot.restart_delays("store") and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertEqual([30], slot.restart_delays("store"))
        slot.signal(signal.SIGTERM)
        self.assertEqual(0, slot.exit_status(timeout=10))
        self.assertEqual(1, len(slot.pids("start", "store")))
        self.assertEqual([index], slot.pids("term", "index"))

    def test_the_aes_build_runs_only_on_a_cpu_that_reports_every_feature_it_needs(self) -> None:
        needed = "fpu cx16 lahf_lm popcnt pni ssse3 sse4_1 sse4_2 avx aes pclmulqdq avx2"
        slot = Slot(self, cpu_flags=needed, plans={"store": ["exit 3"]})
        slot.wait_for("start", "store", 2)
        slot.wait_for("start", "index")
        slot.expect("[store] binary typeahead-ingester.aes\n", 2)
        slot.expect("[supervisor] store binary: the build that uses the CPU's AES\n")
        slot.expect("[index] binary typeahead-tantivy\n")
        self.stop(slot)

        for lacking, named in (("aes", " aes"), ("avx", " avx"), ("pclmulqdq", " pclmulqdq"), ("sse4_2 aes", " sse4_2 aes")):
            flags = " ".join(flag for flag in needed.split() if flag not in lacking.split())
            slot = Slot(self, cpu_flags=flags)
            slot.wait_for("start", "store")
            slot.wait_for("start", "index")
            slot.expect("[store] binary typeahead-ingester\n")
            slot.expect(f"[supervisor] store binary: the baseline build; this CPU does not report{named}\n")
            slot.expect("[index] binary typeahead-tantivy\n")
            self.stop(slot)

    def test_an_image_without_the_aes_build_runs_the_baseline(self) -> None:
        slot = Slot(self)
        slot.wait_for("start", "store")
        slot.wait_for("start", "index")
        slot.expect("[store] binary typeahead-ingester\n")
        slot.expect("[index] binary typeahead-tantivy\n")
        self.assertNotIn("store binary:", slot.output())
        self.stop(slot)

    def test_the_workers_admin_secret_starts_nothing(self) -> None:
        for name in ("ADMIN_SECRET", "TYPEAHEAD_SECRET"):
            with self.subTest(name=name):
                slot = Slot(self, **{name: "not-for-a-slot"})
                self.assertEqual(1, slot.exit_status())
                self.assertEqual([], slot.events())
                self.assertIn(f"{name} is set", slot.output())


if __name__ == "__main__":
    unittest.main()
