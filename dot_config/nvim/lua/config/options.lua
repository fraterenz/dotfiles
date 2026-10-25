vim.opt.number = true
vim.opt.expandtab = true
vim.opt.textwidth = 0
vim.opt.hidden = true
vim.opt.synmaxcol = 400
vim.opt.incsearch = true
vim.opt.scrolloff = 10
vim.opt.formatoptions = 'tcro'
vim.opt.wildmenu = true
vim.opt.wildmode = 'list:longest,full'

vim.opt.tabstop = 2
vim.opt.shiftwidth = 2
vim.opt.softtabstop = 2

local osc52 = require('vim.ui.clipboard.osc52')

-- paste returns the last in-editor yank; it never queries the terminal,
-- so Neovim performs zero clipboard *reads*.
local function paste()
  return { vim.fn.split(vim.fn.getreg('"'), '\n'), vim.fn.getregtype('"') }
end

vim.g.clipboard = {
  name  = 'OSC 52 (copy out, no reads)',
  copy  = { ['+'] = osc52.copy('+'), ['*'] = osc52.copy('*') },
  paste = { ['+'] = paste,           ['*'] = paste },
}
vim.opt.clipboard = 'unnamedplus'
