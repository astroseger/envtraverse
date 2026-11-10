; Load the installed Omega runtime and a local one-prompt channel.
; The Python launcher runs from the task workspace, whose repos symlink points
; to /PeTTa/repos. Omega's source and continuous loop remain unchanged.
!(import! &self (library lib_import))
!(git-import! "https://github.com/astroseger/Omega.git")
!(import! &self (library Omega lib_omega))
!(import! &self ./prompt_channel.py)
!(py-call (prompt_channel.loadOmegaPlugin))
!(omega)
