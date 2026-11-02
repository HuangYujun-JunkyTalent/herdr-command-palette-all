# SPDX-FileCopyrightText: 2026 Yujun Huang
# SPDX-License-Identifier: MIT
"""A command palette for the Herdr TUI.

`palette.py --open` runs as the plugin action `command-palette.open`, without a terminal.
It opens the plugin pane `palette` as a popup over the active pane, and passes the pane,
tab, workspace and folder of the invocation in the variable PALETTE_CONTEXT.

`palette.py` runs in that popup, lists every command with its key in fzf, and runs the
one picked on the pane, tab and workspace of the invocation.
`palette.py --list` prints the same rows, for a check outside Herdr.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

CONFIG_PATH = os.path.expanduser("~/.config/herdr/config.toml")
DEFAULT_SOCKET_PATH = os.path.expanduser("~/.config/herdr/herdr.sock")
DEFAULT_PREFIX = "ctrl+b"
STATUS_GLYPHS = {"blocked": "◉", "working": "◐", "done": "●", "idle": "○", "unknown": "·"}
PLATFORM = {"darwin": "macos", "win32": "windows"}.get(sys.platform, "linux")
CLIENT_HINT_SECONDS = 2

# Actions that only the Herdr client runs, which no API reaches, with their labels.
CLIENT_ACTIONS = {
    "help": "Herdr: Keybinding help",
    "settings": "Herdr: Settings",
    "detach": "Herdr: Detach",
    "goto": "Herdr: Go to agent or terminal (picker)",
    "open_notification_target": "Herdr: Open notification target",
    "toggle_sidebar": "Herdr: Toggle workspace sidebar",
    "workspace_picker": "Workspace: Workspace navigation",
    "new_worktree": "Workspace: New worktree",
    "resize_mode": "Pane: Resize mode",
    "edit_scrollback": "Pane: Open scrollback in editor",
    "cycle_pane_next": "Pane: Next pane",
    "cycle_pane_previous": "Pane: Previous pane",
    "copy_mode": "Pane: Copy mode",
}
# Copy mode has a default key that `herdr --default-config` does not print as an action.
UNPRINTED_DEFAULT_KEYS = {"copy_mode": ["prefix+["]}


class HerdrError(RuntimeError):
    pass


@dataclass(frozen=True)
class Context:
    """Where the palette was opened, which the commands act on."""

    pane: Optional[str] = None
    tab: Optional[str] = None
    workspace: Optional[str] = None
    folder: Optional[str] = None


@dataclass(frozen=True)
class Row:
    label: str
    key: str
    run: Callable[[Context], None]


@dataclass(frozen=True)
class Config:
    keys: dict[str, list[str]]
    custom_commands: list[dict[str, str]] = field(default_factory=list)

    @property
    def prefix(self) -> str:
        return (self.keys.get("prefix") or [DEFAULT_PREFIX])[0]

    def key_text(self, keys: list[str]) -> str:
        return " / ".join(key.replace("prefix+", f"{self.prefix} ") for key in keys if key)


class Herdr:
    """A client of the JSON API on the socket of the Herdr server."""

    def __init__(self, socket_path: str) -> None:
        self.socket_path = socket_path

    def call(self, method: str, **params: object) -> dict:
        connection = socket.socket(socket.AF_UNIX)
        connection.settimeout(10)
        try:
            connection.connect(self.socket_path)
            request = {"id": "command-palette", "method": method, "params": params}
            connection.sendall((json.dumps(request) + "\n").encode())
            reply = json.loads(connection.makefile("rb").readline())
        finally:
            connection.close()
        if "error" in reply:
            raise HerdrError(f"{method}: {reply['error'].get('message') or reply['error']}")
        return reply["result"]


# ---------------------------------------------------------------- configuration

def read_config(herdr_binary: str) -> Config:
    """The keys of Herdr's defaults overridden by config.toml, and its custom commands."""
    try:
        defaults = subprocess.run([herdr_binary, "--default-config"], capture_output=True, text=True,
                                  timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        defaults = ""
    try:
        with open(CONFIG_PATH) as handle:
            user_config = handle.read()
    except OSError:
        user_config = ""
    keys = {**UNPRINTED_DEFAULT_KEYS, **parse_keys(uncomment(defaults)), **parse_keys(user_config)}
    return Config(keys=keys, custom_commands=parse_custom_commands(user_config))


def uncomment(text: str) -> str:
    return "\n".join(re.sub(r"^\s*#\s?", "", line) for line in text.splitlines())


def parse_keys(text: str) -> dict[str, list[str]]:
    """The `action = "key"` and `action = ["key", ...]` lines of the [keys] table."""
    keys, in_keys_table = {}, False
    for line in (raw.strip() for raw in text.splitlines()):
        if line.startswith("["):
            in_keys_table = line == "[keys]"
        elif in_keys_table and (match := re.match(r'^([a-z_]+)\s*=\s*(".*?"|\[.*?\])', line)):
            keys[match.group(1)] = [key for key in re.findall(r'"([^"]*)"', match.group(2)) if key]
    return keys


def parse_custom_commands(text: str) -> list[dict[str, str]]:
    """The string fields of each [[keys.command]] table."""
    commands, current = [], None
    for line in (raw.strip() for raw in text.splitlines()):
        if line.startswith("["):
            current = {} if line == "[[keys.command]]" else None
            if current is not None:
                commands.append(current)
        elif current is not None and (match := re.match(r'^([a-z_]+)\s*=\s*"(.*)"\s*(#.*)?$', line)):
            current[match.group(1)] = match.group(2).replace('\\"', '"').replace("\\\\", "\\")
    return [command for command in commands if command.get("command")]


def invoked_plugin_action(command: dict[str, str]) -> Optional[str]:
    """The qualified plugin action that a custom command runs, also through the CLI, as in
    `herdr plugin action invoke open-sidebar --plugin herdr-sidebar`."""
    if command.get("type") == "plugin_action":
        return command.get("command")
    words = command.get("command", "").split()
    if "invoke" not in words[:-1]:
        return None
    action = words[words.index("invoke") + 1]
    if "--plugin" in words[:-1]:
        return f"{words[words.index('--plugin') + 1]}.{action}"
    return action


# ---------------------------------------------------------------- input in the popup

def ask(prompt: str) -> str:
    sys.stdout.write(f"\n  {prompt}")
    sys.stdout.flush()
    try:
        return input().strip()
    except EOFError:
        return ""


def confirm(question: str) -> bool:
    return ask(f"{question} [y/N] ").lower() in ("y", "yes")


def show_for_a_moment(message: str) -> None:
    sys.stdout.write(f"\n  {message}\n")
    sys.stdout.flush()
    time.sleep(CLIENT_HINT_SECONDS)


# ---------------------------------------------------------------- Herdr commands

HerdrCommand = Callable[[Herdr, Context], None]
HERDR_COMMANDS: dict[str, tuple[str, HerdrCommand]] = {}


def herdr_command(action: str, label: str) -> Callable[[HerdrCommand], HerdrCommand]:
    """Registers a command under the action name that Herdr's [keys] table binds."""
    def register(command: HerdrCommand) -> HerdrCommand:
        HERDR_COMMANDS[action] = (label, command)
        return command
    return register


def neighbour(ids: list[str], current: Optional[str], step: int) -> Optional[str]:
    if current not in ids:
        return None
    return ids[(ids.index(current) + step) % len(ids)]


@herdr_command("new_tab", "Tab: New tab")
def new_tab(herdr: Herdr, context: Context) -> None:
    herdr.call("tab.create", workspace_id=context.workspace, cwd=context.folder, focus=True)


@herdr_command("rename_tab", "Tab: Rename tab")
def rename_tab(herdr: Herdr, context: Context) -> None:
    if name := ask("New tab name: "):
        herdr.call("tab.rename", tab_id=context.tab, label=name)


@herdr_command("close_tab", "Tab: Close tab")
def close_tab(herdr: Herdr, context: Context) -> None:
    if confirm("Close this tab and its panes?"):
        herdr.call("tab.close", tab_id=context.tab)


def focus_neighbour_tab(herdr: Herdr, context: Context, step: int) -> None:
    tabs = herdr.call("tab.list", workspace_id=context.workspace).get("tabs", [])
    if target := neighbour([tab["tab_id"] for tab in tabs], context.tab, step):
        herdr.call("tab.focus", tab_id=target)


@herdr_command("next_tab", "Tab: Next tab")
def next_tab(herdr: Herdr, context: Context) -> None:
    focus_neighbour_tab(herdr, context, 1)


@herdr_command("previous_tab", "Tab: Previous tab")
def previous_tab(herdr: Herdr, context: Context) -> None:
    focus_neighbour_tab(herdr, context, -1)


@herdr_command("split_vertical", "Pane: Split right")
def split_right(herdr: Herdr, context: Context) -> None:
    herdr.call("pane.split", target_pane_id=context.pane, direction="right", cwd=context.folder, focus=True)


@herdr_command("split_horizontal", "Pane: Split down")
def split_down(herdr: Herdr, context: Context) -> None:
    herdr.call("pane.split", target_pane_id=context.pane, direction="down", cwd=context.folder, focus=True)


@herdr_command("zoom", "Pane: Toggle zoom")
def toggle_zoom(herdr: Herdr, context: Context) -> None:
    herdr.call("pane.zoom", pane_id=context.pane, mode="toggle")


@herdr_command("rename_pane", "Pane: Rename pane")
def rename_pane(herdr: Herdr, context: Context) -> None:
    herdr.call("pane.rename", pane_id=context.pane, label=ask("New pane name (empty clears it): ") or None)


@herdr_command("close_pane", "Pane: Close pane")
def close_pane(herdr: Herdr, context: Context) -> None:
    if confirm("Close this pane?"):
        herdr.call("pane.close", pane_id=context.pane)


def register_focus_commands() -> None:
    for direction in ("left", "right", "up", "down"):
        def focus(herdr: Herdr, context: Context, direction: str = direction) -> None:
            herdr.call("pane.focus_direction", pane_id=context.pane, direction=direction)
        herdr_command(f"focus_pane_{direction}", f"Pane: Focus {direction}")(focus)


register_focus_commands()


@herdr_command("new_workspace", "Workspace: New workspace here")
def new_workspace(herdr: Herdr, context: Context) -> None:
    herdr.call("workspace.create", cwd=context.folder, focus=True)


@herdr_command("rename_workspace", "Workspace: Rename workspace")
def rename_workspace(herdr: Herdr, context: Context) -> None:
    if name := ask("New workspace name: "):
        herdr.call("workspace.rename", workspace_id=context.workspace, label=name)


@herdr_command("close_workspace", "Workspace: Close workspace")
def close_workspace(herdr: Herdr, context: Context) -> None:
    if confirm("Close this workspace and all its tabs?"):
        herdr.call("workspace.close", workspace_id=context.workspace)


def focus_neighbour_workspace(herdr: Herdr, context: Context, step: int) -> None:
    workspaces = herdr.call("workspace.list").get("workspaces", [])
    if target := neighbour([workspace["workspace_id"] for workspace in workspaces], context.workspace, step):
        herdr.call("workspace.focus", workspace_id=target)


@herdr_command("next_workspace", "Workspace: Next workspace")
def next_workspace(herdr: Herdr, context: Context) -> None:
    focus_neighbour_workspace(herdr, context, 1)


@herdr_command("previous_workspace", "Workspace: Previous workspace")
def previous_workspace(herdr: Herdr, context: Context) -> None:
    focus_neighbour_workspace(herdr, context, -1)


@herdr_command("reload_config", "Herdr: Reload configuration")
def reload_config(herdr: Herdr, context: Context) -> None:
    herdr.call("server.reload_config")


# ---------------------------------------------------------------- rows

def herdr_command_rows(herdr: Herdr, config: Config) -> list[Row]:
    return [Row(label, config.key_text(config.keys.get(action, [])),
                lambda context, command=command: command(herdr, context))
            for action, (label, command) in HERDR_COMMANDS.items()]


def plugin_action_rows(herdr: Herdr, config: Config, own_plugin_id: str) -> list[Row]:
    rows = []
    for action in herdr.call("plugin.action.list").get("actions", []):
        qualified = f"{action['plugin_id']}.{action['action_id']}"
        if PLATFORM not in action.get("platforms", [PLATFORM]) or action["plugin_id"] == own_plugin_id:
            continue
        keys = [command["key"] for command in config.custom_commands if invoked_plugin_action(command) == qualified]

        def invoke(context: Context, qualified: str = qualified) -> None:
            invocation = {"focused_pane_id": context.pane, "tab_id": context.tab, "workspace_id": context.workspace,
                          "focused_pane_cwd": context.folder, "invocation_source": own_plugin_id}
            herdr.call("plugin.action.invoke", action_id=qualified, context=invocation)

        label = f"Plugin: {action['plugin_id']} › {action.get('title') or action['action_id']}"
        rows.append(Row(label, config.key_text(keys), invoke))
    return rows


def run_custom_command(command: dict[str, str], context: Context) -> None:
    if command.get("type", "shell") == "shell":
        subprocess.Popen(["sh", "-c", command["command"]], cwd=context.folder or None, start_new_session=True,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        # A popup or pane command needs a terminal, and takes over the palette's popup.
        os.execvp("sh", ["sh", "-c", command["command"]])


def custom_command_rows(config: Config) -> list[Row]:
    return [Row(f"Command: {command.get('description') or command['command']}",
                config.key_text([command.get("key", "")]),
                lambda context, command=command: run_custom_command(command, context))
            for command in config.custom_commands if not invoked_plugin_action(command)]


def jump_rows(herdr: Herdr) -> list[Row]:
    workspaces = herdr.call("workspace.list").get("workspaces", [])
    names = {workspace["workspace_id"]: workspace.get("label") or workspace["workspace_id"] for workspace in workspaces}
    rows = [Row(f"Go to workspace: {names[workspace['workspace_id']]}", "",
                lambda context, target=workspace["workspace_id"]: herdr.call("workspace.focus", workspace_id=target))
            for workspace in workspaces]
    rows += [Row(f"Go to tab: {names.get(tab['workspace_id'], tab['workspace_id'])} › {tab.get('label') or tab.get('number')}",
                 "", lambda context, target=tab["tab_id"]: herdr.call("tab.focus", tab_id=target))
             for tab in herdr.call("tab.list").get("tabs", [])]
    for agent in herdr.call("agent.list").get("agents", []):
        status = agent.get("agent_status") or "unknown"
        title = agent.get("terminal_title_stripped") or agent.get("agent")
        label = f"Go to agent: {STATUS_GLYPHS.get(status, '·')} {title} · {names.get(agent['workspace_id'], '')} ({status})"
        rows.append(Row(label, "", lambda context, target=agent["pane_id"]: herdr.call("agent.focus", target=target)))
    return rows


def client_action_rows(config: Config) -> list[Row]:
    rows = []
    for action, label in CLIENT_ACTIONS.items():
        if key := config.key_text(config.keys.get(action, [])):
            message = f"Only the Herdr client runs this command: press {key}."
            rows.append(Row(label, key, lambda context, message=message: show_for_a_moment(message)))
    return rows


def all_rows(herdr: Herdr, config: Config, own_plugin_id: str) -> list[Row]:
    return (herdr_command_rows(herdr, config) + plugin_action_rows(herdr, config, own_plugin_id)
            + custom_command_rows(config) + jump_rows(herdr) + client_action_rows(config))


# ---------------------------------------------------------------- entry points

def pick_with_fzf(rows: list[Row]) -> Optional[Row]:
    width = max(len(row.label) for row in rows) + 2
    lines = [f"{row.label.ljust(width)}\x1b[2m{row.key}\x1b[0m\t{index}" for index, row in enumerate(rows)]
    fzf = subprocess.run(
        ["fzf", "--ansi", "--delimiter", "\t", "--with-nth", "1", "--nth", "1", "--no-sort",
         "--tiebreak", "begin", "--layout", "reverse", "--prompt", "› ", "--info", "inline-right",
         "--no-scrollbar", "--header", "Enter runs · Esc closes"],
        input="\n".join(lines), stdout=subprocess.PIPE, text=True,
    )
    if fzf.returncode != 0 or not fzf.stdout.strip():
        return None
    return rows[int(fzf.stdout.rstrip("\n").split("\t")[-1])]


def show_palette(herdr: Herdr, config: Config, context: Context, own_plugin_id: str) -> None:
    if not shutil.which("fzf"):
        ask("The command palette needs fzf on the PATH. Press Enter to close.")
        return
    try:
        if row := pick_with_fzf(all_rows(herdr, config, own_plugin_id)):
            row.run(context)
    except (OSError, HerdrError, ValueError) as error:
        ask(f"{error}\n  Press Enter to close.")


def open_popup(herdr: Herdr, context: Context, own_plugin_id: str) -> None:
    """A popup always opens over the active pane and starts in the plugin folder, so the
    request names neither a pane nor a folder, and the context travels in PALETTE_CONTEXT."""
    herdr.call("plugin.pane.open", plugin_id=own_plugin_id, entrypoint="palette", placement="popup",
               env={"PALETTE_CONTEXT": json.dumps(context.__dict__)}, focus=True)


def invocation_context(environment: dict[str, str]) -> Context:
    """The context of a plugin action, from the variables that Herdr gives it."""
    details = json.loads(environment.get("HERDR_PLUGIN_CONTEXT_JSON") or "{}")
    return Context(
        pane=environment.get("HERDR_PANE_ID") or details.get("focused_pane_id"),
        tab=environment.get("HERDR_TAB_ID") or details.get("tab_id"),
        workspace=environment.get("HERDR_WORKSPACE_ID") or details.get("workspace_id"),
        folder=details.get("focused_pane_cwd") or details.get("workspace_cwd"),
    )


def main() -> None:
    herdr = Herdr(os.environ.get("HERDR_SOCKET_PATH") or DEFAULT_SOCKET_PATH)
    own_plugin_id = os.environ.get("HERDR_PLUGIN_ID", "command-palette")
    if "--open" in sys.argv:
        open_popup(herdr, invocation_context(dict(os.environ)), own_plugin_id)
        return
    config = read_config(os.environ.get("HERDR_BIN_PATH") or shutil.which("herdr") or "herdr")
    context = Context(**json.loads(os.environ.get("PALETTE_CONTEXT") or "{}"))
    if "--list" in sys.argv:
        for row in all_rows(herdr, config, own_plugin_id):
            print(f"{row.label}  [{row.key}]")
    else:
        show_palette(herdr, config, context, own_plugin_id)


if __name__ == "__main__":
    main()
