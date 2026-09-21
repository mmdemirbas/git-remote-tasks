---
title: Service setup
order: 30
summary: Tokens and ids for Jira, Vikunja, Microsoft To Do and Notion, and the git config lines each one needs.
---

> [!TLDR]
> Each service needs a token and one or two ids; the exact `git config tasks-remote.<name>.*` lines are below, per service.


## Jira (Atlassian Cloud)

1. Log in at https://id.atlassian.com/manage-profile/security/api-tokens.
2. Create a token labelled something like `git-remote-tasks`.
3. Save it securely — you'll only see it once.
4. Configure the remote:

   ```bash
   git remote add jira-work jira://company.atlassian.net
   git config tasks-remote.jira-work.scheme    jira
   git config tasks-remote.jira-work.baseUrl   https://company.atlassian.net
   git config tasks-remote.jira-work.email     me@company.com
   git config tasks-remote.jira-work.apiToken  "$TOKEN"
   # Optional: to create new issues from local commits.
   git config tasks-remote.jira-work.projectKey PROJ
   # Optional: override the default JQL (`created is not EMPTY ORDER BY updated DESC`).
   git config tasks-remote.jira-work.jql       "assignee = currentUser()"
   ```

   The driver targets Atlassian's new `/rest/api/3/search/jql`
   endpoint by default, falling back to the legacy `/search` on 404/410
   for self-hosted Jira Data Center. `Accept: application/json` is set
   automatically so Atlassian returns real data rather than the schema
   preview.

## Vikunja (self-hosted)

1. In the Vikunja UI: **Settings → API Tokens → New token** with
   `tasks.read` and `tasks.write` scopes.
2. Copy the token and register the remote:

   ```bash
   git remote add vikunja vikunja://localhost:3456
   git config tasks-remote.vikunja.scheme    vikunja
   git config tasks-remote.vikunja.baseUrl   http://localhost:3456
   git config tasks-remote.vikunja.apiToken  "$TOKEN"
   # Required for CREATE; points at the project new tasks land in.
   git config tasks-remote.vikunja.projectId 1
   ```

## Microsoft To Do

1. Register an app in the Azure portal under **App registrations** with
   redirect URI `http://localhost:1234`.
2. Grant `Tasks.ReadWrite` delegated permission.
3. Copy the Application (client) ID. Pick a tenant — `consumers` for
   personal Microsoft accounts.
4. Configure the remote:

   ```bash
   git remote add todo mstodo://consumers
   git config tasks-remote.todo.scheme        mstodo
   git config tasks-remote.todo.tenantId      consumers
   git config tasks-remote.todo.clientId      "$CLIENT_ID"
   # Required for CREATE + DELETE (the removed task file no longer
   # carries the list id).
   git config tasks-remote.todo.defaultListId "$LIST_ID"
   ```

5. Install `msal` for the device-code flow:

   ```bash
   pip install msal
   ```

   The first fetch prompts on stderr with the device-code URL. The
   refresh token is persisted under `tasks-remote.todo.refreshToken`
   so subsequent runs are silent.

## Notion

1. Go to https://www.notion.so/my-integrations, create a new internal
   integration, copy its Internal Integration Token.
2. Share the target database with the integration (database → share).
3. Find the database ID in the URL (the 32-hex segment).
4. Configure the remote:

   ```bash
   git remote add notion-inbox notion://inbox
   git config tasks-remote.notion-inbox.scheme     notion
   git config tasks-remote.notion-inbox.databaseId "$DB_ID"
   git config tasks-remote.notion-inbox.token      "$INTEGRATION_TOKEN"
   ```

   Notion push supports create (`POST /v1/pages`), update
   (`PATCH /v1/pages/{id}`), and archive-on-delete
   (`PATCH archived: true`). The title column's name is auto-discovered
   from the database schema. Each column's payload shape (`select` vs
   the newer `status` type, `multi_select`, `date`, `rich_text`) is
   adapted per-column so we don't 400 on database-specific config.
