"""Report the cgroup limits this process is running under.

A container is not a small virtual machine. It is an ordinary process on the
host kernel, fenced in by namespaces (what it can see) and cgroups (what it can
use). The kernel exposes the second half through a filesystem, so the process
can read its own limits — which is what makes `docker run -m 128m` visible on
the page.
"""

from pathlib import Path

CGROUP_ROOT = Path("/sys/fs/cgroup")

# cgroup v1 has no "max": an unbounded memory cgroup reports a number close to
# the largest signed 64-bit integer instead.
V1_UNLIMITED = 2**62

UNLIMITED = "unlimited"


def read_limits(cgroup_root: Path = CGROUP_ROOT) -> dict:
    return {
        "memory_limit": _memory_limit(cgroup_root),
        "cpu_limit": _cpu_limit(cgroup_root),
    }


def _read(path: Path) -> str | None:
    try:
        return path.read_text().strip()
    except OSError:
        return None


def _memory_limit(root: Path) -> str:
    raw = _read(root / "memory.max")  # cgroup v2
    if raw is None:
        raw = _read(root / "memory" / "memory.limit_in_bytes")  # cgroup v1
    if raw is None or raw == "max":
        return UNLIMITED

    try:
        limit = int(raw)
    except ValueError:
        return UNLIMITED

    if limit >= V1_UNLIMITED:
        return UNLIMITED
    return f"{limit / (1024 * 1024):.0f} MB"


def _cpu_limit(root: Path) -> str:
    raw = _read(root / "cpu.max")  # cgroup v2: "<quota> <period>", or "max ..."
    if raw is not None:
        quota, _, period = raw.partition(" ")
        if quota == "max":
            return UNLIMITED
        return _as_cpus(quota, period or "100000")

    quota = _read(root / "cpu" / "cpu.cfs_quota_us")  # cgroup v1
    period = _read(root / "cpu" / "cpu.cfs_period_us")
    if quota is None or period is None or quota == "-1":
        return UNLIMITED
    return _as_cpus(quota, period)


def _as_cpus(quota: str, period: str) -> str:
    """A quota of 50000µs per 100000µs period is half a CPU."""
    try:
        cpus = int(quota) / int(period)
    except (ValueError, ZeroDivisionError):
        return UNLIMITED
    return f"{cpus:g} CPUs"
