"""Shared infrastructure used by every other package: settings, the Claude
client wrapper, and logging setup. Nothing here talks to external data
sources (SEC, FRED, yfinance) or the Evidence Store — see backend/data/ and
backend/evidence/ for those, once they exist.
"""
