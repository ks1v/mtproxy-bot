"""
log_cron.py — runs every 5 minutes via supervisord
Reads new telemt Docker log lines, strips ANSI, extracts per-user
connection and error stats, writes to stats.json in hourly buckets.
"""

import re
import json
import subprocess
import logging
from datetime import datetime, timezone
from pathlib import Path

STATS_PATH  = Path("/data/stats.json")
STATE_PATH  = Path("/data/.log_cursor")   # stores last processed log timestamp
CONTAINER   = "telemt"

ANSI        = re.compile(r"\x1b\[[0-9;]*m")
RE_TS       = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})")
RE_PEER     = re.compile(r"peer=([\d.]+(?::\d+)?)")
RE_USER     = re.compile(r"user=(\S+)")
RE_ERROR    = re.compile(r"error=(.+)$")

logging.basicConfig(
    format="%(asctime)s [cron] %(levelname)s %(message)s",
    level=logging.INFO
)
log = logging.getLogger(__name__)


def load_stats() -> dict:
    if STATS_PATH.exists():
        try:
            return json.loads(STATS_PATH.read_text())
        except Exception as e:
            log.warning(f"Failed to load stats.json: {e}")
    return {}


def save_stats(stats: dict):
    STATS_PATH.write_text(json.dumps(stats, indent=2))


def load_cursor() -> str | None:
    if STATE_PATH.exists():
        return STATE_PATH.read_text().strip() or None
    return None


def save_cursor(ts: str):
    STATE_PATH.write_text(ts)


def fetch_logs(since: str | None) -> list[str]:
    """Pull logs from Docker container, optionally since a timestamp."""
    cmd = ["docker", "logs", CONTAINER, "--timestamps"]
    if since:
        cmd += ["--since", since]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True, text=True, timeout=30
        )
        # docker logs writes to stderr
        raw = result.stderr or result.stdout
        return raw.splitlines()
    except subprocess.TimeoutExpired:
        log.error("docker logs timed out")
        return []
    except Exception as e:
        log.error(f"docker logs failed: {e}")
        return []


def hour_bucket(ts_str: str) -> str:
    """Convert '2026-03-01T14:23:45' → '2026-03-01T14'"""
    return ts_str[:13]


def process_lines(lines: list[str], stats: dict) -> tuple[dict, str | None]:
    """
    Parse lines, update stats dict.
    Returns (updated_stats, last_timestamp_seen).
    """
    last_ts = None

    for raw_line in lines:
        line = ANSI.sub("", raw_line).strip()
        if not line:
            continue

        m_ts = RE_TS.search(line)
        if not m_ts:
            continue
        ts_str = m_ts.group(1)
        last_ts = ts_str
        bucket  = hour_bucket(ts_str)

        m_user = RE_USER.search(line)
        username = m_user.group(1) if m_user else "unknown"

        # Skip internal/infrastructure lines with no real user context
        if username == "unknown" and "telemt::transport" in line:
            continue

        # Ensure structure
        if username not in stats:
            stats[username] = {"buckets": {}}
        if bucket not in stats[username]["buckets"]:
            stats[username]["buckets"][bucket] = {
                "conn": 0,
                "errors": 0,
                "warnings": 0,
                "error_types": {},
                "peer_ips": {},
            }

        b = stats[username]["buckets"][bucket]

        # Track unique peer IPs (and port if present, e.g. "1.2.3.4:54321")
        m_peer = RE_PEER.search(line)
        if m_peer:
            peer = m_peer.group(1)
            if "peer_ips" not in b:
                b["peer_ips"] = {}
            b["peer_ips"][peer] = b["peer_ips"].get(peer, 0) + 1

        if "MTProto handshake successful" in line:
            b["conn"] += 1

        elif "ERROR" in line:
            m_err = RE_ERROR.search(line)
            if m_err:
                b["errors"] += 1
                err_raw = m_err.group(1).strip()
                # Normalise: strip IPs and ports for grouping
                err_norm = re.sub(r"\d+\.\d+\.\d+\.\d+:\d+", "X", err_raw)
                err_norm = re.sub(r"\d+\.\d+\.\d+\.\d+", "X", err_norm)
                b["error_types"][err_norm] = b["error_types"].get(err_norm, 0) + 1

        elif "WARN" in line:
            m_err = RE_ERROR.search(line)
            if m_err:
                b["warnings"] = b.get("warnings", 0) + 1

    return stats, last_ts


def prune_old_buckets(stats: dict, keep_days: int = 35) -> dict:
    """Remove buckets older than keep_days to prevent unbounded growth."""
    now = datetime.now(timezone.utc)
    cutoff = now.timestamp() - keep_days * 86400

    for username in list(stats.keys()):
        buckets = stats[username].get("buckets", {})
        to_delete = []
        for bucket_key in buckets:
            try:
                dt = datetime.strptime(bucket_key, "%Y-%m-%dT%H").replace(tzinfo=timezone.utc)
                if dt.timestamp() < cutoff:
                    to_delete.append(bucket_key)
            except ValueError:
                to_delete.append(bucket_key)
        for k in to_delete:
            del buckets[k]
        if not buckets:
            del stats[username]

    return stats


def main():
    log.info("Log aggregation run started")

    cursor = load_cursor()
    log.info(f"Cursor: {cursor or 'beginning'}")

    lines = fetch_logs(since=cursor)
    log.info(f"Fetched {len(lines)} lines")

    if not lines:
        log.info("No new lines, exiting")
        return

    stats = load_stats()
    stats, last_ts = process_lines(lines, stats)

    save_stats(stats)

    if last_ts:
        save_cursor(last_ts)
        log.info(f"Cursor advanced to {last_ts}")

    total_conn = sum(
        b.get("conn", 0)
        for u in stats.values()
        for b in u.get("buckets", {}).values()
    )
    log.info(f"Stats saved. Total connections in DB: {total_conn:,}")


if __name__ == "__main__":
    main()
