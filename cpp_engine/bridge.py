"""Bridge between Python brain and C++ quant engine (server mode)."""
import subprocess, json, threading, time, os, select

class CppEngine:
    def __init__(self):
        self.proc = None
        self.connected = False
        self.stats = {'trades': 0, 'accuracy': 0, 'pnl': 0, 'buffer': 0}
        self.last_prediction = None
        self.lock = threading.Lock()
    
    def start(self):
        engine_path = os.path.join(os.path.dirname(__file__), 'server')
        if not os.path.exists(engine_path):
            return False
        try:
            self.proc = subprocess.Popen(
                [engine_path],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, bufsize=1
            )
            self.connected = True
            # Drain any startup messages from stderr
            self._drain_stderr()
            # Send initial stats with timeout
            result = self._send({'cmd': 'stats'}, timeout=5)
            if result:
                self.stats = result
            return True
        except Exception as e:
            print(f"  [C++ ENGINE] Start failed: {e}")
            return False
    
    def _drain_stderr(self):
        """Drain stderr non-blocking to prevent buffer fill."""
        if not self.proc:
            return
        try:
            import fcntl
            flags = fcntl.fcntl(self.proc.stderr, fcntl.F_GETFL)
            fcntl.fcntl(self.proc.stderr, fcntl.F_SETFL, flags | os.O_NONBLOCK)
        except:
            pass
        try:
            self.proc.stderr.read()
        except:
            pass
    
    def _send(self, cmd, timeout=5):
        if not self.connected or not self.proc:
            return None
        if self.proc.poll() is not None:
            self.connected = False
            return None
        try:
            with self.lock:
                self.proc.stdin.write(json.dumps(cmd) + '\n')
                self.proc.stdin.flush()
                # Read with timeout using select
                ready, _, _ = select.select([self.proc.stdout], [], [], timeout)
                if ready:
                    line = self.proc.stdout.readline().strip()
                    if line:
                        return json.loads(line)
                else:
                    print(f"  [C++ ENGINE] Timeout on: {cmd.get('cmd','?')}")
        except Exception as e:
            print(f"  [C++ ENGINE] Error: {e}")
            self.connected = False
        return None
    
    def tick(self, price, epoch):
        result = self._send({'cmd': 'tick', 'price': price, 'epoch': epoch}, timeout=3)
        if result and 'signal' in result:
            self.last_prediction = result
        return result
    
    def predict(self):
        result = self._send({'cmd': 'predict'}, timeout=3)
        if result:
            self.last_prediction = result
        return result
    
    def learn(self, profit, stake):
        return self._send({'cmd': 'learn', 'profit': profit, 'stake': stake}, timeout=3)
    
    def get_stats(self):
        result = self._send({'cmd': 'stats'}, timeout=3)
        if result:
            self.stats = result
        return self.stats
    
    def save(self):
        return self._send({'cmd': 'save'}, timeout=5)
    
    def get_status(self):
        return {
            'connected': self.connected,
            'trades_learned': self.stats.get('trades', 0),
            'accuracy': self.stats.get('accuracy', 0),
            'pnl': self.stats.get('pnl', 0),
            'buffer_size': self.stats.get('buffer', 0),
            'last_prediction': self.last_prediction,
        }
    
    def shutdown(self):
        if self.proc:
            try:
                self._send({'cmd': 'quit'}, timeout=3)
                self.proc.terminate()
            except: pass
            self.connected = False
