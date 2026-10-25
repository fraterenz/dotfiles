-- Override markdown injections to avoid a buggy custom directive
-- (`set-lang-from-info-string!`) shipped by the system-installed
-- nvim-treesitter. The plugin's directive uses the pre-0.10 match API
-- (single node per capture) but Neovim 0.10+ passes a list of nodes,
-- so node:range(true) blows up at runtime/lua/vim/treesitter.lua:196.
-- This mirrors the clean query Neovim ships at
-- /usr/share/nvim/runtime/queries/markdown/injections.scm.
vim.treesitter.query.set('markdown', 'injections', [[
(fenced_code_block
  (info_string
    (language) @injection.language)
  (code_fence_content) @injection.content)

((html_block) @injection.content
  (#set! injection.language "html")
  (#set! injection.combined)
  (#set! injection.include-children))

((minus_metadata) @injection.content
  (#set! injection.language "yaml")
  (#offset! @injection.content 1 0 -1 0)
  (#set! injection.include-children))

((plus_metadata) @injection.content
  (#set! injection.language "toml")
  (#offset! @injection.content 1 0 -1 0)
  (#set! injection.include-children))

([
  (inline)
  (pipe_table_cell)
] @injection.content
  (#set! injection.language "markdown_inline"))
]])
