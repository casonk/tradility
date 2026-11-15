# Deployment modes: tradility

tradility runs three ways. You choose with one setting, and **only the `k3s` mode needs a cluster**. If you just
want to run it, you need Python and nothing else.

| Mode | What it is | Needs |
|---|---|---|
| `dev` (default) | one run in the foreground, no systemd, no cluster | the language runtime |
| `host` | the systemd user timer | systemd |
| `k3s` | the same host timer triggers the refresh, which runs in a pod | K3s, podman, kubectl, the in-cluster registry, and the host timer |

The application code is the same in all three: nothing under `src/` knows which mode it is in.

## Choose a mode

```bash
deploy/deploy.sh mode                  # print the mode in effect and where it came from
deploy/deploy.sh up                    # run it in that mode
deploy/deploy.sh status | verify | down
```

The mode is read from, first match wins:

1. `--mode dev|host|k3s` on the command line
2. `TRADILITY_DEPLOY_MODE` in the environment
3. `TRADILITY_DEPLOY_MODE` in `config/deploy.local.env` (copy `config/deploy.example.env`; it is gitignored)
4. `dev`

An unknown value, or a config file that cannot be read, is an error, never a silent fallback.

## What each mode does

### dev
`up` runs the job once in the foreground. `status` and `verify` check it without changing anything.

### host
`up` enables the systemd user timer (install it once with Clockwork; `up` says so if it is missing). `down` disables it.

### k3s
`up` refuses to start, and changes nothing, unless kubectl, podman, the in-cluster registry and the host timer are
present. Then it builds the image (refusing uncommitted source), applies the manifests (the CronJob is only a
**suspended template**), runs `verify.sh` (which must pass), and only then switches the unit over: the timer is never
disabled, and its `ExecStart` runs the pod. A failed step changes nothing: the unit keeps running on the host.

## Switching

| From | To | Command |
|---|---|---|
| dev | host | `deploy/deploy.sh --mode host up` |
| host | k3s | `deploy/deploy.sh --mode k3s up` (verifies first, then hands over) |
| k3s | host | `deploy/deploy.sh --mode host up` (removes the drop-in; the unit runs on the host again) |

`--mode k3s down` is the same as `--mode host up`. A marker in `~/.local/state/tradility/` records which mode last
held the work, so `host up` knows what to stop first.

## For contributors without K3s

- `pytest` needs no cluster: the deployment tests parse manifests and run the scripts against stub commands.
- Nothing under `deploy/k3s/` runs unless you select the `k3s` mode.
- This file, the scripts and their tests started from traction-control's `scripts/scaffold_deployment_modes.py`;
  the contract is in its `docs/deployment-modes.md`. Run `scripts/check_deployment_modes.py --repo .` to check.

## tradility specifics (k3s)

- **Data.** The pod mounts two host folders: tradility's `exports/` (read-write, where the analysis and its caches
  live, so the web UI and Clockwork keep reading the same files) and the folder holding the finance pipeline's
  `holdings-aggregate.json` (**read-only**). `deploy/k3s/prepare-host.sh` labels both for containers; its output
  says how to undo it.
- **Schwab.** The pod cannot reach the vault. `deploy/k3s/feed-token.sh` resolves a short-lived access token on the
  host with your `TRADILITY_SCHWAB_TOKEN_COMMAND` and stores it as the `tradility-schwab` Secret. A user timer
  (installed by the hand-over, removed on the way back) feeds one at 16:15 and 16:25 on weekdays, before the 16:30
  run. The long-lived refresh token never enters the cluster. With no valid token the run falls back to Yahoo.
- **Orders stay on the host.** The pod does not fetch orders (`TRADILITY_FETCH_ORDERS=0`): the host's
  `tradility-orders` timer owns `tradility-orders.json`, and the pod only rebuilds the actual-performance view from it.
- **Binance.US stays on the host.** Its key is long-lived and can see an exchange account, so the pod has neither
  the key nor a network path to the exchange. The order fetch there reports it and the analysis is unaffected.
- **Network.** Egress is limited to the Schwab API and the Yahoo Finance hosts yfinance uses (see
  `deploy/k3s/manifests/30-network-policy.yaml`). Everything else, the host and the Kubernetes API are denied.
- **Schedule and guards.** The Clockwork timer (weekdays 16:30) stays the trigger, so the job guard and resource limits
  still apply. k3s mode installs a drop-in (`zz-k3s-run.conf`) that replaces only `ExecStart` with
  `deploy/k3s/run-job.sh`, which runs one Job from the suspended CronJob template and exits with its status. The
  token feeder (16:15 and 16:25) is installed and removed with it. The drop-in is named `zz-` so it sorts after this unit's
  own `local.conf`, which also sets `ExecStart`; `handover.sh pod-on` checks the effective `ExecStart` and undoes the
  drop-in if another one still wins.
- **What runs is what is committed.** The image is built from committed code only. Uncommitted changes in a checkout
  keep running on the host and in dev mode, but not in the pod, so commit before `--mode k3s up`.
