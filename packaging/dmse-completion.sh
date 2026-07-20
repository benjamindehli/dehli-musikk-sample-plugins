# Shell completion for the ./dmse workflow CLI (works in zsh and bash).
#
# Enable it by sourcing this file from your shell rc, e.g. in ~/.zshrc:
#     source ~/Repos/dehli-musikk-sample-plugins/packaging/dmse-completion.sh
# then open a new shell (or `source ~/.zshrc`). Completes commands, plugin names,
# and build kinds. Plugin names come from `dmse __plugins` (fast, no build), so
# they stay correct as plugins are added or removed.

_dmse_commands="list convert build run test package tarball configure help"
_dmse_kinds="standalone all au vst3"
# Commands whose next argument is a plugin name (or "all").
_dmse_takes_plugin() { case " convert build run package tarball " in *" $1 "*) return 0;; *) return 1;; esac; }

# Ask the dmse being completed for its plugin list ($1 = the command word typed).
_dmse_plugin_names() { "$1" __plugins 2>/dev/null; }

if [ -n "${ZSH_VERSION:-}" ]; then
    _dmse() {
        local cmd_word="${words[1]}"
        if (( CURRENT == 2 )); then
            compadd -- ${=_dmse_commands}
        elif (( CURRENT == 3 )); then
            if _dmse_takes_plugin "${words[2]}"; then
                compadd -- ${(f)"$(_dmse_plugin_names "$cmd_word")"}
            fi
        elif (( CURRENT == 4 )) && [ "${words[2]}" = build ]; then
            compadd -- ${=_dmse_kinds}
        fi
    }
    compdef _dmse dmse ./dmse

elif [ -n "${BASH_VERSION:-}" ]; then
    _dmse() {
        local cur="${COMP_WORDS[COMP_CWORD]}" cmd_word="${COMP_WORDS[0]}"
        COMPREPLY=()
        if [ "$COMP_CWORD" -eq 1 ]; then
            COMPREPLY=( $(compgen -W "$_dmse_commands" -- "$cur") )
        elif [ "$COMP_CWORD" -eq 2 ]; then
            _dmse_takes_plugin "${COMP_WORDS[1]}" && \
                COMPREPLY=( $(compgen -W "$(_dmse_plugin_names "$cmd_word")" -- "$cur") )
        elif [ "$COMP_CWORD" -eq 3 ] && [ "${COMP_WORDS[1]}" = build ]; then
            COMPREPLY=( $(compgen -W "$_dmse_kinds" -- "$cur") )
        fi
    }
    complete -F _dmse dmse ./dmse
fi
