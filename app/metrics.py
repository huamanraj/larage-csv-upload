"""CPU and memory of this process plus its children (the worker's validation pool), and CPU pinning."""
import os
import time

import psutil

CORES = psutil.cpu_count() or 1
RAM_TOTAL_MB = round(psutil.virtual_memory().total / 2**20)
# CPUs this process may run on (a container can see fewer than the host has).
ALLOWED_CPUS = sorted(os.sched_getaffinity(0)) if hasattr(os, "sched_getaffinity") else list(range(CORES))


def pin(pids, cpus):
    """Restrict processes to the given CPU ids (Linux). Returns False where affinity isn't supported."""
    if not hasattr(os, "sched_setaffinity"):
        return False
    ok = True
    for pid in pids:
        try:
            os.sched_setaffinity(pid, cpus)
        except OSError:  # process exited, or not permitted
            ok = False
    return ok


def tree_pids():
    """This process and all its children (the validation pool)."""
    try:
        return [os.getpid(), *(c.pid for c in psutil.Process().children(recursive=True))]
    except psutil.Error:
        return [os.getpid()]


class Usage:
    """Call tick() at phase boundaries; it returns (cores used since the last tick, RSS MB now).

    The OS counts CPU time in ~10 ms steps, so windows shorter than `min_window` seconds would read as
    0 or several cores at random; those ticks repeat the last value and keep measuring.
    """

    def __init__(self, min_window=0.0):
        self.proc = psutil.Process()
        self.min_window, self.last, self.measured = min_window, 0.0, False
        self.cpu, self.t = self._cpu(), time.monotonic()

    def _tree(self):
        try:
            return [self.proc, *self.proc.children(recursive=True)]
        except psutil.Error:
            return [self.proc]

    def _cpu(self):
        total = 0.0
        for p in self._tree():
            try:
                c = p.cpu_times()
                total += c.user + c.system
            except psutil.Error:  # a child exited between listing and reading
                pass
        return total

    def rss_mb(self):
        total = 0
        for p in self._tree():
            try:
                total += p.memory_info().rss
            except psutil.Error:
                pass
        return round(total / 2**20, 1)

    def tick(self, force=False):
        t = time.monotonic()
        self.measured = force or t - self.t >= self.min_window
        if self.measured:
            cpu = self._cpu()
            self.last = round(min(max((cpu - self.cpu) / max(t - self.t, 1e-6), 0.0), CORES), 2)
            self.cpu, self.t = cpu, t
        return self.last, self.rss_mb()


def _mem_mb(p):
    """PSS where the OS reports it (shared pages split between the processes using them), else RSS."""
    try:
        return p.memory_full_info().pss / 2**20
    except (AttributeError, psutil.AccessDenied):
        return p.memory_info().rss / 2**20


class SystemSampler:
    """One sample per tick() for the live system charts:

      self     CPU (cores busy) and memory of this process and its children (the worker's validation pool)
      db       the same for Postgres, when its processes are visible (same machine, not another container)
      machine  CPU busy and memory used on the whole machine (or the Docker VM), which includes the database
               and everything else even when `db` can't be seen
    """
    EVERY = 0.5  # seconds between samples

    def __init__(self, source, watch_postgres=False):
        self.source, self.watch_pg = source, watch_postgres
        self.me = psutil.Process()
        self.cpu_prev, self.pg, self.pg_found = {}, [], 0.0
        self.t, self.busy = time.monotonic(), self._busy()

    @staticmethod
    def _busy():
        c = psutil.cpu_times()
        return sum(c) - c.idle - getattr(c, "iowait", 0.0)

    def _postgres(self, mine):
        if time.monotonic() - self.pg_found > 5:  # new backends appear as connections open
            self.pg = [p for p in psutil.process_iter(["name"])
                       if (p.info["name"] or "").startswith("postgres") and p.pid not in mine]
            self.pg_found = time.monotonic()
        return self.pg

    def _group(self, procs, cpu_now, dt):
        cpu = mem = 0.0
        alive = 0
        for p in procs:
            try:
                ct = p.cpu_times()
                v = ct.user + ct.system
                cpu += v - self.cpu_prev.get(p.pid, v)  # a process seen for the first time counts from now
                cpu_now[p.pid] = v
                mem += _mem_mb(p)
                alive += 1
            except psutil.Error:
                pass
        # Clamp: CPU time and wall time are read a moment apart, so a busy tick can read a little over the cores.
        return {"cpu": round(min(max(cpu, 0.0) / dt, CORES), 2), "mem": round(mem, 1), "procs": alive}

    def tick(self):
        t = time.monotonic()
        dt = max(t - self.t, 1e-3)
        try:
            tree = [self.me, *self.me.children(recursive=True)]
        except psutil.Error:
            tree = [self.me]
        cpu_now = {}
        out = {"source": self.source, "cores": CORES, "self": self._group(tree, cpu_now, dt), "db": None}
        if self.watch_pg:
            pg = self._postgres({p.pid for p in tree})
            if pg:
                out["db"] = self._group(pg, cpu_now, dt)
        busy = self._busy()
        vm = psutil.virtual_memory()
        out["machine"] = {"cpu": round(min(max(busy - self.busy, 0.0) / dt, CORES), 2),
                          "mem_used": round((vm.total - vm.available) / 2**20), "mem_total": round(vm.total / 2**20)}
        self.cpu_prev, self.t, self.busy = cpu_now, t, busy
        return out
