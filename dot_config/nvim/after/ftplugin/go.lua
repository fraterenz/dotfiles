require('config.spacing').eight()
vim.bo.expandtab = false
vim.opt_local.colorcolumn = '100'

vim.keymap.set('n', '<Leader>i', function()
  vim.lsp.buf.code_action({
    context = { only = { 'source.organizeImports' }, diagnostics = {} },
    apply = true,
  })
end, { buffer = 0 })
