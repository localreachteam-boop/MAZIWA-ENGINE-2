"""
PERFORMANCE TRACKER — Measurable goals, rollback on regression.

Targets:
- Win rate target
- Risk limit
- Maximum drawdown
- Profit factor
- Execution speed
- Quality opportunities found

Rules:
- Do not increase risk to hide losses
- Explain every strategy change
- Roll back changes that reduce performance
"""
import json
import time
from pathlib import Path
from datetime import datetime, timezone

PERF_FILE = Path(__file__).parent.parent / 'perf_tracker.json'


class PerformanceTracker:
    """
    Tracks measurable performance targets.
    Rolls back changes that reduce performance.
    """

    def __init__(self):
        self.state = self._load()

    def _load(self):
        if PERF_FILE.exists():
            try:
                return json.loads(PERF_FILE.read_text())
            except Exception:
                pass
        return self._default()

    def _default(self):
        return {
            "version": 1,
            "targets": {
                "win_rate_min": 55.0,
                "win_rate_target": 60.0,
                "max_drawdown": 10.0,
                "max_daily_loss": 5.0,
                "min_profit_factor": 1.2,
                "max_trades_per_day": 20,
                "min_ev_per_trade": 0.01,
                "target_daily_pnl": 2.00,
                "target_monthly_pnl": 60.00,
            },
            "current": {
                "trades_today": 0,
                "wins_today": 0,
                "pnl_today": 0.0,
                "peak_balance": 0.0,
                "current_balance": 0.0,
                "max_drawdown_today": 0.0,
                "quality_opps_found": 0,
                "quality_opps_taken": 0,
                "avg_execution_ms": 0,
            },
            "history": [],
            "rollbacks": [],
            "improvements": [],
        }

    def _save(self):
        try:
            PERF_FILE.write_text(json.dumps(self.state, indent=2, default=str))
        except Exception:
            pass

    def record_trade(self, profit, balance, execution_ms=None):
        """Record a trade and update metrics."""
        c = self.state["current"]
        t = self.state["targets"]

        c["trades_today"] += 1
        if profit > 0:
            c["wins_today"] += 1
        c["pnl_today"] = round(c["pnl_today"] + profit, 4)
        c["current_balance"] = balance

        if c["peak_balance"] == 0:
            c["peak_balance"] = balance
        if balance > c["peak_balance"]:
            c["peak_balance"] = balance

        drawdown = c["peak_balance"] - balance
        if drawdown > c["max_drawdown_today"]:
            c["max_drawdown_today"] = round(drawdown, 4)

        if execution_ms is not None:
            prev_avg = c.get("avg_execution_ms", 0)
            count = c["trades_today"]
            c["avg_execution_ms"] = round((prev_avg * (count - 1) + execution_ms) / count, 0)

        self._save()

    def record_opportunity(self, found, taken=False):
        """Record quality opportunities found vs taken."""
        c = self.state["current"]
        if found:
            c["quality_opps_found"] = c.get("quality_opps_found", 0) + 1
        if taken:
            c["quality_opps_taken"] = c.get("quality_opps_taken", 0) + 1
        self._save()

    def check_targets(self):
        """
        Check all targets against current performance.
        Returns: {all_met: bool, violations: [...], passed: [...]}
        """
        c = self.state["current"]
        t = self.state["targets"]

        violations = []
        passed = []

        # Win rate
        if c["trades_today"] >= 5:
            wr = c["wins_today"] / c["trades_today"] * 100
            if wr < t["win_rate_min"]:
                violations.append({
                    "target": "WIN_RATE",
                    "current": f"{wr:.1f}%",
                    "minimum": f"{t['win_rate_min']}%",
                    "severity": "HIGH" if wr < t["win_rate_min"] - 10 else "MEDIUM",
                })
            else:
                passed.append({"target": "WIN_RATE", "value": f"{wr:.1f}%"})

        # Daily loss
        if c["pnl_today"] < -t["max_daily_loss"]:
            violations.append({
                "target": "DAILY_LOSS",
                "current": f"${c['pnl_today']:.2f}",
                "limit": f"-${t['max_daily_loss']:.2f}",
                "severity": "CRITICAL",
            })
        else:
            passed.append({"target": "DAILY_LOSS", "value": f"${c['pnl_today']:.2f}"})

        # Max drawdown
        if c["max_drawdown_today"] > t["max_drawdown"]:
            violations.append({
                "target": "MAX_DRAWDOWN",
                "current": f"${c['max_drawdown_today']:.2f}",
                "limit": f"${t['max_drawdown']:.2f}",
                "severity": "HIGH",
            })
        else:
            passed.append({"target": "DRAWDOWN", "value": f"${c['max_drawdown_today']:.2f}"})

        # Trade count
        if c["trades_today"] > t["max_trades_per_day"]:
            violations.append({
                "target": "TRADE_COUNT",
                "current": c["trades_today"],
                "limit": t["max_trades_per_day"],
                "severity": "MEDIUM",
            })
        else:
            passed.append({"target": "TRADE_COUNT", "value": c["trades_today"]})

        # Profit factor (if enough data)
        if c["trades_today"] >= 10:
            gross_profit = c["wins_today"] * 0.95  # avg win
            gross_loss = (c["trades_today"] - c["wins_today"]) * 1.0  # avg loss
            pf = gross_profit / max(gross_loss, 0.01)
            if pf < t["min_profit_factor"]:
                violations.append({
                    "target": "PROFIT_FACTOR",
                    "current": f"{pf:.2f}",
                    "minimum": f"{t['min_profit_factor']}",
                    "severity": "HIGH",
                })
            else:
                passed.append({"target": "PROFIT_FACTOR", "value": f"{pf:.2f}"})

        all_met = len(violations) == 0

        return {
            "all_met": all_met,
            "violations": violations,
            "passed": passed,
            "summary": f"{len(passed)}/{len(passed)+len(violations)} targets met",
        }

    def should_rollback(self, change_time, change_description):
        """
        Check if a recent change reduced performance.
        Returns: (should_rollback, reason)
        """
        c = self.state["current"]
        t = self.state["targets"]

        # If we're violating targets after a change, rollback
        targets = self.check_targets()
        critical = [v for v in targets["violations"] if v["severity"] == "CRITICAL"]
        high = [v for v in targets["violations"] if v["severity"] == "HIGH"]

        if critical:
            self.state.setdefault("rollbacks", []).append({
                "change": change_description,
                "reason": f"CRITICAL violation after change: {critical[0]['target']}",
                "time": int(time.time() * 1000),
            })
            self._save()
            return True, f"CRITICAL: {critical[0]['target']} violated"

        if len(high) >= 2:
            self.state.setdefault("rollbacks", []).append({
                "change": change_description,
                "reason": f"Multiple HIGH violations after change",
                "time": int(time.time() * 1000),
            })
            self._save()
            return True, "Multiple HIGH violations"

        return False, "performance acceptable"

    def end_of_day_review(self):
        """
        End of day summary with targets assessment.
        Returns: full performance report.
        """
        c = self.state["current"]
        t = self.state["targets"]

        wr = c["wins_today"] / c["trades_today"] * 100 if c["trades_today"] > 0 else 0
        targets = self.check_targets()

        report = {
            "date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            "trades": c["trades_today"],
            "wins": c["wins_today"],
            "losses": c["trades_today"] - c["wins_today"],
            "win_rate": round(wr, 1),
            "pnl": round(c["pnl_today"], 4),
            "max_drawdown": round(c["max_drawdown_today"], 4),
            "avg_execution_ms": c.get("avg_execution_ms", 0),
            "quality_found": c.get("quality_opps_found", 0),
            "quality_taken": c.get("quality_opps_taken", 0),
            "targets_met": targets["summary"],
            "violations": targets["violations"],
            "rollbacks_today": len(self.state.get("rollbacks", [])),
        }

        # Store in history
        self.state.setdefault("history", []).append(report)
        if len(self.state["history"]) > 30:
            self.state["history"] = self.state["history"][-30:]

        # Reset daily counters
        self.state["current"] = {
            "trades_today": 0,
            "wins_today": 0,
            "pnl_today": 0.0,
            "peak_balance": c["current_balance"],
            "current_balance": c["current_balance"],
            "max_drawdown_today": 0.0,
            "quality_opps_found": 0,
            "quality_opps_taken": 0,
            "avg_execution_ms": 0,
        }
        self._save()
        return report

    def get_trend(self, days=5):
        """Get performance trend over last N days."""
        history = self.state.get("history", [])[-days:]
        if not history:
            return {"trend": "NO_DATA"}

        avg_pnl = sum(h.get("pnl", 0) for h in history) / len(history)
        avg_wr = sum(h.get("win_rate", 0) for h in history) / len(history)
        total_pnl = sum(h.get("pnl", 0) for h in history)

        if total_pnl > 0 and avg_wr > 55:
            trend = "PROFITABLE"
        elif total_pnl > 0:
            trend = "MARGINAL"
        elif avg_wr > 50:
            trend = "BREAKEVEN"
        else:
            trend = "LOSING"

        return {
            "trend": trend,
            "avg_daily_pnl": round(avg_pnl, 4),
            "avg_win_rate": round(avg_wr, 1),
            "total_pnl": round(total_pnl, 4),
            "days": len(history),
        }

    def get_status(self):
        c = self.state["current"]
        targets = self.check_targets()
        return {
            "trades_today": c["trades_today"],
            "pnl_today": round(c["pnl_today"], 4),
            "win_rate": round(c["wins_today"] / c["trades_today"] * 100, 1) if c["trades_today"] > 0 else 0,
            "max_drawdown": round(c["max_drawdown_today"], 4),
            "targets": targets,
            "trend": self.get_trend(),
            "rollbacks": len(self.state.get("rollbacks", [])),
        }
