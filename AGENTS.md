# Repository workflow

- Local Git commits are welcome for completed work. Keep commits focused and use
  descriptive messages.
- Leave pushing to the user; do not push unless explicitly asked.
- Do not discard or overwrite unrelated user changes, or include them in a commit
  without permission.
- Use temporary audio fixtures for tests; never modify the user's music library.
- `main` only changes through pull requests that pass CI: commit to a branch.

# Before a release

Check dependencies before tagging a release, and when asked to review them:

1. `zsh build.sh --detect && zsh build.sh --release`, then
   `.venv/bin/python tools/check_deps.py`. It compares the pins in `requirements*.txt`
   with PyPI, checks what ships in the release app (and, separately, the development
   `.venv`) against OSV for known vulnerabilities, and checks the standalone Python in
   `build.sh` and the GitHub Actions versions. It exits 1 if anything that ships is
   vulnerable.
2. A vulnerability in something that ships blocks the release until it is updated or
   shown not to apply. Report it to the user either way.
3. For each newer version, read its changelog before updating, especially Mutagen (it
   writes every tag) and PyTorch/beat_this (detection). A version held back by another
   package's requirement (the script shows which) is not a missed update.
4. Update on a branch: change the pin (or the URL and SHA-256 in `build.sh`), run
   `zsh tools/lock_deps.sh` to regenerate `requirements-release.lock` (add `--upgrade`
   to move unpinned dependencies too, and review the diff), rebuild both ways, and run
   `.venv/bin/python -m unittest` and
   `AUDIO_SORTER_RELEASE_APP=".build/release/Audio Sorter.app" .venv/bin/python -m unittest tests.test_release`.
   Mention what changed and why in the commit message.
