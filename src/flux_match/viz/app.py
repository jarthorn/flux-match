"""Streamlit dashboard for a single run.

Usage: bin/viz output/<run-folder>
"""

import sys
from pathlib import Path

import streamlit as st

from flux_match.errors import FluxMatchError
from flux_match.run_context import open_run


def main() -> None:
    if len(sys.argv) != 2:
        st.error("Usage: bin/viz output/<run-folder>")
        st.stop()
    try:
        ctx = open_run(Path(sys.argv[1]))
        ctx.require_complete("dispatch")
    except FluxMatchError as e:
        st.error(str(e))
        st.stop()

    st.title(f"Flux Match: {ctx.run_dir.name}")
    st.info("Dashboard not implemented yet.")


if __name__ == "__main__":
    main()
