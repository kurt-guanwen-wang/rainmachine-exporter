# rainmachine-exporter

A [Prometheus](https://prometheus.io/) exporter for [RainMachine](https://www.rainmachine.com/)
smart sprinkler controllers. Talks to the controller's local HTTP API and exposes device,
zone, program, forecast, watering activity, restriction, and flow meter metrics.

## Tested with

- **Device**: RainMachine Pro Touch HD - 12 zones
- **Firmware**: 4.0.1144
- **API version**: 4.6.1

## Metrics

The exporter collects, from the local RainMachine API:

- **Device** software/hardware/API version info, MAC address
- **Diagnostics** uptime, CPU/memory usage, network/internet/location/time/weather status
- **Zones** active/running state, remaining time, master valve flag, restriction flag
- **Programs** active/running state, whether scheduled to run today
- **Forecast** weather-adjusted watering percentage and min/max temperature for upcoming days
- **Today's activity** scheduled vs. actually watered time, water-saved percent, live watering/queue state
- **Restrictions** per-reason active restrictions, rain delay counter, freeze protection
- **Flow meter** watering/leak click counters

## Configuration

The exporter is configured entirely via environment variables:

| Variable                | Required | Default                                       | Description                                          |
|-------------------------|----------|------------------------------------------------|-------------------------------------------------------|
| `RAINMACHINE_BASE_URL`  | yes      | —                                                | Base URL of the RainMachine local API, e.g. `https://RAIM_MACHINE_IP_OR_DNS:8080/api/4` |
| `RAINMACHINE_PASSWORD`  | yes      | —                                                | RainMachine controller admin password                 |
| `TIMEZONE`              | no       | `America/Los_Angeles`                          | IANA timezone used to compute "today"                  |
| `EXPORTER_PORT`         | no       | `9100`                                          | Port the exporter listens on                           |
| `SCRAPE_INTERVAL`       | no       | `60`                                            | Idle loop interval in seconds (metrics are collected on each Prometheus scrape) |
| `FORECAST_DAYS`         | no       | `6`                                             | Number of upcoming dailystats forecast days to export  |
| `LOG_LEVEL`             | no       | `INFO`                                          | Python logging level                                   |

The RainMachine local API uses a self-signed TLS certificate; the exporter disables certificate
verification for local API calls only.

## Running with Docker

```bash
docker run -d \
  -e RAINMACHINE_BASE_URL=https://RAIM_MACHINE_IP_OR_DNS:8080/api/4 \
  -e RAINMACHINE_PASSWORD=your-password \
  -p 9100:9100 \
  nirvanawgw/rainmachine-exporter:latest
```

Then scrape `http://<host>:9100/metrics` from Prometheus.

## Running locally

This project uses [`uv`](https://docs.astral.sh/uv/) for dependency management.

```bash
uv sync --no-dev
RAINMACHINE_BASE_URL=https://RAIM_MACHINE_IP_OR_DNS:8080/api/4 RAINMACHINE_PASSWORD=your-password uv run python exporter.py
```

## Development

```bash
uv sync --all-extras
uv run python -m py_compile exporter.py
```

Commit messages must follow [Conventional Commits](https://www.conventionalcommits.org/); this is enforced
on pull requests via [gitlint](https://jorisroovers.com/gitlint/). Releases are automated with
[python-semantic-release](https://python-semantic-release.readthedocs.io/): every merge to `main` determines
the next version from commit history, tags the release, generates a changelog, publishes a GitHub Release,
and builds/pushes a multi-arch Docker image to Docker Hub.

## License

[MIT](LICENSE)
