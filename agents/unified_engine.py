"""Unified Brain Engine — dual AI backend with auto-fallback and smart rotation.
Wraps both Ollama (local) and OpenRouter (cloud) into one interface.
Auto-switches on failure, rotates when system decides.
"""
import json, time, os, urllib.request, urllib.error
from pathlib import Path


class UnifiedEngine:
    """Dual-engine: Ollama + OpenRouter with fallback and rotation."""

    ENGINE_OLLAMA = 'ollama'
    ENGINE_OPENROUTER = 'openrouter'

    def __init__(self, openrouter_key=None):
        self.openrouter_key = openrouter_key or os.environ.get('OPENROUTER_API_KEY', '')
        self.or_url = 'https://openrouter.ai/api/v1/chat/completions'

        # Engine state
        self.active_engine = self.ENGINE_OPENROUTER  # force OpenRouter (gemma)
        self.ollama_model = 'minimax-m2.5:cloud'
        self.ollama_url = 'http://localhost:11434/api/generate'
        self.openrouter_model = 'google/gemma-4-26b-a4b-it:free'
        self.or_model_index = 0

        # Fallback / rotation
        self.ollama_consec_fails = 0
        self.or_consec_fails = 0
        self.ollama_enabled = True
        self.or_enabled = bool(self.openrouter_key)
        self.connected = self.ollama_enabled or self.or_enabled
        self.rotation_mode = 'smart'  # smart, ollama_first, or_first, round_robin
        self.last_switch_time = 0
        self.switch_cooldown = 30  # seconds between switches

        # Per-engine stats
        self.engines = {
            self.ENGINE_OLLAMA: {
                'name': 'Ollama', 'queries': 0, 'successes': 0, 'failures': 0,
                'total_tokens': 0, 'total_latency': 0, 'avg_latency': 0,
                'consec_fails': 0, 'last_result': 'none', 'last_error': '',
                'trade_pnl': 0, 'trade_wins': 0, 'trade_losses': 0, 'score': 50,
            },
            self.ENGINE_OPENROUTER: {
                'name': 'OpenRouter', 'queries': 0, 'successes': 0, 'failures': 0,
                'total_tokens': 0, 'total_latency': 0, 'avg_latency': 0,
                'consec_fails': 0, 'last_result': 'none', 'last_error': '',
                'trade_pnl': 0, 'trade_wins': 0, 'trade_losses': 0, 'score': 50,
            },
        }

        # OpenRouter model pool
        self.or_models = [
            'google/gemma-4-26b-a4b-it:free',
            'tencent/hy3:free',
            'nvidia/nemotron-3-ultra-550b-a55b:free',
            'nvidia/nemotron-3-super-120b-a12b:free',
            'meta-llama/llama-3.3-70b-instruct:free',
            'nousresearch/hermes-3-llama-3.1-405b:free',
            'qwen/qwen3-coder:free',
            'qwen/qwen3-next-80b-a3b-instruct:free',
            'google/gemma-4-31b-it:free',
            'openai/gpt-oss-20b:free',
        ]

        # Query log
        self.query_log = []
        self.daily_queries = 0
        self.daily_tokens = 0
        self.daily_reset = 0
        # Per-model stats for OpenRouter pool
        self.or_model_stats = {m: {"calls": 0, "tokens": 0, "successes": 0, "failures": 0, "avg_latency": 0, "pnl": 0} for m in self.or_models}
        self.rpm_limit = 20
        self.last_minute_queries = []

        # Load persisted stats
        self._load_stats()

    def _stats_file(self):
        return Path(__file__).parent.parent / 'model_benchmarks.json'

    def _load_stats(self):
        f = self._stats_file()
        if f.exists():
            try:
                data = json.loads(f.read_text())
                for eng_name in (self.ENGINE_OLLAMA, self.ENGINE_OPENROUTER):
                    if eng_name in data.get('engines', {}):
                        self.engines[eng_name].update(data['engines'][eng_name])
                self.rotation_mode = data.get('rotation_mode', 'smart')
            except:
                pass

    def _save_stats(self):
        try:
            self._stats_file().write_text(json.dumps({
                'engines': {k: {kk: vv for kk, vv in v.items()} for k, v in self.engines.items()},
                'rotation_mode': self.rotation_mode,
            }, indent=1))
        except:
            pass

    def _update_score(self, eng_name):
        e = self.engines[eng_name]
        q = e['queries']
        if q == 0:
            return
        success_rate = e['successes'] / q
        avg_lat = e['avg_latency'] or 5
        tw = e.get('trade_wins', 0)
        tl = e.get('trade_losses', 0)
        trade_wr = tw / max(tw + tl, 1)
        speed_score = max(0, 100 - (avg_lat * 15))
        trade_score = trade_wr * 100
        e['score'] = round((success_rate * 40) + (min(speed_score, 100) * 0.3) + (trade_score * 0.3), 1)

    def _pick_engine(self):
        """Smart engine selection: prefer higher score, auto-fallback."""
        now = time.time()

        # If both disabled, nothing to do
        if not self.ollama_enabled and not self.or_enabled:
            return self.ENGINE_OLLAMA

        # Check if we should switch
        should_switch = False
        current = self.engines[self.active_engine]

        # Switch if current engine has 3+ consecutive failures
        if current['consec_fails'] >= 3:
            should_switch = True

        # Switch if current engine score is low and other is higher
        if current['queries'] >= 3 and current['score'] < 35:
            other_name = self.ENGINE_OPENROUTER if self.active_engine == self.ENGINE_OLLAMA else self.ENGINE_OLLAMA
            other = self.engines[other_name]
            if other['score'] > current['score'] + 15:
                should_switch = True

        # Cooldown check
        if should_switch and (now - self.last_switch_time) < self.switch_cooldown:
            should_switch = False

        if should_switch:
            self._switch_engine()
            self.last_switch_time = now

        return self.active_engine

    def _switch_engine(self):
        """Switch to the other engine."""
        if self.active_engine == self.ENGINE_OLLAMA:
            if self.or_enabled:
                self.active_engine = self.ENGINE_OPENROUTER
        else:
            if self.ollama_enabled:
                self.active_engine = self.ENGINE_OLLAMA

    def _record_success(self, eng_name, latency, tokens):
        e = self.engines[eng_name]
        e['successes'] += 1
        e['queries'] += 1
        e['total_tokens'] += tokens
        e['total_latency'] += latency
        e['avg_latency'] = round(e['total_latency'] / e['queries'], 2)
        e['consec_fails'] = 0
        e['last_result'] = 'success'
        self._update_score(eng_name)
        self._save_stats()

    def _record_failure(self, eng_name, error=''):
        e = self.engines[eng_name]
        e['failures'] += 1
        e['queries'] += 1
        e['consec_fails'] += 1
        e['last_result'] = 'fail'
        e['last_error'] = error[:100]
        self._update_score(eng_name)
        self._save_stats()

    def _rate_limit_check(self):
        now = time.time()
        if now - self.daily_reset > 86400:
            self.daily_queries = 0
            self.daily_tokens = 0
            self.daily_reset = now
        self.last_minute_queries = [t for t in self.last_minute_queries if now - t < 60]
        if len(self.last_minute_queries) >= self.rpm_limit:
            return False, "RPM limit"
        return True, "ok"

    def benchmark_engine(self, eng_name):
        """Quick benchmark: send a simple query, score based on speed + result."""
        # Removed socket.setdefaulttimeout — it kills global WebSocket connections
        test_prompt = "Say OK in exactly 2 words. Nothing else."
        for attempt in range(min(3, len(self.or_models))):
            if eng_name == self.ENGINE_OLLAMA:
                text, err, lat, tokens = self._query_ollama(test_prompt, None, 200)
                if err is None:
                    s = self.engines[self.ENGINE_OLLAMA]
                    s['queries'] += 1
                    s['successes'] += 1
                    s['total_latency'] += lat
                    s['avg_latency'] = s['total_latency'] / s['queries']
                    self._update_score(self.ENGINE_OLLAMA)
                    self._save_stats()
                    print(f"  [BENCH] Ollama: {lat:.1f}s score={s['score']:.0f} resp={'OK' if text else 'empty'}")
                    return s['score']
                else:
                    print(f"  [BENCH] Ollama failed: {err}")
                    return 0
            elif eng_name == self.ENGINE_OPENROUTER:
                if not self.or_enabled:
                    return 0
                model = self.or_models[attempt % len(self.or_models)]
                try:
                    messages = [{"role": "user", "content": test_prompt}]
                    payload = json.dumps({"model": model, "messages": messages, "max_tokens": 10, "temperature": 0.3}).encode()
                    req = urllib.request.Request(self.or_url, data=payload, headers={
                        'Content-Type': 'application/json',
                        'Authorization': f'Bearer {self.openrouter_key}',
                    })
                    start = time.time()
                    resp = urllib.request.urlopen(req, timeout=15)
                    lat = time.time() - start
                    data = json.loads(resp.read().decode())
                    text = data['choices'][0]['message']['content']
                    if text:
                        s = self.engines[self.ENGINE_OPENROUTER]
                        s['queries'] += 1
                        s['successes'] += 1
                        s['total_latency'] += lat
                        s['avg_latency'] = s['total_latency'] / s['queries']
                        self._update_score(self.ENGINE_OPENROUTER)
                        ms = self.or_model_stats.setdefault(model, {"calls": 0, "tokens": 0, "successes": 0, "failures": 0, "avg_latency": 0, "pnl": 0})
                        ms["calls"] += 1
                        ms["tokens"] += data.get('usage', {}).get('total_tokens', 0)
                        ms["successes"] += 1
                        ms["avg_latency"] = (ms["avg_latency"] * (ms["calls"] - 1) + lat) / ms["calls"]
                        self._save_stats()
                        self.openrouter_model = model
                        self.or_model_index = self.or_models.index(model) if model in self.or_models else 0
                        print(f"  [BENCH] OpenRouter ({model.split('/')[-1][:20]}): {lat:.1f}s score={s['score']:.0f}")
                        return s['score']
                except:
                    continue
            return 0

    def query(self, prompt, system_prompt=None, max_tokens=500):
        """Send query with auto-fallback between engines."""
        ok, reason = self._rate_limit_check()
        if not ok:
            return None, reason

        engine = self._pick_engine()

        # Try primary, then fallback
        engines_to_try = [engine]
        fallback = self.ENGINE_OPENROUTER if engine == self.ENGINE_OLLAMA else self.ENGINE_OLLAMA
        if (fallback == self.ENGINE_OPENROUTER and self.or_enabled) or \
           (fallback == self.ENGINE_OLLAMA and self.ollama_enabled):
            engines_to_try.append(fallback)

        for eng in engines_to_try:
            if eng == self.ENGINE_OLLAMA:
                text, err, latency, tokens = self._query_ollama(prompt, system_prompt, max_tokens)
            else:
                text, err, latency, tokens = self._query_openrouter(prompt, system_prompt, max_tokens)

            if text is not None:
                self._record_success(eng, latency, tokens)
                self.daily_queries += 1
                self.daily_tokens += tokens
                self.last_minute_queries.append(time.time())

                self.query_log.append({
                    'time': int(time.time() * 1000),
                    'engine': eng,
                    'model': self.ollama_model if eng == self.ENGINE_OLLAMA else self.openrouter_model,
                    'tokens': tokens,
                    'elapsed': round(latency, 2),
                    'prompt_preview': prompt[:80],
                    'response_preview': text[:100],
                })
                if len(self.query_log) > 30:
                    self.query_log = self.query_log[-30:]
                return text, None
            else:
                self._record_failure(eng, err)

        # All engines failed
        return None, "All engines failed"

    def _query_ollama(self, prompt, system_prompt, max_tokens):
        """Query Ollama local/cloud model."""
        try:
            full_prompt = prompt
            if system_prompt:
                full_prompt = system_prompt + "\n\n" + prompt

            payload = json.dumps({
                "model": self.ollama_model,
                "prompt": full_prompt,
                "stream": False,
                "options": {"num_predict": max_tokens, "temperature": 0.3},
            }).encode()

            req = urllib.request.Request(self.ollama_url, data=payload,
                headers={'Content-Type': 'application/json'})
            start = time.time()
            resp = urllib.request.urlopen(req, timeout=8)
            elapsed = time.time() - start
            data = json.loads(resp.read().decode())
            text = (data.get('response', '') or '').strip()
            tokens = data.get('eval_count', len(text.split()))
            # Remove trailing chars that aren't meaningful
            if text:
                # Strip trailing period/whitespace
                text = text.rstrip('. \n\r\t')
            return text, None, elapsed, tokens
        except Exception as e:
            return None, str(e)[:100], 0, 0

    def _query_openrouter(self, prompt, system_prompt, max_tokens):
        """Query OpenRouter free models with rotation."""
        for attempt in range(min(5, len(self.or_models))):
            model = self.or_models[(self.or_model_index + attempt) % len(self.or_models)]
            try:
                messages = []
                if system_prompt:
                    messages.append({"role": "system", "content": system_prompt})
                messages.append({"role": "user", "content": prompt})

                payload = json.dumps({
                    "model": model,
                    "messages": messages,
                    "max_tokens": max_tokens,
                    "temperature": 0.3,
                }).encode()

                req = urllib.request.Request(self.or_url, data=payload, headers={
                    'Content-Type': 'application/json',
                    'Authorization': f'Bearer {self.openrouter_key}',
                    'HTTP-Referer': 'https://ad-smta.local',
                    'X-Title': 'AD-SMTA Trading Brain',
                })
                start = time.time()
                resp = urllib.request.urlopen(req, timeout=20)
                elapsed = time.time() - start
                data = json.loads(resp.read().decode())
                text = data['choices'][0]['message']['content']
                usage = data.get('usage', {})
                tokens = usage.get('total_tokens', 0)
                self.openrouter_model = model
                self.or_model_index = self.or_models.index(model) if model in self.or_models else 0
                return text, None, elapsed, tokens
            except urllib.error.HTTPError as e:
                if e.code in (429, 404):
                    self.or_model_index = (self.or_model_index + 1) % len(self.or_models)
                    time.sleep(0.5)
                    continue
                return None, f"HTTP {e.code}", 0, 0
            except Exception as e:
                return None, str(e)[:100], 0, 0

        return None, "All OR models failed", 0, 0

    def notify_trade_result(self, profit):
        """Notify both engines of trade outcome."""
        eng = self.engines[self.active_engine]
        eng.setdefault('trade_pnl', 0)
        eng.setdefault('trade_wins', 0)
        eng.setdefault('trade_losses', 0)
        eng['trade_pnl'] = round(eng.get('trade_pnl', 0) + profit, 2)
        if profit > 0:
            eng['trade_wins'] += 1
        else:
            eng['trade_losses'] += 1
        self._update_score(self.active_engine)
        self._save_stats()

    def get_status(self):
        ollama_e = self.engines[self.ENGINE_OLLAMA]
        or_e = self.engines[self.ENGINE_OPENROUTER]

        return {
            'connected': self.ollama_enabled or self.or_enabled,
            'active_engine': self.active_engine,
            'model': self.ollama_model if self.active_engine == self.ENGINE_OLLAMA else self.openrouter_model,
            'provider': self.active_engine,

            # Ollama
            'ollama_connected': self.ollama_enabled,
            'ollama_model': self.ollama_model,
            'ollama_queries': ollama_e['queries'],
            'ollama_success_rate': round(ollama_e['successes'] / max(ollama_e['queries'], 1) * 100, 1),
            'ollama_avg_latency': ollama_e['avg_latency'],
            'ollama_score': ollama_e['score'],
            'ollama_trade_pnl': ollama_e.get('trade_pnl', 0),
            'ollama_trade_wr': round(ollama_e.get('trade_wins', 0) / max(ollama_e.get('trade_wins', 0) + ollama_e.get('trade_losses', 0), 1) * 100, 1),

            # OpenRouter
            'or_connected': self.or_enabled,
            'or_model': self.openrouter_model,
            'or_queries': or_e['queries'],
            'or_success_rate': round(or_e['successes'] / max(or_e['queries'], 1) * 100, 1),
            'or_avg_latency': or_e['avg_latency'],
            'or_score': or_e['score'],
            'or_trade_pnl': or_e.get('trade_pnl', 0),
            'or_trade_wr': round(or_e.get('trade_wins', 0) / max(or_e.get('trade_wins', 0) + or_e.get('trade_losses', 0), 1) * 100, 1),

            # Combined
            'total_queries': ollama_e['queries'] + or_e['queries'],
            'total_tokens': ollama_e['total_tokens'] + or_e['total_tokens'],
            'rotation_mode': self.rotation_mode,
            'last_switch': self.last_switch_time,
            'consec_fails': max(ollama_e['consec_fails'], or_e['consec_fails']),

            # Model pool
            'or_models': self.or_models,
            'or_active_model': self.openrouter_model,
            'or_model_index': self.or_model_index,
            'or_model_stats': {k.split('/')[-1].replace(':free',''): dict(v) for k, v in self.or_model_stats.items()},
            'daily_queries': self.daily_queries,
            'daily_tokens': self.daily_tokens,
            # Logs
            'query_log': self.query_log[-15:],
            'engines': {k: dict(v) for k, v in self.engines.items()},
            
            # Model benchmarks (for dashboard chart)
            'model_benchmarks': {
                'current_model': self.ollama_model if self.active_engine == self.ENGINE_OLLAMA else self.openrouter_model,
                'ranked': [
                    {
                        'model': v.get('name', k),
                        'full_name': self.ollama_model if k == self.ENGINE_OLLAMA else self.openrouter_model,
                        'queries': v.get('queries', 0),
                        'successes': v.get('successes', 0),
                        'failures': v.get('failures', 0),
                        'success_rate': round(v.get('successes', 0) / max(v.get('queries', 1), 1) * 100, 1),
                        'avg_latency': round(v.get('avg_latency', 0), 2),
                        'avg_tokens': round(v.get('total_tokens', 0) / max(v.get('queries', 1), 1)),
                        'score': v.get('score', 50),
                        'trade_pnl': round(v.get('trade_pnl', 0), 2),
                        'trade_wr': round(v.get('trade_wins', 0) / max(v.get('trade_wins', 0) + v.get('trade_losses', 0), 1) * 100, 1),
                        'last_result': v.get('last_result', 'none'),
                    }
                    for k, v in sorted(self.engines.items(), key=lambda x: -x[1].get('score', 0))
                ],
            },
        }
