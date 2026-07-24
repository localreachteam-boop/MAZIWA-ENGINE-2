
"""
YMCRC — Yield-Maximizing Contract Risk Controller

Blocks low-yield contracts (especially Digit Differs ~6% payout).
Enforces 70% minimum net yield threshold.
Pivots to high-payout alternatives when yield fails.
"""
import json
import time
from pathlib import Path

YMCRC_FILE = Path(__file__).parent.parent / 'ymcrc_state.json'

# Deriv typical payouts (can be overridden by live data)
TYPICAL_PAYOUTS = {
    'DIGITDIFF': 0.095,    # ~9.5% payout (90% WR but tiny reward)
    'DIGITMATCH': 9.0,     # ~900% payout (10% WR but huge reward)
    'RISE': 0.95,          # ~95% payout
    'FALL': 0.95,          # ~95% payout
    'CALL': 0.95,
    'PUT': 0.95,
    'EVEN': 0.95,
    'ODD': 0.95,
    'OVER': 0.95,
    'UNDER': 0.95,
    'ASIANU': 0.95,
    'ASIAND': 0.95,
}

# Win rate estimates for EV calculation
TYPICAL_WIN_RATES = {
    'DIGITDIFF': 0.90,
    'DIGITMATCH': 0.10,
    'RISE': 0.50,
    'FALL': 0.50,
    'CALL': 0.50,
    'PUT': 0.50,
    'EVEN': 0.50,
    'ODD': 0.50,
    'OVER': 0.50,
    'UNDER': 0.50,
    'ASIANU': 0.50,
    'ASIAND': 0.50,
}

# Hard floor: minimum net yield ratio
HARD_FLOOR = 0.70  # 70% net profit margin

# Contract swap mapping (low-yield -> high-yield alternatives)
SWAP_MAP = {
    'DIGITDIFF': ['DIGITMATCH', 'RISE', 'FALL'],
    'EVEN': ['RISE', 'FALL', 'DIGITMATCH'],
    'ODD': ['RISE', 'FALL', 'DIGITMATCH'],
    'OVER': ['RISE', 'FALL'],
    'UNDER': ['RISE', 'FALL'],
}


class YieldGatekeeper:
    """Enforces minimum yield on all contracts. Blocks toxic house-edge traps."""

    def __init__(self):
        self.state = self._load()
        self.live_payouts = {}
        self.kill_count = 0
        self.swap_count = 0

    def _load(self):
        if YMCRC_FILE.exists():
            try:
                return json.loads(YMCRC_FILE.read_text())
            except Exception:
                pass
        return {
            'version': 1,
            'kills': 0,
            'swaps': 0,
            'kills_by_contract': {},
            'last_kill': None,
            'blocked_contracts': [],
        }

    def _save(self):
        try:
            YMCRC_FILE.write_text(json.dumps(self.state, indent=2, default=str))
        except Exception:
            pass

    def update_payout(self, contract_type, payout_ratio):
        """Update live payout for a contract type."""
        self.live_payouts[contract_type.upper()] = payout_ratio

    def calculate_net_yield(self, contract_type, ask_price=1.0, stake=1.0):
        """
        Calculate Net Yield Ratio:
          Net_Yield = (Total_Payout - Ask_Price) / Ask_Price
        
        For Digit Differs:
          payout = stake * (1 + payout_ratio)  e.g., stake=1, ratio=0.095 -> payout=1.095
          net_yield = (payout - stake) / stake = payout_ratio
        """
        ct = contract_type.upper()
        payout_ratio = self.live_payouts.get(ct, TYPICAL_PAYOUTS.get(ct, 0.5))
        # Net yield = payout ratio (since ask_price = stake)
        net_yield = payout_ratio
        return round(net_yield, 4)

    def check_yield(self, contract_type, ask_price=1.0, stake=1.0):
        """
        Check if contract passes the yield gate.
        Returns: (pass_bool, net_yield, action, reason, recommended_contract)
        """
        ct = contract_type.upper()
        net_yield = self.calculate_net_yield(ct, ask_price, stake)

        # HARD FLOOR: 70% net yield
        if net_yield < HARD_FLOOR:
            # Find high-yield alternative
            alternatives = SWAP_MAP.get(ct, ['RISE', 'FALL'])
            best_alt = alternatives[0] if alternatives else 'RISE'
            alt_yield = self.calculate_net_yield(best_alt)

            # Record kill
            self.state['kills'] = self.state.get('kills', 0) + 1
            self.state['kills_by_contract'][ct] = self.state.get('kills_by_contract', {}).get(ct, 0) + 1
            self.state['last_kill'] = {
                'contract': ct,
                'net_yield': net_yield,
                'time': time.time(),
                'swap_to': best_alt,
            }
            self.state.setdefault('blocked_contracts', [])
            if ct not in self.state['blocked_contracts']:
                self.state['blocked_contracts'].append(ct)
            self._save()

            return False, net_yield, 'KILL_PROPOSAL_LOW_YIELD',                 f'Yield {net_yield:.1%} < 70% floor — BLOCKED. Swap to {best_alt} ({alt_yield:.1%})',                 best_alt

        # PASSED
        return True, net_yield, 'EXECUTE_PURCHASE',             f'Yield {net_yield:.1%} >= 70% floor — CLEARED',             ct

    def audit_all_contracts(self):
        """Audit all known contracts and return yield report."""
        report = []
        for ct in list(TYPICAL_PAYOUTS.keys()):
            net_yield = self.calculate_net_yield(ct)
            passes = net_yield >= HARD_FLOOR
            report.append({
                'contract': ct,
                'net_yield': net_yield,
                'pct': f'{net_yield:.1%}',
                'passes': passes,
                'action': 'EXECUTE' if passes else 'BLOCK',
                'typical_wr': TYPICAL_WIN_RATES.get(ct, 0),
            })
        report.sort(key=lambda x: x['net_yield'], reverse=True)
        return report

    def get_ev(self, contract_type, win_rate=None):
        """Calculate Expected Value for a contract."""
        ct = contract_type.upper()
        payout = self.live_payouts.get(ct, TYPICAL_PAYOUTS.get(ct, 0.5))
        wr = win_rate or TYPICAL_WIN_RATES.get(ct, 0.5)
        ev = wr * (1 + payout) - 1
        return round(ev, 4)

    def get_status(self):
        return {
            'total_kills': self.state.get('kills', 0),
            'total_swaps': self.state.get('swaps', 0),
            'kills_by_contract': self.state.get('kills_by_contract', {}),
            'blocked_contracts': self.state.get('blocked_contracts', []),
            'last_kill': self.state.get('last_kill'),
            'hard_floor': HARD_FLOOR,
        }

    def get_yield_report_for_dashboard(self):
        """Build HTML for dashboard yield audit."""
        report = self.audit_all_contracts()
        rows = ''
        for r in report:
            color = '#22c55e' if r['passes'] else '#ef4444'
            action_color = '#22c55e' if r['action'] == 'EXECUTE' else '#ef4444'
            icon = '&#10003;' if r['passes'] else '&#10007;'
            rows += '<div style="display:flex;gap:6px;padding:2px 0;border-bottom:1px solid #1a2332;font-size:10px;align-items:center">'
            rows += '<span style="color:%s;font-weight:700;min-width:16px">%s</span>' % (action_color, icon)
            rows += '<span style="color:#3b82f6;font-weight:600;min-width:80px">%s</span>' % r['contract']
            rows += '<span style="color:%s;font-weight:700;min-width:40px">%s</span>' % (color, r['pct'])
            rows += '<span style="color:#64748b;font-size:8px">WR=%.0f%%</span>' % (r['typical_wr'] * 100)
            rows += '<span style="color:%s;font-weight:600;text-transform:uppercase;font-size:8px">%s</span>' % (action_color, r['action'])
            rows += '</div>'
        if not rows:
            rows = '<div style="color:#64748b">No contracts to audit</div>'
        return rows
