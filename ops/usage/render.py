#!/usr/bin/env python3
"""Compatibility entry point for the shared-theme costs preview."""
import subprocess
from pathlib import Path
subprocess.run(['bun', 'ops/usage/preview.ts'], cwd=Path(__file__).resolve().parents[2], check=True)
