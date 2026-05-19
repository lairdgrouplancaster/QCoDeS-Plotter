# LabPulse Project Review (May 19, 2026)

This review highlights immediate bugs first, then recommendations for user experience, documentation, maintainability, and future roadmap.

## 1) Bugs / high-risk defects to fix first

1. **Import-time crash in `pressure_pub.py` due to constant initialization order**  
   `current_threshold = load_threshold_from_file("pressure")` executes before `DEFAULT_PRESSURE_THRESHOLD_BAR` is defined. If the config file is missing (or key absent), `load_threshold_from_file` returns `DEFAULT_PRESSURE_THRESHOLD_BAR`, which is not defined yet and raises `NameError` at startup.  
   **Fix:** Move `DEFAULT_PRESSURE_THRESHOLD_BAR` above any function or global that references it, or delay threshold loading until `main()`. 

2. **Config filename mismatch likely breaks recipient loading**  
   Several scripts load `/home/monitorpi/Desktop/Phone_Numbers.json`, but repository config is named `phone_number_config.json` (different casing and name). On Linux this causes `FileNotFoundError` and blocks alerting.  
   **Fix:** Standardize one canonical config filename and path, then update all scripts and docs.

3. **Hardcoded absolute paths reduce portability and fail outside one host setup**  
   Scripts assume `/home/monitorpi/Desktop/...` for config and thresholds. This will fail on clean installs, containers, and any user account not named `monitorpi`.  
   **Fix:** Use environment variables + repo-local defaults (`Path(__file__).resolve().parents[...]`).

4. **Serial port collision risk across scripts**  
   Multiple publishers default to `/dev/ttyACM0`; if multiple services run simultaneously and one Arduino re-enumerates, scripts can read wrong devices or fail to open.  
   **Fix:** Use stable udev symlinks (`/dev/labpulse/*`) per device serial ID.

5. **Broad exception handling hides root-cause failures**  
   Some blocks (e.g., UPS SMS worker) use bare `except:` which suppresses diagnostics and can leave silent alert failures.  
   **Fix:** Catch specific exception classes and log traceback/context.

## 2) User experience improvements

1. **Add one-command installer/setup**  
   Provide `./scripts/setup.sh` to install Python deps, register systemd services, copy config templates, and validate hardware prerequisites.

2. **Ship a lightweight health dashboard status topic**  
   Publish per-service heartbeat (last-seen timestamps) so Home Assistant can show “sensor script offline” clearly.

3. **Improve alert message quality**  
   Include location, sensor label, threshold, and suggested operator action in SMS text.

4. **Configuration UX**  
   Keep all adjustable settings in one YAML/JSON file instead of per-script constants.

## 3) Documentation improvements

1. **Create “Quick Start (30 min)” section** with exact steps:
   - Flash Pi OS and enable interfaces.
   - Install Mosquitto/Home Assistant prerequisites.
   - Upload each Arduino sketch.
   - Configure phone numbers and thresholds.
   - Start/enable all services.

2. **Add architecture diagram + dataflow**
   - Sensor -> Arduino -> serial -> Python publisher -> MQTT -> Home Assistant -> SMS path.

3. **Create troubleshooting matrix**
   - “No MQTT data”, “No SMS”, “Wrong serial device”, “sensor reads None”, etc., each with commands and expected outputs.

4. **Explicit compatibility table**
   - Tested OS image, Python version, library versions, hardware revisions.

## 4) Maintainability improvements

1. **Refactor duplicated logic into shared module**  
   SMS sending, modem discovery, recipient loading, threshold persistence, and timestamp formatting are duplicated across scripts. Consolidate into `labpulse_common/` package.

2. **Introduce project packaging and dependency pinning**  
   Add `pyproject.toml` + lock file or requirements with pinned versions to make deployments reproducible.

3. **Add static checks and tests**  
   - `ruff`/`flake8`, `black`, `mypy` (or pyright)
   - Unit tests for threshold logic and parser behavior.
   - Integration mocks for serial and MQTT publishing.

4. **Use structured logging**  
   Replace `print` with `logging` (timestamps, levels, module names) and log to journal for service debugging.

5. **Use typed config schema**  
   Validate config on startup with `pydantic` or JSON schema and fail fast with clear errors.

## 5) Future roadmap suggestions

1. **Sensor abstraction plugin model**
   Build a generic sensor-driver interface so adding CO2/O2/current/future sensors is mostly config, not new full scripts.

2. **Rule engine for alerts**
   Move from fixed threshold checks to reusable rules (time windows, hysteresis, debounce, multi-sensor voting).

3. **Remote configuration and OTA updates**
   Centralize config changes and deploy script updates safely with rollback.

4. **Data persistence + analytics**
   Store measurements in InfluxDB/Timescale and add trend anomaly detection to reduce nuisance alerts.

5. **Security hardening**
   Add MQTT auth/TLS, secrets management, least-privilege service users, and signed release process.

## Priority order

- **P0 (immediate):** fix import-order crash; unify config filenames/paths; stable serial mapping.  
- **P1 (next):** shared common module + logging + setup automation.  
- **P2 (later):** tests/CI, pluginized architecture, analytics/security hardening.
