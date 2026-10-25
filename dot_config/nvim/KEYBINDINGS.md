# Neovim keybindings cheatsheet

Leader is **space**. LSP keys lean on Neovim's built-in defaults; rust buffers
shadow some of them with richer `rustaceanvim` (`RustLsp`) variants.

## Sanity check

```sh
nvim --headless -c 'lua print("leader=[" .. vim.g.mapleader .. "]")' -c q
# expect: leader=[ ]
```

## Generic LSP keys (work in any language with an LSP attached)

| Key            | Action                                  | Source           |
|----------------|-----------------------------------------|------------------|
| `K`            | Hover                                   | Neovim default   |
| `gra`          | Code action                             | Neovim default   |
| `crn`          | Rename                                  | Neovim default   |
| `grr`          | References                              | Neovim default   |
| `[d` / `]d`    | Prev / next diagnostic                  | Neovim default   |
| `<leader>d`    | Diagnostic float at cursor              | `keybinds.lua`   |
| `<leader>f`    | LSP format buffer                       | `autocmds.lua` (on `LspAttach`) |

## Global (non-LSP) keys

| Key                  | Action                                  |
|----------------------|-----------------------------------------|
| `<leader><leader>`   | Clear search highlight (`:noh`)          |
| `<leader>f` (visual) | `par` formatter, width 71 (markdown etc.) |
| `<leader>g` (visual) | `par` formatter, width 79                |
| `<leader>h` (visual) | `par` formatter, T8 / width 79           |
| `<leader>r`          | Prompt for ripgrep search, open quickfix results |
| `<ESC><ESC>`         | Clear search highlight                   |
| `<C-J/K/L/H>`        | Window navigation                        |
| `Q`                  | Insert blank line                        |
| `<F10>`              | Re-sync syntax from start                |

`:Rg PATTERN` searches the current working directory directly; `:Rg` or
`<leader>r` prompts for a pattern. Search uses smart case and ripgrep's usual
ignore rules, then opens Neovim's built-in quickfix list (no search plugin
required). Use `<CR>` on a result, `:cnext`, and `:cprev` to navigate. Patterns
are passed directly to `rg`, not interpreted by a shell. Invalid patterns leave
the previous quickfix list intact; no matches clear it.

The image provides both `rg` and `par`, and activation checks require them.
The formatting mappings refuse to filter text when `par` is missing, instead of
replacing the selection with a shell error. Normal-mode `<leader>f/g/h` formats
the current line; visual mode formats the selection (LSP buffers override only
the normal-mode `<leader>f` mapping).

## Per-filetype behavior

### Python (`ruff` + `ty` LSP)

| Key / event   | Action                                  |
|---------------|-----------------------------------------|
| `<leader>f`   | LSP format                              |
| `:w`          | Format on save                          |
| `<leader>i`   | Organize imports (code action)          |

### Go (`gopls`)

| Key / event   | Action                                  |
|---------------|-----------------------------------------|
| `<leader>f`   | gopls format                            |
| `:w`          | Organize imports, then format           |

### Rust (`rustaceanvim`)

| Key            | Action                                                          |
|----------------|-----------------------------------------------------------------|
| `K`            | `RustLsp { 'hover', 'actions' }` — hover with action menu      |
| `gra`          | `RustLsp codeAction` — grouped code actions                    |
| `<leader>d`    | rustc rendered snippet if available, else `vim.diagnostic.open_float()` |
| `<leader>f`    | LSP format (rust-analyzer → rustfmt)                            |
| `<leader>xe`   | `RustLsp explainError` — `rustc --explain Exxxx`               |
| `<leader>xr`   | `RustLsp runnables` — runnables picker                         |
| `<leader>xD`   | `RustLsp debuggables` — debuggables picker                     |
| `<leader>xp`   | `RustLsp parentModule` — jump to parent module                 |
| `<leader>xj`   | `RustLsp joinLines` — rust-aware join                          |

Cursor must be on a relevant token for `explainError` (error code) and
`renderDiagnostic` (line with a diagnostic). `<leader>d` only routes to
`renderDiagnostic` when the diagnostic on the line came from rustc/clippy
(has a `data.rendered` field); rust-analyzer-source diagnostics (most type
errors flagged before save) fall through to the standard diagnostic float.

If any rust row errors with `attempt to index field 'cmd'` or
`Unknown function: RustLsp`, `rustaceanvim` is not installed.

## Global save behavior

- Trailing whitespace is stripped on every `:w` (cursor position preserved).
