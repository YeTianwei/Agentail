# M0 verification record

Fill this in on your own machines (see docs/PLAN.md, M0-a). Cloud sessions cannot run these.

## Local desktop

- Distro / GNOME version / session type (`echo $XDG_SESSION_TYPE`):
- `claude --version`:
- `codex --version`:
- `pytest` result:

## Remote hosts

Run `./scripts/m0-check-host.sh <alias>` for each host and paste the full output.

### <alias 1>

```
(paste here)
```

### <alias 2>

```
(paste here)
```

## Conclusions

- Hosts where forwarding works:
- Hosts without `/run/user/<uid>` (fallback path, NFS warning?):
- Hosts with python3 < 3.6 or missing:
- SoC cluster: which login node will run agents:
