# M0 verification record

Fill this in on your own machines (see docs/PLAN.md, M0-a). Cloud sessions cannot run these.

## Local desktop

- Distro / GNOME version / session type (`echo $XDG_SESSION_TYPE`): x11
- `claude --version`: 2.1.278 (Claude Code)
- `codex --version`: codex-cli 0.155.1
- `pytest` result:
```
============================= test session starts ==============================
platform linux -- Python 3.12.3, pytest-9.1.1, pluggy-1.6.0
rootdir: /data2/Personal/Development/Agentail
configfile: pyproject.toml
testpaths: tests
plugins: asyncio-1.4.0
asyncio: mode=Mode.AUTO, debug=False, asyncio_default_fixture_loop_scope=None, asyncio_default_test_loop_scope=function
collected 45 items                                                             

tests/test_claude_adapter.py ................                            [ 35%]
tests/test_claude_config.py ..                                           [ 40%]
tests/test_hook.py .........                                             [ 60%]
tests/test_ingest.py ..                                                  [ 64%]
tests/test_protocol.py ...........                                       [ 88%]
tests/test_state.py .....                                                [100%]

============================== 45 passed in 0.92s ==============================
```


## Remote hosts

Run `./scripts/m0-check-host.sh <alias>` for each host and paste the full output.

### gpu0

```
== probe: gpu0
uid=10006
home=/data/twye
hostname=cvrp-gpu-0
python3=/usr/bin/python3
python3_version=3.8.10
run_user=yes
home_fs=nfs
claude=MISSING 
codex=MISSING 
tmux=/usr/bin/tmux

== forward test: -R /run/user/10006/agentail-m0.sock
srw------- 1 twye twye 0 Sep 30 15:08 /run/user/10006/agentail-m0.sock
OK: remote -> local unix socket forwarding works

== stale socket check (after an unclean disconnect the file may remain):
srw------- 1 twye twye 0 Sep 30 15:08 /run/user/10006/agentail-m0.sock
```

### gpu1

```
== probe: gpu1
uid=10006
home=/data/twye
hostname=cvrp-gpu-1
python3=/usr/bin/python3
python3_version=3.8.10
run_user=yes
home_fs=nfs
claude=MISSING 
codex=MISSING 
tmux=/usr/bin/tmux

== forward test: -R /run/user/10006/agentail-m0.sock
srw------- 1 twye twye 0 Sep 30 15:10 /run/user/10006/agentail-m0.sock
OK: remote -> local unix socket forwarding works

== stale socket check (after an unclean disconnect the file may remain):
srw------- 1 twye twye 0 Sep 30 15:10 /run/user/10006/agentail-m0.sock
```

### gpu2
```
== probe: gpu2
uid=10006
home=/data/twye
hostname=cvrp-gpu-2
python3=/usr/bin/python3
python3_version=3.10.4
run_user=yes
home_fs=nfs
claude=MISSING 
codex=MISSING 
tmux=/usr/bin/tmux

== forward test: -R /run/user/10006/agentail-m0.sock
srw------- 1 twye twye 0 Sep 30 15:11 /run/user/10006/agentail-m0.sock
OK: remote -> local unix socket forwarding works

== stale socket check (after an unclean disconnect the file may remain):
srw------- 1 twye twye 0 Sep 30 15:11 /run/user/10006/agentail-m0.sock
```

### gpu3
```
== probe: gpu3
uid=10006
home=/data/twye
hostname=cvrp-gpu-3
python3=/usr/bin/python3
python3_version=3.10.4
run_user=yes
home_fs=nfs
claude=MISSING 
codex=MISSING 
tmux=/usr/bin/tmux

== forward test: -R /run/user/10006/agentail-m0.sock
srw------- 1 twye twye 0 Sep 30 15:11 /run/user/10006/agentail-m0.sock
OK: remote -> local unix socket forwarding works

== stale socket check (after an unclean disconnect the file may remain):
srw------- 1 twye twye 0 Sep 30 15:11 /run/user/10006/agentail-m0.sock
```

### gpu4
```
== probe: gpu4
uid=10006
home=/data/twye
hostname=cvrp-gpu-4
python3=/usr/bin/python3
python3_version=3.10.4
run_user=yes
home_fs=nfs
claude=MISSING 
codex=MISSING 
tmux=/usr/bin/tmux

== forward test: -R /run/user/10006/agentail-m0.sock
srw------- 1 twye twye 0 Sep 30 15:17 /run/user/10006/agentail-m0.sock
OK: remote -> local unix socket forwarding works

== stale socket check (after an unclean disconnect the file may remain):
srw------- 1 twye twye 0 Sep 30 15:17 /run/user/10006/agentail-m0.sock
```

### gpu5
```
== probe: gpu5
uid=10006
home=/data/twye
hostname=cvrp-gpu-5
python3=/usr/bin/python3
python3_version=3.10.12
run_user=yes
home_fs=nfs
claude=MISSING 
codex=MISSING 
tmux=/usr/bin/tmux

== forward test: -R /run/user/10006/agentail-m0.sock
srw------- 1 twye twye 0 Sep 30 16:17 /run/user/10006/agentail-m0.sock
OK: remote -> local unix socket forwarding works

== stale socket check (after an unclean disconnect the file may remain):
srw------- 1 twye twye 0 Sep 30 16:17 /run/user/10006/agentail-m0.sock
```

### gpu6
```
== probe: gpu6
uid=10006
home=/data/twye
hostname=cvrp-gpu-6
python3=/usr/bin/python3
python3_version=3.10.12
run_user=yes
home_fs=nfs
claude=MISSING 
codex=MISSING 
tmux=/usr/bin/tmux

== forward test: -R /run/user/10006/agentail-m0.sock
srw------- 1 twye twye 0 Sep 30 15:17 /run/user/10006/agentail-m0.sock
OK: remote -> local unix socket forwarding works

== stale socket check (after an unclean disconnect the file may remain):
srw------- 1 twye twye 0 Sep 30 15:17 /run/user/10006/agentail-m0.sock
```

### gpu7
```
== probe: gpu7
uid=10006
home=/data/twye
hostname=cvrp-gpu-7
python3=/usr/bin/python3
python3_version=3.10.12
run_user=yes
home_fs=nfs
claude=MISSING 
codex=MISSING 
tmux=/usr/bin/tmux

== forward test: -R /run/user/10006/agentail-m0.sock
srw------- 1 twye twye 0 Sep 30 15:18 /run/user/10006/agentail-m0.sock
OK: remote -> local unix socket forwarding works

== stale socket check (after an unclean disconnect the file may remain):
srw------- 1 twye twye 0 Sep 30 15:18 /run/user/10006/agentail-m0.sock
```

### gpu8
```
== probe: gpu8
uid=10006
home=/data/twye
hostname=cvrp-gpu-8
python3=/usr/bin/python3
python3_version=3.10.12
run_user=yes
home_fs=nfs
claude=MISSING 
codex=MISSING 
tmux=/usr/bin/tmux

== forward test: -R /run/user/10006/agentail-m0.sock
srw------- 1 twye twye 0 Sep 30 07:18 /run/user/10006/agentail-m0.sock
OK: remote -> local unix socket forwarding works

== stale socket check (after an unclean disconnect the file may remain):
srw------- 1 twye twye 0 Sep 30 07:18 /run/user/10006/agentail-m0.sock
```

### gpu9
```
== probe: gpu9
uid=10006
home=/data/twye
hostname=cvrp-gpu-9
python3=/usr/bin/python3
python3_version=3.10.12
run_user=yes
home_fs=nfs
claude=MISSING 
codex=MISSING 
tmux=/usr/bin/tmux

== forward test: -R /run/user/10006/agentail-m0.sock
srw------- 1 twye twye 0 Sep 30 08:15 /run/user/10006/agentail-m0.sock
OK: remote -> local unix socket forwarding works

== stale socket check (after an unclean disconnect the file may remain):
srw------- 1 twye twye 0 Sep 30 08:15 /run/user/10006/agentail-m0.sock
```

### gpu10
```
== probe: gpu10
uid=10006
home=/data/twye
hostname=cvrp-gpu-10
python3=/usr/bin/python3
python3_version=3.10.12
run_user=yes
home_fs=nfs
claude=MISSING 
codex=MISSING 
tmux=/usr/bin/tmux

== forward test: -R /run/user/10006/agentail-m0.sock
srw------- 1 twye twye 0 Sep 30 16:16 /run/user/10006/agentail-m0.sock
OK: remote -> local unix socket forwarding works

== stale socket check (after an unclean disconnect the file may remain):
srw------- 1 twye twye 0 Sep 30 16:16 /run/user/10006/agentail-m0.sock
```

### gpu11
```
== probe: gpu11
uid=10006
home=/data/twye
hostname=cvrp-gpu-11
python3=/usr/bin/python3
python3_version=3.10.12
run_user=yes
home_fs=nfs
claude=MISSING 
codex=MISSING 
tmux=/usr/bin/tmux

== forward test: -R /run/user/10006/agentail-m0.sock
srw------- 1 twye twye 0 Sep 30 16:19 /run/user/10006/agentail-m0.sock
OK: remote -> local unix socket forwarding works

== stale socket check (after an unclean disconnect the file may remain):
srw------- 1 twye twye 0 Sep 30 16:19 /run/user/10006/agentail-m0.sock
```

### gpu12
```
== probe: gpu12
uid=10006
home=/data/twye
hostname=cvrp-gpu-12
python3=/usr/bin/python3
python3_version=3.10.12
run_user=yes
home_fs=nfs
claude=MISSING 
codex=MISSING 
tmux=/usr/bin/tmux

== forward test: -R /run/user/10006/agentail-m0.sock
srw------- 1 twye twye 0 Sep 30 16:17 /run/user/10006/agentail-m0.sock
OK: remote -> local unix socket forwarding works

== stale socket check (after an unclean disconnect the file may remain):
srw------- 1 twye twye 0 Sep 30 16:17 /run/user/10006/agentail-m0.sock
```

## Conclusions

- Hosts where forwarding works: all 13 (gpu0–gpu12). `OK: remote -> local unix socket forwarding works` on every host.
- Hosts without `/run/user/<uid>` (fallback path, NFS warning?): none. `run_user=yes` everywhere, so the remote
  socket goes in `/run/user/10006/` (tmpfs, per-host). HOME (`/data/twye`) is NFS and shared across all hosts,
  so nothing per-host (sockets, runtime state) may live under `~`; only the hook script and config go there.
- Hosts with python3 < 3.6 or missing: none. gpu0–1: 3.8.10; gpu2–4: 3.10.4; gpu5–12: 3.10.12.
- SoC cluster: which login node will run agents: N/A (no SoC cluster in use).

### Other observations

- Stale socket: on every host the socket file still exists after the ssh disconnect. M3 must clean it up
  (`rm -f` before `-R`, and/or `-o StreamLocalBindUnlink=yes`), or the next `-R` will fail to bind.
- `claude` / `codex` show MISSING in the non-interactive ssh shell, but both are installed: an interactive
  login finds `/data/twye/.local/bin/claude` (gpu7, gpu8) and `/data/twye/.local/bin/codex` (gpu7), both on
  the shared NFS HOME, so one install serves all hosts. `~/.local/bin` is only added to PATH by the
  interactive shell setup. M3 consequence: `add-host` must not detect agents with a bare `command -v` over
  `ssh host cmd`; check `~/.local/bin/{claude,codex}` explicitly (or run the check via `bash -lc`), and never
  make the hook itself depend on PATH.
- Shared NFS HOME means `~/.claude/settings.json` and `~/.codex/config.toml` are shared by all 13 hosts: one `add-host` install writes the
  hook config for all of them. M3's `install/remote.py` should detect this (same file seen from several hosts)
  and not install or back up twice.
