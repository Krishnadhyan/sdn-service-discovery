#!/usr/bin/env python3
"""Launch this checkout's controller application through OS-Ken."""
import gc
from pathlib import Path
import sys

from os_ken.base.app_manager import AppManager
from os_ken.lib import hub


def controller_application_path():
    """Resolve the controller beside this launcher, independent of cwd."""
    return Path(__file__).resolve().with_name("controller.py")


def load_controller_application(app_mgr):
    """Tell OS-Ken to load the exact project file, not a cwd module name."""
    source = controller_application_path()
    if not source.is_file():
        raise RuntimeError(f"Controller application not found: {source}")
    existing = sys.modules.get("controller")
    existing_file = getattr(existing, "__file__", None) if existing else None
    if existing_file and Path(existing_file).resolve() != source:
        del sys.modules["controller"]
    app_mgr.load_apps([str(source)])
    loaded = sys.modules.get("controller")
    loaded_file = getattr(loaded, "__file__", None) if loaded else None
    if not loaded_file or Path(loaded_file).resolve() != source:
        raise RuntimeError(
            f"OS-Ken loaded {loaded_file!r}, expected controller source {source}"
        )
    print(f"Controller source loaded: {source}", flush=True)
    return source


def main():
    print(f"Controller launcher: {Path(__file__).resolve()}", flush=True)
    print(f"Python executable: {sys.executable}", flush=True)
    print(f"Working directory: {Path.cwd()}", flush=True)
    app_mgr = AppManager.get_instance()
    load_controller_application(app_mgr)
    contexts = app_mgr.create_contexts()
    services = app_mgr.instantiate_apps(**contexts)

    try:
        hub.joinall(services)
    finally:
        app_mgr.close()
        hub.joinall(services)
        gc.collect()


if __name__ == "__main__":
    main()
