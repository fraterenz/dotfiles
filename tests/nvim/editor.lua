-- Injected source modules run with -u NONE, no plugins, no shada, and no swap.
local root = vim.fn.tempname()
local original_cwd = vim.fn.getcwd()
vim.fn.mkdir(root, 'p')
local notices = {}
vim.notify = function(message, level)
  notices[#notices + 1] = { message = message, level = level }
end

local function equal(expected, actual)
  assert(vim.deep_equal(expected, actual), vim.inspect({ expected = expected, actual = actual }))
end

local function feed(keys)
  vim.api.nvim_feedkeys(vim.api.nvim_replace_termcodes(keys, true, false, true), 'xt', false)
end

local function run()
  assert(vim.fn.executable('rg') == 1, 'rg is required for these runtime tests')
  assert(vim.fn.executable('par') == 1, 'par is required for these runtime tests')
  vim.cmd('cd ' .. vim.fn.fnameescape(root))
  vim.opt.shell = '/bin/sh'
  assert(loadstring(sources.search, '@config/search.lua'))()
  assert(loadstring(sources.keybinds, '@config/keybinds.lua'))()
  equal(2, vim.fn.exists(':Rg'))
  equal(':Rg<CR>', vim.fn.maparg('<Space>r', 'n'))

  local filename = root .. '/a file:with spaces.txt'
  vim.fn.writefile({ 'before', 'alpha needle here', 'needle two needle', '-needle' }, filename)
  vim.fn.mkdir(root .. '/.git', 'p')
  vim.fn.writefile({ 'ignored.txt' }, root .. '/.gitignore')
  vim.fn.writefile({ 'needle' }, root .. '/ignored.txt')
  vim.fn.writefile({ 'needle' }, root .. '/.hidden.txt')
  vim.fn.writefile({ 'MiXeDToken' }, root .. '/case.txt')

  vim.cmd('Rg needle')
  local matches = vim.fn.getqflist()
  equal(4, #matches)
  equal(filename, vim.api.nvim_buf_get_name(matches[1].bufnr))
  equal({ 2, 7, 'alpha needle here' }, { matches[1].lnum, matches[1].col, matches[1].text })
  equal({ 1, 12 }, { matches[2].col, matches[3].col })
  equal(1, #vim.tbl_filter(function(win) return win.quickfix == 1 end, vim.fn.getwininfo()))
  vim.cmd('cfirst')
  equal(filename, vim.api.nvim_buf_get_name(0))
  equal({ 2, 6 }, vim.api.nvim_win_get_cursor(0))

  vim.cmd('Rg mixedtoken')
  equal(1, #vim.fn.getqflist())
  vim.cmd('Rg MIXEDTOKEN')
  equal(0, #vim.fn.getqflist())
  equal(0, #vim.tbl_filter(function(win) return win.quickfix == 1 end, vim.fn.getwininfo()))
  equal(vim.log.levels.INFO, notices[#notices].level)

  vim.cmd('Rg -needle')
  equal(1, #vim.fn.getqflist())
  equal(4, vim.fn.getqflist()[1].lnum)
  local list_id = vim.fn.getqflist({ id = 0 }).id
  vim.cmd('Rg [')
  equal(list_id, vim.fn.getqflist({ id = 0 }).id)
  equal(vim.log.levels.ERROR, notices[#notices].level)
  assert(notices[#notices].message:find('Ripgrep failed:', 1, true))

  -- A bar is regex syntax, not an Ex-command separator; shell punctuation is
  -- literal argv data and must not create a file.
  vim.cmd('Rg needle|never_match_this')
  equal(4, #vim.fn.getqflist())
  local injected = root .. '/INJECTED'
  vim.cmd('Rg ; touch ' .. injected)
  equal(0, vim.fn.filereadable(injected))

  local input = vim.ui.input
  vim.ui.input = function(options, callback)
    equal('Ripgrep: ', options.prompt)
    callback('needle')
  end
  vim.cmd('Rg')
  equal(4, #vim.fn.getqflist())
  list_id = vim.fn.getqflist({ id = 0 }).id
  for _, response in ipairs({ false, '' }) do
    vim.ui.input = function(_, callback) callback(response or nil) end
    vim.cmd('Rg')
    equal(list_id, vim.fn.getqflist({ id = 0 }).id)
  end
  vim.ui.input = input

  -- Ripgrep encodes invalid-UTF8 file names/lines as base64 in its JSON stream.
  local bytes_filename = root .. '/raw-' .. string.char(255) .. '.txt'
  vim.fn.writefile({ 'bytes-case' }, bytes_filename)
  vim.cmd('Rg bytes-case')
  equal(bytes_filename, vim.api.nvim_buf_get_name(vim.fn.getqflist()[1].bufnr))
  local bytes_line = 'raw ' .. string.char(255) .. ' line-marker'
  vim.fn.writefile({ bytes_line }, root .. '/raw-line.txt')
  vim.cmd('Rg line-marker')
  equal(bytes_line, vim.fn.getqflist()[1].text)

  local executable = vim.fn.executable
  vim.fn.executable = function(tool) return tool == 'rg' and 0 or executable(tool) end
  list_id = vim.fn.getqflist({ id = 0 }).id
  vim.cmd('Rg needle')
  equal(list_id, vim.fn.getqflist({ id = 0 }).id)
  assert(notices[#notices].message:find('unavailable', 1, true))
  vim.fn.executable = executable

  vim.cmd('cclose')
  vim.cmd('enew!')
  local paragraph = string.rep('This is a paragraph with several words to reflow. ', 6):gsub('%s+$', '')
  for _, example in ipairs({ { 'f', 71 }, { 'g', 79 }, { 'h', 79 } }) do
    vim.api.nvim_buf_set_lines(0, 0, -1, false, { paragraph })
    feed('ggVG ' .. example[1])
    local lines = vim.api.nvim_buf_get_lines(0, 0, -1, false)
    assert(#lines > 1, 'The real visual par mapping did not reflow text')
    equal(paragraph, table.concat(lines, ' '))
    for _, line in ipairs(lines) do
      assert(vim.fn.strdisplaywidth(line) <= example[2], 'par exceeded the requested width')
    end
  end
  vim.api.nvim_buf_set_lines(0, 0, -1, false, { paragraph })
  feed('gg0 f')
  assert(vim.api.nvim_buf_line_count(0) > 1, 'The normal-mode par mapping did not reflow text')

  local protected = { 'Keep this text exactly as it is.' }
  vim.api.nvim_buf_set_lines(0, 0, -1, false, protected)
  vim.fn.executable = function(tool) return tool == 'par' and 0 or executable(tool) end
  feed('gg0 f')
  vim.wait(100, function() return notices[#notices].message:find('par is unavailable', 1, true) ~= nil end)
  equal(protected, vim.api.nvim_buf_get_lines(0, 0, -1, false))
  equal(vim.log.levels.ERROR, notices[#notices].level)
  vim.fn.executable = executable
end

local ok, error = xpcall(run, debug.traceback)
vim.cmd('cd ' .. vim.fn.fnameescape(original_cwd))
vim.fn.delete(root, 'rf')
if not ok then
  io.stderr:write(error .. '\n')
  vim.cmd('cquit 1')
end
print('Editor search/formatter checks passed')
vim.cmd('qa!')
