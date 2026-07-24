"""
Deriv v1 API Client — OTP-based WebSocket authentication
Uses PAT token for REST, OTP for WebSocket trading.
"""
import json, time, asyncio, urllib.request, urllib.error
import websockets

class DerivClient:
    def __init__(self, pat_token, app_id, rest_base='https://api.derivws.com', ws_base='wss://api.derivws.com/trading/v1/options/ws'):
        self.pat_token = pat_token
        self.app_id = app_id
        self.rest_base = rest_base
        self.ws_base = ws_base
        self.ws = None
        self.connected = False
        self.req_id = 0
        self.balance = 0
        self.loginid = ''
        self.currency = 'USD'
    
    def _rest_headers(self):
        return {
            'Authorization': f'Bearer {self.pat_token}',
            'Deriv-App-ID': self.app_id,
            'Content-Type': 'application/json',
        }
    
    def _rest_get(self, path):
        url = f'{self.rest_base}{path}'
        req = urllib.request.Request(url, headers=self._rest_headers())
        resp = urllib.request.urlopen(req, timeout=10)
        return json.loads(resp.read().decode())
    
    def _rest_post(self, path, data=b'{}'):
        url = f'{self.rest_base}{path}'
        req = urllib.request.Request(url, data=data, headers=self._rest_headers(), method='POST')
        resp = urllib.request.urlopen(req, timeout=10)
        return json.loads(resp.read().decode())
    
    def list_accounts(self):
        """Get all trading accounts."""
        return self._rest_get('/trading/v1/options/accounts')['data']
    
    def get_otp(self, account_id):
        """Request OTP for WebSocket auth. Returns WS URL with embedded OTP."""
        result = self._rest_post(f'/trading/v1/options/accounts/{account_id}/otp')
        return result['data']['url']
    
    async def connect(self, account_id=None):
        """Connect to WebSocket using OTP."""
        accounts = self.list_accounts()
        if not accounts:
            raise Exception("No accounts found")
        
        if account_id is None:
            # Prefer demo first, then real
            for acc in accounts:
                if acc['account_type'] == 'demo' and float(acc['balance']) > 0:
                    account_id = acc['account_id']
                    break
            if account_id is None:
                account_id = accounts[0]['account_id']
        
        ws_url = self.get_otp(account_id)
        self.ws = await websockets.connect(ws_url, ping_interval=20)
        
        # Get balance
        await self._send({'balance': 1})
        r = await self._recv()
        if r.get('error'):
            raise Exception(f"Balance error: {r['error']['message']}")
        
        b = r.get('balance', {})
        self.balance = float(b.get('balance', 0))
        self.loginid = b.get('loginid', '')
        self.currency = b.get('currency', 'USD')
        self.connected = True
        return True
    
    async def _send(self, msg):
        self.req_id += 1
        msg['req_id'] = self.req_id
        await self.ws.send(json.dumps(msg))
        return self.req_id
    
    async def _recv(self, timeout=10):
        msg = await asyncio.wait_for(self.ws.recv(), timeout=timeout)
        return json.loads(msg)
    
    async def propose(self, contract_type, symbol, amount, duration=1, duration_unit='t', barrier=None):
        """Get price proposal."""
        params = {
            'proposal': 1, 'contract_type': contract_type,
            'underlying_symbol': symbol, 'amount': amount,
            'currency': self.currency, 'duration': duration,
            'duration_unit': duration_unit, 'basis': 'stake',
        }
        if barrier is not None:
            params['barrier'] = str(barrier)
        await self._send(params)
        r = await self._recv()
        if r.get('error'):
            return None, r['error']['message']
        return r['proposal'], None
    
    async def buy(self, proposal_id, price):
        """Buy a contract."""
        await self._send({'buy': proposal_id, 'price': price})
        r = await self._recv(timeout=15)
        if r.get('error'):
            return None, r['error']['message']
        return r.get('buy', {}), None
    
    async def wait_result(self, contract_id, timeout=30):
        """Wait for contract result."""
        await self._send({'proposal_open_contract': 1, 'contract_id': contract_id, 'subscribe': 1})
        deadline = time.time() + timeout
        while time.time() < deadline:
            r = await self._recv(timeout=min(10, deadline - time.time()))
            poc = r.get('proposal_open_contract', {})
            if poc.get('is_sold'):
                return poc.get('profit', 0), poc
        return 0.0, {}
    
    async def get_ticks(self, symbol, count=10):
        """Get recent ticks."""
        end = int(time.time())
        await self._send({'ticks_history': symbol, 'adjust_start_time': 1, 'count': count, 'end': end})
        r = await self._recv()
        return r.get('history', {})
    
    async def get_active_symbols(self):
        """Get all active symbols."""
        await self._send({'active_symbols': 'full'})
        r = await self._recv()
        return r.get('active_symbols', [])
    
    async def close(self):
        if self.ws:
            await self.ws.close()
            self.connected = False

# Convenience: auto-reconnect on OTP expiry
class AutoReconnectDerivClient(DerivClient):
    """DerivClient that auto-reconnects when OTP expires."""
    
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._account_id = None
    
    async def connect(self, account_id=None):
        self._account_id = account_id
        return await super().connect(account_id)
    
    async def ensure_connected(self):
        """Reconnect if disconnected."""
        if not self.connected or self.ws is None:
            await self.connect(self._account_id)
    
    async def safe_buy(self, contract_type, symbol, amount, stake, **kwargs):
        """Full trade cycle: propose → buy → wait result."""
        await self.ensure_connected()
        
        proposal, err = await self.propose(contract_type, symbol, amount, **kwargs)
        if err:
            return None, err
        
        buy_result, err = await self.buy(proposal['id'], stake)
        if err:
            # If OTP expired, reconnect and retry once
            if 'otp' in err.lower() or 'expired' in err.lower() or 'invalid' in err.lower():
                self.connected = False
                await self.ensure_connected()
                proposal, err = await self.propose(contract_type, symbol, amount, **kwargs)
                if err:
                    return None, err
                buy_result, err = await self.buy(proposal['id'], stake)
                if err:
                    return None, err
            else:
                return None, err
        
        cid = buy_result.get('contract_id')
        profit, details = await self.wait_result(cid)
        return profit, details

