-- Set vim.g.rustaceanvim before rustaceanvim's plugin/ files load.
-- rust-analyzer is wired by rustaceanvim, not by vim.lsp.config below.
vim.g.rustaceanvim = {
  server = {
    default_settings = {
      ['rust-analyzer'] = {
        cargo = { features = 'all' },
        check = {
          command = 'clippy',
          extraArgs = { '--no-deps' },
        },
        checkOnSave = true,
      },
    },
  },
}

vim.lsp.config('ruff', {
  cmd = { 'ruff', 'server' },
  filetypes = { 'python' },
  root_markers = { 'pyproject.toml', '.git' },
})

vim.lsp.config('ty', {
  cmd = { 'ty', 'server' },
  filetypes = { 'python' },
  root_markers = { 'pyproject.toml', '.venv', '.git' },
})

vim.lsp.config('gopls', {
  cmd = { 'gopls' },
  filetypes = { 'go', 'gomod', 'gowork' },
  root_markers = { 'go.mod', 'go.work', '.git' },
})

vim.lsp.enable({ 'ruff', 'ty', 'gopls' })

require('gitsigns').setup()

require'nvim-treesitter.configs'.setup{
  highlight = { enable = true },
}

if vim.o.shell:match('fish$') then
  vim.o.shell = 'sh'
end

vim.opt.encoding = 'utf-8'
vim.opt.backup = false
vim.opt.writebackup = false
vim.opt.cmdheight = 2
vim.opt.updatetime = 300
vim.opt.shortmess:append('c')
vim.opt.signcolumn = 'yes'
