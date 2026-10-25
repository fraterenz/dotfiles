require('config.spacing').four()
vim.bo.makeprg = 'clear; python3 %'
vim.bo.errorformat = [[%A %#File "%f"\, line %l\,%.%#,%-GTrace%.%#,%C %.%#,%Z%m,%-G%.%#]]
vim.opt_local.foldmethod = 'expr'
vim.opt_local.foldexpr   = 'v:lua.vim.treesitter.foldexpr()'
