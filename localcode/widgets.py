from __future__ import annotations

import re
from collections.abc import Callable

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Gdk, Gtk, Pango  # noqa: E402

from .context import format_token_count
from .models import Activity, Chat, ContextReport, Message


def clear_box(box: Gtk.Box) -> None:
    child = box.get_first_child()
    while child is not None:
        next_child = child.get_next_sibling()
        box.remove(child)
        child = next_child


class ContextMeter(Gtk.Box):
    def __init__(self) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=3)
        self.add_css_class("context-meter")
        self.set_tooltip_text("No context has been sent yet.")

        label_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.title = Gtk.Label(label="Context", xalign=0)
        self.title.add_css_class("caption")
        self.value = Gtk.Label(label="Fresh", xalign=1)
        self.value.add_css_class("caption")
        self.value.add_css_class("numeric")
        label_row.append(self.title)
        label_row.append(Gtk.Box(hexpand=True))
        label_row.append(self.value)
        self.append(label_row)

        self.progress = Gtk.ProgressBar(show_text=False)
        self.progress.set_size_request(168, 5)
        self.append(self.progress)
        self._state = "fresh"

    def update_report(self, report: ContextReport) -> None:
        for state in ("fresh", "healthy", "warning", "critical", "exhausted", "compacted"):
            self.remove_css_class(f"context-{state}")
        self._state = report.state
        self.add_css_class(f"context-{report.state}")
        self.progress.set_fraction(report.fraction)
        prefix = "~" if report.estimated else ""
        if report.limit:
            self.value.set_label(
                f"{prefix}{format_token_count(report.used)} / {format_token_count(report.limit)}"
            )
        else:
            self.value.set_label("Fresh")
        exactness = "Estimated before generation" if report.estimated else "Reported by Ollama"
        detail = f"{exactness}. {report.reason}" if report.reason else exactness
        self.set_tooltip_text(detail)


class MarkdownView(Gtk.TextView):
    def __init__(self, content: str = "") -> None:
        super().__init__()
        self.set_editable(False)
        self.set_cursor_visible(False)
        self.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self.set_top_margin(2)
        self.set_bottom_margin(2)
        self.set_left_margin(0)
        self.set_right_margin(0)
        self.add_css_class("message-text")
        self.buffer = self.get_buffer()
        self.buffer.create_tag(
            "heading",
            weight=Pango.Weight.BOLD,
            scale=1.12,
            pixels_above_lines=7,
            pixels_below_lines=2,
        )
        self.buffer.create_tag(
            "code",
            family="monospace",
            pixels_above_lines=1,
            pixels_below_lines=1,
        )
        self.buffer.create_tag("inline-code", family="monospace", weight=Pango.Weight.MEDIUM)
        self.buffer.create_tag("quote", style=Pango.Style.ITALIC, left_margin=14)
        self.set_content(content)

    def set_content(self, content: str) -> None:
        self.buffer.set_text(content)
        self._apply_markdown_tags(content)

    def append_content(self, content: str) -> None:
        end = self.buffer.get_end_iter()
        self.buffer.insert(end, content)

    def get_content(self) -> str:
        start, end = self.buffer.get_bounds()
        return self.buffer.get_text(start, end, True)

    def _apply_markdown_tags(self, content: str) -> None:
        self.buffer.remove_all_tags(*self.buffer.get_bounds())
        offset = 0
        fenced = False
        for line in content.splitlines(keepends=True):
            stripped = line.lstrip()
            start = self.buffer.get_iter_at_offset(offset)
            end = self.buffer.get_iter_at_offset(offset + len(line))
            if stripped.startswith("```"):
                self.buffer.apply_tag_by_name("code", start, end)
                fenced = not fenced
            elif fenced:
                self.buffer.apply_tag_by_name("code", start, end)
            elif stripped.startswith("#"):
                self.buffer.apply_tag_by_name("heading", start, end)
            elif stripped.startswith(">"):
                self.buffer.apply_tag_by_name("quote", start, end)
            else:
                for match in re.finditer(r"`[^`\n]+`", line):
                    inline_start = self.buffer.get_iter_at_offset(offset + match.start())
                    inline_end = self.buffer.get_iter_at_offset(offset + match.end())
                    self.buffer.apply_tag_by_name("inline-code", inline_start, inline_end)
            offset += len(line)


class MessageBubble(Gtk.Box):
    def __init__(self, role: str, content: str = "", created_at: str = "") -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=7)
        self.role = role
        self.add_css_class("message-bubble")
        self.add_css_class(f"message-{role}")
        self.set_hexpand(True)
        if role == "user":
            self.set_margin_start(116)
        elif role == "assistant":
            self.set_margin_end(72)

        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=7)
        role_label = {
            "user": "YOU",
            "assistant": "LOCAL MODEL",
            "system": "SYSTEM",
            "event": "SESSION",
        }.get(role, role.upper())
        self.role_label = Gtk.Label(label=role_label, xalign=0)
        self.role_label.add_css_class("message-role")
        header.append(self.role_label)
        header.append(Gtk.Box(hexpand=True))
        if created_at:
            timestamp = Gtk.Label(label=_short_time(created_at), xalign=1)
            timestamp.add_css_class("caption")
            timestamp.add_css_class("dim-label")
            header.append(timestamp)
        self.append(header)

        self.text = MarkdownView(content)
        self.append(self.text)

    def append_chunk(self, chunk: str) -> None:
        self.text.append_content(chunk)

    def finalize(self) -> None:
        self.text.set_content(self.text.get_content())


class ActivityRow(Gtk.Box):
    ICONS = {
        "tool": "utilities-terminal-symbolic",
        "context": "view-refresh-symbolic",
        "agents": "document-edit-symbolic",
        "memory": "folder-saved-search-symbolic",
    }

    def __init__(self, kind: str, title: str, detail: str = "", status: str = "running") -> None:
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        self.kind = kind
        self.title_text = title
        self.add_css_class("activity-row")
        self.set_hexpand(True)

        self.icon = Gtk.Image.new_from_icon_name(self.ICONS.get(kind, "emblem-system-symbolic"))
        self.icon.set_pixel_size(16)
        self.append(self.icon)

        labels = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, hexpand=True)
        self.title = Gtk.Label(label=title, xalign=0, ellipsize=Pango.EllipsizeMode.END)
        self.title.add_css_class("activity-title")
        labels.append(self.title)
        self.detail = Gtk.Label(
            label=_one_line(detail),
            xalign=0,
            ellipsize=Pango.EllipsizeMode.END,
            max_width_chars=100,
        )
        self.detail.add_css_class("caption")
        self.detail.add_css_class("dim-label")
        labels.append(self.detail)
        self.append(labels)

        self.spinner = Gtk.Spinner(spinning=status == "running")
        self.status_icon = Gtk.Image()
        self.append(self.spinner)
        self.append(self.status_icon)
        self.update(detail, status)

    def update(self, detail: str, status: str) -> None:
        self.detail.set_label(_one_line(detail))
        self.detail.set_tooltip_text(detail or None)
        self.spinner.set_visible(status == "running")
        self.spinner.set_spinning(status == "running")
        self.status_icon.set_visible(status != "running")
        self.remove_css_class("activity-error")
        if status == "error":
            self.status_icon.set_from_icon_name("dialog-warning-symbolic")
            self.add_css_class("activity-error")
        elif status != "running":
            self.status_icon.set_from_icon_name("emblem-ok-symbolic")


class ChatRow(Gtk.ListBoxRow):
    def __init__(self, chat: Chat, on_delete: Callable[[Chat], None]) -> None:
        super().__init__()
        self.chat = chat
        self.add_css_class("chat-row")
        content = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        content.set_margin_top(8)
        content.set_margin_bottom(8)
        content.set_margin_start(10)
        content.set_margin_end(6)

        state = Gtk.Box()
        state.set_size_request(4, 28)
        state.add_css_class("chat-context-indicator")
        state.add_css_class(f"context-{chat.context_state}")
        content.append(state)

        labels = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1, hexpand=True)
        title = Gtk.Label(label=chat.title, xalign=0, ellipsize=Pango.EllipsizeMode.END)
        title.add_css_class("chat-title")
        labels.append(title)
        subtitle = Gtk.Label(label=_chat_subtitle(chat), xalign=0)
        subtitle.add_css_class("caption")
        subtitle.add_css_class("dim-label")
        labels.append(subtitle)
        content.append(labels)

        delete = Gtk.Button.new_from_icon_name("user-trash-symbolic")
        delete.add_css_class("flat")
        delete.add_css_class("circular")
        delete.set_tooltip_text("Delete chat")
        delete.connect("clicked", lambda _button: on_delete(chat))
        content.append(delete)
        self.set_child(content)


class Composer(Gtk.Box):
    def __init__(self, send: Callable[[], None]) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.send_callback = send
        self.add_css_class("composer-shell")

        prompt_label = Gtk.Label(label="MESSAGE THE PROJECT", xalign=0)
        prompt_label.add_css_class("composer-label")
        self.append(prompt_label)

        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroll.set_min_content_height(72)
        scroll.set_max_content_height(180)
        self.text = Gtk.TextView()
        self.text.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self.text.set_accepts_tab(False)
        self.text.set_top_margin(5)
        self.text.set_bottom_margin(5)
        self.text.set_left_margin(2)
        self.text.set_right_margin(2)
        self.text.add_css_class("composer-text")
        scroll.set_child(self.text)
        self.append(scroll)

        controller = Gtk.EventControllerKey()
        controller.connect("key-pressed", self._on_key_pressed)
        self.text.add_controller(controller)

        footer = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        hint = Gtk.Label(label="Ctrl+Enter to send", xalign=0)
        hint.add_css_class("caption")
        hint.add_css_class("dim-label")
        footer.append(hint)
        footer.append(Gtk.Box(hexpand=True))

        self.mode = Gtk.DropDown.new_from_strings(
            ["Ask before changes", "Allow changes", "Read only"]
        )
        self.mode.add_css_class("permission-mode")
        self.mode.set_tooltip_text("Project tool permissions")
        footer.append(self.mode)

        self.send_button = Gtk.Button(label="Send")
        self.send_button.add_css_class("suggested-action")
        self.send_button.add_css_class("pill")
        self.send_button.connect("clicked", lambda _button: self.send_callback())
        footer.append(self.send_button)
        self.append(footer)

    def get_text(self) -> str:
        buffer = self.text.get_buffer()
        start, end = buffer.get_bounds()
        return buffer.get_text(start, end, True)

    def clear(self) -> None:
        self.text.get_buffer().set_text("")

    def set_busy(self, busy: bool) -> None:
        self.text.set_editable(not busy)
        self.send_button.set_sensitive(not busy)
        self.mode.set_sensitive(not busy)

    def focus(self) -> None:
        self.text.grab_focus()

    def _on_key_pressed(
        self,
        _controller: Gtk.EventControllerKey,
        keyval: int,
        _keycode: int,
        state: Gdk.ModifierType,
    ) -> bool:
        if keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter) and state & Gdk.ModifierType.CONTROL_MASK:
            self.send_callback()
            return True
        return False


def widget_for_message(message: Message) -> Gtk.Widget:
    return MessageBubble(message.role, message.content, message.created_at)


def widget_for_activity(activity: Activity) -> Gtk.Widget:
    return ActivityRow(activity.kind, activity.title, activity.detail, activity.status)


def _one_line(value: str) -> str:
    return " ".join(value.strip().split())[:500]


def _short_time(timestamp: str) -> str:
    if "T" not in timestamp:
        return ""
    return timestamp.split("T", 1)[1][:5]


def _chat_subtitle(chat: Chat) -> str:
    if chat.context_limit and chat.context_used:
        return f"{format_token_count(chat.context_used)} context · {_short_time(chat.updated_at)}"
    return _short_time(chat.updated_at) or "Empty"
