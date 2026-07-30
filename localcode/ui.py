from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gdk, Gio, GLib, Gtk, Pango  # noqa: E402

from .agent import AgentCallbacks, AgentRunner
from .agents_file import AgentsFileManager
from .context import make_report
from .database import Database, utc_now
from .memory import MemPalaceManager
from .models import Chat, ContextReport, Project
from .ollama import ModelInfo, OllamaClient
from .paths import APP_NAME, PACKAGE_ROOT, transcript_dir
from .settings import AppSettings
from .widgets import (
    ActivityRow,
    ChatRow,
    Composer,
    ContextMeter,
    MessageBubble,
    clear_box,
    widget_for_activity,
    widget_for_message,
)


class MainWindow(Adw.ApplicationWindow):
    def __init__(self, application: Adw.Application) -> None:
        super().__init__(application=application)
        self.set_title(APP_NAME)
        self.set_default_size(1280, 820)
        self.set_size_request(540, 520)

        self.database = Database()
        self.settings = AppSettings(self.database)
        self.memory = MemPalaceManager()
        self.runner = AgentRunner(self.database, self.settings, self.memory)

        self.projects: list[Project] = []
        self.chats: list[Chat] = []
        self.models: list[ModelInfo] = []
        self.current_project: Project | None = None
        self.current_chat: Chat | None = None
        self.worker: threading.Thread | None = None
        self.streaming_bubble: MessageBubble | None = None
        self.activity_rows: dict[tuple[str, str], ActivityRow] = {}
        self._updating_projects = False
        self._updating_models = False
        self._running_chat_id = ""
        self._banner_persistent = False
        self._force_close = False
        self._close_polling = False
        self._background_jobs: set[threading.Thread] = set()

        self._install_actions()
        self._build_ui()
        self.connect("close-request", self._on_close_request)
        self._load_css()
        self._refresh_projects()
        self._refresh_models_async()

    def _build_ui(self) -> None:
        self.toast_overlay = Adw.ToastOverlay()
        self.split_view = Adw.NavigationSplitView()
        self.split_view.set_sidebar_width_fraction(0.25)
        self.split_view.set_min_sidebar_width(260)
        self.split_view.set_max_sidebar_width(340)

        self.sidebar_shell = self._build_sidebar()
        self.split_view.set_sidebar(Adw.NavigationPage.new(self.sidebar_shell, "Workspace"))
        self.split_view.set_content(Adw.NavigationPage.new(self._build_content(), "Project chat"))
        self.toast_overlay.set_child(self.split_view)
        self.set_content(self.toast_overlay)

        breakpoint = Adw.Breakpoint.new(Adw.BreakpointCondition.parse("max-width: 760sp"))
        breakpoint.add_setter(self.split_view, "collapsed", True)
        self.add_breakpoint(breakpoint)
        compact_header = Adw.Breakpoint.new(Adw.BreakpointCondition.parse("max-width: 1100sp"))
        compact_header.add_setter(self.model_dropdown, "visible", False)
        self.add_breakpoint(compact_header)

    def _build_sidebar(self) -> Gtk.Widget:
        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        header.set_title_widget(Adw.WindowTitle.new("LocalCode", "On-device workspace"))

        add_project = Gtk.Button.new_from_icon_name("folder-new-symbolic")
        add_project.add_css_class("flat")
        add_project.set_tooltip_text("Add an existing project")
        add_project.connect("clicked", lambda _button: self._choose_project())
        header.pack_end(add_project)
        toolbar.add_top_bar(header)

        panel = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        panel.add_css_class("sidebar-panel")

        project_area = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=7)
        project_area.set_margin_top(12)
        project_area.set_margin_bottom(12)
        project_area.set_margin_start(12)
        project_area.set_margin_end(12)
        project_heading = Gtk.Label(label="PROJECT", xalign=0)
        project_heading.add_css_class("section-kicker")
        project_area.append(project_heading)

        project_picker = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.project_store = Gtk.StringList.new([])
        self.project_dropdown = Gtk.DropDown.new(self.project_store, None)
        self.project_dropdown.set_enable_search(True)
        self.project_dropdown.set_hexpand(True)
        self.project_dropdown.connect("notify::selected", self._on_project_selected)
        project_picker.append(self.project_dropdown)

        project_menu = Gtk.MenuButton(icon_name="view-more-symbolic")
        project_menu.add_css_class("flat")
        menu = Gio.Menu()
        menu.append("Open Project Folder", "win.open-project-folder")
        menu.append("Sync Memory Now", "win.sync-memory")
        menu.append("Forget Project", "win.forget-project")
        project_menu.set_menu_model(menu)
        project_picker.append(project_menu)
        project_area.append(project_picker)
        panel.append(project_area)

        divider = Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL)
        panel.append(divider)

        chats_header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        chats_header.set_margin_top(12)
        chats_header.set_margin_bottom(7)
        chats_header.set_margin_start(12)
        chats_header.set_margin_end(8)
        chats_label = Gtk.Label(label="CHATS", xalign=0, hexpand=True)
        chats_label.add_css_class("section-kicker")
        chats_header.append(chats_label)
        new_chat = Gtk.Button.new_from_icon_name("list-add-symbolic")
        new_chat.add_css_class("flat")
        new_chat.add_css_class("circular")
        new_chat.set_tooltip_text("New chat")
        new_chat.connect("clicked", lambda _button: self._new_chat())
        chats_header.append(new_chat)
        panel.append(chats_header)

        chat_scroll = Gtk.ScrolledWindow(vexpand=True)
        chat_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.chat_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.SINGLE)
        self.chat_list.add_css_class("navigation-sidebar")
        self.chat_list.connect("row-selected", self._on_chat_selected)
        chat_scroll.set_child(self.chat_list)
        panel.append(chat_scroll)

        footer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        footer.set_margin_top(10)
        footer.set_margin_bottom(10)
        footer.set_margin_start(12)
        footer.set_margin_end(12)
        footer.add_css_class("sidebar-footer")

        self.phase_label = Gtk.Label(label="Ready", xalign=0, ellipsize=Pango.EllipsizeMode.END)
        self.phase_label.add_css_class("caption")
        footer.append(self.phase_label)

        memory_button = Gtk.Button()
        memory_button.add_css_class("flat")
        memory_button.connect("clicked", lambda _button: self._show_preferences())
        memory_content = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=7)
        self.memory_icon = Gtk.Image.new_from_icon_name("folder-saved-search-symbolic")
        self.memory_icon.set_pixel_size(15)
        memory_content.append(self.memory_icon)
        self.memory_label = Gtk.Label(label="Checking local memory...", xalign=0, hexpand=True)
        self.memory_label.add_css_class("caption")
        memory_content.append(self.memory_label)
        memory_button.set_child(memory_content)
        footer.append(memory_button)
        panel.append(footer)

        toolbar.set_content(panel)
        return toolbar

    def _build_content(self) -> Gtk.Widget:
        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        self.content_header = header
        self.content_title = Adw.WindowTitle.new("LocalCode", "Choose a project")
        header.set_title_widget(self.content_title)

        self.sidebar_toggle = Gtk.Button.new_from_icon_name("sidebar-show-symbolic")
        self.sidebar_toggle.add_css_class("flat")
        self.sidebar_toggle.set_tooltip_text("Show projects and chats")
        self.sidebar_toggle.connect(
            "clicked", lambda _button: self.split_view.set_show_content(False)
        )
        header.pack_start(self.sidebar_toggle)

        self.model_store = Gtk.StringList.new([])
        self.model_dropdown = Gtk.DropDown.new(self.model_store, None)
        self.model_dropdown.set_enable_search(True)
        self.model_dropdown.set_size_request(180, -1)
        self.model_dropdown.set_tooltip_text("Ollama model for this chat")
        self.model_dropdown.connect("notify::selected", self._on_model_selected)
        header.pack_end(self.model_dropdown)

        self.context_meter = ContextMeter()
        header.pack_end(self.context_meter)

        self.stop_button = Gtk.Button.new_from_icon_name("media-playback-stop-symbolic")
        self.stop_button.add_css_class("destructive-action")
        self.stop_button.add_css_class("circular")
        self.stop_button.set_tooltip_text("Stop generation")
        self.stop_button.set_visible(False)
        self.stop_button.connect("clicked", lambda _button: self.runner.cancel())
        header.pack_end(self.stop_button)

        app_menu = Gtk.MenuButton(icon_name="open-menu-symbolic")
        app_menu.add_css_class("flat")
        menu = Gio.Menu()
        menu.append("Preferences", "win.preferences")
        menu.append("Add Project", "win.add-project")
        menu.append("About LocalCode", "win.about")
        app_menu.set_menu_model(menu)
        header.pack_end(app_menu)
        toolbar.add_top_bar(header)

        self.content_stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE)
        self.content_stack.add_named(self._build_welcome(), "welcome")
        self.content_stack.add_named(self._build_chat_view(), "chat")
        self.content_stack.set_visible_child_name("welcome")
        toolbar.set_content(self.content_stack)
        return toolbar

    def _build_welcome(self) -> Gtk.Widget:
        clamp = Adw.Clamp(maximum_size=760, tightening_threshold=560)
        welcome = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        welcome.set_valign(Gtk.Align.CENTER)
        welcome.set_margin_top(36)
        welcome.set_margin_bottom(36)
        welcome.set_margin_start(30)
        welcome.set_margin_end(30)
        welcome.add_css_class("welcome-panel")

        mark = Gtk.Label(label="</>")
        mark.add_css_class("welcome-mark")
        welcome.append(mark)
        title = Gtk.Label(
            label="Keep the code.\nCompress the conversation.", justify=Gtk.Justification.CENTER
        )
        title.add_css_class("welcome-title")
        welcome.append(title)
        body = Gtk.Label(
            label=(
                "A private coding workspace for Ollama. Every transcript stays local, "
                "context pressure stays visible, and project knowledge survives compaction."
            ),
            wrap=True,
            justify=Gtk.Justification.CENTER,
            max_width_chars=62,
        )
        body.add_css_class("welcome-copy")
        body.add_css_class("dim-label")
        welcome.append(body)
        add = Gtk.Button(label="Add a Project")
        add.set_halign(Gtk.Align.CENTER)
        add.add_css_class("suggested-action")
        add.add_css_class("pill")
        add.connect("clicked", lambda _button: self._choose_project())
        welcome.append(add)

        privacy = Gtk.Label(label="No account · No telemetry · Localhost by default")
        privacy.add_css_class("caption")
        privacy.add_css_class("dim-label")
        welcome.append(privacy)
        clamp.set_child(welcome)
        return clamp

    def _build_chat_view(self) -> Gtk.Widget:
        layout = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)

        self.banner = Gtk.Revealer(transition_type=Gtk.RevealerTransitionType.SLIDE_DOWN)
        self.banner_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        self.banner_box.add_css_class("context-banner")
        self.banner_box.set_margin_top(10)
        self.banner_box.set_margin_start(18)
        self.banner_box.set_margin_end(18)
        self.banner_icon = Gtk.Image.new_from_icon_name("dialog-information-symbolic")
        self.banner_box.append(self.banner_icon)
        banner_labels = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1, hexpand=True)
        self.banner_title = Gtk.Label(label="", xalign=0)
        self.banner_title.add_css_class("heading")
        banner_labels.append(self.banner_title)
        self.banner_body = Gtk.Label(label="", xalign=0, wrap=True)
        self.banner_body.add_css_class("caption")
        banner_labels.append(self.banner_body)
        self.banner_box.append(banner_labels)
        close = Gtk.Button.new_from_icon_name("window-close-symbolic")
        close.add_css_class("flat")
        close.add_css_class("circular")
        close.connect("clicked", lambda _button: self._dismiss_banner())
        self.banner_box.append(close)
        self.banner.set_child(self.banner_box)
        layout.append(self.banner)

        self.message_scroll = Gtk.ScrolledWindow(vexpand=True)
        self.message_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        message_clamp = Adw.Clamp(maximum_size=980, tightening_threshold=700)
        self.message_feed = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        self.message_feed.set_margin_top(22)
        self.message_feed.set_margin_bottom(26)
        self.message_feed.set_margin_start(22)
        self.message_feed.set_margin_end(22)
        message_clamp.set_child(self.message_feed)
        self.message_scroll.set_child(message_clamp)
        layout.append(self.message_scroll)

        composer_clamp = Adw.Clamp(maximum_size=980, tightening_threshold=700)
        self.composer = Composer(self._send_message)
        self.composer.set_margin_start(18)
        self.composer.set_margin_end(18)
        self.composer.set_margin_bottom(18)
        self.composer.mode.connect("notify::selected", self._on_permission_selected)
        composer_clamp.set_child(self.composer)
        layout.append(composer_clamp)
        return layout

    def _refresh_projects(self, select_id: str = "") -> None:
        previous_id = select_id or (self.current_project.id if self.current_project else "")
        self.projects = self.database.list_projects()
        self._updating_projects = True
        self.project_store.splice(
            0, self.project_store.get_n_items(), [p.name for p in self.projects]
        )
        self.project_dropdown.set_sensitive(bool(self.projects))
        selected = next((index for index, p in enumerate(self.projects) if p.id == previous_id), 0)
        if self.projects:
            self.project_dropdown.set_selected(selected)
        self._updating_projects = False
        if self.projects:
            self._select_project(self.projects[selected])
        else:
            self.current_project = None
            self.current_chat = None
            clear_box(self.chat_list)
            clear_box(self.message_feed)
            self.content_stack.set_visible_child_name("welcome")
            self.content_title.set_title("LocalCode")
            self.content_title.set_subtitle("Choose a project")
            self._update_action_sensitivity()

    def _select_project(self, project: Project) -> None:
        self.current_project = self.database.update_project(project.id, last_opened_at=utc_now())
        self._set_permission_dropdown(self.current_project.permission_mode)
        self._refresh_chats()
        self._update_action_sensitivity()

    def _refresh_chats(self, select_id: str = "") -> None:
        clear_box(self.chat_list)
        if not self.current_project:
            self.chats = []
            return
        self.chats = self.database.list_chats(self.current_project.id)
        if not self.chats:
            self.chats = [self.database.create_chat(self.current_project.id)]
        selected_chat = next(
            (
                chat
                for chat in self.chats
                if chat.id == (select_id or (self.current_chat.id if self.current_chat else ""))
            ),
            self.chats[0],
        )
        selected_row = None
        for chat in self.chats:
            row = ChatRow(chat, self._confirm_delete_chat)
            self.chat_list.append(row)
            if chat.id == selected_chat.id:
                selected_row = row
        if selected_row:
            self.chat_list.select_row(selected_row)

    def _select_chat(self, chat: Chat) -> None:
        self.current_chat = self.database.get_chat(chat.id) or chat
        if not self.current_project or self.current_chat.project_id != self.current_project.id:
            return
        self.content_stack.set_visible_child_name("chat")
        self._dismiss_banner()
        self.content_title.set_title(self.current_chat.title)
        self.content_title.set_subtitle(self.current_project.path)
        self._set_model_dropdown()
        self.context_meter.update_report(
            make_report(
                self.current_chat.context_used,
                self.current_chat.context_limit or self.current_project.context_window,
                estimated=False,
                reason="Last completed model step",
            )
        )
        self._load_transcript()
        self.split_view.set_show_content(True)
        self.composer.focus()

    def _load_transcript(self) -> None:
        clear_box(self.message_feed)
        self.activity_rows.clear()
        self.streaming_bubble = None
        if not self.current_chat:
            return
        items: list[tuple[str, int, object]] = []
        for message in self.database.list_messages(self.current_chat.id):
            items.append((message.created_at, message.id * 2, message))
        for activity in self.database.list_activities(self.current_chat.id):
            items.append((activity.created_at, activity.id * 2 + 1, activity))
        items.sort(key=lambda item: (item[0], item[1]))
        if not items:
            empty = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
            empty.set_valign(Gtk.Align.CENTER)
            empty.set_vexpand(True)
            icon = Gtk.Image.new_from_icon_name("utilities-terminal-symbolic")
            icon.set_pixel_size(36)
            icon.add_css_class("dim-label")
            empty.append(icon)
            label = Gtk.Label(label="Ask about the project or request a code change.")
            label.add_css_class("dim-label")
            empty.append(label)
            self.message_feed.append(empty)
            return
        for _timestamp, _order, item in items:
            if hasattr(item, "role"):
                self.message_feed.append(widget_for_message(item))
            else:
                row = widget_for_activity(item)
                self.message_feed.append(row)
        self._scroll_to_bottom()

    def _refresh_models_async(self) -> None:
        self.phase_label.set_label("Connecting to Ollama...")

        def worker() -> None:
            try:
                models = OllamaClient(self.settings.ollama_url).list_models()
                self._idle(self._models_loaded, models, "")
            except Exception as error:  # transport errors are rendered in the shell
                self._idle(self._models_loaded, [], str(error))
            status = self.memory.status()
            self._idle(
                self._memory_status_loaded, status.available, status.initialized, status.detail
            )

        threading.Thread(target=worker, name="service-discovery", daemon=True).start()

    def _models_loaded(self, models: list[ModelInfo], error: str) -> None:
        self.models = models
        self.phase_label.set_label("Ready" if models else "Ollama unavailable")
        self._set_model_dropdown()
        if error:
            self._toast(error, 6)

    def _memory_status_loaded(self, available: bool, initialized: bool, detail: str) -> None:
        if available and initialized:
            self.memory_label.set_label("MemPalace ready")
            self.memory_icon.set_from_icon_name("emblem-ok-symbolic")
        elif available:
            self.memory_label.set_label("MemPalace needs a project")
            self.memory_icon.set_from_icon_name("dialog-information-symbolic")
        else:
            self.memory_label.set_label("Set up MemPalace")
            self.memory_icon.set_from_icon_name("folder-saved-search-symbolic")
        self.memory_label.set_tooltip_text(detail or None)

    def _set_model_dropdown(self) -> None:
        desired = ""
        if self.current_chat:
            desired = self.current_chat.model
        if not desired and self.current_project:
            desired = self.current_project.model
        desired = desired or self.settings.default_model
        names = [model.name for model in self.models]
        if desired and desired not in names:
            names.append(desired)
        if not names:
            names = ["No completion models"]
        self._updating_models = True
        self.model_store.splice(0, self.model_store.get_n_items(), names)
        selected = names.index(desired) if desired in names else 0
        self.model_dropdown.set_selected(selected)
        self.model_dropdown.set_sensitive(bool(self.models))
        self._updating_models = False

    def _send_message(self) -> None:
        if self.worker and self.worker.is_alive():
            return
        if not self.current_chat or not self.current_project:
            self._toast("Choose a project first.")
            return
        content = self.composer.get_text().strip()
        if not content:
            return
        chat_id = self.current_chat.id
        self._running_chat_id = chat_id
        self.runner.reset_cancel()
        self.composer.clear()
        self._set_interaction_busy(True)
        self.stop_button.set_visible(True)
        self.streaming_bubble = None

        if self.message_feed.get_first_child() and not self.database.list_messages(chat_id):
            clear_box(self.message_feed)
        self.message_feed.append(MessageBubble("user", content, utc_now()))
        self._scroll_to_bottom()

        callbacks = AgentCallbacks(
            phase=lambda value: self._idle(self._set_phase, chat_id, value),
            chunk=lambda value: self._idle(self._append_chunk, chat_id, value),
            activity=lambda kind, title, detail, status: self._idle(
                self._show_activity, chat_id, kind, title, detail, status
            ),
            context=lambda report: self._idle(self._update_context, chat_id, report),
            notice=lambda level, title, body: self._idle(
                self._show_notice_for_chat, chat_id, level, title, body
            ),
            complete=lambda value: self._idle(self._turn_complete, chat_id, value),
            error=lambda value: self._idle(self._turn_error, chat_id, value),
            approval=self._request_approval,
        )
        def worker() -> None:
            try:
                self.runner.run_turn(chat_id, content, callbacks)
            finally:
                self._idle(self._worker_exited, chat_id)

        self.worker = threading.Thread(
            target=worker, name=f"agent-{chat_id[:8]}", daemon=True
        )
        self.worker.start()

    def _append_chunk(self, chat_id: str, chunk: str) -> None:
        if not self._chat_is_visible(chat_id):
            return
        if self.streaming_bubble is None:
            self.streaming_bubble = MessageBubble("assistant")
            self.message_feed.append(self.streaming_bubble)
        self.streaming_bubble.append_chunk(chunk)
        self._scroll_to_bottom()

    def _show_activity(
        self,
        chat_id: str,
        kind: str,
        title: str,
        detail: str,
        status: str,
    ) -> None:
        if not self._chat_is_visible(chat_id):
            return
        if status == "running" and self.streaming_bubble:
            self.streaming_bubble.finalize()
            self.streaming_bubble = None
        key = (kind, title)
        row = self.activity_rows.get(key)
        if row is None and status != "running":
            matching_key = next((item for item in self.activity_rows if item[0] == kind), None)
            if matching_key:
                row = self.activity_rows.pop(matching_key)
        if row and status != "running":
            row.update(detail, status)
            self.activity_rows.pop(key, None)
        else:
            row = ActivityRow(kind, title, detail, status)
            self.message_feed.append(row)
            if status == "running":
                self.activity_rows[key] = row
        self._scroll_to_bottom()

    def _update_context(self, chat_id: str, report: ContextReport) -> None:
        if not self._chat_is_visible(chat_id):
            return
        self.context_meter.update_report(report)
        if report.state == "warning":
            self._show_notice(
                "warning",
                "Context is filling up",
                "LocalCode will compact older turns before Ollama can silently truncate them.",
            )
        elif report.state in {"critical", "exhausted"}:
            self._show_notice(
                "error",
                "Context limit reached" if report.state == "exhausted" else "Context nearly full",
                "The full chat stays on disk. The active model context is being "
                "compacted around current code.",
                persistent=report.state == "exhausted",
            )
        elif report.state in {"healthy", "fresh", "compacted"} and not self._banner_persistent:
            self.banner.set_reveal_child(False)

    def _turn_complete(self, chat_id: str, _content: str) -> None:
        if self._chat_is_visible(chat_id) and self.streaming_bubble:
            self.streaming_bubble.finalize()
        if self._chat_is_visible(chat_id) and self.current_project:
            self._refresh_chats(select_id=chat_id)

    def _turn_error(self, chat_id: str, error: str) -> None:
        self.database.add_message(chat_id, "event", f"Agent error: {error}", {"level": "error"})
        if self._chat_is_visible(chat_id):
            self.message_feed.append(MessageBubble("event", f"Agent error: {error}", utc_now()))
            self._show_notice("error", "The model could not finish", error, persistent=True)
            self._scroll_to_bottom()

    def _worker_exited(self, chat_id: str) -> None:
        self._finish_running_state(chat_id)

    def _finish_running_state(self, chat_id: str) -> None:
        if self._running_chat_id == chat_id:
            self._set_interaction_busy(False)
            self.stop_button.set_visible(False)
            self.phase_label.set_label("Ready")
            self._running_chat_id = ""
            self.streaming_bubble = None
            self.composer.focus()

    def _request_approval(self, tool_name: str, description: str) -> bool:
        completed = threading.Event()
        answer = {"allowed": False}
        holder: dict[str, Adw.AlertDialog] = {}

        def show_dialog() -> bool:
            if self.runner.is_cancelled():
                completed.set()
                return GLib.SOURCE_REMOVE
            heading = f"Allow {tool_name.replace('_', ' ').title()}?"
            project_label = self.current_project.path if self.current_project else "Unknown project"
            dialog = Adw.AlertDialog(
                heading=heading,
                body=f"Project: {project_label}\n\n{description[:12000]}",
            )
            holder["dialog"] = dialog
            dialog.add_response("cancel", "Cancel")
            dialog.add_response("allow", "Allow Once")
            dialog.set_close_response("cancel")
            dialog.set_default_response("cancel")
            dialog.set_response_appearance("allow", Adw.ResponseAppearance.SUGGESTED)

            def chosen(current: Adw.AlertDialog, result: Gio.AsyncResult) -> None:
                try:
                    answer["allowed"] = current.choose_finish(result) == "allow"
                except GLib.Error:
                    answer["allowed"] = False
                completed.set()

            dialog.choose(self, None, chosen)
            return GLib.SOURCE_REMOVE

        GLib.idle_add(show_dialog)
        while not completed.wait(0.1):
            if self.runner.is_cancelled():
                self._idle(lambda: holder.get("dialog") and holder["dialog"].close())
                return False
        return answer["allowed"]

    def _choose_project(self) -> None:
        dialog = Gtk.FileDialog(
            title="Choose a Project Folder",
            modal=True,
            accept_label="Add Project",
        )
        dialog.select_folder(self, None, self._project_chosen)

    def _project_chosen(self, dialog: Gtk.FileDialog, result: Gio.AsyncResult) -> None:
        try:
            folder = dialog.select_folder_finish(result)
        except GLib.Error as error:
            if error.matches(Gtk.dialog_error_quark(), Gtk.DialogError.CANCELLED) or error.matches(
                Gtk.dialog_error_quark(), Gtk.DialogError.DISMISSED
            ):
                return
            self._toast(str(error))
            return
        path = folder.get_path()
        if not path:
            self._toast("Only local project folders are supported.")
            return
        agents = AgentsFileManager(path)
        agents_existed = agents.path.exists() or agents.path.is_symlink()
        can_restore_agents = (
            agents_existed and agents.path.is_file() and not agents.path.is_symlink()
        )
        original_agents = (
            agents.path.read_text(encoding="utf-8", errors="replace")
            if can_restore_agents
            else ""
        )
        try:
            agents.ensure()
            project = self.database.add_project(
                path,
                model=self.settings.default_model,
                context_window=self.settings.default_context_window,
            )
        except (OSError, ValueError, sqlite3.Error) as error:
            if can_restore_agents:
                agents._write(original_agents)
            elif not agents_existed:
                agents.path.unlink(missing_ok=True)
            self._toast(str(error), 6)
            return
        self._refresh_projects(select_id=project.id)
        self._toast(f"Added {project.name}")
        if self.memory.executable():
            self._initialize_memory_async(project)

    def _new_chat(self) -> None:
        if not self.current_project:
            self._choose_project()
            return
        chat = self.database.create_chat(self.current_project.id)
        self._refresh_chats(select_id=chat.id)

    def _confirm_delete_chat(self, chat: Chat) -> None:
        dialog = Adw.AlertDialog(
            heading="Delete this chat?",
            body=(
                "The local transcript for this chat will be removed from LocalCode. "
                "Project files are not changed. MemPalace cleanup is attempted in "
                "the background when available."
            ),
        )
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("delete", "Delete")
        dialog.set_close_response("cancel")
        dialog.set_response_appearance("delete", Adw.ResponseAppearance.DESTRUCTIVE)

        def chosen(current: Adw.AlertDialog, result: Gio.AsyncResult) -> None:
            try:
                response = current.choose_finish(result)
            except GLib.Error:
                return
            if response == "delete":
                transcript = transcript_dir(chat.project_id) / f"{chat.id}.jsonl"
                transcript.unlink(missing_ok=True)
                self.database.delete_chat(chat.id)
                if self.current_chat and self.current_chat.id == chat.id:
                    self.current_chat = None
                self._refresh_chats()
                if self.current_project:
                    self.memory.prune_in_background(self.current_project)

        dialog.choose(self, None, chosen)

    def _forget_project(self) -> None:
        if not self.current_project:
            return
        project = self.current_project
        dialog = Adw.AlertDialog(
            heading=f"Forget {project.name}?",
            body=(
                "LocalCode will remove its chats and settings for this project. The "
                "project folder, "
                "AGENTS.md, and MemPalace project-file archive remain on disk. Exported "
                "LocalCode chat files are deleted."
            ),
        )
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("forget", "Forget Project")
        dialog.set_close_response("cancel")
        dialog.set_response_appearance("forget", Adw.ResponseAppearance.DESTRUCTIVE)

        def chosen(current: Adw.AlertDialog, result: Gio.AsyncResult) -> None:
            try:
                response = current.choose_finish(result)
            except GLib.Error:
                return
            if response == "forget":
                transcript_root = transcript_dir(project.id)
                if transcript_root.exists():
                    for transcript in transcript_root.glob("*.jsonl"):
                        transcript.unlink(missing_ok=True)
                transcript_root.mkdir(parents=True, exist_ok=True)
                transcript_root.chmod(0o700)
                self.memory.prune_in_background(project)
                self.database.remove_project(project.id)
                self.current_project = None
                self.current_chat = None
                self._refresh_projects()

        dialog.choose(self, None, chosen)

    def _show_preferences(self) -> None:
        dialog = Adw.PreferencesDialog(title="LocalCode Preferences", search_enabled=False)
        general = Adw.PreferencesPage(title="General", icon_name="preferences-system-symbolic")
        dialog.add(general)

        ollama_group = Adw.PreferencesGroup(
            title="Ollama",
            description=(
                "Requests go directly to this endpoint. Localhost is the "
                "privacy-preserving default."
            ),
        )
        general.add(ollama_group)
        endpoint = Adw.EntryRow(title="Endpoint", text=self.settings.ollama_url)
        endpoint.connect(
            "notify::text",
            lambda row, _param: self.settings.set("ollama_url", row.get_text().rstrip("/")),
        )
        ollama_group.add(endpoint)

        context_row = Adw.SpinRow.new_with_range(2048, 262144, 1024)
        context_row.set_title("Default context window")
        context_row.set_subtitle("Passed to Ollama as num_ctx for newly added projects")
        context_row.set_value(self.settings.default_context_window)
        context_row.connect(
            "notify::value",
            lambda row, _param: self.settings.set("default_context_window", int(row.get_value())),
        )
        ollama_group.add(context_row)

        compact_row = Adw.SpinRow.new_with_range(50, 92, 1)
        compact_row.set_title("Auto-compact at")
        compact_row.set_subtitle("Percentage including reserved response space")
        compact_row.set_value(round(self.settings.compact_threshold * 100))
        compact_row.connect(
            "notify::value",
            lambda row, _param: self.settings.set("compact_threshold", row.get_value() / 100),
        )
        ollama_group.add(compact_row)

        if self.current_project:
            project_group = Adw.PreferencesGroup(
                title=self.current_project.name,
                description="Settings for the selected project",
            )
            general.add(project_group)
            project_context = Adw.SpinRow.new_with_range(2048, 262144, 1024)
            project_context.set_title("Project context window")
            project_context.set_value(self.current_project.context_window)

            def project_context_changed(row: Adw.SpinRow, _param) -> None:
                if self.current_project:
                    self.current_project = self.database.update_project(
                        self.current_project.id, context_window=int(row.get_value())
                    )

            project_context.connect("notify::value", project_context_changed)
            project_group.add(project_context)

            memory_switch = Adw.SwitchRow(
                title="Use project memory",
                subtitle="Retrieve and archive this project through MemPalace",
                active=self.current_project.memory_enabled,
            )

            def memory_toggled(row: Adw.SwitchRow, _param) -> None:
                if self.current_project:
                    self.current_project = self.database.update_project(
                        self.current_project.id, memory_enabled=row.get_active()
                    )

            memory_switch.connect("notify::active", memory_toggled)
            project_group.add(memory_switch)

        memory_page = Adw.PreferencesPage(title="Memory", icon_name="folder-saved-search-symbolic")
        dialog.add(memory_page)
        memory_group = Adw.PreferencesGroup(
            title="MemPalace",
            description="Verbatim local retrieval for project files and full chat transcripts.",
        )
        memory_page.add(memory_group)
        available = bool(self.memory.executable())
        status_row = Adw.ActionRow(
            title="Memory service",
            subtitle=("Installed locally" if available else "Not installed"),
        )
        status_row.set_icon_name(
            "emblem-ok-symbolic" if available else "folder-download-symbolic"
        )
        setup = Gtk.Button(label="Sync" if available else "Install")
        setup.set_valign(Gtk.Align.CENTER)
        setup.add_css_class("suggested-action" if not available else "flat")
        status_row.add_suffix(setup)
        memory_group.add(status_row)

        def setup_clicked(_button: Gtk.Button) -> None:
            setup.set_sensitive(False)
            status_row.set_subtitle("Preparing local memory...")
            selected_project = self.current_project

            def progress(value: str) -> None:
                self._idle(status_row.set_subtitle, value)

            def worker() -> None:
                try:
                    if not self.memory.executable():
                        self.memory.install(progress)
                    if selected_project:
                        progress("Initializing and indexing the selected project...")
                        success, detail = self.memory.sync_project(selected_project)
                        if not success:
                            raise RuntimeError(detail or "MemPalace initialization failed.")
                    final_status = self.memory.status()
                    final_text = (
                        "MemPalace is installed and indexed."
                        if final_status.initialized
                        else "MemPalace is installed. Add a project to begin indexing."
                    )
                    self._idle(status_row.set_subtitle, final_text)
                    self._idle(setup.set_label, "Sync")
                    self._idle(self._memory_status_loaded, True, True, "Ready")
                except Exception as error:
                    self._idle(status_row.set_subtitle, str(error)[-1000:])
                    self._idle(dialog.add_toast, Adw.Toast.new("MemPalace setup failed"))
                finally:
                    self._idle(setup.set_sensitive, True)

            self._start_background_job("mempalace-setup", worker)

        setup.connect("clicked", setup_clicked)

        source_row = Adw.ActionRow(
            title="Bundled source",
            subtitle="vendor/mempalace · isolated Python environment · no cloud API",
            icon_name="system-software-install-symbolic",
        )
        memory_group.add(source_row)
        dialog.connect("closed", lambda _dialog: self._refresh_models_async())
        dialog.present(self)

    def _initialize_memory_async(self, project: Project) -> None:
        chat_id = self.current_chat.id if self.current_chat else ""
        self._show_activity(
            chat_id,
            "memory",
            "Indexing project memory",
            "MemPalace is scanning project files in the background.",
            "running",
        )

        def worker() -> None:
            success, detail = self.memory.initialize_project(project)
            if chat_id:
                self._idle(
                    self._show_activity,
                    chat_id,
                    "memory",
                    "Indexing project memory",
                    detail,
                    "complete" if success else "error",
                )

        self._start_background_job("mempalace-init", worker)

    def _sync_memory(self) -> None:
        if not self.current_project:
            return
        if not self.memory.executable():
            self._show_preferences()
            return
        project = self.current_project
        chat_id = self.current_chat.id if self.current_chat else ""
        self._show_activity(chat_id, "memory", "Syncing memory", project.path, "running")

        def completed(success: bool, detail: str) -> None:
            self._idle(
                self._show_activity,
                chat_id,
                "memory",
                "Syncing memory",
                detail,
                "complete" if success else "error",
            )

        self.memory.sync_in_background(project, completed)

    def _show_about(self) -> None:
        about = Adw.AboutDialog(
            application_name="LocalCode",
            application_icon="io.localcode.LocalCode",
            developer_name="LocalCode contributors",
            version="0.1.0",
            comments="Local-first Ollama coding for GNOME",
            website="https://github.com/MemPalace/mempalace",
            license_type=Gtk.License.MIT_X11,
        )
        about.add_credit_section("Local memory", ["MemPalace contributors"])
        about.present(self)

    def _show_notice_for_chat(self, chat_id: str, level: str, title: str, body: str) -> None:
        if self._chat_is_visible(chat_id):
            self._show_notice(level, title, body, persistent=level == "error")

    def _show_notice(
        self,
        level: str,
        title: str,
        body: str,
        *,
        persistent: bool = False,
    ) -> None:
        for css_class in ("notice-info", "notice-warning", "notice-error"):
            self.banner_box.remove_css_class(css_class)
        self.banner_box.add_css_class(f"notice-{level}")
        icon = {
            "error": "dialog-error-symbolic",
            "warning": "dialog-warning-symbolic",
        }.get(level, "dialog-information-symbolic")
        self.banner_icon.set_from_icon_name(icon)
        self.banner_title.set_label(title)
        self.banner_body.set_label(body)
        self._banner_persistent = persistent
        self.banner.set_reveal_child(True)
        self._toast(title, 4)

    def _dismiss_banner(self) -> None:
        self._banner_persistent = False
        self.banner.set_reveal_child(False)

    def _toast(self, title: str, timeout: int = 3) -> None:
        toast = Adw.Toast.new(title)
        toast.set_use_markup(False)
        toast.set_timeout(timeout)
        self.toast_overlay.add_toast(toast)

    def _scroll_to_bottom(self) -> None:
        def scroll() -> bool:
            adjustment = self.message_scroll.get_vadjustment()
            adjustment.set_value(max(0, adjustment.get_upper() - adjustment.get_page_size()))
            return GLib.SOURCE_REMOVE

        GLib.idle_add(scroll, priority=GLib.PRIORITY_LOW)

    def _on_project_selected(self, dropdown: Gtk.DropDown, _param) -> None:
        if self._updating_projects:
            return
        index = dropdown.get_selected()
        if 0 <= index < len(self.projects):
            self._select_project(self.projects[index])

    def _on_chat_selected(self, _listbox: Gtk.ListBox, row: Gtk.ListBoxRow | None) -> None:
        if isinstance(row, ChatRow):
            self._select_chat(row.chat)

    def _on_model_selected(self, dropdown: Gtk.DropDown, _param) -> None:
        if self._updating_models or not self.current_chat:
            return
        index = dropdown.get_selected()
        if not (0 <= index < self.model_store.get_n_items()):
            return
        item = self.model_store.get_item(index)
        model = item.get_string() if item else ""
        if model and model != "No completion models":
            self.current_chat = self.database.update_chat(self.current_chat.id, model=model)

    def _on_permission_selected(self, dropdown: Gtk.DropDown, _param) -> None:
        if not self.current_project:
            return
        modes = ["ask", "allow", "read-only"]
        index = dropdown.get_selected()
        if 0 <= index < len(modes) and self.current_project.permission_mode != modes[index]:
            self.current_project = self.database.update_project(
                self.current_project.id, permission_mode=modes[index]
            )

    def _set_permission_dropdown(self, mode: str) -> None:
        modes = ["ask", "allow", "read-only"]
        self.composer.mode.set_selected(modes.index(mode) if mode in modes else 0)

    def _set_phase(self, chat_id: str, value: str) -> None:
        if self._running_chat_id == chat_id:
            self.phase_label.set_label(value)

    def _chat_is_visible(self, chat_id: str) -> bool:
        return bool(self.current_chat and self.current_chat.id == chat_id)

    def _open_project_folder(self) -> None:
        if not self.current_project:
            return
        Gio.AppInfo.launch_default_for_uri(Path(self.current_project.path).as_uri(), None)

    def _update_action_sensitivity(self) -> None:
        for name in ("open-project-folder", "sync-memory", "forget-project"):
            action = self.lookup_action(name)
            if action:
                action.set_enabled(self.current_project is not None)

    def _set_interaction_busy(self, busy: bool) -> None:
        self.composer.set_busy(busy)
        self.sidebar_shell.set_sensitive(not busy)
        self.sidebar_toggle.set_sensitive(not busy)
        self.content_header.set_show_back_button(not busy)
        if busy:
            self.split_view.set_show_content(True)
        self.model_dropdown.set_sensitive(not busy and bool(self.models))
        for name in ("preferences", "add-project"):
            action = self.lookup_action(name)
            if action:
                action.set_enabled(not busy)
        for name in ("sync-memory", "forget-project"):
            action = self.lookup_action(name)
            if action:
                action.set_enabled(not busy and self.current_project is not None)

    def _on_close_request(self, _window: Adw.ApplicationWindow) -> bool:
        if not self._has_active_work():
            return False
        if not self._force_close:
            self._force_close = True
            self.runner.cancel()
            self.phase_label.set_label("Stopping safely before closing...")
        if not self._close_polling:
            self._close_polling = True
            GLib.timeout_add(100, self._poll_close)
        return True

    def _has_active_work(self) -> bool:
        return bool(
            (self.worker and self.worker.is_alive())
            or self._background_jobs
            or self.memory.has_active_work()
        )

    def _poll_close(self) -> bool:
        if self._has_active_work():
            return GLib.SOURCE_CONTINUE
        self._close_polling = False
        self.close()
        return GLib.SOURCE_REMOVE

    def _start_background_job(self, name: str, target) -> threading.Thread:
        def worker() -> None:
            try:
                target()
            finally:
                self._idle(self._background_job_finished, threading.current_thread())

        thread = threading.Thread(target=worker, name=name, daemon=True)
        self._background_jobs.add(thread)
        thread.start()
        return thread

    def _background_job_finished(self, thread: threading.Thread) -> None:
        self._background_jobs.discard(thread)

    def _install_actions(self) -> None:
        callbacks = {
            "preferences": self._show_preferences,
            "add-project": self._choose_project,
            "about": self._show_about,
            "open-project-folder": self._open_project_folder,
            "sync-memory": self._sync_memory,
            "forget-project": self._forget_project,
        }
        for name, callback in callbacks.items():
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", lambda _action, _parameter, fn=callback: fn())
            self.add_action(action)
        self._update_action_sensitivity()

    def _load_css(self) -> None:
        provider = Gtk.CssProvider()
        provider.load_from_path(str(PACKAGE_ROOT / "style.css"))
        display = Gdk.Display.get_default()
        if display:
            Gtk.StyleContext.add_provider_for_display(
                display, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
            )

    @staticmethod
    def _idle(function, *args) -> None:
        def invoke() -> bool:
            function(*args)
            return GLib.SOURCE_REMOVE

        GLib.idle_add(invoke)
