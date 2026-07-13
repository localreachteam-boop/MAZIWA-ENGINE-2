"""
CONTRACT PICKER — Shuffle Algorithm
Rotates contracts and markets based on win/loss arcs.
After profit → shuffle to different contract family.
After consecutive profit → shuffle market + family.
After loss → switch family. After consecutive losses → switch market + family.
Accumulation/Even-Odd have special rotation rules.
"""
import time
import random

# ── Contract Families ─────────────────────────────────
# Contracts are grouped by family. After a win, shuffle to a DIFFERENT family.
FAMILIES = {
    "directional": ["CALL", "PUT"],
    "parity":      ["DIGITEVEN", "DIGITODD"],
    "barrier":     ["ASIANU", "ASIAND"],
    "digit":       ["DIGITMATCH", "DIGITDIFF"],
    "touch":       []  # DISABLED: 0% win rate across 19 trades,
}
FAMILY_NAMES = list(FAMILIES.keys())

# Flat list of all contracts
ALL_CONTRACTS = []
for family, contracts in FAMILIES.items():
    ALL_CONTRACTS.extend(contracts)

# Which family does each contract belong to?
# BLACKLISTED: strategies/contracts that have proven to destroy capital
BLACKLISTED_STRATEGIES = ["DIGIT_DIFF_0"]  # 94 trades, 33% WR, -$78.80
BLACKLISTED_CONTRACTS = ["ONETOUCH"]       # 19 trades, 0% WR, -$70.50

# Market priority weights (from research: R_75 best, JD10 worst)
MARKET_WEIGHTS = {
    "R_75": 2.0, "JD100": 1.5, "JD50": 1.2, "JD25": 1.1,
    "R_10": 0.3, "R_100": 0.5, "R_25": 0.6, "R_50": 0.4,
    "JD75": 0.3, "JD10": 0.2
}

# Strategy priority weights (from research)
STRATEGY_WEIGHTS = {
    "DIGIT_DIFF_1": 2.0, "DIGIT_MATCH_2": 1.8, "DIGIT_DIFF_3": 1.3,
    "DIGIT_DIFF_0": 0.0,  # Blacklisted
}

CONTRACT_FAMILY = {}
for family, contracts in FAMILIES.items():
    for c in contracts:
        CONTRACT_FAMILY[c] = family

# Contract metadata
CONTRACT_CATALOG = {
    "CALL":        {"type": "directional", "name": "Rise",         "be": 0.53, "payout": 0.95, "risk": "medium"},
    "PUT":         {"type": "directional", "name": "Fall",         "be": 0.53, "payout": 0.95, "risk": "medium"},
    "DIGITMATCH":  {"type": "digit",       "name": "Digit Match",  "be": 0.11, "payout": 8.0,  "risk": "very_high"},
    "DIGITDIFF":   {"type": "digit",       "name": "Digit Differs","be": 0.95, "payout": 0.06, "risk": "low"},
    "DIGITEVEN":   {"type": "parity",      "name": "Even",         "be": 0.53, "payout": 0.95, "risk": "medium"},
    "DIGITODD":    {"type": "parity",      "name": "Odd",          "be": 0.53, "payout": 0.95, "risk": "medium"},
    "ASIANU":      {"type": "barrier",     "name": "Asian Up",     "be": 0.53, "payout": 0.95, "risk": "medium"},
    "ASIAND":      {"type": "barrier",     "name": "Asian Down",   "be": 0.53, "payout": 0.95, "risk": "medium"},
    "ONETOUCH":    {"type": "touch",       "name": "Touch",        "be": 0.05, "payout": 18.0, "risk": "very_high"},
}

# ── Shuffle Rules ─────────────────────────────────────
# What family to go to AFTER a result on a given family
SHUFFLE_AFTER_WIN = {
    "directional": ["parity", "barrier", "digit"],
    "parity":      ["directional", "barrier", "digit"],
    "barrier":     ["directional", "parity", "digit"],
    "digit":       ["directional", "parity", "barrier"],
    "touch":       ["directional", "parity", "digit"],
}

SHUFFLE_AFTER_LOSS = {
    "directional": ["parity", "barrier", "digit"],
    "parity":      ["directional", "barrier", "digit"],
    "barrier":     ["directional", "parity", "digit"],
    "digit":       ["directional", "parity", "barrier"],
    "touch":       ["directional", "parity", "digit"],
}


class ContractPicker:
    """
    Shuffle Algorithm for contract + market rotation.

    Rules:
    1 win  → shuffle to different contract FAMILY
    2 wins → shuffle to different contract FAMILY + force market switch
    1 loss → if same family 2x in a row, switch family
    2 losses → shuffle to different FAMILY + force market switch
    3+ trades on same contract → rotate to next contract in family
    Accumulation detected → force switch to non-accumulation family + market
    Even/Odd → after win, prefer Rise/Fall on different market
    """

    def __init__(self):
        self.catalog = dict(CONTRACT_CATALOG)
        self.live_payouts = {}
        self.live_break_even = {}

        # Per-contract performance tracking
        self.contract_stats = {}

        # Current state
        self.active_contract = "CALL"
        self.active_family = "directional"
        self.consecutive_losses = 0
        self.consecutive_wins = 0
        self.total_trades = 0
        self.total_pnl = 0

        # Family tracking (recent family usage)
        self.recent_families = []  # last N families used
        self.MAX_RECENT = 5

        # Trade count per contract
        self.trades_per_contract = {}
        self.TRADES_PER_CONTRACT_LIMIT = 3

        # Switching
        self.switch_count = 0
        self.last_switch_time = 0
        self.SWITCH_COOLDOWN = 3
        self.force_market_switch = False

        # Per-market contract performance
        self.market_contract_stats = {}

        self.pick_history = []

        # YA-CRC: Contract Quality Rankings
        self.contract_quality = {}     # contract_type -> {level, score, ev, net_yield, ...}
        self.evaluation_history = []   # last N evaluations
        self.MAX_EVAL_HISTORY = 100

    # ================================================================
    # YA-CRC: YIELD-AWARE CONTRACT RISK CONTROLLER
    # ================================================================

    def evaluate_contract(self, contract_type, stake, probability, payout=None,
                          market=None, signal=None):
        """
        YA-CRC core: evaluate a contract proposal before approval.
        Returns structured evaluation with Net_Yield, P_implied, EV,
        payout_efficiency, risk_adjusted_score, and LEVEL A/B/C.
        """
        info = self.catalog.get(contract_type, {})
        if payout is None:
            payout = self.live_payouts.get(contract_type, info.get("payout", 1.0))

        # A) NET PAYOUT YIELD
        net_yield = payout - 1.0

        # B) IMPLIED PROBABILITY
        total_payout = 1.0 + payout
        p_implied = 1.0 / total_payout if total_payout > 0 else 1.0

        # C) EXPECTED VALUE
        ev = (probability * payout) - (1 - probability)

        # D) RISK PENALTY from contract type
        risk_penalty = 1.0
        if info.get("risk") == "very_high":
            risk_penalty = 0.6
        elif info.get("risk") == "high":
            risk_penalty = 0.8
        elif info.get("risk") == "medium":
            risk_penalty = 1.0
        else:
            risk_penalty = 1.1

        payout_efficiency = (ev * payout) * risk_penalty if ev > 0 else ev * risk_penalty

        # E) HISTORICAL RELIABILITY
        cs = self.contract_stats.get(contract_type, {})
        trades = cs.get("trades", 0)
        wins = cs.get("wins", 0)
        total_pnl = cs.get("total_pnl", 0)

        if trades >= 3:
            historical_wr = wins / trades
            reliability = min(trades / 30, 1.0) * historical_wr
        else:
            reliability = 0.3

        # F) RISK PENALTY from history
        loss_streak = cs.get("streak", 0)
        risk_penalty_score = 0.0
        if trades >= 3:
            if total_pnl < -5:
                risk_penalty_score += 0.3
            if total_pnl < -10:
                risk_penalty_score += 0.3
            if loss_streak < -2:
                risk_penalty_score += 0.2
            if trades >= 5:
                loss_rate = (trades - wins) / trades
                if loss_rate > 0.7:
                    risk_penalty_score += 0.2

        # G) RISK ADJUSTED SCORE (YA-CRC Formula)
        # Score = EV + Reliability - Risk_Penalty
        risk_adjusted_score = ev + reliability - risk_penalty_score

        # H) QUALITY LEVEL (A/B/C)
        if ev > 0 and trades >= 10 and (wins / trades if trades > 0 else 0) >= 0.50 and risk_adjusted_score > 0.1:
            level = "A"
            level_label = "APPROVED"
        elif ev > 0 and risk_adjusted_score > -0.1:
            level = "B"
            level_label = "TEST_ONLY"
        else:
            level = "C"
            level_label = "BLOCKED"

        if ev <= 0:
            level = "C"
            level_label = "BLOCKED"

        evaluation = {
            "contract_type": contract_type,
            "action": "EXECUTE_PURCHASE" if level in ("A", "B") else "KILL_PROPOSAL",
            "level": level,
            "level_label": level_label,
            "calculated_net_yield": round(net_yield, 4),
            "p_implied": round(p_implied, 4),
            "expected_value": round(ev, 4),
            "payout_efficiency": round(payout_efficiency, 4),
            "risk_adjusted_score": round(risk_adjusted_score, 4),
            "reliability": round(reliability, 4),
            "risk_penalty_score": round(risk_penalty_score, 4),
            "payout": round(payout, 4),
            "probability": round(probability, 4),
            "stake": stake,
            "market": market,
            "historical_trades": trades,
            "historical_wr": round(cs.get("wins", 0) / trades, 4) if trades > 0 else 0,
            "reason": "EV={:.4f} RAS={:.4f} Level={}".format(ev, risk_adjusted_score, level),
            "realign_strategy": level == "C",
            "recommended_contract_type": self._suggest_alternative(contract_type, ev) if level == "C" else contract_type,
        }

        self.contract_quality[contract_type] = evaluation
        self.evaluation_history.append(evaluation)
        if len(self.evaluation_history) > self.MAX_EVAL_HISTORY:
            self.evaluation_history = self.evaluation_history[-self.MAX_EVAL_HISTORY:]

        return evaluation

    def _suggest_alternative(self, current_type, current_ev):
        """YA-CRC: suggest a better contract when current is BLOCKED."""
        current_family = CONTRACT_FAMILY.get(current_type, "directional")
        best_alt = None
        best_score = -999
        for ctype, quality in self.contract_quality.items():
            if ctype == current_type:
                continue
            if quality.get("level") == "C":
                continue
            score = quality.get("risk_adjusted_score", 0)
            if score > best_score:
                best_score = score
                best_alt = ctype
        if best_alt:
            return best_alt
        for family, contracts in FAMILIES.items():
            if family == current_family:
                continue
            for c in contracts:
                info = self.catalog.get(c, {})
                if info.get("risk") in ("low", "medium"):
                    return c
        return "CALL"

    def get_all_contract_rankings(self):
        """YA-CRC: rank all contracts by risk_adjusted_score."""
        rankings = []
        for ctype, info in self.catalog.items():
            quality = self.contract_quality.get(ctype, {})
            if quality:
                rankings.append({
                    "contract": ctype,
                    "level": quality.get("level", "B"),
                    "risk_adjusted_score": quality.get("risk_adjusted_score", 0),
                    "ev": quality.get("expected_value", 0),
                    "payout_efficiency": quality.get("payout_efficiency", 0),
                })
            else:
                rankings.append({
                    "contract": ctype,
                    "level": "B",
                    "risk_adjusted_score": 0,
                    "ev": 0,
                    "payout_efficiency": 0,
                })
        rankings.sort(key=lambda x: x["risk_adjusted_score"], reverse=True)
        return rankings

    def record_result(self, contract_type, profit, market=None):
        """Record trade result and apply shuffle algorithm."""
        if contract_type not in self.contract_stats:
            self.contract_stats[contract_type] = {
                "wins": 0, "losses": 0, "streak": 0,
                "last_result": None, "total_pnl": 0, "trades": 0,
            }

        cs = self.contract_stats[contract_type]
        cs["trades"] += 1
        cs["total_pnl"] = round(cs["total_pnl"] + profit, 4)
        self.total_trades += 1
        self.total_pnl = round(self.total_pnl + profit, 4)
        self.trades_per_contract[contract_type] = self.trades_per_contract.get(contract_type, 0) + 1

        # Per-market tracking
        if market:
            mc_key = f"{market}:{contract_type}"
            if mc_key not in self.market_contract_stats:
                self.market_contract_stats[mc_key] = {"wins": 0, "losses": 0, "total_pnl": 0, "trades": 0}
            mc = self.market_contract_stats[mc_key]
            mc["trades"] += 1
            mc["total_pnl"] = round(mc["total_pnl"] + profit, 4)
            if profit > 0:
                mc["wins"] += 1
            else:
                mc["losses"] += 1

        if profit > 0:
            cs["wins"] += 1
            cs["streak"] = max(cs["streak"], 0) + 1
            cs["last_result"] = "WIN"
            self.consecutive_wins += 1
            self.consecutive_losses = 0
        else:
            cs["losses"] += 1
            cs["streak"] = min(cs["streak"], 0) - 1
            cs["last_result"] = "LOSS"
            self.consecutive_losses += 1
            self.consecutive_wins = 0

        # ── SHUFFLE ALGORITHM ──
        self._shuffle(contract_type, profit)

    def _shuffle(self, current_type, profit):
        """
        Core shuffle algorithm.
        Decides whether to switch contract family and/or market.
        """
        now = time.time()
        if now - self.last_switch_time < self.SWITCH_COOLDOWN:
            return

        current_family = CONTRACT_FAMILY.get(current_type, "directional")
        cs = self.contract_stats.get(current_type, {})

        # ── RULE 1: 1 WIN → shuffle to different family ──
        if profit > 0 and self.consecutive_wins == 1:
            new_family = self._pick_different_family(current_family, "win")
            new_contract = self._pick_from_family(new_family)
            if new_contract and new_contract != current_type:
                self._do_switch(new_contract, f"1 win → shuffle {current_family} → {new_family}")
                return

        # ── RULE 2: 2+ CONSECUTIVE WINS → shuffle family + market ──
        if profit > 0 and self.consecutive_wins >= 2:
            new_family = self._pick_different_family(current_family, "win")
            new_contract = self._pick_from_family(new_family)
            if new_contract and new_contract != current_type:
                self._do_switch(new_contract, f"{self.consecutive_wins} wins → shuffle family + market")
                self.force_market_switch = True
                return

        # ── RULE 3: 1 LOSS → if same family 2x recently, switch ──
        if profit < 0 and self.consecutive_losses == 1:
            recent_same = sum(1 for f in self.recent_families[-3:] if f == current_family)
            if recent_same >= 2:
                new_family = self._pick_different_family(current_family, "loss")
                new_contract = self._pick_from_family(new_family)
                if new_contract:
                    self._do_switch(new_contract, f"Same family {current_family} {recent_same}x → shuffle")
                    return

        # ── RULE 4: 2+ CONSECUTIVE LOSSES → shuffle family + market ──
        if profit < 0 and self.consecutive_losses >= 2:
            new_family = self._pick_different_family(current_family, "loss")
            new_contract = self._pick_from_family(new_family)
            if new_contract:
                self._do_switch(new_contract, f"{self.consecutive_losses} losses → shuffle family + market")
                self.force_market_switch = True
                return

        # ── RULE 5: TOO MANY trades on same contract → rotate within family ──
        if self.trades_per_contract.get(current_type, 0) >= self.TRADES_PER_CONTRACT_LIMIT:
            new_contract = self._pick_from_family(current_family, exclude=current_type)
            if new_contract:
                self._do_switch(new_contract, f"Trade limit on {current_type} → rotate in {current_family}")
                self.trades_per_contract[current_type] = 0
                return

        # ── RULE 6: Negative PnL after enough trades → shuffle ──
        if cs.get("trades", 0) >= 4 and cs.get("total_pnl", 0) < -3:
            new_family = self._pick_different_family(current_family, "loss")
            new_contract = self._pick_from_family(new_family)
            if new_contract:
                self._do_switch(new_contract, f"Negative PnL ${cs['total_pnl']:.2f} → shuffle")
                self.force_market_switch = True
                return

        # ── RULE 7: Win rate < 35% after 5+ trades → shuffle ──
        if cs.get("trades", 0) >= 5:
            wr = cs["wins"] / cs["trades"]
            if wr < 0.35:
                new_family = self._pick_different_family(current_family, "loss")
                new_contract = self._pick_from_family(new_family)
                if new_contract:
                    self._do_switch(new_contract, f"Low WR {wr:.0%} → shuffle")
                    self.force_market_switch = True
                    return

    def _do_switch(self, new_contract, reason):
        """Execute the switch."""
        old = self.active_contract
        self.active_contract = new_contract
        self.active_family = CONTRACT_FAMILY.get(new_contract, "directional")
        self.recent_families.append(self.active_family)
        if len(self.recent_families) > self.MAX_RECENT:
            self.recent_families.pop(0)
        self.last_switch_time = time.time()
        self.switch_count += 1
        self.consecutive_losses = 0
        self.consecutive_wins = 0
        print(f"  [SHUFFLE] {old} → {new_contract} ({self.active_family}) | {reason}")

    def _pick_different_family(self, current_family, context="win"):
        """Pick a different family, weighted by context."""
        candidates = [f for f in FAMILY_NAMES if f != current_family]

        # Weight: avoid families used recently
        weights = []
        for f in candidates:
            w = 1.0
            recent_count = sum(1 for rf in self.recent_families[-3:] if rf == f)
            w *= max(0.1, 1.0 - recent_count * 0.3)

            # Context bonuses
            if context == "win":
                # After wins, prefer higher-payout families (more reward)
                if f == "digit":
                    w *= 1.5  # DIGITMATCH has 8x payout
                elif f == "touch":
                    w *= 1.3  # ONETOUCH has 18x payout
            elif context == "loss":
                # After losses, prefer safer families
                if f == "directional":
                    w *= 1.5  # CALL/PUT are straightforward
                elif f == "parity":
                    w *= 1.3  # EVEN/ODD are simple
                elif f == "digit":
                    w *= 0.5  # avoid risky digit match after losses

            weights.append(w)

        if not candidates:
            return current_family

        # Weighted random selection
        total = sum(weights)
        r = random.uniform(0, total)
        cumulative = 0
        for i, (f, w) in enumerate(zip(candidates, weights)):
            cumulative += w
            if r <= cumulative:
                return f
        return candidates[-1]

    def _pick_from_family(self, family, exclude=None):
        """Pick a contract from a family using YA-CRC risk_adjusted_score."""
        contracts = FAMILIES.get(family, [])
        available = [c for c in contracts if c != exclude]
        if not available:
            available = contracts
        if not available:
            return None

        best = None
        best_score = -999
        for c in available:
            cs = self.contract_stats.get(c, {})
            trades = cs.get("trades", 0)

            # Prefer YA-CRC evaluated score if available
            quality = self.contract_quality.get(c)
            if quality and quality.get("risk_adjusted_score") is not None:
                score = quality["risk_adjusted_score"] * 10
                if quality.get("level") == "C":
                    score -= 50  # heavily penalize BLOCKED contracts
                elif quality.get("level") == "A":
                    score += 10  # bonus for PROVEN contracts
            elif trades >= 3:
                wr = cs["wins"] / trades
                pnl = cs.get("total_pnl", 0)
                streak = cs.get("streak", 0)
                score = wr * 10 + pnl * 0.5
                if streak < -2:
                    score -= 20
            else:
                score = 5  # untested: neutral

            if score > best_score:
                best_score = score
                best = c

        return best or random.choice(available)

    def pick_best(self, regime, strategy_name, signal, digit_freq=None):
        """Pick the best contract using shuffle algorithm state."""
        # Check trade limit rotation
        if self.trades_per_contract.get(self.active_contract, 0) >= self.TRADES_PER_CONTRACT_LIMIT:
            family = CONTRACT_FAMILY.get(self.active_contract, "directional")
            new_c = self._pick_from_family(family, exclude=self.active_contract)
            if new_c:
                self._do_switch(new_c, f"Trade limit → rotate in {family}")
                self.trades_per_contract[self.active_contract] = 0

        # Use active contract
        cs = self.contract_stats.get(self.active_contract, {})
        info = self.catalog.get(self.active_contract, {})
        return self._build_pick(self.active_contract, info, cs)

    def _build_pick(self, ctype, info, cs):
        # Include YA-CRC evaluation if available
        quality = self.contract_quality.get(ctype, {})
        pick = {
            "contract_type": ctype,
            "name": info.get("name", ctype),
            "type": info.get("type", "unknown"),
            "break_even": self.live_break_even.get(ctype, info.get("be", 0.5)),
            "payout": self.live_payouts.get(ctype, info.get("payout", 1.0)),
            "risk": info.get("risk", "medium"),
            "reason": "shuffle_" + self.active_family,
            "score": quality.get("risk_adjusted_score", 0),
            "ya_crc_level": quality.get("level", "B"),
            "ya_crc_action": quality.get("action", "TEST_ONLY"),
            "ya_crc_ev": quality.get("expected_value", 0),
            "ya_crc_payout_efficiency": quality.get("payout_efficiency", 0),
            "active_contract": self.active_contract,
            "active_family": self.active_family,
            "consecutive_losses": self.consecutive_losses,
            "consecutive_wins": self.consecutive_wins,
            "switch_count": self.switch_count,
            "recent_families": list(self.recent_families),
            "contract_stats": {
                k: {"wins": v["wins"], "losses": v["losses"], "pnl": v["total_pnl"]}
                for k, v in self.contract_stats.items()
            },
        }
        self.pick_history.append(pick)
        if len(self.pick_history) > 50:
            self.pick_history = self.pick_history[-50:]
        return pick

    def force_rotate(self):
        """Force rotation to a different contract family."""
        new_family = self._pick_different_family(self.active_family, "force_rotate")
        if new_family:
            self._shuffle(self.active_contract, 1)  # treat as win to shuffle
            self._note(f"Force rotated from {self.active_family} to {new_family}")

    def _note(self, text):
        # Compatibility stub for executor calls
        pass

    def should_rotate_market(self):
        # Also rotate if market P&L is deeply negative
        """Check if we should force a market rotation."""
        if self.force_market_switch:
            self.force_market_switch = False
            return True
        return False

    def get_status(self):
        return {
            "active_contract": self.active_contract,
            "active_family": self.active_family,
            "total_trades": self.total_trades,
            "total_pnl": round(self.total_pnl, 2),
            "consecutive_losses": self.consecutive_losses,
            "consecutive_wins": self.consecutive_wins,
            "switch_count": self.switch_count,
            "recent_families": list(self.recent_families),
            "contract_stats": {
                k: {"wins": v["wins"], "losses": v["losses"], "pnl": round(v["total_pnl"], 2)}
                for k, v in self.contract_stats.items()
            },
            "last_pick": self.pick_history[-1] if self.pick_history else None,
            "catalog": {
                ctype: {
                    "name": info["name"], "type": info["type"], "risk": info["risk"],
                    "family": CONTRACT_FAMILY.get(ctype, "unknown"),
                    "be": info["be"],
                    "live_payout": self.live_payouts.get(ctype, info["payout"]),
                }
                for ctype, info in self.catalog.items()
            },
            "ya_crc": {
                "evaluations": len(self.evaluation_history),
                "rankings": self.get_all_contract_rankings(),
                "quality": {
                    k: {"level": v.get("level"), "ev": v.get("expected_value"),
                        "ras": v.get("risk_adjusted_score"), "action": v.get("action")}
                    for k, v in self.contract_quality.items()
                },
            },
        }
