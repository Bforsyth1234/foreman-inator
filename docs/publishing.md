# Sharing foreman-inator with other people

foreman-inator acts on GitHub as a GitHub App, and every worker authenticates
with that App's private key. That key is a secret: it lives in
`~/.foreman-inator/` on the Mac that runs the worker (mode 700) and nowhere
else (SPEC invariant 7d). Whatever you choose below, the key never leaves that
Mac.

There are two ways to let someone else use foreman-inator. Pick by one
question: **whose Mac runs the worker?**

| | Each person runs their own worker (recommended) | You run one worker for other accounts |
| --- | --- | --- |
| Who creates an App | Each person creates their own, with `fman setup` | You, once, with `fman setup --public` |
| App visibility | Private (the default) | Public |
| Where the key lives | On each person's own Mac | On your Mac only |
| Who pays for cloud models | Each person, with their own API keys | You |
| Who can start work | Each person's own `allowed_users` | Your `allowed_users` |
| Code and traces | Stay on each person's Mac | Their code is cloned and traced on your Mac |

## Option 1: each person creates their own App (recommended)

Nothing has to be published. The App is created from a manifest that is part
of foreman-inator itself (`setup/manifest.py`), so anyone who installs
foreman-inator can create an identical App of their own with one command.
Send them this:

1. Install the prerequisites in [setup.md](setup.md#prerequisites) (macOS,
   git, uv, OpenCode, Ollama and the local model).
2. Install foreman-inator and run setup:

   ```sh
   uv tool install git+https://github.com/Bforsyth1234/foreman-inator
   fman setup --allowed-user <your-github-login>
   ```

   Setup opens the browser on GitHub's "Create GitHub App" page with the name,
   permissions and description already filled in. Click **Create GitHub App**.
   To create the App under an organization you administer, add `--org <org>`.
3. On the install page GitHub opens next, choose **Only select repositories**,
   pick the repos and click **Install**. Setup then asks which of them the
   worker may work on.
4. Store your cloud API keys (`fman config set-key ...`, see
   [setup.md](setup.md)) and you are done: the worker starts as a launchd
   agent.

Each person's App is private ("it can only be installed on the account that
owns the app", GitHub docs), has its own key on its own Mac, and only that
person's `allowed_users` can start work with it. The App's name gets a random
suffix (`foreman-inator-1a2b3c`) because App names are unique across GitHub.

This is also why foreman-inator is not on the GitHub Marketplace: there is no
single App to list.

## Option 2: a public App you run for other accounts

Use this only when **you** will run the worker for repos owned by another
account, for example a friend's personal repos or a second organization, and
that account should not run its own worker.

If all the repos belong to one organization, you probably do not need a public
App: create a private App owned by that organization instead with
`fman setup --org <org>` (you need to be an owner of the organization). A
private App can be installed on every repo of the account that owns it.

Otherwise:

1. Create the App as public:

   ```sh
   fman setup --public --allowed-user <your-github-login>
   ```

   The manifest is sent with `"public": true`; everything else (permissions,
   description, no webhook) is the same as a private App. Setup prints the
   App's install link, `https://github.com/apps/<slug>/installations/new`.
   An existing private App can be made public instead: on GitHub open
   **Settings > Developer settings > GitHub Apps**, click **Edit** next to the
   App, then **Advanced**, and **Make public**. **Not yet checked live:** the
   button names on that page.
2. Send the install link to the other account's owner. They choose **Only
   select repositories**, pick the repos and click **Install**.
3. Run `fman setup` again. It records the new installation and asks you which
   of its repos the worker may work on. Nothing is worked on until you choose
   it.
4. Add the people who may start work to `allowed_users`
   (`fman setup --allowed-user <login>`). A label applied by anyone else is
   ignored (invariant 7b).

What a public App means, from GitHub's docs: "any user on GitHub can install it
and authorize it". There is no list of accounts that may install it. A stranger
who installs it gains nothing: the worker works only on repos you chose in
setup, and only on labels from your `allowed_users`. But your App can then
read their repos, and "public apps cannot be made private if they're installed
on other accounts", so to go back you first ask every other account to
uninstall it.

The other account should know that their code is cloned to your Mac, their
issue text and code go to the cloud models you configured (unless you set the
repo `cloud = false`), and that you see the traces and recordings.

## Why you can never share the App's private key

It can be tempting to make one public App and send its `.pem` to everyone so
each person's worker can sign in as it. Never do this:

- **It breaks invariant 7d.** The key would live on many Macs, in chat
  messages and email. Anyone holding it can act as the App on every repo of
  every account that installed it, not only their own.
- **It cannot be taken back from one person.** A GitHub App key is not tied to
  a person. Revoking one copy means deleting the key, which stops every worker
  using it.
- **Every action looks the same.** Every PR and comment would come from the
  same bot, so nobody could tell whose worker did what.
- **Every worker would see every installation.** Each worker would list all
  installations and could request a token for any of them.

If someone wants their own worker, they create their own App (option 1). It
takes one command and a click, and their key never leaves their Mac.

If a key was ever shared by mistake: open the App's settings on GitHub
(**Settings > Developer settings > GitHub Apps > Edit**), under **Private keys**
click **Generate a private key**, save the new `.pem` as
`~/.foreman-inator/app.pem` (or run `fman setup` again after removing the old
App from `config.toml`), then delete the old key on the same page.

## GitHub docs used (read 2026-11-25)

- The manifest's `public` field:
  https://docs.github.com/en/apps/sharing-github-apps/registering-a-github-app-from-a-manifest
- Public and private Apps:
  https://docs.github.com/en/apps/creating-github-apps/registering-a-github-app/making-a-github-app-public-or-private
- The install link of a public App:
  https://docs.github.com/en/apps/sharing-github-apps/sharing-your-github-app
- Changing visibility later:
  https://docs.github.com/en/apps/maintaining-github-apps/modifying-a-github-app-registration
