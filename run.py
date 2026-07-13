#!/usr/bin/env python3
"""
AMTO - Autonomous Multi-Agent Trading Orchestrator
Entry point. Activate venv first, then: python run.py
"""
import asyncio
import sys

def main():
    try:
        from orchestrator import main as run_amto
        asyncio.run(run_amto())
    except KeyboardInterrupt:
        print("\n[AMTO] Interrupted by user")
    except Exception as e:
        print(f"\n[AMTO] Fatal error: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()
