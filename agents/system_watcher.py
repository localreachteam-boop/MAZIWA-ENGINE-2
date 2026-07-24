"""
SYSTEM WATCHER — Domain-based architecture to prevent brain_active.py sprawl.

Groups related agents into logical domains. Each domain owns its tools
and exposes clean query/mutation methods. The brain calls watcher.trade.score()
instead of tools.bunch_runner.get_status() scattered 262 times across 6800 lines.

Domains:
  trade   — bunch_runner, profit_mirror, replicator, mission_tracker
  intel   — trade_intel, state_brain, anomaly, tz_intel, eat
  ai      — alm_brain, openrouter, self_diagnostic, improver
  risk    — risk, protection, profit_guard, ymcrc, bleeding
  memory  — log_manager, memory, self_improver
  growth  — growth, goal_manager, mission
"""

import time
import json


class TradeDomain:
    """Bunch runs, profit mirror, replicator, mission tracking."""

    def __init__(self, bunch_runner=None, profit_mirror=None,
                 replicator=None, mission_tracker=None):
        self.bunch = bunch_runner
        self.mirror = profit_mirror
        self.replicator = replicator
        self.mission = mission_tracker

    def get_status(self):
        return {
            "bunch": self.bunch.get_status() if self.bunch else {},
            "mirror": self.mirror.get_status() if self.mirror else {},
            "replicator": self.replicator.get_status() if self.replicator else {},
            "mission": self.mission.get_status() if self.mission else {},
        }

    def get_bunch_daily(self):
        """Quick bunch stats for state writes."""
        if not self.bunch:
            return {}
        return self.bunch.get_status()

    def is_bunch_active(self):
        if self.bunch and hasattr(self.bunch, "current_run"):
            return self.bunch.current_run is not None
        return False

    def get_mission_mode(self):
        if self.mission:
            return self.mission.get_status().get("mode", "UNKNOWN")
        return "UNKNOWN"


class IntelDomain:
    """Trade intelligence, market state, anomalies, timezone, sessions."""

    def __init__(self, trade_intel=None, state_brain=None,
                 anomaly=None, tz_intel=None, eat=None):
        self.trade_intel = trade_intel
        self.state_brain = state_brain
        self.anomaly = anomaly
        self.tz_intel = tz_intel
        self.eat = eat

    def get_status(self):
        return {
            "trade_intel": self.trade_intel.get_status() if self.trade_intel else {},
            "market_state": self.state_brain.get_status() if self.state_brain else {},
            "anomalies": self.anomaly.get_status() if self.anomaly else {},
            "timezone": self.tz_intel.get_status() if self.tz_intel else {},
            "eat": self.eat.get_status() if self.eat else {},
        }

    def is_bleeding(self):
        if self.tz_intel:
            return self.tz_intel.is_bleeding()
        return False, "", "safe"

    def get_session_quality(self):
        if self.eat:
            return self.eat.get_time_quality()
        return 0, "no session data"

    def score_candidates(self, candidates):
        """Delegate to trade_intel for candidate scoring."""
        if self.trade_intel:
            return self.trade_intel.score_candidates(candidates)
        return candidates


class AIDomain:
    """AI engines, ALM brain, diagnostics, self-improvement."""

    def __init__(self, alm_brain=None, openrouter=None,
                 self_diagnostic=None, improver=None):
        self.alm = alm_brain
        self.openrouter = openrouter
        self.diagnostic = self_diagnostic
        self.improver = improver

    def get_status(self):
        return {
            "alm_brain": self.alm.get_status() if self.alm else {},
            "openrouter": self.openrouter.get_status() if self.openrouter else {},
            "diagnostic": self.diagnostic.get_status() if self.diagnostic else {},
            "self_improver": self.improver.get_status() if self.improver else {},
        }

    def get_model(self):
        if self.openrouter and getattr(self.openrouter, "active_engine", None) == getattr(self.openrouter, "ENGINE_OPENROUTER", None):
            return self.openrouter.openrouter_model
        return getattr(self.openrouter, "ollama_model", "none")

    def get_evolution_log(self):
        if self.alm:
            return self.alm.get_status().get("evolution_log", [])
        return []

    def get_health_score(self):
        if self.diagnostic:
            return self.diagnostic.get_status().get("health_score", 0)
        return 0


class RiskDomain:
    """Risk management, protection, bleeding, YMCRC, PL manager."""

    def __init__(self, risk=None, profit_guard=None, ymcrc=None, pl_manager=None):
        self.risk = risk
        self.profit_guard = profit_guard
        self.ymcrc = ymcrc
        self.pl = pl_manager

    def get_status(self):
        return {
            "protection": {
                "daily_loss_pct": round(abs(min(0, self.risk.pnl)) / self.risk.start * 100, 2) if self.risk and self.risk.start else 0,
                "daily_loss_limit_pct": 2.0,
                "peak_balance": round(max(self.risk.start, self.risk.balance), 2) if self.risk else 0,
                "drawdown_from_peak": round((1 - self.risk.balance / max(self.risk.start, self.risk.balance)) * 100, 2) if self.risk and self.risk.balance > 0 else 0,
                "frozen": self.risk.consec_loss >= 4 if self.risk else False,
                "freeze_reason": self.risk.mode_reason if self.risk else "",
            } if self.risk else {},
            "profit_guard": self.profit_guard.get_status() if self.profit_guard else {},
            "ymcrc": self.ymcrc.get_status() if self.ymcrc else {},
            "pl_manager": self.pl.get_status() if self.pl else {},
        }

    def is_frozen(self):
        if self.risk:
            return self.risk.consec_loss >= 4
        return False


class MemoryDomain:
    """Logs, memory, trade history."""

    def __init__(self, log_manager=None, memory=None):
        self.log_manager = log_manager
        self.memory = memory

    def get_sys_log(self, count=100):
        if self.log_manager:
            return self.log_manager.get_recent("sys_log", count)
        return []

    def get_agent_log(self, count=100):
        if self.log_manager:
            return self.log_manager.get_recent("agent_log", count)
        return []

    def get_trades(self, count=200):
        if self.log_manager:
            return self.log_manager.get_trades_formatted(count)
        return []

    def get_strategy_count(self):
        if self.memory and hasattr(self.memory, "strategies"):
            return len(self.memory.strategies)
        return 0


class GrowthDomain:
    """Growth engine, goal management, mission."""

    def __init__(self, growth=None, goal_manager=None, mission=None):
        self.growth = growth
        self.goal = goal_manager
        self.mission = mission

    def get_status(self):
        return {
            "growth": self.growth.get_growth_status() if self.growth else {},
            "goal": self.goal.get_status() if self.goal else {},
            "mission": self.mission.get_status() if self.mission else {},
        }


class SystemWatcher:
    """
    Central coordinator — one object to own all subsystems.

    Usage:
        watcher = SystemWatcher(tools=tools)
        watcher.refresh()
        state = watcher.build_state(risk, cycle, best_market, best_data)
    """

    def __init__(self, tools=None):
        self.trade = TradeDomain(
            bunch_runner=getattr(tools, "bunch_runner", None),
            profit_mirror=getattr(tools, "profit_mirror", None),
            replicator=getattr(tools, "replicator", None),
            mission_tracker=getattr(tools, "mission_tracker", None),
        )
        self.intel = IntelDomain(
            trade_intel=getattr(tools, "trade_intel", None),
            state_brain=getattr(tools, "state_brain", None),
            anomaly=getattr(tools, "anomaly", None),
            tz_intel=getattr(tools, "tz_intel", None),
            eat=getattr(tools, "eat", None),
        )
        self.ai = AIDomain(
            alm_brain=getattr(tools, "alm", None),
            openrouter=getattr(tools, "openrouter", None),
            self_diagnostic=getattr(tools, "diagnostic", None),
            improver=getattr(tools, "improver", None),
        )
        self.risk = RiskDomain(
            risk=getattr(tools, "risk", None),
            profit_guard=getattr(tools, "profit_guard", None),
            ymcrc=getattr(tools, "ymcrc", None),
            pl_manager=getattr(tools, "pl", None),
        )
        self.mem = MemoryDomain(
            log_manager=getattr(tools, "log_manager", None),
            memory=getattr(tools, "memory", None),
        )
        self.growth = GrowthDomain(
            growth=getattr(tools, "growth", None),
            goal_manager=getattr(tools, "goal", None),
            mission=getattr(tools, "mission", None),
        )

        self.session_mgr = getattr(tools, "session_mgr", None)
        self.backtester = getattr(tools, "backtester", None)
        self.prob_engine = getattr(tools, "prob_engine", None)
        self.round_profit = getattr(tools, "round_profit", None)
        self.research = getattr(tools, "research", None)
        self.price_action = getattr(tools, "price_action", None)
        self.memory = getattr(tools, "memory", None)
        self._last_refresh = 0.0

    def refresh(self):
        """Re-read fresh status from all domains. Call once per cycle."""
        self._last_refresh = time.time()


    def _get_fresh_mission(self):
        """Get mission status with fresh self_test from disk."""
        try:
            status = self.growth.mission.get_status() if self.growth.mission else {}
            # Read self_test directly from mission_state.json (Mission class may be stale)
            from pathlib import Path
            mf = Path(__file__).parent.parent / 'mission_state.json'
            if mf.exists():
                _mf = json.loads(mf.read_text())
                _st = _mf.get('self_test', {})
                if _st:
                    status['self_test_runs'] = _st.get('runs', 0)
                    status['self_test_score'] = _st.get('last_score', None)
                    status['self_test_recommendations'] = (_st.get('last_recommendation') or [])[:5]
        except Exception:
            status = self.growth.mission.get_status() if self.growth.mission else {}
        return status
    def build_state(self, risk, cycle, best_market=None, best_data=None,
                    session_start=None):
        """Build the complete trading_state.json dict."""
        bal = risk.balance
        model = self.ai.get_model()

        state = {
            "type": "state",
            "balance": bal,
            "real_balance": bal,
            "paper_balance": bal,
            "startBalance": risk.start,
            "trades": risk.total,
            "wins": risk.wins,
            "losses": risk.losses,
            "win_rate": round(risk.wr(), 1),
            "total_pnl": round(risk.pnl, 4),
            "daily_loss": round(abs(min(0, risk.pnl)) / risk.start * 100, 2) if risk.start else 0,
            "cycles": cycle,
            "total_trades": risk.total,
            "active_agent": getattr(self.ai.openrouter, "active_engine", "none"),
            "model_used": model,
            "bestStreak": risk.consec_win,
            "selected_market": best_market or "SCANNING",
            "selected_type": best_data.get("contract", "") if best_data else "",
            "selected_strategy": best_data.get("strategy", "") if best_data else "",
            "selected_ev": round(best_data.get("ev", 0), 4) if best_data else 0,
            "edge": round(best_data.get("ev", 0), 4) if best_data else 0,
            "regime": best_data.get("regime", "UNKNOWN") if best_data else "UNKNOWN",
            "regime_confidence": best_data.get("confidence", 0) if best_data else 0,
            "accuracy": round(risk.wr(), 1),
            "mode": risk.trading_mode,
            "mode_reason": risk.mode_reason,
            "trading_mode": risk.trading_mode,
            "confidence_score": risk.confidence_score,
            "alignment_score": risk.alignment_score,
            # Domain data
            "trade_intel": self.intel.trade_intel.get_status() if self.intel.trade_intel else {},
            "market_state": self.intel.state_brain.get_status() if self.intel.state_brain else {},
            "anomalies": self.intel.anomaly.get_status() if self.intel.anomaly else {},
            "timezone": self.intel.tz_intel.get_status() if self.intel.tz_intel else {},
            "eat": self.intel.eat.get_status() if self.intel.eat else {},
            "bunch_runner": self.trade.get_bunch_daily(),
            "profit_mirror": self.trade.mirror.get_status() if self.trade.mirror else {},
            "replicator": self.trade.replicator.get_status() if self.trade.replicator else {},
            "mission_tracker": self.trade.mission.get_status() if self.trade.mission else {},
            "mission": self._get_fresh_mission(),
            "growth": self.growth.growth.get_growth_status() if self.growth.growth else {},
            "goal_manager": self.growth.goal.get_status() if self.growth.goal else {},
            "self_improver": self.ai.improver.get_status() if self.ai.improver else {},
            "alm_brain": self.ai.alm.get_status() if self.ai.alm else {"connected": False},
            "openrouter": self.ai.openrouter.get_status() if self.ai.openrouter else {"connected": False},
            "diagnostic": self.ai.diagnostic.get_status() if self.ai.diagnostic else {},
            "ymcrc": self.risk.ymcrc.get_status() if self.risk.ymcrc else {},
            "pl_manager": self.risk.pl.get_status() if self.risk.pl else {},
            "bleeding": {
                "is_bleeding": self.intel.is_bleeding()[0],
                "reason": self.intel.is_bleeding()[1],
                "severity": self.intel.is_bleeding()[2],
            },
            "protection": self.risk.get_status().get("protection", {}),
            # Logs
            "sys_log": self.mem.get_sys_log(100),
            "agent_notes": self.mem.get_agent_log(100),
            "trade_log_full": self.mem.get_trades(200),
            "cpp_engine": {"connected": False, "enabled": False},
            "simulation": {
                "paper_trades": risk.total, "sim_wins": risk.wins,
                "sim_losses": risk.losses, "sim_win_rate": round(risk.wr(), 1),
                "sim_pnl": round(risk.pnl, 4), "sim_tests": risk.total,
                "sim_strategies": 0,
            },
            "backtester": self.backtester.get_status() if self.backtester else {},
            "prob_engine": self.prob_engine.get_status() if self.prob_engine else {},
            "round_profit": self.round_profit.get_status() if self.round_profit else {},
            "profit_guard": self.risk.profit_guard.get_status() if self.risk.profit_guard else {},
            "research": self.research.get_status() if self.research else {},
            "judge": {"last_decision": "TRADE", "recent_trades": risk.total, "recent_wins": risk.wins, "total_decisions": risk.total},
            "brain_status": {"mode": "ACTIVE", "version": "v3", "consecutive_losses": risk.consec_loss, "consecutive_wins": risk.consec_win},
            "picker": {"last_pick": {"name": best_data["strategy"] if best_data else "NONE"}, "catalog": []},
            "session": {
                "mode": risk.trading_mode,
                "session_pnl_pct": round(risk.pnl / risk.start * 100, 2) if risk.start else 0,
                "win_streak": risk.consec_win,
                "alignment_score": risk.alignment_score,
                "session_mgr": self.session_mgr.get_status() if self.session_mgr else {},
            },
            "executor": {},
            "memory": {
                "total_trades": risk.total,
                "markets_traded": 0,
                "total_strategies": len(self.memory.strategies) if self.memory and hasattr(self.memory, 'strategies') else 0,
                "active_strategies": len([k for k,v in self.memory.strategies.items() if isinstance(v, dict) and v.get('trades', 0) > 0]) if self.memory and hasattr(self.memory, 'strategies') else 0,
                "retired_strategies": len([k for k,v in self.memory.strategies.items() if isinstance(v, dict) and v.get('status') == 'retired']) if self.memory and hasattr(self.memory, 'strategies') else 0,
            },
            "recommendation": {
                "regime": best_data.get("regime", "UNKNOWN") if best_data else "UNKNOWN",
                "strategy": best_data["strategy"] if best_data else "NONE",
            },
            "uptime": int(time.time() - session_start) if session_start else 0,
            "phone_resources": self._get_resources(),
            "session_start": int((session_start or time.time()) * 1000),
            "time": int(time.time() * 1000),
        }

        return state

    def _get_resources(self):
        """Collect live system resources."""
        import os
        res = {'battery': 0, 'is_charging': False, 'temperature': 0.0,
               'ram_pct': 0, 'ram_available_mb': 0, 'ram_used': 0,
               'swap_pct': 0, 'disk_pct': 0, 'disk_free': '0GB', 'cpu_load': '0'}
        try:
            with open('/proc/meminfo') as f:
                mi = {}
                for line in f:
                    p = line.split()
                    if len(p) >= 2:
                        mi[p[0].rstrip(':')] = int(p[1])
            total = mi.get('MemTotal', 1)
            avail = mi.get('MemAvailable', 0)
            used = total - avail
            res['ram_pct'] = round(used / total * 100) if total else 0
            res['ram_available_mb'] = avail // 1024
            res['ram_used'] = used // 1024
            st = mi.get('SwapTotal', 0)
            sf = mi.get('SwapFree', 0)
            res['swap_pct'] = round((st - sf) / st * 100) if st else 0
        except: pass
        try:
            import shutil
            td, ud, fd = shutil.disk_usage('/')
            res['disk_pct'] = round(ud / td * 100)
            res['disk_free'] = f'{fd // (1024**3)}GB'
        except: pass
        try:
            with open('/proc/loadavg') as f:
                res['cpu_load'] = f.read().strip().split()[0]
        except (PermissionError, FileNotFoundError):
            try:
                import os
                res['cpu_load'] = str(os.getloadavg()[0])
            except: pass
        return res

    def summary(self):
        """One-line health summary for logging."""
        bstats = self.trade.get_bunch_daily()
        return (
            f"trade={bstats.get('completed_today',0)} bunches "
            f"ai={self.ai.get_health_score()}/100 "
            f"model={self.ai.get_model()[:20]}"
        )
