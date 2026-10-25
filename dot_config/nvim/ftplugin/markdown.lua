-- Must run BEFORE the runtime markdown ftplugin, which reads g:markdown_folding
-- at filetype-detect time to decide whether to set foldmethod=expr. Setting it
-- in after/ftplugin/markdown.lua would be too late.
vim.g.markdown_folding = 1
