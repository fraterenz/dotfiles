require('config.spacing').four()
vim.opt_local.foldmethod = 'expr'
vim.opt_local.foldexpr   = 'v:lua.vim.treesitter.foldexpr()'

local function lsp(cmd)
  return function() vim.cmd.RustLsp(cmd) end
end

local map  = vim.keymap.set
local opts = function(desc) return { buffer = 0, silent = true, desc = desc } end

local function has_rustc_render(d)
  return d.source == 'rustc'
    or vim.tbl_get(d, 'user_data', 'lsp', 'data', 'rendered') ~= nil
end

local function render_or_float()
  local lnum = vim.fn.line('.') - 1
  for _, d in ipairs(vim.diagnostic.get(0, { lnum = lnum })) do
    if has_rustc_render(d) then
      vim.cmd.RustLsp('renderDiagnostic')
      return
    end
  end
  vim.diagnostic.open_float()
end

map('n', 'K',          lsp({ 'hover', 'actions' }), opts('Rust: hover + actions'))
map('n', 'gra',        lsp('codeAction'),           opts('Rust: code action'))
map('n', '<leader>d',  render_or_float,             opts('Rust: render diagnostic (or float)'))

map('n', '<leader>xe', lsp('explainError'),         opts('Rust: explain error'))
map('n', '<leader>xr', lsp('runnables'),            opts('Rust: runnables'))
map('n', '<leader>xD', lsp('debuggables'),          opts('Rust: debuggables'))
map('n', '<leader>xp', lsp('parentModule'),         opts('Rust: parent module'))
map('n', '<leader>xj', lsp('joinLines'),            opts('Rust: join lines'))
