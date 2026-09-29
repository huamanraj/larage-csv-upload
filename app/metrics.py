"""CPU and memory of this process plus its children (the worker's validation pool)."""
import time

import psutil

CORES = psutil.cpu_count() or 1
RAM_TOTAL_MB = round(psutil.virtual_memory().total / 2**20)


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
            self.last = round(max((cpu - self.cpu) / max(t - self.t, 1e-6), 0.0), 2)
            self.cpu, self.t = cpu, t
        return self.last, self.rss_mb()
