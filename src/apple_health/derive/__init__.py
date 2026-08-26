"""Derivations over stored primitives.

Nothing here is persisted. Zone distributions, drift and cadence are
computed on read because the models behind them are dated and
revisable — see docs/adr-006-sinks-are-plugins.md.
"""
