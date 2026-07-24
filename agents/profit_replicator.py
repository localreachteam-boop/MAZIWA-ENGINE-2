"""
PROFIT REPLICATOR - Track winning patterns, detect when they break, auto-block
"""
import json
import time
from pathlib import Path
from datetime import datetime, timezone

REPLICATOR_FILE = Path(__file__).parent.parent / 'profit_replicator.json'

def now_utc():
    return datetime.now(timezone.utc)

class ProfitReplicator:
    def __init__(self):
        self.state = self._load()
        self._ensure_today()

    def _load(self):
        if REPLICATOR_FILE.exists():
            try:
                return json.loads(REPLICATOR_FILE.read_text())
            except Exception:
                pass
        return self._default_state()

    def _default_state(self):
        return {
            "version": 2,
            "today": now_utc().strftime("%Y-%m-%d"),
            "combos": {},
            "blocks": {},
            "profitable_patterns": [],
            "dead_patterns": [],
        }

    def _ensure_today(self):
        today = now_utc().strftime("%Y-%m-%d")
        if self.state.get("today") != today:
            old = self.state.get("combos", {})
            self.state["daily_archive"] = self.state.get("daily_archive", [])
            if old:
                self.state["daily_archive"].append({
                    "date": self.state.get("today", "?"),
                    "combos": {k: v for k, v in old.items() if v.get("trades", 0) >= 2}
                })
            if len(self.state["daily_archive"]) > 7:
                self.state["daily_archive"] = self.state["daily_archive"][-7:]
            self.state["today"] = today
            self.state["combos"] = {}
            self._save()

    def _save(self):
        try:
            REPLICATOR_FILE.write_text(json.dumps(self.state, indent=2, default=str))
        except Exception:
            pass

    def _combo_key(self, market, hour, strategy="ALL"):
        return f"{market}|{hour}|{strategy}"

    def _block_key(self, market, hour):
        return f"{market}|{hour}"

    def record_trade(self, market, hour, strategy, pnl, won):
        key = self._combo_key(market, hour, strategy)
        hour_key = self._block_key(market, hour)

        if key not in self.state["combos"]:
            self.state["combos"][key] = {
                "trades": 0, "wins": 0, "losses": 0,
                "pnl": 0.0, "peak_pnl": 0.0,
                "consec_losses": 0, "max_consec_losses": 0,
                "status": "tracking",
                "first_seen": time.time(),
                "last_trade": time.time(),
                "recent_5_pnl": [],
            }

        c = self.state["combos"][key]
        c["trades"] += 1
        c["pnl"] = round(c["pnl"] + pnl, 4)
        c["last_trade"] = time.time()

        if won:
            c["wins"] += 1
            c["consec_losses"] = 0
        else:
            c["losses"] += 1
            c["consec_losses"] += 1
            c["max_consec_losses"] = max(c["max_consec_losses"], c["consec_losses"])

        if c["pnl"] > c["peak_pnl"]:
            c["peak_pnl"] = c["pnl"]

        c["recent_5_pnl"].append(pnl)
        if len(c["recent_5_pnl"]) > 5:
            c["recent_5_pnl"] = c["recent_5_pnl"][-5:]

        self._detect_pattern_death(key, c, hour_key)
        self._detect_profitable_pattern(key, c)
        self._save()

    def _detect_pattern_death(self, key, combo, block_key):
        trades = combo.get("trades", 0)
        if trades < 3:
            return False

        peak = combo.get("peak_pnl", 0)
        current = combo.get("pnl", 0)
        consec = combo.get("consec_losses", 0)
        wr = (combo.get("wins", 0) / trades * 100) if trades > 0 else 0
        recent = combo.get("recent_5_pnl", [])

        reasons = []

        if peak > 0.5 and current < -0.5:
            reasons.append(f"reversed from ${peak:+.2f} to ${current:+.2f}")
        if peak > 0.5 and (peak - current) > 2.0:
            reasons.append(f"eroded ${peak-current:.2f} from peak")
        if consec >= 3:
            reasons.append(f"{consec} consecutive losses")
        if trades >= 5 and wr < 30:
            reasons.append(f"WR={wr:.0f}% over {trades} trades")
        if len(recent) >= 3 and all(p < 0 for p in recent[-3:]):
            reasons.append("3 recent losses in a row")

        if reasons:
            severity = self._calc_severity(combo, peak, current)
            duration = self._get_block_duration(severity)
            self._apply_block(block_key, reasons, severity, duration)
            combo["status"] = "blocked"

            self.state["dead_patterns"].append({
                "key": key, "time": time.time(),
                "reasons": reasons, "severity": severity,
                "peak_pnl": peak, "final_pnl": current,
                "trades": trades, "wr": round(wr, 1),
                "block_duration": duration,
            })
            if len(self.state["dead_patterns"]) > 50:
                self.state["dead_patterns"] = self.state["dead_patterns"][-50:]
            return True
        return False

    def _calc_severity(self, combo, peak, current):
        loss = peak - current
        consec = combo.get("consec_losses", 0)
        if consec >= 5 or loss > 5.0:
            return "critical"
        elif consec >= 3 or loss > 3.0:
            return "severe"
        elif consec >= 2 or loss > 1.5:
            return "moderate"
        return "mild"

    def _get_block_duration(self, severity):
        return {"critical": 14400, "severe": 7200, "moderate": 3600, "mild": 1800}.get(severity, 1800)

    def _apply_block(self, block_key, reasons, severity, duration):
        now = time.time()
        existing = self.state["blocks"].get(block_key, {})
        existing_until = existing.get("until", 0)
        new_until = now + duration
        if existing_until > now:
            new_until = max(new_until, existing_until + duration * 0.5)
        self.state["blocks"][block_key] = {
            "until": new_until, "severity": severity,
            "reasons": reasons, "applied": now, "duration": duration,
        }

    def _detect_profitable_pattern(self, key, combo):
        trades = combo.get("trades", 0)
        if trades < 5:
            return
        wr = (combo.get("wins", 0) / trades * 100) if trades > 0 else 0
        pnl = combo.get("pnl", 0)
        if wr >= 60 and pnl > 1.0:
            pattern = {
                "key": key, "wr": round(wr, 1), "pnl": round(pnl, 2),
                "trades": trades,
                "score": round(wr * 0.4 + min(pnl, 10) * 6, 1),
                "updated": time.time(),
            }
            existing = [p for p in self.state["profitable_patterns"] if p["key"] == key]
            if existing:
                existing[0].update(pattern)
            else:
                self.state["profitable_patterns"].append(pattern)
            self.state["profitable_patterns"].sort(key=lambda x: x.get("score", 0), reverse=True)
            self.state["profitable_patterns"] = self.state["profitable_patterns"][:20]

    def is_blocked(self, market, hour, strategy=None):
        # Check market+hour block
        block_key = self._block_key(market, hour)
        block = self.state["blocks"].get(block_key)
        if block:
            now = time.time()
            if now < block["until"]:
                return True, {
                    "severity": block["severity"],
                    "remaining_min": round((block["until"] - now) / 60, 1),
                    "reasons": block.get("reasons", []),
                }
            del self.state["blocks"][block_key]
            self._save()
        
        # Check strategy-specific block
        if strategy and strategy != "ALL":
            strat_key = self._combo_key(market, hour, strategy)
            strat_block = self.state["blocks"].get(strat_key)
            if strat_block:
                now = time.time()
                if now < strat_block["until"]:
                    return True, {
                        "severity": strat_block["severity"],
                        "remaining_min": round((strat_block["until"] - now) / 60, 1),
                        "reasons": strat_block.get("reasons", []),
                    }
                del self.state["blocks"][strat_key]
                self._save()
        
        return False, None

    def is_hour_blocked(self, hour):
        blocked = []
        for block_key, block in list(self.state["blocks"].items()):
            now = time.time()
            if now >= block["until"]:
                del self.state["blocks"][block_key]
                continue
            parts = block_key.split("|")
            if len(parts) >= 2 and parts[1] == str(hour):
                blocked.append({
                    "market": parts[0],
                    "severity": block["severity"],
                    "remaining_min": round((block["until"] - now) / 60, 1),
                })
        if blocked:
            self._save()
        return blocked

    def get_recommended_stake_mult(self, market, hour, strategy="ALL"):
        key = self._combo_key(market, hour, strategy)
        combo = self.state["combos"].get(key)
        
        # 1. ALWAYS check historical patterns FIRST — this is the proactive boost
        hist_mult, hist_reason = self._check_historical_pattern(market, hour, strategy)
        
        # 2. Check if this combo is actively dying today
        if combo:
            trades = combo.get("trades", 0)
            consec_losses = combo.get("consec_losses", 0)
            pnl = combo.get("pnl", 0)
            
            # Active death — override boost with reduction
            if consec_losses >= 2:
                return 0.3, f"dying_today_{consec_losses}L"
            if pnl < -1.0 and trades >= 2:
                return 0.5, f"bleeding_today_{pnl:.2f}"
            
            # Today has data — use it
            wr = (combo.get("wins", 0) / trades * 100) if trades > 0 else 0
            
            # Winning combo today — boost aggressively
            if wr >= 60 and pnl > 0.5:
                mult = round(min(1.5, 1.0 + pnl / 10), 2)
                # Stack with historical boost
                if hist_mult > 1.0:
                    mult = round(min(1.5, mult * hist_mult), 2)
                return mult, f"profitable_today_wr{int(wr)}"
            
            # Just won first trade — stack with historical
            if combo.get("wins", 0) >= 1 and trades <= 2 and hist_mult > 1.0:
                return round(min(2.0, hist_mult), 2), f"early_win_{hist_reason}"
        
        # 3. No data today or combo is neutral — PROACTIVELY boost from historical
        if hist_mult > 1.0:
            # Check if this combo is blocked before boosting
            if self.is_blocked(market, hour):
                return 0.7, "blocked_hist_override"
            return round(min(1.5, hist_mult), 2), f"proactive_{hist_reason}"
        
        # 4. No data anywhere
        if combo and combo.get("trades", 0) >= 3:
            trades = combo["trades"]
            wr = (combo.get("wins", 0) / trades * 100) if trades > 0 else 0
            pnl = combo.get("pnl", 0)
            if combo.get("consec_losses", 0) >= 2:
                return 0.5, "dying"
            if pnl < -1.0:
                return 0.7, "bleeding"
        return 1.0, "neutral"
    
    def _check_historical_pattern(self, market, hour, strategy="ALL"):
        """Check if this market has strong historical patterns (hour-specific OR overall).
        Returns (multiplier, reason) — multiplier > 1.0 means PROACTIVE boost.
        """
        best_mult = 1.0
        best_reason = "none"
        
        # 1. Exact hour + exact strategy match (strongest)
        key = self._combo_key(market, hour, strategy)
        for p in self.state.get("profitable_patterns", []):
            if p.get("key") == key and p.get("wr", 0) >= 60:
                mult = round(min(2.5, 1.0 + p["score"] / 80), 2)
                if mult > best_mult:
                    best_mult = mult
                    best_reason = f"exact_match_{int(p.get('wr',0))}wr_{int(p.get('score',0))}sc"
        
        # 2. Exact hour + ALL strategy (very strong)
        all_key = self._combo_key(market, hour, "ALL")
        for p in self.state.get("profitable_patterns", []):
            if p.get("key") == all_key and p.get("wr", 0) >= 60:
                mult = round(min(2.3, 1.0 + p["score"] / 90), 2)
                if mult > best_mult:
                    best_mult = mult
                    best_reason = f"hour_all_{int(p.get('wr',0))}wr_{int(p.get('score',0))}sc"
        
        # 3. Same market, similar hour (+-1h), any strategy
        for p in self.state.get("profitable_patterns", []):
            parts = p.get("key", "").split("|")
            if len(parts) >= 2 and parts[0] == market:
                try:
                    p_hour = int(parts[1])
                    if abs(p_hour - hour) <= 1 and p.get("wr", 0) >= 60:
                        mult = round(min(2.0, 1.0 + p["score"] / 120), 2)
                        if mult > best_mult:
                            best_mult = mult
                            best_reason = f"near_hour_{p_hour}wr{int(p.get('wr',0))}"
                except: pass
        
        # 4. Same market, any hour — use best pattern
        for p in self.state.get("profitable_patterns", []):
            parts = p.get("key", "").split("|")
            if len(parts) >= 1 and parts[0] == market and p.get("wr", 0) >= 65 and p.get("score", 0) >= 40:
                mult = round(min(1.8, 1.0 + p["score"] / 150), 2)
                if mult > best_mult:
                    best_mult = mult
                    best_reason = f"market_best_{int(p.get('wr',0))}wr_{int(p.get('score',0))}sc"
        
        return best_mult, best_reason

    def get_status(self):
        now = time.time()
        expired = [k for k, v in self.state["blocks"].items() if now >= v.get("until", 0)]
        for k in expired:
            del self.state["blocks"][k]
        active_blocks = {}
        for k, v in self.state["blocks"].items():
            remaining = v["until"] - now
            if remaining > 0:
                active_blocks[k] = {
                    "severity": v["severity"],
                    "remaining_min": round(remaining / 60, 1),
                    "reasons": v.get("reasons", []),
                }
        patterns = [p for p in self.state.get("profitable_patterns", []) if p.get("score", 0) > 30]
        return {
            "active_blocks": active_blocks,
            "block_count": len(active_blocks),
            "profitable_patterns": patterns[:10],
            "dead_patterns_count": len(self.state.get("dead_patterns", [])),
            "total_combos_tracked": len(self.state.get("combos", {})),
            "today": self.state.get("today", "?"),
        }

    def get_active_blocks_for_dashboard(self):
        now = time.time()
        blocks = []
        for k, v in list(self.state.get("blocks", {}).items()):
            remaining = v.get("until", 0) - now
            if remaining > 0:
                parts = k.split("|")
                blocks.append({
                    "market": parts[0] if parts else "?",
                    "hour": parts[1] if len(parts) > 1 else "?",
                    "severity": v.get("severity", "?"),
                    "remaining_min": round(remaining / 60, 1),
                    "reasons": v.get("reasons", []),
                })
        return sorted(blocks, key=lambda x: x.get("remaining_min", 0), reverse=True)

    def assess_recovery(self, market, hour, recent_ticks, session_stats):
        """Can we recover this loss? Checks market behavior, session health, decay patterns."""
        key = self._combo_key(market, hour)
        combo = self.state.get('combos', {}).get(key, {})
        
        current_pnl = combo.get('pnl', 0)
        peak = combo.get('peak_pnl', 0)
        loss_from_peak = peak - current_pnl
        consec_losses = combo.get('consec_losses', 0)
        
        # CHECK 1: Market tick behavior (entropy)
        tick_conf = 50
        if recent_ticks and len(recent_ticks) >= 10:
            prices = [t.get('quote', 0) for t in recent_ticks[-30:] if t.get('quote')]
            if len(prices) >= 10:
                mean_p = sum(prices) / len(prices)
                variance = sum((p - mean_p) ** 2 for p in prices) / len(prices)
                tick_conf = 70 if variance > 0.5 else 55 if variance > 0.1 else 20
                if len(prices) >= 5:
                    recent_avg = sum(prices[-5:]) / 5
                    older_avg = sum(prices[-10:-5]) / 10 if len(prices) >= 10 else mean_p
                    drift = abs(recent_avg - older_avg) / (mean_p or 1)
                    tick_conf += 10 if drift < 0.001 else -15

        # CHECK 2: Session health at this hour
        sess_conf = 50
        if session_stats:
            hs = session_stats.get(str(hour), {})
            if hs.get('trades', 0) >= 3:
                if hs.get('pnl', 0) > 0 and hs.get('wr', 0) >= 50:
                    sess_conf = 75
                elif hs.get('pnl', 0) > -1:
                    sess_conf = 50
                else:
                    sess_conf = 25

        # CHECK 3: Historical recovery from archives
        rec_conf = 50
        for archive in self.state.get('daily_archive', []):
            archived = archive.get('combos', {}).get(key, {})
            if archived.get('trades', 0) >= 3:
                if archived.get('peak_pnl', 0) > 1.0 and archived.get('pnl', 0) > -1.0:
                    rec_conf += 15
                elif archived.get('peak_pnl', 0) > 0 and archived.get('pnl', 0) < -2.0:
                    rec_conf -= 15

        # CHECK 4: Consecutive loss pattern
        consec_conf = 80 if consec_losses == 0 else 60 if consec_losses == 1 else 35 if consec_losses == 2 else 10

        # COMPOSITE
        confidence = round(tick_conf * 0.30 + sess_conf * 0.25 + rec_conf * 0.25 + consec_conf * 0.20)
        confidence = max(0, min(100, confidence))

        if confidence >= 70:
            action, reason, stake_mult = 'continue', 'Recovery confident — market stable, loss is noise', 0.85
        elif confidence >= 45:
            action, reason, stake_mult = 'cautious', 'Moderate confidence — reducing stake, watching', 0.5
        elif confidence >= 25:
            action, reason, stake_mult = 'pause', 'Low confidence — pausing 10min to reassess', 0.0
        else:
            action, reason, stake_mult = 'block', 'Decay detected — blocking this combo', 0.0

        self.state.setdefault('recovery_assessments', {})[f'{market}|{hour}'] = {
            'confidence': confidence, 'action': action, 'reason': reason,
            'tick_confidence': tick_conf, 'session_confidence': sess_conf,
            'recovery_confidence': rec_conf, 'consec_confidence': consec_conf,
            'loss_from_peak': round(loss_from_peak, 2), 'consec_losses': consec_losses,
            'timestamp': time.time(),
        }
        self._save()
        return confidence, action, reason, stake_mult

    def get_recovery_status(self):
        assessments = self.state.get('recovery_assessments', {})
        now = time.time()
        return {k: v for k, v in assessments.items() if now - v.get('timestamp', 0) < 600}
