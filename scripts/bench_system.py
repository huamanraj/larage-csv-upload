"""Whole-system benchmark: API + worker + Postgres on N cores, for several file sizes.

Run on a Linux VM (not inside Docker) with Postgres running locally:
    python scripts/bench_system.py --cores 2,4,8 --sizes 100000,200000,500000,1000000
It starts the API and worker itself, pinned with `taskset`, so stop any running copies first.
Results are printed as JSON lines and written to bench_results.json.

For each core count the API, the worker (and its pool) and every Postgres process are pinned to the same N CPUs,
which is how a single N-core VM running the whole app behaves. A sampler pinned to the spare CPU records, every
0.5 s, CPU (cores busy) and memory (PSS, so Postgres shared memory is not double counted) per process group and
for the whole machine. Postgres stats (WAL, checkpoints, DB size) are read before and after each import.
"""
import argparse
import json
import os
import subprocess
import threading
import time
import urllib.request

import psutil
import psycopg

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
S = os.getenv("BENCH_DIR", os.path.join(REPO, "bench"))
API = os.getenv("BENCH_API", "http://127.0.0.1:8000")
DB = os.getenv("DATABASE_URL", "postgresql://postgres@127.0.0.1:5432/csvimport")
ALL_CPUS = sorted(os.sched_getaffinity(0))
SIZES, CORE_SETS, SAMPLER_CPU = [], {}, {ALL_CPUS[-1]}  # filled in by main()


def sh(cmd):
    return subprocess.run(cmd, shell=True, capture_output=True, text=True)


def http(method, path, data=None, headers=None, timeout=600):
    req = urllib.request.Request(API + path, data=data, method=method, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def groups():
    g = {"worker": [], "api": [], "postgres": []}
    for p in psutil.process_iter(["name", "cmdline"]):
        try:
            cmd = " ".join(p.info["cmdline"] or [])
            if "app.worker" in cmd:
                g["worker"].append(p)
            elif "uvicorn app.main" in cmd:
                g["api"].append(p)
            elif p.info["name"] == "postgres" or cmd.startswith("postgres:") or "/postgres " in cmd:
                g["postgres"].append(p)
        except psutil.Error:
            pass
    return g


def cpu_total():
    with open("/proc/stat") as f:
        v = list(map(int, f.readline().split()[1:]))
    idle = v[3] + v[4]
    return sum(v) - idle, sum(v)


class Sampler(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True)
        self.stop = threading.Event()
        self.samples = []  # (t, {group: cores}, {group: pss_mb}, machine_cores, used_mb)

    def run(self):
        os.sched_setaffinity(0, SAMPLER_CPU)
        prev_cpu, prev_t, prev_sys = {}, time.monotonic(), cpu_total()
        while not self.stop.is_set():
            time.sleep(0.5)
            t = time.monotonic()
            cores, pss = {}, {}
            cur_cpu = {}
            for name, procs in groups().items():
                c = m = 0.0
                for p in procs:
                    try:
                        ct = p.cpu_times()
                        v = ct.user + ct.system
                        cur_cpu[p.pid] = v
                        c += v - prev_cpu.get(p.pid, v)
                        m += p.memory_full_info().pss
                    except psutil.Error:
                        pass
                cores[name] = c / (t - prev_t)
                pss[name] = m / 2**20
            busy, total = cpu_total()
            machine = (busy - prev_sys[0]) / max(total - prev_sys[1], 1) * psutil.cpu_count()
            mem = psutil.virtual_memory()
            self.samples.append((t, cores, pss, machine, (mem.total - mem.available) / 2**20))
            prev_cpu, prev_t, prev_sys = cur_cpu, t, (busy, total)


def pg_stats(conn):
    r = conn.execute("""SELECT pg_current_wal_insert_lsn(), pg_database_size('csvimport'),
                               (SELECT checkpoints_timed + checkpoints_req FROM pg_stat_bgwriter),
                               (SELECT temp_bytes FROM pg_stat_database WHERE datname = 'csvimport')""").fetchone()
    return {"lsn": r[0], "db_bytes": r[1], "checkpoints": r[2], "temp_bytes": r[3]}


def start_app(n):
    cpus = CORE_SETS[n]
    g = groups()
    for proc in g["worker"] + g["api"]:
        try:
            proc.kill()
        except psutil.Error:
            pass
    time.sleep(1)
    # pin every postgres process (new backends inherit the postmaster's affinity)
    for p in groups()["postgres"]:
        sh(f"taskset -a -cp {cpus} {p.pid}")
    env = f"cd {REPO} && DATA_DIR={S}/data DATABASE_URL={DB}"
    sh(f"{env} nohup taskset -c {cpus} uvicorn app.main:app --port 8000 > {S}/api.log 2>&1 &")
    sh(f"{env} nohup taskset -c {cpus} python3 -m app.worker > {S}/worker.log 2>&1 &")
    for _ in range(60):
        try:
            http("GET", "/api/config")
            break
        except Exception:
            time.sleep(0.5)
    time.sleep(3)


def one_run(n, rows, path):
    http("POST", "/api/reset")
    with psycopg.connect(DB, autocommit=True) as c:
        c.execute("CHECKPOINT")
        c.execute("VACUUM ANALYZE")
    time.sleep(2)
    sampler = Sampler()
    sampler.start()
    time.sleep(2.2)  # idle baseline samples
    base_n = len(sampler.samples)
    with psycopg.connect(DB, autocommit=True) as c:
        before = pg_stats(c)
    data = open(path, "rb").read()
    t0 = time.monotonic()
    res = http("POST", "/api/imports?campaign_id=1", data=data, headers={"X-File-Name": os.path.basename(path)})
    upload_s = time.monotonic() - t0
    iid = res["import_id"]
    with psycopg.connect(DB, autocommit=True) as c:
        while c.execute("SELECT status FROM imports WHERE id=%s", (iid,)).fetchone()[0] not in ("done", "failed"):
            time.sleep(0.25)
        time.sleep(0.6)
        sampler.stop.set()
        sampler.join()
        after = pg_stats(c)
        imp = c.execute("""SELECT status, checkpoint_row, valid_rows, invalid_rows, duplicate_rows,
                                  extract(epoch FROM finished_at - started_at), extract(epoch FROM started_at - created_at)
                           FROM imports WHERE id=%s""", (iid,)).fetchone()
        ch = c.execute("""SELECT max((data->>'save_ms')::float + (data->>'progress_ms')::float),
                                 avg((data->>'save_ms')::float + (data->>'progress_ms')::float),
                                 max((data->>'wal_mb')::float), avg((data->>'read_ms')::float),
                                 avg((data->>'validate_ms')::float), max((data->>'rss_mb')::float)
                          FROM import_events WHERE import_id=%s AND kind='chunk'""", (iid,)).fetchone()
        wal = c.execute("SELECT pg_wal_lsn_diff(%s, %s)", (after["lsn"], before["lsn"])).fetchone()[0]
    base = sampler.samples[:base_n]
    run = [s for s in sampler.samples[base_n:]]
    def avg(xs):
        return sum(xs) / len(xs) if xs else 0.0
    out = {"cores": n, "rows": rows, "status": imp[0], "processed": imp[1], "valid": imp[2], "invalid": imp[3],
           "dups": imp[4], "process_s": round(float(imp[5]), 2), "queue_wait_s": round(float(imp[6]), 2),
           "upload_s": round(upload_s, 2), "file_mb": round(len(data) / 2**20, 1)}
    for g in ("worker", "postgres", "api"):
        out[f"{g}_cpu_avg"] = round(avg([s[1][g] for s in run]), 2)
        out[f"{g}_cpu_peak"] = round(max([s[1][g] for s in run] or [0]), 2)
        out[f"{g}_mem_idle"] = round(avg([s[2][g] for s in base]))
        out[f"{g}_mem_peak"] = round(max([s[2][g] for s in run] or [0]))
    out["machine_cpu_avg"] = round(avg([s[3] for s in run]), 2)
    out["machine_cpu_peak"] = round(max([s[3] for s in run] or [0]), 2)
    out["machine_mem_idle"] = round(avg([s[4] for s in base]))
    out["machine_mem_peak"] = round(max([s[4] for s in run] or [0]))
    out.update({"wal_mb": round(float(wal) / 2**20), "wal_chunk_peak_mb": round(ch[2] or 0, 2),
                "db_chunk_peak_ms": round(ch[0] or 0), "db_chunk_avg_ms": round(ch[1] or 0),
                "read_avg_ms": round(ch[3] or 0), "validate_avg_ms": round(ch[4] or 0),
                "checkpoints": after["checkpoints"] - before["checkpoints"],
                "temp_mb": round((after["temp_bytes"] - before["temp_bytes"]) / 2**20, 1),
                "db_growth_mb": round((after["db_bytes"] - before["db_bytes"]) / 2**20)})
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cores", default="2,4")
    ap.add_argument("--sizes", default="100000,200000,500000,1000000")
    a = ap.parse_args()
    SIZES.extend(int(x) for x in a.sizes.split(","))
    for n in (int(x) for x in a.cores.split(",")):
        if n > len(ALL_CPUS):
            print(f"skipping {n} cores: only {len(ALL_CPUS)} CPUs available")
            continue
        CORE_SETS[n] = ",".join(map(str, ALL_CPUS[:n]))
    # the sampler uses the last CPU; it only stays out of the way for core counts below the total
    os.makedirs(os.path.join(S, "data"), exist_ok=True)
    os.sched_setaffinity(0, SAMPLER_CPU)
    files = {}
    for rows in SIZES:
        path = f"{S}/bench_{rows}.csv"
        if not os.path.exists(path):
            sh(f"cd {REPO} && python3 scripts/make_sample.py {rows} {path}")
        files[rows] = path
    results = []
    for n in CORE_SETS:
        start_app(n)
        for rows in SIZES:
            r = one_run(n, rows, files[rows])
            print(json.dumps(r), flush=True)
            results.append(r)
    json.dump(results, open(f"{S}/bench_results.json", "w"), indent=1)
    print(f"wrote {S}/bench_results.json")


if __name__ == "__main__":
    main()
