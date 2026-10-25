function clippy --wraps='cargo clippy --all-targets --all-features -- -D warnings' --description 'Run Clippy with warnings treated as errors'
    cargo clippy --all-targets --all-features -- -D warnings $argv
end
