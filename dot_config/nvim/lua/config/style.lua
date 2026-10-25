vim.api.nvim_create_autocmd('FileType', {
  pattern = { 'python', 'rust', 'tex', 'sh', 'zig' },
  callback = function() vim.opt_local.colorcolumn = '80' end,
})

pcall(vim.cmd.colorscheme, 'dayfox')
