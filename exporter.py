#!/usr/bin/env python3
"""RainMachine Prometheus Exporter - full local API metric coverage.

Covers: device info/versions, diagnostics, zones, programs, 6-day
dailystats forecast, today's watering activity, restrictions/rain
delay, and flow meter counters.
"""

import os
import threading
import time
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

import requests
import urllib3
from prometheus_client import start_http_server
from prometheus_client.core import GaugeMetricFamily, REGISTRY

# RainMachine's local API uses a self-signed certificate.
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("rainmachine_exporter")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
RAINMACHINE_BASE_URL = os.environ.get("RAINMACHINE_BASE_URL", "")
RAINMACHINE_PASSWORD = os.environ.get("RAINMACHINE_PASSWORD", "")
TIMEZONE = os.environ.get("TIMEZONE", "America/Los_Angeles")
SCRAPE_INTERVAL = int(os.environ.get("SCRAPE_INTERVAL", "60"))
EXPORTER_PORT = int(os.environ.get("EXPORTER_PORT", "9100"))
# How many of the upcoming dailystats days to export (RainMachine returns 6).
FORECAST_DAYS = int(os.environ.get("FORECAST_DAYS", "6"))

if not RAINMACHINE_BASE_URL:
    raise SystemExit("RAINMACHINE_BASE_URL environment variable is required.")
if not RAINMACHINE_PASSWORD:
    raise SystemExit("RAINMACHINE_PASSWORD environment variable is required.")

# ---------------------------------------------------------------------------
# RainMachine API helpers
# ---------------------------------------------------------------------------

# start_http_server() runs a threaded HTTP server, so concurrent /metrics
# scrapes can race on the shared token state below - guard it with a lock.
_token_lock = threading.Lock()
_access_token = None
_token_expires_at = 0.0

# Reuse a single session for connection pooling across requests.
_session = requests.Session()
_session.verify = False


def _login() -> None:
    """Authenticate against the local RainMachine API and cache the token."""
    global _access_token, _token_expires_at
    log.debug("Logging in to RainMachine at %s", RAINMACHINE_BASE_URL)
    resp = _session.post(
        f"{RAINMACHINE_BASE_URL}/auth/login",
        json={"pwd": RAINMACHINE_PASSWORD, "remember": 1},
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    _access_token = data["access_token"]
    # Refresh a bit early to avoid racing against the actual expiration.
    _token_expires_at = time.monotonic() + int(data.get("expires_in", 3600)) - 30
    log.info("Logged in to RainMachine (token valid for %ss)", data.get("expires_in"))


def rm_get(path: str) -> dict:
    """GET a RainMachine API endpoint, logging in (or relogging in) as needed."""
    with _token_lock:
        if not _access_token or time.monotonic() >= _token_expires_at:
            _login()
        token = _access_token

    url = f"{RAINMACHINE_BASE_URL}{path}"
    resp = _session.get(url, params={"access_token": token}, timeout=30)
    if resp.status_code == 401:
        # Token expired/invalid server-side - relogin once and retry.
        log.warning("Access token rejected, re-authenticating")
        with _token_lock:
            _login()
            token = _access_token
        resp = _session.get(url, params={"access_token": token}, timeout=30)
    resp.raise_for_status()
    return resp.json()


def _today_str() -> str:
    return _now().strftime("%Y-%m-%d")


def _now() -> datetime:
    return datetime.now(ZoneInfo(TIMEZONE))


def _bool_to_num(value) -> int:
    return 1 if value else 0


def _sum_zone_field(programs: list, field: str) -> float:
    """Sum a numeric field across every zone of every program in a dailystats-style list."""
    total = 0.0
    for program in programs or []:
        for zone in program.get("zones", []) or []:
            total += zone.get(field, 0) or 0
    return total


def _sum_watered_seconds(day: dict) -> float:
    """Total actual watering duration for the day, in seconds.

    The plain /watering/log/{date}/{days} endpoint returns a day-level
    "realDuration" total directly (no programs/zones/cycles nesting).
    The nested breakdown is only present on the separate
    /watering/log/details/{date}/{days} endpoint. Support both shapes.
    """
    if "realDuration" in day:
        return day.get("realDuration", 0) or 0
    total = 0.0
    for program in day.get("programs", []) or []:
        for zone in program.get("zones", []) or []:
            for cycle in zone.get("cycles", []) or []:
                total += cycle.get("realDuration", 0) or 0
    return total


class RainMachineCollector:
    """Custom Prometheus collector for RainMachine metrics."""

    def collect(self):
        t0 = time.monotonic()
        log.info("Scrape started")
        errors = 0

        metrics = {}

        def metric(name, help_text, labels=None):
            metrics[name] = GaugeMetricFamily(name, help_text, labels=labels or [])
            return metrics[name]

        # ---- static definitions -------------------------------------------------
        info = metric(
            "rainmachine_info",
            "Static RainMachine device/version info (always 1)",
            ["software_version", "hardware_version", "api_version", "mac_address"],
        )

        uptime_seconds = metric("rainmachine_uptime_seconds", "Controller uptime in seconds")
        cpu_usage_percent = metric("rainmachine_cpu_usage_percent", "Controller CPU usage percent")
        mem_usage = metric("rainmachine_mem_usage", "Controller memory usage as reported by /diag")
        network_status = metric("rainmachine_network_status", "Network link status (1=up, 0=down)")
        internet_status = metric("rainmachine_internet_status", "Internet reachability status (1=up, 0=down)")
        location_status = metric("rainmachine_location_status", "Location lookup status (1=ok, 0=not ok)")
        time_status = metric("rainmachine_time_status", "Time sync status (1=ok, 0=not ok)")
        weather_status = metric("rainmachine_weather_status", "Weather data status (1=ok, 0=not ok)")
        cloud_status = metric("rainmachine_cloud_status", "RainMachine cloud connection status code")
        last_check_timestamp = metric(
            "rainmachine_last_check_timestamp_seconds", "Unix timestamp of the last self-check"
        )

        zone_active = metric("rainmachine_zone_active", "Zone enabled/active (1=active)", ["zone_id", "zone_name"])
        zone_running = metric(
            "rainmachine_zone_running", "Zone currently watering (1=running)", ["zone_id", "zone_name"]
        )
        zone_remaining_seconds = metric(
            "rainmachine_zone_remaining_seconds", "Seconds remaining in the zone's current cycle", ["zone_id", "zone_name"]
        )
        zone_master = metric("rainmachine_zone_master", "Zone is a master valve (1=yes)", ["zone_id", "zone_name"])
        zone_restricted = metric(
            "rainmachine_zone_restricted", "Zone is currently restricted from watering (1=yes)", ["zone_id", "zone_name"]
        )
        zone_count = metric("rainmachine_zone_count", "Total number of zones")
        zone_active_count = metric("rainmachine_zone_active_count", "Number of active (enabled) zones")
        zone_running_count = metric("rainmachine_zone_running_count", "Number of zones currently watering")

        program_active = metric(
            "rainmachine_program_active", "Program enabled/active (1=active)", ["program_id", "program_name"]
        )
        program_running = metric(
            "rainmachine_program_running", "Program currently running (1=running)", ["program_id", "program_name"]
        )
        program_scheduled_today = metric(
            "rainmachine_program_scheduled_today",
            "Program is active and scheduled to run today (1=yes)",
            ["program_id", "program_name"],
        )
        program_count = metric("rainmachine_program_count", "Total number of programs")
        program_active_count = metric("rainmachine_program_active_count", "Number of active (enabled) programs")

        daily_watering_percent = metric(
            "rainmachine_daily_watering_percent",
            "Percent of scheduled watering time actually applied (weather-adjusted), per forecast day",
            ["day", "day_offset"],
        )
        daily_min_temp_celsius = metric(
            "rainmachine_daily_min_temp_celsius", "Forecast minimum temperature, per forecast day", ["day", "day_offset"]
        )
        daily_max_temp_celsius = metric(
            "rainmachine_daily_max_temp_celsius", "Forecast maximum temperature, per forecast day", ["day", "day_offset"]
        )

        water_saved_percent = metric(
            "rainmachine_water_saved_percent",
            "Percentage of today's scheduled watering time saved by smart weather adjustments",
        )
        scheduled_today_seconds = metric(
            "rainmachine_scheduled_today_seconds", "Total watering time scheduled for today, in seconds"
        )
        scheduled_today_count = metric(
            "rainmachine_scheduled_today_count", "Number of active programs scheduled to run today"
        )
        watered_today_seconds = metric(
            "rainmachine_watered_today_seconds", "Total watering time actually applied today, in seconds"
        )
        watering_active = metric("rainmachine_watering_active", "Whether any zone is watering right now (1=yes)")
        watering_queue_length = metric("rainmachine_watering_queue_length", "Number of watering activities queued")

        restriction_active = metric(
            "rainmachine_restriction_active", "Currently active restriction by reason (1=active)", ["reason"]
        )
        rain_delay_counter_seconds = metric(
            "rainmachine_rain_delay_counter_seconds", "Seconds remaining on an active rain delay (-1 = none active)"
        )
        freeze_protect_enabled = metric(
            "rainmachine_freeze_protect_enabled", "Freeze protection restriction enabled (1=yes)"
        )
        freeze_protect_temp_celsius = metric(
            "rainmachine_freeze_protect_temp_celsius", "Freeze protection temperature threshold"
        )

        flowmeter_watering_clicks = metric(
            "rainmachine_flowmeter_watering_clicks_total", "Total flow meter clicks recorded during watering"
        )
        flowmeter_leak_clicks = metric(
            "rainmachine_flowmeter_leak_clicks_total", "Total flow meter clicks recorded outside of watering (possible leak)"
        )
        flowmeter_start_index_clicks = metric(
            "rainmachine_flowmeter_start_index_clicks", "Flow meter click counter at last reset"
        )

        scrape_success = metric(
            "rainmachine_exporter_scrape_success", "Whether the last scrape of the RainMachine API succeeded"
        )
        scrape_errors = metric(
            "rainmachine_exporter_scrape_errors", "Number of API calls that failed during the last scrape"
        )
        scrape_duration = metric("rainmachine_exporter_scrape_duration_seconds", "Duration in seconds of the last scrape")

        def section(name, func):
            nonlocal errors
            try:
                func()
            except Exception as exc:  # noqa: BLE001 - keep scraping other sections
                errors += 1
                log.warning("Section %s failed: %s", name, exc)

        today = _today_str()

        # ---- device info & versions ---------------------------------------------
        def do_info():
            versions = rm_get("/apiVer")
            wifi = rm_get("/provision/wifi")
            info.add_metric(
                [
                    str(versions.get("swVer", "")),
                    str(versions.get("hwVer", "")),
                    str(versions.get("apiVer", "")),
                    str(wifi.get("macAddress", "")),
                ],
                1,
            )

        section("info", do_info)

        # ---- diagnostics ----------------------------------------------------------
        def do_diag():
            diag = rm_get("/diag")
            uptime_seconds.add_metric([], diag.get("uptimeSeconds", 0))
            cpu_usage_percent.add_metric([], diag.get("cpuUsage", 0))
            mem_usage.add_metric([], diag.get("memUsage", 0))
            network_status.add_metric([], _bool_to_num(diag.get("networkStatus")))
            internet_status.add_metric([], _bool_to_num(diag.get("internetStatus")))
            location_status.add_metric([], _bool_to_num(diag.get("locationStatus")))
            time_status.add_metric([], _bool_to_num(diag.get("timeStatus")))
            weather_status.add_metric([], _bool_to_num(diag.get("weatherStatus")))
            cloud_status.add_metric([], diag.get("cloudStatus", 0) or 0)
            last_check_timestamp.add_metric([], diag.get("lastCheckTimestamp", 0))

        section("diag", do_diag)

        # ---- zones ------------------------------------------------------------
        def do_zones():
            zones = rm_get("/zone").get("zones", [])
            active_count = 0
            running_count = 0
            for zone in zones:
                labels = [str(zone["uid"]), zone.get("name", f"Zone {zone['uid']}")]
                active = _bool_to_num(zone.get("active", True))
                running = 1 if zone.get("state", 0) else 0
                zone_active.add_metric(labels, active)
                zone_running.add_metric(labels, running)
                zone_remaining_seconds.add_metric(labels, zone.get("remaining", 0) or 0)
                zone_master.add_metric(labels, _bool_to_num(zone.get("master")))
                zone_restricted.add_metric(labels, _bool_to_num(zone.get("restriction")))
                active_count += active
                running_count += running
            zone_count.add_metric([], len(zones))
            zone_active_count.add_metric([], active_count)
            zone_running_count.add_metric([], running_count)

        section("zones", do_zones)

        # ---- programs -----------------------------------------------------------
        # Note: program["nextRun"] is the *next future* scheduled date (it advances
        # past today once today's run has been computed/started), so it cannot be
        # used to determine "is this scheduled today". That's derived separately in
        # do_dailystats_details(), which is the authoritative source for today.
        def do_programs():
            programs = rm_get("/program").get("programs", [])
            active_count = 0
            for program in programs:
                labels = [str(program["uid"]), program.get("name", f"Program {program['uid']}")]
                active = _bool_to_num(program.get("active"))
                running = 1 if program.get("status", 0) else 0
                program_active.add_metric(labels, active)
                program_running.add_metric(labels, running)
                active_count += active
            program_count.add_metric([], len(programs))
            program_active_count.add_metric([], active_count)

        section("programs", do_programs)

        # ---- dailystats: today's water-saved % + N-day forecast --------------
        def do_dailystats_today():
            stats_today = rm_get(f"/dailystats/{today}")
            percentage = stats_today.get("percentage")
            if percentage is None:
                percentage = 100
            water_saved_percent.add_metric([], max(0, 100 - percentage))

        section("dailystats_today", do_dailystats_today)

        def do_dailystats_forecast():
            upcoming = rm_get("/dailystats").get("DailyStats", [])
            for offset, day in enumerate(upcoming[:FORECAST_DAYS]):
                day_str = day.get("day", "")
                labels = [day_str, str(offset)]
                if day.get("percentage") is not None:
                    daily_watering_percent.add_metric(labels, day["percentage"])
                if day.get("mint") is not None:
                    daily_min_temp_celsius.add_metric(labels, day["mint"])
                if day.get("maxt") is not None:
                    daily_max_temp_celsius.add_metric(labels, day["maxt"])

        section("dailystats_forecast", do_dailystats_forecast)

        # ---- dailystats/details: today's scheduled seconds --------------------
        def do_dailystats_details():
            details = rm_get("/dailystats/details").get("DailyStatsDetails", [])
            today_details = next((d for d in details if d.get("day") == today), None)
            today_programs = today_details.get("programs", []) if today_details else []
            scheduled_seconds = _sum_zone_field(today_programs, "scheduledWateringTime")
            scheduled_today_seconds.add_metric([], scheduled_seconds)

            # today_programs only carries program ids, not names - look up names
            # from the program list so scheduled-today can be labeled consistently
            # with the other per-program metrics.
            scheduled_program_ids = {p["id"] for p in today_programs}
            programs = rm_get("/program").get("programs", [])
            for program in programs:
                labels = [str(program["uid"]), program.get("name", f"Program {program['uid']}")]
                program_scheduled_today.add_metric(labels, 1 if program["uid"] in scheduled_program_ids else 0)
            scheduled_today_count.add_metric([], len(scheduled_program_ids))

        section("dailystats_details", do_dailystats_details)

        # ---- today's actual watering log ---------------------------------------
        def do_watering_log():
            log_resp = rm_get(f"/watering/log/{today}/1")
            days = log_resp.get("waterLog", {}).get("days", [])
            watered_seconds = _sum_watered_seconds(days[0]) if days else 0
            watered_today_seconds.add_metric([], watered_seconds)

        section("watering_log", do_watering_log)

        # ---- live watering activity ---------------------------------------------
        def do_watering_activity():
            running_zones = rm_get("/watering/zone").get("zones", [])
            is_active = any(z.get("state", 0) for z in running_zones)
            watering_active.add_metric([], _bool_to_num(is_active))

            queue = rm_get("/watering/queue").get("queue", [])
            watering_queue_length.add_metric([], len(queue))

        section("watering_activity", do_watering_activity)

        # ---- restrictions --------------------------------------------------------
        def do_restrictions():
            current = rm_get("/restrictions/currently")
            for reason, key in (
                ("hourly", "hourly"),
                ("freeze", "freeze"),
                ("month", "month"),
                ("weekday", "weekDay"),
                ("rain_delay", "rainDelay"),
                ("rain_sensor", "rainSensor"),
            ):
                restriction_active.add_metric([reason], _bool_to_num(current.get(key)))
            rain_delay_counter_seconds.add_metric([], current.get("rainDelayCounter", -1))

            universal = rm_get("/restrictions/global")
            freeze_protect_enabled.add_metric([], _bool_to_num(universal.get("freezeProtectEnabled")))
            freeze_protect_temp_celsius.add_metric([], universal.get("freezeProtectTemp", 0) or 0)

        section("restrictions", do_restrictions)

        # ---- flow meter ------------------------------------------------------------
        def do_flowmeter():
            flow = rm_get("/watering/flowmeter")
            flowmeter_watering_clicks.add_metric([], flow.get("flowMeterWateringClicks", 0) or 0)
            flowmeter_leak_clicks.add_metric([], flow.get("flowMeterLeakClicks", 0) or 0)
            flowmeter_start_index_clicks.add_metric([], flow.get("flowMeterStartIndexClicks", 0) or 0)

        section("flowmeter", do_flowmeter)

        scrape_success.add_metric([], 1 if errors == 0 else 0)
        scrape_errors.add_metric([], errors)
        scrape_duration.add_metric([], time.monotonic() - t0)

        log.info("Scrape complete in %.2fs (%d section error(s))", time.monotonic() - t0, errors)

        yield from metrics.values()


def main():
    REGISTRY.register(RainMachineCollector())
    start_http_server(EXPORTER_PORT)
    log.info("RainMachine exporter listening on :%d", EXPORTER_PORT)
    while True:
        time.sleep(SCRAPE_INTERVAL)


if __name__ == "__main__":
    main()