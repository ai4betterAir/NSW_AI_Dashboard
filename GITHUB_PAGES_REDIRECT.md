# GitHub Pages Redirect

This project can publish a stable front page through GitHub Pages:

```text
https://ai4betterair.github.io/
```

That page reads `current.json` and opens the latest `localhost.run` dashboard
URL. The JSON should contain only the current tunnel URL; old tunnel URLs expire
and should not be kept as fallbacks.

## One-time GitHub setup

In GitHub, open:

```text
ai4betterAir/ai4betterAir.github.io -> Settings -> Pages
```

Set:

```text
Source: Deploy from a branch
Branch: main
Folder: /(root)
```

Save. GitHub may take a minute or two to publish the page.

## Update the redirect URL locally

```bash
cd /mnt/scratch_lustre/ar_ai4ba_scratch/Ai4BetterAir/AI_Nowcasting/NSW_AI_Dashboard
scripts/publish_pages_link.sh
```

This updates local `docs/current.json` from `dashboard_url.txt`.

## Push the latest URL to GitHub Pages

After Git authentication is available on this machine:

```bash
scripts/publish_pages_link.sh --push
```

That command publishes the latest URL to `ai4betterAir.github.io/current.json`.

If normal `git push` authentication is not available, set a GitHub token with
permission to update repository contents:

```bash
export GITHUB_TOKEN='ghp_...'
scripts/publish_pages_link.sh --push
```

With `GITHUB_TOKEN`, the script updates `current.json` through the GitHub
API instead of using `git push`.

For persistent publishing after server restarts, store the token in the local
dashboard `.env` file. This file is ignored by git and the scripts load it
automatically:

```bash
cd /mnt/scratch_lustre/ar_ai4ba_scratch/Ai4BetterAir/AI_Nowcasting/NSW_AI_Dashboard
read -rsp "Paste GitHub token: " GH_PAT
echo
umask 077
printf 'GITHUB_TOKEN=%s\n' "$GH_PAT" > .env
unset GH_PAT
scripts/publish_pages_link.sh --push
```

## Automatic updates

To keep `https://ai4betterair.github.io/` pointing at the current live tunnel,
the dashboard machine must be able to write to the `ai4betterAir.github.io`
repository. Set `GITHUB_TOKEN` before starting the tunnel loop:

```bash
export GITHUB_TOKEN='ghp_...'
PUBLISH_PAGES_ON_URL=1 PUBLISH_PAGES_PUSH=1 scripts/start_pages_watchdog.sh
```

If `GITHUB_TOKEN` is saved in `.env`, no manual `export` is needed:

```bash
PUBLISH_PAGES_ON_URL=1 PUBLISH_PAGES_PUSH=1 scripts/start_pages_watchdog.sh
```

Without `GITHUB_TOKEN` or git push credentials, the local tunnel can keep
running, but GitHub Pages cannot be updated automatically. In that case this
command updates only the local `docs/current.json`:

```bash
PUBLISH_PAGES_ON_URL=1 PUBLISH_PAGES_PUSH=0 scripts/start_pages_watchdog.sh
```

That updates the local `docs/current.json`, but does not publish it to GitHub.
