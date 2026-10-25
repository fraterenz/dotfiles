local M = {}

local function set(n)
  vim.bo.tabstop = n
  vim.bo.shiftwidth = n
  vim.bo.softtabstop = n
end

function M.two() set(2) end
function M.four() set(4) end
function M.eight() set(8) end

function M.text()
  set(2)
  vim.bo.textwidth = 79
  vim.bo.smartindent = true
  vim.opt_local.formatoptions:append('w')
end

return M
