fish_add_path -g ~/.local/bin

if status is-interactive
    # Commands to run in interactive sessions can go here
    function fish_mode_prompt
    end
end

set -g fish_key_bindings fish_vi_key_bindings
bind -M insert \cc kill-whole-line repaint

zoxide init fish | source

if command -q nvim
    set -gx EDITOR nvim
else
    set -gx EDITOR vi
end

set -U fish_greeting

set fish_pager_color_selected_background --background=brwhite
# ssh agent
set -gx SSH_AUTH_SOCK "$XDG_RUNTIME_DIR/ssh-agent.socket"
