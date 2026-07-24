"""
BUNCH RUNNER — Confidence-Based Run Execution

Instead of judging each trade individually, this system:
1. Scores the ENTRY SETUP (not the trade)
2. Commits a "bunch" of trades to that setup
3. Holds through early losses if setup is good
4. Tracks cumulative bunch P&L
5. Exits when setup degrades or target hit

A "bunch" is 3-10 trades committed to a single setup.
The bunch succeeds if cumulative P&L is positive at exit.
"""
import time


class SetupScorer:
    """Scores an entry setup's confidence (not individual trade)."""

    def __init__(self):
        self.history = []  # recent setup scores

    def score(self, market, strategy, regime, entropy, digit_bias, session_label, balance, consec_loss, hour=None, replicator=None):
        """
        Score 0-100 based on setup conditions + historical data.
        Higher = more confident in this setup worth running.
        """
        score = 30  # base (lower — historical data is the main driver now)

        # ═══ HISTORICAL PATTERN BOOST (biggest impact) ═══
        hist_boost = 0
        hist_wr = 0
        if replicator and hour is not None:
            # Check profitable patterns for this market+hour
            for p in replicator.state.get('profitable_patterns', []):
                pk = p.get('key', '')
                parts = pk.split('|')
                if len(parts) >= 2 and parts[0] == market:
                    try:
                        p_hour = int(parts[1])
                    except:
                        continue
                    p_strat = parts[2] if len(parts) > 2 else 'ALL'
                    p_wr = p.get('wr', 0)
                    p_pnl = p.get('pnl', 0)
                    p_score = p.get('score', 0)

                    # Exact hour match
                    if p_hour == hour:
                        if p_strat == strategy:
                            # Exact match — massive boost
                            hist_boost = max(hist_boost, 30 + p_score / 3)
                            hist_wr = max(hist_wr, p_wr)
                        elif p_strat == 'ALL':
                            # All-strategy match — strong boost
                            hist_boost = max(hist_boost, 20 + p_score / 4)
                            hist_wr = max(hist_wr, p_wr)
                        # Also check nearby hours (+-1)
                    elif abs(p_hour - hour) <= 1 and p_wr >= 60:
                        hist_boost = max(hist_boost, 10 + p_score / 5)
                        hist_wr = max(hist_wr, p_wr)

        score += hist_boost

        # ═══ REGIME ═══
        if regime in ('TREND_UP', 'TREND_DOWN', 'COMPRESSION'):
            score += 5
        elif regime == 'CALM':
            score += 3
        elif regime == 'ANOMALOUS':
            score -= 15

        # ═══ ENTROPY ═══
        if entropy < 2.8:
            score += 8
        elif entropy < 3.0:
            score += 5
        elif entropy < 3.15:
            score += 2
        else:
            score -= 8

        # ═══ DIGIT BIAS ═══
        bias = 0
        if isinstance(digit_bias, tuple) and len(digit_bias) >= 3:
            bias = digit_bias[2]
        elif isinstance(digit_bias, dict) and digit_bias.get("bias"):
            bias = digit_bias["bias"]
        if bias:
            if strategy.startswith('DIGIT') and bias > 0.15:
                score += 5
            elif strategy.startswith('FALL') and bias < -0.1:
                score += 4
            elif strategy.startswith('RISE') and bias > 0.1:
                score += 4

        # ═══ SESSION ═══
        if session_label in ('📊 US Afternoon', '📊 US Morning', '📊 EU Session'):
            score += 3

        # ═══ TIME-GATING: dead hours kill score, golden hours boost ═══
        DEAD_HOURS = {3, 4, 19, 23}  # UTC
        GOLDEN_HOURS = {0, 1, 2, 14, 15, 17, 21, 22}  # UTC
        BLOCKED_COMBOS = {('JD10',3), ('JD25',1), ('JD25',9), ('JD75',3), ('JD75',14)}
        if hour is not None:
            if hour in DEAD_HOURS:
                score = 0  # dead hour — no bunch
            elif (market, hour) in BLOCKED_COMBOS:
                score = 0  # proven losing combo
            elif hour in GOLDEN_HOURS:
                score += 8  # golden hour boost
            elif (market, hour) in {('JD25',15), ('JD50',14), ('R_100',14), ('JD10',14), ('R_25',3), ('JD25',0), ('JD25',14)}:
                score += 12  # priority combo boost

        # ═══ LOSS STREAK ═══
        if consec_loss >= 3:
            score -= 15
        elif consec_loss >= 2:
            score -= 8
        elif consec_loss >= 1:
            score -= 3

        # ═══ BALANCE ═══
        if balance > 9900:
            score += 2
        elif balance < 9800:
            score -= 5

        # ═══ HISTORICAL WR BONUS ═══
        if hist_wr >= 80:
            score += 10
        elif hist_wr >= 70:
            score += 7
        elif hist_wr >= 60:
            score += 4

        score = max(0, min(100, score))
        self.history.append({'time': time.time() * 1000, 'score': score, 'market': market, 'strategy': strategy})
        if len(self.history) > 200:
            self.history = self.history[-200:]
        return score

    def get_bunch_size(self, score):
        """Map confidence to bunch size. Bunch-first mode: always run at least 3."""
        if score >= 70:
            return 10  # high confidence → full run
        elif score >= 55:
            return 8   # medium → strong run
        elif score >= 40:
            return 6   # moderate → standard run
        elif score >= 25:
            return 4   # low → short run (still try)
        elif score >= 12:
            return 2   # very low → micro run (bunch-only mode needs at least 2)
        else:
            return 0   # truly too low → skip

    def get_starting_stake(self, score):
        """Map confidence to starting stake."""
        if score >= 70:
            return 1.50
        elif score >= 55:
            return 1.00
        else:
            return 1.00

    def get_status(self):
        return {
            'history_count': len(self.history),
            'avg_score': round(sum(h['score'] for h in self.history[-20:]) / max(len(self.history[-20:]), 1), 1),
        }


class BunchRun:
    """Tracks a single bunch run — a series of trades on one setup."""

    def __init__(self, market, strategy, regime, confidence_score, bunch_size, starting_stake, session_label):
        self.market = market
        self.strategy = strategy
        self.regime = regime
        self.confidence_score = confidence_score
        self.bunch_size = bunch_size
        self.target_trades = bunch_size
        self.stake = starting_stake
        self.session_label = session_label

        self.trades = []  # list of trade results
        self.cumulative_pnl = 0.0
        self.wins = 0
        self.losses = 0
        self.start_time = time.time() * 1000
        self.end_time = None
        self.exit_reason = None
        self.status = 'RUNNING'  # RUNNING, TARGET_HIT, SETUP_DEGRADED, MAX_TRADES, STOPPED

    def record_trade(self, profit):
        """Record a trade result and return whether bunch should continue."""
        self.trades.append({'profit': profit, 'time': time.time() * 1000})
        self.cumulative_pnl += profit
        if profit > 0:
            self.wins += 1
        else:
            self.losses += 1

        # Adapt stake based on bunch momentum
        self._adapt_stake()

        # Check exit conditions
        return self._check_exit()

    def _adapt_stake(self):
        """Increase stake as bunch validates, decrease on losses."""
        total = self.wins + self.losses
        if total < 2:
            return  # too early to adapt

        win_rate = self.wins / total

        if win_rate >= 0.6 and self.cumulative_pnl > 0:
            # Bunch is winning — scale up slightly
            self.stake = min(self.stake + 0.25, 2.50)
        elif win_rate < 0.4 and self.losses >= 2:
            # Bunch struggling — scale down
            self.stake = max(self.stake - 0.25, 0.75)

    def _check_exit(self):
        """Check if bunch should exit. Returns True to continue, False to stop."""
        trades_done = self.wins + self.losses

        # Target reached
        if trades_done >= self.target_trades:
            self.exit_reason = f'max_trades ({trades_done}/{self.target_trades})'
            self.status = 'MAX_TRADES'
            return False

        # Early profit target: if cumulative PnL hits target before max trades
        if self.cumulative_pnl >= self.target_pnl():
            self.exit_reason = f'target_hit (${self.cumulative_pnl:+.2f})'
            self.status = 'TARGET_HIT'
            return False

        # Setup degraded: too many consecutive losses (3+)
        recent = self.trades[-3:] if len(self.trades) >= 3 else self.trades
        recent_losses = sum(1 for t in recent if t['profit'] < 0)
        if recent_losses >= 3 and trades_done >= 3:
            self.exit_reason = f'setup_degraded (3 recent losses)'
            self.status = 'SETUP_DEGRADED'
            return False

        # Net loss too deep: cumulative loss exceeds threshold
        if self.cumulative_pnl < -(self.bunch_size * self.stake * 0.5):
            self.exit_reason = f'deep_loss (${self.cumulative_pnl:+.2f})'
            self.status = 'STOPPED'
            return False

        return True  # continue running

    def target_pnl(self):
        """Bunch target: positive return covering spread."""
        return 0.50  # minimum $0.50 profit to be worth the bunch

    def is_profitable(self):
        return self.cumulative_pnl > 0

    def get_duration_s(self):
        if self.end_time:
            return (self.end_time - self.start_time) / 1000
        return (time.time() * 1000 - self.start_time) / 1000

    def to_dict(self):
        return {
            'market': self.market,
            'strategy': self.strategy,
            'regime': self.regime,
            'confidence': self.confidence_score,
            'bunch_size': self.bunch_size,
            'target_trades': self.target_trades,
            'trades_done': self.wins + self.losses,
            'wins': self.wins,
            'losses': self.losses,
            'cumulative_pnl': round(self.cumulative_pnl, 2),
            'stake': self.stake,
            'status': self.status,
            'exit_reason': self.exit_reason,
            'profitable': self.is_profitable(),
            'session': self.session_label,
            'duration_s': round(self.get_duration_s(), 1),
            'start_time': self.start_time,
            'end_time': self.end_time,
            'trades': self.trades,
        }


class BunchRunner:
    """Manages bunch runs — the top-level controller."""

    def __init__(self):
        self.setup_scorer = SetupScorer()
        self.current_run = None  # active BunchRun or None
        self.completed_runs = []  # history of completed bunches
        self.daily_stats = {
            'date': '',
            'total_runs': 0,
            'profitable_runs': 0,
            'total_pnl': 0.0,
            'best_run_pnl': 0.0,
            'worst_run_pnl': 0.0,
            'total_bunch_trades': 0,
        }
        self._load()

    def _load(self):
        """Load saved state."""
        try:
            import json
            from pathlib import Path
            state_file = Path('bunch_state.json')
            if state_file.exists():
                state = json.loads(state_file.read_text())
                self.completed_runs = state.get('completed_runs', [])[-50:]  # keep last 50
                ds = state.get('daily_stats', {})
                today = __import__('datetime').datetime.now().strftime('%Y-%m-%d')
                if ds.get('date') == today:
                    self.daily_stats = ds
                self.setup_scorer.history = state.get('setup_history', [])[-200:]
        except Exception:
            pass

    def _save(self):
        """Save state to disk."""
        try:
            import json
            from pathlib import Path
            state = {
                'completed_runs': self.completed_runs[-50:],
                'daily_stats': self.daily_stats,
                'setup_history': self.setup_scorer.history[-200:],
                'current_run': self.current_run.to_dict() if self.current_run else None,
            }
            Path('bunch_state.json').write_text(json.dumps(state, indent=2))
        except Exception:
            pass

    def should_start_run(self, market, strategy, regime, entropy, digit_bias, session_label, balance, consec_loss, hour=None, replicator=None):
        """Decide if a new bunch run should start. Returns (should_start, setup_score, bunch_size)."""
        # Don't start if a run is active
        if self.current_run and self.current_run.status == 'RUNNING':
            return False, 0, 0

        # Score the setup (with historical data)
        score = self.setup_scorer.score(market, strategy, regime, entropy, digit_bias, session_label, balance, consec_loss, hour=hour, replicator=replicator)
        bunch_size = self.setup_scorer.get_bunch_size(score)

        if bunch_size == 0:
            return False, score, 0

        # Don't start if too many consecutive losses (global)
        if consec_loss >= 4:
            return False, score, 0

        return True, score, bunch_size

    def start_run(self, market, strategy, regime, confidence_score, bunch_size, session_label):
        """Start a new bunch run."""
        starting_stake = self.setup_scorer.get_starting_stake(confidence_score)
        self.current_run = BunchRun(
            market=market,
            strategy=strategy,
            regime=regime,
            confidence_score=confidence_score,
            bunch_size=bunch_size,
            starting_stake=starting_stake,
            session_label=session_label,
        )
        self._save()
        return self.current_run

    def on_trade_result(self, profit):
        """Call after each trade in the bunch. Returns (continue_bunch, bunch_complete, run_result)."""
        if not self.current_run or self.current_run.status != 'RUNNING':
            return False, False, None

        continue_bunch = self.current_run.record_trade(profit)
        self._save()

        if not continue_bunch:
            # Bunch complete
            self.current_run.end_time = time.time() * 1000
            result = self.current_run.to_dict()
            self.completed_runs.append(result)

            # Update daily stats
            today = __import__('datetime').datetime.now().strftime('%Y-%m-%d')
            if self.daily_stats['date'] != today:
                self._reset_daily(today)

            self.daily_stats['total_runs'] += 1
            self.daily_stats['total_pnl'] += self.current_run.cumulative_pnl
            self.daily_stats['total_bunch_trades'] += (self.current_run.wins + self.current_run.losses)
            if self.current_run.is_profitable():
                self.daily_stats['profitable_runs'] += 1
            self.daily_stats['best_run_pnl'] = max(self.daily_stats['best_run_pnl'], self.current_run.cumulative_pnl)
            self.daily_stats['worst_run_pnl'] = min(self.daily_stats['worst_run_pnl'], self.current_run.cumulative_pnl)

            self._save()
            return False, True, result

        return True, False, None

    def _reset_daily(self, date_str):
        self.daily_stats = {
            'date': date_str,
            'total_runs': 0,
            'profitable_runs': 0,
            'total_pnl': 0.0,
            'best_run_pnl': 0.0,
            'worst_run_pnl': 0.0,
            'total_bunch_trades': 0,
        }

    def get_active_run(self):
        return self.current_run.to_dict() if self.current_run and self.current_run.status == 'RUNNING' else None

    def get_daily_summary(self):
        return self.daily_stats

    def get_recent_runs(self, n=10):
        return self.completed_runs[-n:]

    def get_status(self):
        return {
            'active_run': self.get_active_run(),
            'completed_today': self.daily_stats['total_runs'],
            'profitable_today': self.daily_stats['profitable_runs'],
            'bunch_pnl_today': round(self.daily_stats['total_pnl'], 2),
            'best_run_pnl': round(self.daily_stats.get('best_run_pnl', 0), 2),
            'worst_run_pnl': round(self.daily_stats.get('worst_run_pnl', 0), 2),
            'total_bunch_trades': self.daily_stats.get('total_bunch_trades', 0),
            'setup_scorer': self.setup_scorer.get_status(),
            'recent_runs': self.get_recent_runs(15),
        }
