# Roadmap

[← Back to README](README.md)

## Native AI Solutions layer

The blueprints will soon be installable **directly through the
[Intel® AI for Enterprise Solutions](https://github.com/intel/enterprise-ai-solutions/tree/release-1.2)
installer as their own layer**, alongside `inference` and `agent-toolkit`.
Deployment then becomes:

```bash
./es_auto_installer.sh init blueprints
./es_auto_installer.sh install blueprints
```

Planned shape, following the AI Solutions
[Adding a Solution](https://github.com/intel/enterprise-ai-solutions/blob/release-1.2/docs/reference/adding_solutions.md)
contract:

| Where | What |
|---|---|
| ai-solutions `configs/repos/repos.blueprints.yaml` | Layer manifest: pins `enterprise-inference`, `enterprise-agent-toolkit` and this repo |
| this repo `components.yaml` | Declares the `blueprints` layer (depends on `agent-toolkit`) and one component per use case |
| this repo `config.yaml` | Operator-editable switches, e.g. which blueprints to enable, model names, keys |
| this repo `roles/<blueprint>/` | One Ansible role per blueprint (`install`, `validate`, `teardown`), reusing the existing Helm charts |

With the layer in place, the installer would also take over what `deploy.sh` does by hand today:
- adding each blueprint namespace to `litellm_allowed_namespaces`;
- creating LiteLLM virtual keys instead of using the master key;
- `./es_auto_installer.sh validate blueprints` health checks.

## Until then

Install AI Solutions (agent-toolkit) first, then deploy blueprints with
`./deploy-usecase.sh`, as described in the [README](README.md#deploy-a-use-case).
