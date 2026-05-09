---
name: triage-linear-ticket
description: Workflow for triaging a linear ticket in todo/backlog status
---

When asked to triage a linear ticket [by id or url] please:

(1) open a worktree against the chalk-sandbox-sdk
(2) analyze the linear ticket's contents and review any discussion
(3) attempt to decide:

- if the ticket is still relevant (i.e. has the bug been fixed / the functionality implemented)?

Use https://cmux.com/ to manage each triage workflow as a pane - I'm running cmux currently.

- Please use the staging credentials (tmnmc9beyujew env) to run tests
- Please name your containers/images/etc after the ticket we're testing

Leave a comment on the linear PR with your conclusion and slack me for each investigation with ~/.bin/slack-message
