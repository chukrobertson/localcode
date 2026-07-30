from __future__ import annotations

import sys

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gio  # noqa: E402

from .paths import APP_ID, ensure_app_dirs
from .ui import MainWindow


class LocalCodeApplication(Adw.Application):
    def __init__(self) -> None:
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.DEFAULT_FLAGS)
        self.window: MainWindow | None = None

    def do_activate(self) -> None:
        if self.window is None:
            self.window = MainWindow(self)
        self.window.present()


def main() -> int:
    ensure_app_dirs()
    application = LocalCodeApplication()
    application.set_accels_for_action("win.preferences", ["<Primary>comma"])
    application.set_accels_for_action("win.add-project", ["<Primary>o"])
    return application.run(sys.argv)


if __name__ == "__main__":
    raise SystemExit(main())
