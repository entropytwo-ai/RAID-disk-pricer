# RAID Architect — RAID planning & drive-pricing CLI

> 🌐 **Use it online (no install):** **[raid-architect.abacusai.cloud](https://raid-architect.abacusai.cloud)**

A dependency-light, single-file Python CLI that helps you **plan a RAID array**
and **price the drives** across three market tiers (Recertified, NAS Pro New,
Enterprise New).

It runs with **zero third-party packages** installed — falling back to a static
pricing baseline — and can optionally fetch **live** recertified-drive pricing
from [ServerPartDeals](https://serverpartdeals.com) when a web fetcher is
available.

---

## Table of Contents

- [Use it online (no install)](#use-it-online-no-install)
- [Overview](#overview)
- [Features](#features)
- [Installation](#installation)
  - [Zero-dependency quick run](#zero-dependency-quick-run)
  - [Optional live pricing (venv)](#optional-live-pricing-venv)
  - [Install as a command](#install-as-a-command)
- [Usage](#usage)
  - [Planning modes](#planning-modes)
  - [All options](#all-options)
  - [Example invocations](#example-invocations)
  - [Sample output](#sample-output)
- [RAID levels & tradeoffs](#raid-levels--tradeoffs)
- [Pricing sources & disclaimer](#pricing-sources--disclaimer)
- [JSON output](#json-output)
- [Testing](#testing)
- [Web app (self-hosting)](#web-app-self-hosting)
- [Contributing](#contributing)
- [License](#license)

---

## Use it online (no install)

Prefer not to install anything? A hosted web UI is available — open it in any
browser (desktop or mobile):

### 🌐 **[https://raid-architect.abacusai.cloud](https://raid-architect.abacusai.cloud)**

(The original `https://24ff2001c.abacusai.cloud` URL also still works.)

The web app uses the **exact same** calculation and pricing engine as the CLI
(`raid_architect.py` is imported directly — there is one source of truth). Fill
in the form, pick a RAID level (or a fault-tolerance target), choose a planning
mode (target capacity **or** fixed drive count), and get the full layout,
resilience, performance and three-tier pricing — plus a side-by-side
*Compare all levels* table.

It also exposes a small JSON API if you want to script against it:

```bash
# Single-level plan
curl -s -X POST https://raid-architect.abacusai.cloud/api/plan \
  -H 'Content-Type: application/json' \
  -d '{"size":"22TB","capacity":"340TB","level":"6"}'

# Compare every applicable level for a target
curl -s -X POST https://raid-architect.abacusai.cloud/api/compare \
  -H 'Content-Type: application/json' \
  -d '{"size":"22TB","capacity":"100TB"}'
```

> The same pricing disclaimer applies online: *live* figures reflect a single
> retailer at one moment, and the *baseline* is a rough early-2026 estimate.
> See [Pricing sources & disclaimer](#pricing-sources--disclaimer).

[↑ back to contents](#table-of-contents)

---

## Overview

`raid_architect.py` answers two everyday storage questions:

1. **"How many drives do I need?"** — give a target *usable* capacity and a RAID
   level, and it recommends a drive count.
2. **"What do I get with the drives I have?"** — give a fixed number of drives
   and it reports the resulting usable capacity, efficiency and resilience.

For either question it computes raw/usable capacity, capacity efficiency, fault
tolerance, read/write performance characteristics, and a per-tier price estimate
(total, $/TB and $/drive).

The whole tool lives in a single file so you can `curl` it and run it anywhere a
Python 3.9+ interpreter exists — no install, no dependencies required.

[↑ back to contents](#table-of-contents)

---

## Features

- **7 RAID levels:** RAID 0, 1, 5, 6, 10, 50, 60 — each with correct usable
  capacity, parity/mirror overhead, efficiency %, fault tolerance and
  qualitative read/write behaviour.
- **Two planning modes:** target-capacity → drive count, or fixed-drives →
  usable capacity.
- **Grouped parity (50/60):** choose `--groups` or `--group-size`, with full
  validation (even division, minimum group size).
- **Hot spares:** `--hot-spare N` reserves spares that add to cost and total
  drive count without changing usable capacity.
- **Compare view:** `--compare-levels` tabulates the same target across every
  RAID level so you can see the capacity/cost/resilience tradeoff at a glance.
- **Three pricing tiers:** Recertified, NAS Pro (New), Enterprise (New) with
  total / $per-TB / $per-drive.
- **Robust live pricing:** optional scrape of ServerPartDeals `products.json`
  using `cloudscraper` → `requests` → stdlib `urllib` (whichever is available),
  taking the **median** matching price, with clean timeout/HTTP/JSON handling
  and a within-run cache. Falls back to the static baseline and clearly labels
  the pricing source used.
- **Manual price overrides:** `--rate-refurb/--rate-nas/--rate-ent` ($/TB) or
  `--price-per-drive` keep the tool useful as prices change.
- **Scripting-friendly:** `--json` emits a structured, machine-readable result.
- **Polished, safe output:** aligned colour tables with OSC 8 hyperlinks that
  **auto-disable** for non-TTY output and when `NO_COLOR` is set, so piped
  output stays clean.
- **No stack traces for user errors:** input validation produces clear,
  actionable messages and a non-zero exit code.

[↑ back to contents](#table-of-contents)

---

## Installation

### Zero-dependency quick run

No install needed — just run the script with any Python 3.9+:

```bash
python3 raid_architect.py --size 22TB --level 6 --capacity 100TB
```

Or fetch and run it in one shot:

```bash
curl -fsSL https://raw.githubusercontent.com/entropytwo-ai/RAID-disk-pricer/main/raid_architect.py \
  | python3 - --size 22TB --level 6 --capacity 100TB
```

In this mode pricing uses the built-in static baseline (and, if the machine has
network access, the stdlib `urllib` fetcher may still retrieve live pricing).

[↑ back to contents](#table-of-contents)

### Optional live pricing (venv)

For the most reliable live pricing (Cloudflare bypass), install `cloudscraper`
into a virtualenv:

```bash
python3 -m venv ~/raid-env
source ~/raid-env/bin/activate
pip install -r requirements.txt     # or: pip install cloudscraper
python3 raid_architect.py --size 22TB --level 6 --capacity 100TB --compare
```

[↑ back to contents](#table-of-contents)

### Install as a command

Install the console script (`raid-architect` / `raid-pricer`) via `pip`:

```bash
pip install .                 # core tool, zero runtime deps
pip install ".[live]"         # + live-pricing fetchers (cloudscraper, requests)
raid-architect --size 22TB --level 6 --capacity 100TB
```

[↑ back to contents](#table-of-contents)

---

## Usage

### Planning modes

Choose **exactly one** mode:

| Mode | Flag | Meaning |
|------|------|---------|
| Capacity | `-c, --capacity <SIZE>` | Target **usable** capacity; tool recommends a drive count. |
| Drives | `-d, --drives <N>` | Fixed array drive count; tool reports usable capacity. |

The RAID level comes from `-l/--level`; alternatively `-f/--fault` derives it
from a desired fault tolerance (`0`→RAID 0, `1`→RAID 5, `2`→RAID 6).

[↑ back to contents](#table-of-contents)

### All options

```
array:
  -s, --size SIZE         Per-drive size, e.g. 22TB, 20, 500GB, 2PB. (required)
  -l, --level LEVEL       RAID level: 0, 1, 5, 6, 10, 50, 60.
  -f, --fault FAULT       Fault tolerance (0->0, 1->5, 2->6) when --level absent.
      --groups N          Number of parity groups for RAID 50/60.
      --group-size N      Drives per parity group for RAID 50/60.
      --hot-spare N       Reserve N hot-spare drives (cost only, no capacity).

planning mode (choose one):
  -c, --capacity SIZE     Target USABLE capacity; recommends a drive count.
  -d, --drives N          Fixed array drive count; reports usable capacity.

pricing:
      --compare           Deprecated no-op (live pricing is now the default).
      --historical        Force the static baseline (never hit the network).
                          (alias: --baseline)
      --rate-refurb USD   Override Recertified $/TB.
      --rate-nas USD      Override NAS Pro $/TB.
      --rate-ent USD      Override Enterprise $/TB.
      --price-per-drive USD   Known per-drive price (refurb basis); derives tiers.

output:
      --compare-levels    Tabulate ALL RAID levels for the same target.
      --json              Emit machine-readable JSON instead of a table.
      --color {auto,always,never}   Colour/hyperlink output (default: auto).
      --timeout SECONDS   Live-pricing network timeout (default: 12).
      --version           Show version and exit.
  -h, --help              Show help with examples and exit.
```

[↑ back to contents](#table-of-contents)

### Example invocations

```bash
# Plan for 100 TB usable on RAID 6 with 22 TB drives:
raid_architect.py --size 22TB --level 6 --capacity 100TB

# Fixed array: 8 x 20TB drives in RAID 10, resulting usable capacity:
raid_architect.py --size 20 --level 10 --drives 8

# Derive the level from a desired fault tolerance (2 -> RAID 6):
raid_architect.py --size 18TB --fault 2 --capacity 60TB

# RAID 60 across 2 groups with a hot spare, live pricing:
raid_architect.py --size 22TB --level 60 --capacity 200TB --groups 2 --hot-spare 1 --compare

# Compare every level for the same target, machine-readable:
raid_architect.py --size 22TB --capacity 100TB --compare-levels --json

# Override pricing manually (prices change!):
raid_architect.py --size 24TB --level 5 --drives 6 --rate-refurb 12.5 --rate-nas 19
```

[↑ back to contents](#table-of-contents)

### Sample output

Single-level plan (`--size 22TB --level 6 --capacity 100TB --historical`):

```text
══════════════════════════════════════════════════════════════
                 RAID 6 — RAID Plan & Pricing
══════════════════════════════════════════════════════════════

Layout
  Level                 RAID 6  — Dual distributed parity. Survives 2 drive failures; safer rebuilds.
  Drive size            22 TB
  Array drives          7
  Minimum drives        4

Capacity
  Raw capacity          154 TB
  Usable capacity       110 TB
  Efficiency            71.4%  ██████████████░░░░░░

Resilience & Performance
  Fault tolerance       2 drive(s)
                        Survives 2 drive failures.
  Read                  Excellent (striped reads)
  Write                 Moderate/Slow (double-parity write penalty)

Pricing   (source: baseline)
  Static early-2026 estimates.
  Tier                    $/TB      $/drive           Total
  ───────────────────────────────────────────────────────
  Recertified           $15.50      $341.00       $2,387.00
  NAS Pro (New)         $22.50      $495.00       $3,465.00
  Enterprise (New)      $25.00      $550.00       $3,850.00
```

Compare view (`--size 22TB --capacity 100TB --compare-levels --historical`):

```text
Level     Drives      Usable   Eff%  Tol     Refurb $        NAS $        Ent $
──────────────────────────────────────────────────────────────────────────────────────
RAID 0         5      110 TB   100%    0    $1,705.00    $2,475.00    $2,750.00
RAID 5         6      110 TB    83%    1    $2,046.00    $2,970.00    $3,300.00
RAID 6         7      110 TB    71%    2    $2,387.00    $3,465.00    $3,850.00
RAID 10       10      110 TB    50%    1    $3,410.00    $4,950.00    $5,500.00
RAID 50        8      132 TB    75%    1    $2,728.00    $3,960.00    $4,400.00
RAID 60       10      132 TB    60%    2    $3,410.00    $4,950.00    $5,500.00
```

[↑ back to contents](#table-of-contents)

---

## RAID levels & tradeoffs

| Level | Min drives | Usable (n drives of size s) | Tolerates | Efficiency | Best for |
|-------|-----------:|-----------------------------|-----------|-----------:|----------|
| **RAID 0** | 2 | `n · s` | 0 | 100% | Scratch/throughput only. No redundancy — any failure loses all data. |
| **RAID 1** | 2 | `s` | `n − 1` | `1/n` | Small, critical volumes; maximum redundancy. |
| **RAID 5** | 3 | `(n − 1) · s` | 1 | `(n−1)/n` | Balanced capacity/resilience for smaller arrays. |
| **RAID 6** | 4 | `(n − 2) · s` | 2 | `(n−2)/n` | Large arrays where a second failure during rebuild is a real risk. |
| **RAID 10** | 4 (even) | `(n/2) · s` | ≥1 (up to 1/mirror) | 50% | Low-latency, write-heavy workloads (databases, VMs). |
| **RAID 50** | 6 | `(n − g) · s` | 1 per group | `(n−g)/n` | Large arrays wanting better write throughput than RAID 5. |
| **RAID 60** | 8 | `(n − 2g) · s` | 2 per group | `(n−2g)/n` | Very large arrays needing double-parity safety with striping. |

*`g` = number of parity groups for grouped levels.*

**Rules of thumb**

- Prefer **RAID 6/60** over RAID 5/50 for large modern drives — rebuild times are
  long, and a second failure during rebuild is not rare.
- **RAID 10** trades half your capacity for the best random-write performance and
  fast rebuilds.
- **RAID 0** has **no** redundancy; use it only where data loss is acceptable.
- Fault tolerance for mirrored/grouped levels is a *guaranteed* minimum; the
  best case can be higher if failures are spread across mirrors/groups.

[↑ back to contents](#table-of-contents)

---

## Pricing sources & disclaimer

Three tiers are reported:

| Tier | Description | Baseline $/TB |
|------|-------------|--------------:|
| **Recertified** | Manufacturer-recertified / refurbished drives | 15.50 |
| **NAS Pro (New)** | New NAS-class drives (WD Red Pro, IronWolf Pro) | 22.50 |
| **Enterprise (New)** | New enterprise drives (WD Gold, Exos) | 25.00 |

**How pricing is resolved (in order):**

1. **Live (default)** — scrapes ServerPartDeals `products.json`, matches the
   requested size, and takes the **median** price across matching variants. The
   fetcher is chosen automatically: `cloudscraper` → `requests` → stdlib
   `urllib`. New-drive tiers are derived from the live refurb price via fixed
   markups. On any timeout/HTTP/JSON error it falls back to the baseline. Pass
   `--historical` (alias `--baseline`) to skip the network and force the baseline.
2. **Static baseline** — built-in early-2026 estimates. Always available, no
   network; used on `--historical` or whenever a live fetch fails.
3. **Manual overrides** — `--rate-*` ($/TB) or `--price-per-drive` always win,
   so you can plug in a quote you already have.

The pricing source used is always printed (`source: baseline | live | manual`).

> **Disclaimer:** Drive prices are volatile and vary by region, capacity, seller
> and availability. The baseline figures are rough estimates and the live scrape
> reflects one retailer at one moment. **Always verify current prices before
> purchasing.** This tool is for planning/estimation only and is not affiliated
> with any retailer or manufacturer.

[↑ back to contents](#table-of-contents)

---

## JSON output

`--json` prints a structured object (layout + pricing) for scripting:

```bash
raid_architect.py --size 22TB --level 6 --drives 7 --json
```

```json
{
  "version": "2.0.0",
  "layout": {
    "level": "6",
    "level_name": "RAID 6",
    "drive_size_tb": 22.0,
    "data_drives": 7,
    "usable_capacity_tb": 110.0,
    "efficiency_pct": 71.42857142857143,
    "fault_tolerance": 2,
    "...": "..."
  },
  "pricing": {
    "source": "baseline",
    "rates_per_tb": { "Refurb": 15.5, "NAS": 22.5, "Ent": 25.0 },
    "quotes": [ { "tier": "Refurb", "per_drive": 341.0, "total": 2387.0 } ]
  }
}
```

`--compare-levels --json` emits a `levels` array covering every applicable RAID
level for the target.

[↑ back to contents](#table-of-contents)

---

## Testing

The test suite covers the capacity/parity/fault-tolerance math for **every**
RAID level, size/argument parsing, pricing resolution and the end-to-end CLI. It
runs under both `pytest` and the stdlib `unittest` (no deps required):

```bash
python3 -m unittest discover -s tests -v
# or
pytest -q
```

[↑ back to contents](#table-of-contents)

---

## Web app (self-hosting)

The browser UI is a thin Flask layer over `raid_architect.py` — it imports the
library functions, so there is no duplicated RAID/pricing logic.

```
webapp.py            Flask backend (routes: / , /api/levels, /api/plan, /api/compare, /healthz)
static/              Single-page frontend (index.html, style.css, app.js) — no build step, no framework
deploy/              systemd unit + nginx vhost + idempotent install.sh
```

**Run locally:**

```bash
pip install flask gunicorn        # web-only deps (the CLI never needs these)
python3 webapp.py                 # dev server on http://127.0.0.1:8137
# or production:
gunicorn --workers 2 --bind 127.0.0.1:8137 webapp:app
```

**Deploy behind nginx (Abacus supercomputer, Pattern A):** the unit file and
vhost live in `deploy/` and are symlinked into `/etc`. One command sets it up and
is safe to re-run (e.g. after an image update):

```bash
bash deploy/install.sh
```

That installs `flask`/`gunicorn` into `/opt/abacus-python`, links
`deploy/raid-web.service` → `/etc/systemd/system/`, links
`deploy/24ff2001c.conf` and `deploy/raid-architect.conf` → `/etc/nginx/conf.d/`,
enables the service and reloads
nginx. The vhost sends a `Cache-Control` header and
`Content-Security-Policy: frame-ancestors 'self' https://*.abacus.ai` (and sets
no `X-Frame-Options`) so the app renders in the platform's iframe preview.

**Tear down:**

```bash
sudo systemctl disable --now raid-web
sudo rm /etc/systemd/system/raid-web.service
sudo rm /etc/nginx/conf.d/24ff2001c.conf /etc/nginx/conf.d/raid-architect.conf
sudo systemctl daemon-reload && sudo nginx -t && sudo systemctl reload nginx
```

[↑ back to contents](#table-of-contents)

---

## Contributing

Contributions are welcome! Please:

1. Keep the tool **runnable with zero third-party dependencies** — optional deps
   must degrade gracefully.
2. Add/adjust tests in `tests/` for any behaviour change and make sure the suite
   passes in an environment **without** optional deps installed.
3. Keep user-facing errors clear and traceback-free, and keep output clean for
   non-TTY / `NO_COLOR` consumers.

[↑ back to contents](#table-of-contents)

---

## License

Released under the **GNU General Public License v3.0** — see [LICENSE](LICENSE).

[↑ back to contents](#table-of-contents)
