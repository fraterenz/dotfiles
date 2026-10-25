vim.g.mapleader      = ' '
vim.g.maplocalleader = ' '

local map = vim.keymap.set

map({ 'n', 'v' }, '<Space>', '<Nop>', { silent = true })

map({ 'n', 'v' }, 'Q', ":put=''<CR>", { silent = true })

map('n', '<ESC><ESC>', ':nohlsearch<CR>', { silent = true })
map('n', '<Leader><space>', ':noh<CR>', { silent = true })

local function paragraph_filter(tabstop, width)
  return function()
    if vim.fn.executable('par') ~= 1 then
      vim.schedule(function()
        vim.notify('par is unavailable; the devbox image must provide it.', vim.log.levels.ERROR)
      end)
      return '<Esc>'
    end
    return string.format("!par T%d 'B=.,?_A_a' %dqr<CR>", tabstop, width)
  end
end

map('v', '<Leader>f', paragraph_filter(4, 71), { expr = true, desc = 'Reflow paragraph to 71 columns' })
map('n', '<Leader>f', '<S-v><Leader>f', { remap = true })
map('v', '<Leader>g', paragraph_filter(4, 79), { expr = true, desc = 'Reflow paragraph to 79 columns' })
map('n', '<Leader>g', '<S-v><Leader>g', { remap = true })
map('v', '<Leader>h', paragraph_filter(8, 79), { expr = true, desc = 'Reflow paragraph with 8-column tabs' })
map('n', '<Leader>h', '<S-v><Leader>h', { remap = true })

map({ 'n', 'v' }, '<F10>', ':syntax sync fromstart<CR>', { silent = true })

map('n', '<Leader>r', ':Rg<CR>', { desc = 'Ripgrep search (quickfix)' })
map('n', '<Leader>d', vim.diagnostic.open_float, { silent = true, desc = 'Diagnostic float' })

map('n', '<C-J>', '<C-W><C-J>')
map('n', '<C-K>', '<C-W><C-K>')
map('n', '<C-L>', '<C-W><C-L>')
map('n', '<C-H>', '<C-W><C-H>')

vim.opt.splitbelow = true
vim.opt.splitright = true
