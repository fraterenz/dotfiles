function fish_mode_prompt
end

#function fish_mode_prompt
#  switch $fish_bind_mode
#    case default
#      set_color --bold red
#      echo 'N'
#    case insert
#      set_color --bold green
#      echo 'I'
#    case replace_one
#      set_color --bold green
#      echo 'R'
#    case visual
#      set_color --bold brmagenta
#      echo 'V'
#    case '*'
#      set_color --bold red
#      echo '?'
#  end
#  set_color normal
#end

function fish_prompt --description 'Write out the prompt'
    set -l laststatus $status

    set -l git_info
    if git rev-parse 2>/dev/null
        set -l git_branch (
            command git symbolic-ref HEAD 2>/dev/null | string replace 'refs/heads/' ''
            or command git describe HEAD 2>/dev/null
            or echo unknown
        )
        set git_branch (set_color -o magenta)"$git_branch"
        set -l git_status
        if git rev-parse --quiet --verify HEAD >/dev/null
            and not command git diff-index --quiet HEAD --

            for i in (git status --porcelain | string sub -l 2 | sort | uniq)
                switch $i
                    case "."
                        set git_status "$git_status"(set_color black)✚
                    case " D"
                        set git_status "$git_status"(set_color black)-
                    case "*M*"
                        set git_status "$git_status"(set_color black)✱
                    case "*R*"
                        set git_status "$git_status"(set_color black)➜
                    case "*U*"
                        set git_status "$git_status"(set_color black)═
                    case "??"
                        set git_status "$git_status"(set_color black)≠
                end
            end
            set git_info " $git_status $git_branch"
        else
            set git_info " $git_branch"
        end
    end

    set -q fish_prompt_pwd_dir_length=2
    ## Disable PWD shortening by default.
    #set -q fish_prompt_pwd_dir_length
    #or set -lx fish_prompt_pwd_dir_length 0

    # set_color -b none
    printf '%s%s %s' (set_color -o cyan) $USER (set_color -o blue) (prompt_pwd) $git_info
    if test $laststatus -ne 0
        string join '' -- (set_color red) '✘' ' '
    end
    # if test $laststatus -eq 0
    #     printf "%s✔  " (set_color green)
    # else
    #     printf "%s✘ " (set_color red)
    # end
end
