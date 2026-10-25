local lsp_group = vim.api.nvim_create_augroup('EnvironmentLspConfig', { clear = true })

vim.api.nvim_create_autocmd('LspAttach', {
  group = lsp_group,
  callback = function(args)
    local buf = args.buf
    local ft  = vim.bo[buf].filetype

    vim.keymap.set('n', '<leader>f', vim.lsp.buf.format,
      { buffer = buf, desc = 'LSP: format buffer' })

    vim.keymap.set('n', 'gd', vim.lsp.buf.definition,
      { buffer = buf, desc = 'LSP: go to definition' })

    if ft == 'python' or ft == 'rust' then
      vim.api.nvim_create_autocmd('BufWritePre', {
        group = lsp_group,
        buffer = buf,
        callback = function() vim.lsp.buf.format() end,
      })
    elseif ft == 'go' then
      vim.api.nvim_create_autocmd('BufWritePre', {
        group = lsp_group,
        buffer = buf,
        callback = function()
          local params = vim.lsp.util.make_range_params(0, 'utf-8')
          params.context = { only = { 'source.organizeImports' }, diagnostics = {} }
          local result = vim.lsp.buf_request_sync(buf, 'textDocument/codeAction', params, 1000)
          for _, res in pairs(result or {}) do
            for _, action in pairs(res.result or {}) do
              if action.edit then
                vim.lsp.util.apply_workspace_edit(action.edit, 'utf-8')
              end
            end
          end
          vim.lsp.buf.format({ async = false })
        end,
      })
    end
  end,
})

vim.api.nvim_create_autocmd('BufWritePre', {
  pattern = '*',
  callback = function()
    local l = vim.fn.line('.')
    local c = vim.fn.col('.')
    pcall(vim.cmd, [[%s/\s\+$//e]])
    vim.fn.cursor(l, c)
  end,
})
