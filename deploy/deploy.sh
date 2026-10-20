#!/bin/sh
# Deploys the latest main on the server: update the code, rebuild the image, restart, wait until the site is
# healthy. GitHub Actions runs it after the tests pass (the deploy key in ~/.ssh/authorized_keys may run nothing
# else, see docs/deploy.md); it can also be run by hand.
#
# The whole script is one block, read before it runs: `git merge` may replace this file while it executes.
{
set -eu
cd "$(dirname "$0")/.."
git fetch --quiet origin main
git merge --ff-only --quiet origin/main
echo "Building $(git log --oneline -1)"
docker build --quiet -t pdf2epub:latest . >/dev/null
docker build --quiet -t pdf2epub-voice:latest voice-worker >/dev/null
cd deploy
# The voice worker gets its token and nothing else from secrets.env.
(umask 077 && grep '^PDF2EPUB_VOICE_TOKEN=' secrets.env > voice.env)
docker compose up -d
for _ in $(seq 1 36); do
    if [ "$(docker compose ps web --format '{{.Health}}')" = "healthy" ]; then
        echo "Deployed $(git log --oneline -1)"
        exit 0
    fi
    sleep 5
done
echo "The web container did not become healthy" >&2
docker compose ps >&2
exit 1
}
