# Chalk Sandbox SDK examples

These examples show how to build and run workloads with the Chalk Sandbox SDK. Each
folder focuses on a specific workflow, such as running commands in a sandbox, deploying
functions, mounting persistent volumes, scheduling work, or hosting a model.

## Before you start

You need `uv` and credentials for a Chalk environment. Most examples use the standard
Chalk environment variables:

- `CHALK_CLIENT_ID`
- `CHALK_CLIENT_SECRET`
- `CHALK_ENVIRONMENT_ID`
- `CHALK_API_SERVER`

Some examples also use services such as OpenAI, Anthropic, Hugging Face, Slack, GitHub,
or S3-compatible object storage. Those examples describe any additional credentials they
need in their source.

## Running an example

Runnable Python files are executable. Run one directly from the repository root or from
its own folder; `uv` installs its Python dependencies automatically on first use.

Read the beginning of the script before running it. It explains what the example does,
what it creates, and anything you need to configure first. Most examples clean up the
resources they create. When an example intentionally leaves a function, schedule, model
service, or volume running so you can use or inspect it afterward, the script says so.

Initial image builds and dependency installation can take a few minutes. Subsequent runs
usually reuse cached work and start faster.

## Safety and cost

Some examples intentionally create many resources, reserve GPUs, retain persistent
storage or deployments, execute model-generated code, or test destructive behavior
inside an isolated sandbox. Check the warnings in the script before using real data or
running an example in a shared environment.

Examples that send prompts, files, command output, or configuration to an external model
or service should be used only with data you are permitted to share with that provider.
