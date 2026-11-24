# Drone Watch

Multi-sensor drone detection: a fusion tracker, a real-time HTTP/WebSocket service with a dashboard on an offline 3D map,
a sensor emulator and a Docker stand with an MQTT broker. The full documentation is in Ukrainian ([README.md](README.md));
this page is the short English version.

[![CI](https://github.com/Nostradam4ik/drone-watch/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/Nostradam4ik/drone-watch/actions/workflows/ci.yml)

## What it does

A radar measures range and bearing; an RF detector and an acoustic sensor measure bearing only. Drone Watch fuses those
measurements into **one track per drone**, places each track against protected zones (buffer and critical) and raises
alerts for the operator, who confirms or dismisses them.

- **Fusion tracker** (`src/dronewatch/fusion`): extended Kalman filters with interacting multiple models (IMM),
  Mahalanobis-distance gating, Hungarian assignment, triangulation from bearings alone.
- **Service** (`src/dronewatch/service`): FastAPI, WebSocket dashboard, MQTT ingestion over TLS with per-sensor ACLs,
  authentication, an audit log, incident replay, a hot standby that takes over within `lease_ttl_s` (6 s by default).
- **Offline 3D map**: the map file is served by the application itself, so no third-party tile server learns which area
  is being watched and the map still works without Internet.
- **Operator assistant** (optional profile): RAG over past incidents (Qdrant) with tool-calling on Mistral.
- **Emulator** (`src/dronewatch/emulator`): standard-library only, drives the stand with realistic sensor events.

## Run it

Python 3.12+.

```
pip install -e ".[dev]"
pytest                      # 616 tests
ruff check . && mypy        # both clean
python -m uvicorn dronewatch.service.app:app --port 8000
```

Then open http://127.0.0.1:8000 (demo mode: a built-in swarm simulator, plain HTTP, localhost only).
`docker compose up --build` brings up the whole stand over HTTPS with demo certificates.
To get the 3D map, build the map file once: `pip install -e ".[map]"` then `dronewatch-mapbuild --lat 48.8584 --lon 2.2945 --radius-km 5 --out data/map/paris.pmtiles`.

## Honest limits

- **Every measurement comes from a simulator.** There are no real sensor recordings in this repository. The evaluation
  tool (`dronewatch-eval`: OSPA, RMSE, missed and false tracks, ID switches) runs the real engine on a recording and is
  ready for real data, but no accuracy on real hardware is claimed.
- The drone names shown in the demo are simulation profiles.
- The passwords in `docker-compose.yml` are demo secrets, marked as such and published on purpose. Replace them for
  anything beyond a local demo.

## Measured

With 300 random detections per sensor and no ceilings, one scan could take up to 27 s. After optimising the gating
(analytic Jacobian, batched computations) and adding explicit ceilings, the worst measured scan is about 0.03 s on the
author's machine. `dronewatch-bench` reproduces the flood test on your own hardware.
