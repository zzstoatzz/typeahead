import argparse
import ctypes
import os
from pathlib import Path
import resource
import signal
import subprocess
import sys
import time


def temperature_sensors():
    paths = []
    for label in Path("/sys/class/hwmon").glob("hwmon*/temp*_label"):
        if label.read_text().strip().startswith("Package id"):
            paths.append(label.with_name(label.name.replace("_label", "_input")))
    if not paths:
        raise RuntimeError("No CPU package temperature sensor; quiet mode cannot enforce its thermal limit")
    return paths


def temperature(paths):
    return max(int(path.read_text()) / 1000 for path in paths)


def efficiency_cpu():
    choices = []
    for cpu in os.sched_getaffinity(0):
        path = Path(f"/sys/devices/system/cpu/cpu{cpu}/cpufreq/cpuinfo_max_freq")
        if not path.is_file():
            raise RuntimeError("CPU frequency topology unavailable")
        choices.append((int(path.read_text()), cpu))
    return min(choices)[1]


def run(command, cpu_percent=20, max_temp=65, cool_temp=60):
    if sys.platform != "linux":
        raise RuntimeError("Quiet mode requires Linux affinity, signals, and temperature sensors")
    if not 0 < cpu_percent <= 100 or not 0 < cool_temp < max_temp <= 90:
        raise ValueError("Invalid CPU or temperature limits")
    sensors = temperature_sensors()
    cpu = efficiency_cpu()
    parent = os.getpid()
    libc = ctypes.CDLL(None, use_errno=True)

    def prepare():
        if libc.prctl(1, signal.SIGKILL, 0, 0, 0) != 0:
            raise OSError(ctypes.get_errno(), "PR_SET_PDEATHSIG")
        if os.getppid() != parent:
            os._exit(125)
        os.sched_setaffinity(0, {cpu})
        os.nice(19)

    print(f"quiet: CPU {cpu}, {cpu_percent}% duty cycle, pause at {max_temp}C/resume at {cool_temp}C", file=sys.stderr, flush=True)
    period = 0.02
    active = period * cpu_percent / 100
    peak = temperature(sensors)
    cooling = peak >= max_temp
    last_check = 0.0
    started = time.monotonic()
    usage = resource.getrusage(resource.RUSAGE_CHILDREN)
    cpu_before = usage.ru_utime + usage.ru_stime

    def send(sig):
        try:
            os.killpg(proc.pid, sig)
        except ProcessLookupError:
            pass

    def interrupted(signum, frame):
        raise KeyboardInterrupt

    def stop():
        send(signal.SIGSTOP)
        _, status = os.waitpid(proc.pid, os.WUNTRACED)
        if not os.WIFSTOPPED(status):
            proc.returncode = os.waitstatus_to_exitcode(status)

    previous_handler = signal.signal(signal.SIGTERM, interrupted)
    proc = None
    try:
        while cooling:
            time.sleep(0.25)
            current = temperature(sensors)
            peak = max(peak, current)
            cooling = current > cool_temp
        proc = subprocess.Popen(command, start_new_session=True, preexec_fn=prepare)
        stop()
        while proc.poll() is None:
            now = time.monotonic()
            if now - last_check >= 1:
                current = temperature(sensors)
                peak = max(peak, current)
                last_check = now
                if current >= max_temp and not cooling:
                    cooling = True
                    print(f"quiet: cooling at {current}C", file=sys.stderr, flush=True)
                elif cooling and current <= cool_temp:
                    cooling = False
                    print(f"quiet: resumed at {current}C", file=sys.stderr, flush=True)
            if cooling:
                time.sleep(0.25)
                continue
            running = time.monotonic()
            send(signal.SIGCONT)
            time.sleep(active)
            stop()
            elapsed = time.monotonic() - running
            time.sleep(elapsed * (100 / cpu_percent - 1))
        usage = resource.getrusage(resource.RUSAGE_CHILDREN)
        cpu_seconds = usage.ru_utime + usage.ru_stime - cpu_before
        print(f"quiet: finished in {time.monotonic() - started:.2f}s; CPU {cpu_seconds:.2f}s; sampled package peak {peak}C", file=sys.stderr, flush=True)
        return proc.returncode
    finally:
        signal.signal(signal.SIGTERM, previous_handler)
        if proc is not None and proc.poll() is None:
            send(signal.SIGCONT)
            send(signal.SIGTERM)
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                send(signal.SIGKILL)
                proc.wait()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--cpu-percent", type=float, default=20)
    parser.add_argument("--max-temp", type=float, default=65)
    parser.add_argument("--cool-temp", type=float, default=60)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        parser.error("a command is required")
    sys.exit(run(command, args.cpu_percent, args.max_temp, args.cool_temp))


if __name__ == "__main__":
    main()
