"""Streamlit entrypoint for the 1C configurator agent UI."""

from app.ui import run_app


def main() -> None:
    run_app()


if __name__ == "__main__":
    main()
