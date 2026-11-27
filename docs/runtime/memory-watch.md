# Memory watch

A hosted runner's tests share a cgroup with the runner agent, and free mode gives them swap up to twice RAM ([runners.md](runners.md#runner-modes)). Tests thrashing that swap starve the agent. GitHub then reports "The hosted runner lost communication with the server" and keeps no log of the step.

`test.memory_watch` fails the stage before that, so the log stays. Rust test command, Linux only, off by default:

```yaml
test:
  memory_watch:
    enabled: true
    abort_stall_percent: 50   # 0 logs and never stops
    abort_after_samples: 4
    abort_below_available_percent: 20   # 100 counts the stall alone
```

Every 30s it logs one line:

```text
memory watch 90s: 1.2 GiB available, swap 6.3 GiB used, full stall 62%; largest: <5 processes by RSS>
```

Available is the smaller of the host's `MemAvailable` and the cgroup's headroom under its `memory.max` or `memory.high`. The host figure counts page cache as free and `memory.high` counts it as used, so a throttled cgroup stalls with the host figure high.

Full stall is the share of the interval every task in the cgroup was waiting on memory (PSI "full"), read from the cgroup's `memory.pressure`. A kernel without PSI logs `no PSI` and never stops.

A heartbeat counts toward a stop only while available memory is under `abort_below_available_percent` of the memory limit, the smaller of the cgroup limit and total RAM. Direct reclaim and compaction stall tasks with plenty free, and that is not the thrash that loses a runner. An unknown limit counts on the stall alone.

After `abort_after_samples` heartbeats in a row that count, the watch SIGTERMs the test command and everything under it. A heartbeat later it SIGKILLs anything left, and the stage fails naming the setting. The defaults stop a run after 2 minutes with tasks stalled half the time, which a healthy run never reaches.
