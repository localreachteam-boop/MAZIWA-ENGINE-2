"""
LOG MANAGER — Disk-based log rotation with daily archives

Problem: logs capped at 200 entries in trading_state.json, old logs lost.
Solution: rotate logs to disk files daily, keep recent in state, archive old.

Features:
  - Daily rotation (sys_log_YYYY-MM-DD.json, agent_log_YYYY-MM-DD.json)
  - Configurable in-state limit (default 100 recent entries)
  - Auto-cleanup of logs older than 7 days
  - Dashboard can query archives
  - Thread-safe writes
"""
import json
import os
import time
import threading
from pathlib import Path
from datetime import datetime, timezone, timedelta

LOG_DIR = Path(__file__).parent.parent / 'logs'
STATE_FILE = Path(__file__).parent.parent / 'log_state.json'
KEEP_DAYS = 7        # archive logs for 7 days
IN_STATE_LIMIT = 100  # recent entries kept in trading_state.json

_lock = threading.Lock()


def _today_str():
    return datetime.now(timezone.utc).strftime('%Y-%m-%d')


def _log_path(log_type, date_str=None):
    date_str = date_str or _today_str()
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    return LOG_DIR / f'{log_type}_{date_str}.json'


def _load_archive(log_type, date_str):
    path = _log_path(log_type, date_str)
    if path.exists():
        try:
            return json.loads(path.read_text())
        except Exception:
            pass
    return []


def _save_archive(log_type, date_str, entries):
    path = _log_path(log_type, date_str)
    try:
        path.write_text(json.dumps(entries, indent=1, default=str))
    except Exception as e:
        print(f'[LOGMGR] save error: {e}')


class LogManager:
    """Manages log rotation between in-state and disk archives."""

    def __init__(self):
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        self._rotate_today()

    def _rotate_today(self):
        """Ensure today's archive file exists and state is trimmed."""
        with _lock:
            today = _today_str()
            for log_type in ['sys_log', 'agent_log']:
                archive = _load_archive(log_type, today)
                # Load any state entries from today and merge
                state = self._read_state()
                state_entries = state.get(log_type, [])
                today_entries = [e for e in state_entries
                                if self._entry_date(e) == today]
                if today_entries:
                    # Merge: archive + new from state (dedup by time)
                    existing_times = {e.get('time') for e in archive}
                    new_entries = [e for e in today_entries
                                  if e.get('time') not in existing_times]
                    archive.extend(new_entries)
                    archive.sort(key=lambda x: x.get('time', 0))
                    _save_archive(log_type, today, archive)

    def _read_state(self):
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            return {}

    def _write_state(self, state):
        try:
            STATE_FILE.write_text(json.dumps(state, indent=1, default=str))
        except Exception as e:
            print(f'[LOGMGR] state write error: {e}')

    def _entry_date(self, entry):
        t = entry.get('time', 0)
        if t > 1e12:
            t /= 1000
        return datetime.fromtimestamp(t, tz=timezone.utc).strftime('%Y-%m-%d') if t else _today_str()

    def _entry_hour(self, entry):
        t = entry.get('time', 0)
        if t > 1e12:
            t /= 1000
        return datetime.fromtimestamp(t, tz=timezone.utc).strftime('%H:%M:%S') if t else '??:??'

    def add_log(self, log_type, entry):
        """Add a log entry. Rotates to disk when state exceeds limit."""
        with _lock:
            state = self._read_state()
            entries = state.get(log_type, [])
            entries.append(entry)
            state[log_type] = entries

            # If over limit, rotate oldest to disk
            if len(entries) > IN_STATE_LIMIT:
                # Sort by time
                entries.sort(key=lambda x: x.get('time', 0))
                # Keep newest IN_STATE_LIMIT in state
                to_archive = entries[:-IN_STATE_LIMIT]
                entries = entries[-IN_STATE_LIMIT:]
                state[log_type] = entries

                # Write old entries to their daily archive files
                day_buckets = {}
                for e in to_archive:
                    day = self._entry_date(e)
                    day_buckets.setdefault(day, []).append(e)

                for day, bucket in day_buckets.items():
                    existing = _load_archive(log_type, day)
                    existing_times = {e.get('time') for e in existing}
                    new_entries = [e for e in bucket if e.get('time') not in existing_times]
                    existing.extend(new_entries)
                    existing.sort(key=lambda x: x.get('time', 0))
                    _save_archive(log_type, day, existing)

            self._write_state(state)

    def get_recent(self, log_type, count=50):
        """Get recent entries from state."""
        state = self._read_state()
        entries = state.get(log_type, [])
        return entries[-count:]

    def get_archive(self, log_type, date_str):
        """Get archived entries for a specific date."""
        return _load_archive(log_type, date_str)

    def get_archive_range(self, log_type, start_date, end_date):
        """Get entries across a date range."""
        entries = []
        current = datetime.strptime(start_date, '%Y-%m-%d')
        end = datetime.strptime(end_date, '%Y-%m-%d')
        while current <= end:
            day = current.strftime('%Y-%m-%d')
            entries.extend(_load_archive(log_type, day))
            current += timedelta(days=1)
        entries.sort(key=lambda x: x.get('time', 0))
        return entries

    def get_all_logs_formatted(self, log_type, max_entries=100):
        """Get all logs (recent + archive) formatted for dashboard display."""
        from datetime import datetime, timezone
        # Recent from state
        recent = self.get_recent(log_type, max_entries)
        # Today's archive (might have more than what's in state)
        today = _today_str()
        today_archive = _load_archive(log_type, today)
        # Merge recent with today's archive
        all_times = {e.get('time') for e in recent}
        extra = [e for e in today_archive if e.get('time') not in all_times]
        combined = recent + extra
        combined.sort(key=lambda x: x.get('time', 0))
        return combined[-max_entries:]

    def add_trade(self, entry):
        """Add a trade entry. Rotates old trades to disk."""
        with _lock:
            state = self._read_state()
            entries = state.get('trade_log_full', [])
            entries.append(entry)
            state['trade_log_full'] = entries

            if len(entries) > IN_STATE_LIMIT:
                entries.sort(key=lambda x: x.get('time', 0))
                to_archive = entries[:-IN_STATE_LIMIT]
                entries = entries[-IN_STATE_LIMIT:]
                state['trade_log_full'] = entries

                day_buckets = {}
                for e in to_archive:
                    day = self._entry_date(e)
                    day_buckets.setdefault(day, []).append(e)

                for day, bucket in day_buckets.items():
                    existing = _load_archive('trades', day)
                    existing_times = {(e.get('market'), e.get('time')) for e in existing}
                    new_entries = [e for e in bucket if (e.get('market'), e.get('time')) not in existing_times]
                    existing.extend(new_entries)
                    existing.sort(key=lambda x: x.get('time', 0))
                    _save_archive('trades', day, existing)

            self._write_state(state)

    def get_trades_formatted(self, count=100):
        """Get recent trades from state + today's archive."""
        state = self._read_state()
        recent = state.get('trade_log_full', [])[-count:]
        today = _today_str()
        today_archive = _load_archive('trades', today)
        all_times = {(e.get('market'), e.get('time')) for e in recent}
        extra = [e for e in today_archive if (e.get('market'), e.get('time')) not in all_times]
        combined = recent + extra
        combined.sort(key=lambda x: x.get('time', 0))
        return combined[-count:]

    def cleanup_old(self, days=KEEP_DAYS):
        """Remove archive files older than KEEP_DAYS."""
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        removed = 0
        for f in LOG_DIR.glob('*.json'):
            try:
                date_str = f.stem.split('_')[-1]  # e.g., sys_log_2026-07-19 -> 2026-07-19
                file_date = datetime.strptime(date_str, '%Y-%m-%d')
                if file_date.replace(tzinfo=timezone.utc) < cutoff:
                    f.unlink()
                    removed += 1
            except Exception:
                pass
        return removed

    def get_status(self):
        """Dashboard status."""
        state = self._read_state()
        today = _today_str()
        
        # Count entries in state
        sys_in_state = len(state.get('sys_log', []))
        agent_in_state = len(state.get('agent_log', []))
        
        # Count entries in today's archive
        sys_archive = len(_load_archive('sys_log', today))
        agent_archive = len(_load_archive('agent_log', today))
        
        # Count archive files
        archive_files = list(LOG_DIR.glob('*.json'))
        
        # Total size
        total_size = sum(f.stat().st_size for f in archive_files)
        
        return {
            'sys_log_in_state': sys_in_state,
            'sys_log_archive': sys_archive,
            'agent_log_in_state': agent_in_state,
            'agent_log_archive': agent_archive,
            'archive_files': len(archive_files),
            'total_size_kb': round(total_size / 1024, 1),
            'log_dir': str(LOG_DIR),
            'keep_days': KEEP_DAYS,
            'in_state_limit': IN_STATE_LIMIT,
        }


# Global instance
log_manager = LogManager()
