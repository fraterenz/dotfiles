-- Native ripgrep search: argv execution, JSON results, and the built-in quickfix
-- list. No undeclared fuzzy-finder plugin or shell interpolation is required.
local function result_text(value)
  return value.text or vim.base64.decode(value.bytes)
end

local function search(pattern)
  if vim.fn.executable('rg') ~= 1 then
    vim.notify('Ripgrep (rg) is unavailable; the devbox image must provide it.', vim.log.levels.ERROR)
    return
  end

  local cwd = vim.fn.getcwd()
  local result = vim.system({ 'rg', '--no-config', '--json', '--smart-case', '--', pattern, '.' }, {
    cwd = cwd,
    text = true,
  }):wait()
  if result.code ~= 0 and result.code ~= 1 then
    vim.notify('Ripgrep failed: ' .. vim.trim(result.stderr or ''), vim.log.levels.ERROR)
    return
  end

  local items = {}
  for line in (result.stdout or ''):gmatch('[^\n]+') do
    local event = vim.json.decode(line)
    if event.type == 'match' then
      local data = event.data
      local filename = vim.fs.joinpath(cwd, result_text(data.path))
      local text = result_text(data.lines):gsub('[\r\n]+$', '')
      for _, match in ipairs(data.submatches) do
        items[#items + 1] = {
          filename = filename,
          lnum = data.line_number,
          col = match.start + 1,
          text = text,
        }
      end
    end
  end

  vim.fn.setqflist({}, ' ', { title = 'Rg: ' .. pattern, items = items })
  if #items > 0 then
    vim.cmd('copen')
  else
    vim.cmd('cclose')
    vim.notify('No matches for: ' .. pattern, vim.log.levels.INFO)
  end
end

vim.api.nvim_create_user_command('Rg', function(options)
  if options.args ~= '' then
    search(options.args)
    return
  end
  vim.ui.input({ prompt = 'Ripgrep: ' }, function(pattern)
    if pattern and pattern ~= '' then
      search(pattern)
    end
  end)
end, { nargs = '*', bar = false, desc = 'Search the current directory with ripgrep (quickfix)' })
