# herdr-command-palette

A command palette for the [Herdr](https://herdr.dev) TUI, like `cmd+shift+p` in an editor.
One key opens a popup over the active pane, you type a few letters, and Enter runs the command.
Every row shows the key that runs the same command, so the palette also teaches the keys.

```text
› split
  Pane: Split right    ctrl+b v
  Pane: Split down     ctrl+b minus
```

## The palette lists every command that a key can run, and more

- Herdr's own commands create, rename, close and switch tabs and workspaces, split, zoom, rename, close and focus panes, and reload the configuration.
  They act on the pane, tab and workspace from which you opened the palette.
  A rename asks for the name, and a close asks for a confirmation.
- Every action of every enabled plugin is a row, with the key you bound to it.
- The `[[keys.command]]` entries of your `config.toml` are rows.
- A jump goes to any workspace, any tab, or any agent, which shows its status.
- Settings, copy mode, detach, the goto picker and the other commands that only the Herdr client runs are rows as well.
  No API reaches them, so the palette shows their key instead of running them.

The keys come from the defaults of your Herdr version and from your `~/.config/herdr/config.toml`.

## Requirements

- Herdr 0.9.5 or later, on macOS or Linux
- Python 3.9 or later, as `python3` on the `PATH` of the Herdr server
- [fzf](https://github.com/junegunn/fzf) on the same `PATH`

## Install

```sh
herdr plugin install HuangYujun-JunkyTalent/herdr-command-palette
```

Herdr binds no key for a plugin, so add one to `~/.config/herdr/config.toml`:

```toml
[[keys.command]]
key = "prefix+space"
type = "plugin_action"
command = "command-palette.open"
description = "Command palette"
```

Then reload the configuration with `prefix+shift+r` or `herdr server reload-config`.

## Open it with cmd+shift+p on macOS

A terminal on macOS keeps the Cmd shortcuts for its own menus, so Herdr never sees `cmd+shift+p`.
Map the shortcut in the terminal to `ctrl+alt+p`, which every common terminal passes to the program, and bind that chord in Herdr.

1. Bind `ctrl+alt+p` in `~/.config/herdr/config.toml`, as above with `key = "ctrl+alt+p"`, and reload.
2. Make the terminal send the bytes of `ctrl+alt+p`, which are `0x1b 0x10`, on `cmd+shift+p`:
   - In iTerm2, open Settings → Profiles → Keys → Key Mappings → **+**, record `⇧⌘P`, choose the action **Send Hex Codes**, and enter `0x1b 0x10`.
   - In Ghostty, add `keybind = super+shift+p=text:\x1b\x10` to the configuration and reload it.

A `prefix` binding, such as `prefix+space`, still serves a terminal without a Cmd key, such as an SSH client on a phone.

## The action opens a popup, and the popup runs fzf against the Herdr API

A plugin action runs without a terminal, so the action `command-palette.open` does one thing: it opens the plugin pane `palette` as a popup.
It passes the pane, tab, workspace and folder of the invocation to the popup in the variable `PALETTE_CONTEXT`.
In the popup, `palette.py` reads the commands from the Herdr API on `HERDR_SOCKET_PATH`, lets fzf pick one, and runs it through the same API.
`python3 palette.py --list` prints the rows outside Herdr.

## Related plugins

- [herdr-navigator](https://github.com/thanhdat77/herdr-navigator) jumps to workspaces, agents, projects, sessions, remotes and directories.
- [herdr-bar](https://github.com/jeffarese/herdr-bar) searches agents, tabs and workspaces, with a live preview.
- [JanTvrdik/herdr-command-palette](https://github.com/JanTvrdik/herdr-command-palette) lists the actions of the installed plugins.

## Licence

[MIT](LICENSE)
