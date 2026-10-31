#!/usr/bin/env python3
# Copyright (c) 2026 Maksim Nagaev
# SPDX-License-Identifier: Apache-2.0
"""Compatibility entry point. The portable viewer is espmirror.py."""
from espmirror import main

if __name__ == '__main__':
    raise SystemExit(main())
